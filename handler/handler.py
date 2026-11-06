"""Mock LLM API (Anthropic Messages + OpenAI Chat Completions/Responses/Models) on a Lambda Function URL. Lab use only.

An AI gateway calls it as an `anthropic` or `openai` target with custom_host = <function-url>/<TOKEN>/v1. Requests whose
path doesn't start with /<TOKEN>/ get a 404. Answers, with canned replies (JSON, or SSE when "stream": true):
  Anthropic: POST .../messages, POST .../messages/count_tokens
  OpenAI:    POST .../chat/completions, POST .../responses, GET .../models
Logs one JSON line per request to CloudWatch: header names with masked values, the tool names, tool_choice, message
block types and a body hash (for guardrail-edit tests), and "leaks": identity-provider data found in headers or body
(JWT-shaped strings, plus env LEAK_NEEDLES, comma-separated).
MOCK_TOKENS=<n> anywhere in the body sets the reported input tokens, so one request can exhaust a gateway usage limit.
"""

import base64
import hashlib
import json
import os
import re
import time
import uuid

TOKEN = os.environ["TOKEN"]
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
TOKENS_RE = re.compile(r"MOCK_TOKENS=(\d+)")
NEEDLES = ["keycloak", "portkey_oid", "portkey_workspace"] + [n for n in os.environ.get("LEAK_NEEDLES", "").split(",") if n]


def leaks(headers, body):
    found = []
    for k, v in list(headers.items()) + [("<body>", body)]:
        if JWT_RE.search(v):
            found.append(f"{k}: JWT")
        found += [f"{k}: {n}" for n in NEEDLES if n.lower() in v.lower()]
    return found


def blocks(m):
    """role:type,type... for one message or Responses input item, with the tool name on tool calls (tool_use=<name>)."""
    c = m.get("content")
    kinds = [f"tool_use={b.get('name')}" if b.get("type") == "tool_use" else str(b.get("type")) for b in c if isinstance(b, dict)] \
        if isinstance(c, list) else ["text"] if c else []
    kinds += [f"tool_use={(tc.get('function') or {}).get('name')}" for tc in m.get("tool_calls") or [] if isinstance(tc, dict)]
    if m.get("type") == "function_call":
        kinds.append(f"tool_use={m.get('name')}")
    elif m.get("type") == "function_call_output":
        kinds.append("tool_result")
    return f"{m.get('role') or m.get('type')}:" + ",".join(kinds)


def tool_name(t):
    return t.get("name") or (t.get("function") or {}).get("name")


def mask(v):
    return v if len(v) <= 24 else f"{v[:14]}…{v[-6:]} ({len(v)} chars)"


def reply(code, obj, ctype="application/json"):
    return {"statusCode": code, "headers": {"content-type": ctype}, "body": obj if isinstance(obj, str) else json.dumps(obj)}


def not_found(message):
    # Carries both the Anthropic and the OpenAI error fields so either client parses it.
    return reply(404, {"type": "error", "error": {"type": "not_found_error", "code": "not_found", "message": message}})


def sse(events):
    return reply(200, "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" if e else f"data: {d if isinstance(d, str) else json.dumps(d)}\n\n"
                              for e, d in events), "text/event-stream")


def anthropic_messages(req, tokens):
    msg = {"id": "msg_mock_" + uuid.uuid4().hex[:12], "type": "message", "role": "assistant", "model": req.get("model") or "mock",
           "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": tokens, "output_tokens": 1}}
    if not req.get("stream"):
        return reply(200, dict(msg, content=[{"type": "text", "text": "ok"}]))
    return sse([
        ("message_start", {"type": "message_start", "message": dict(msg, content=[], stop_reason=None,
                                                                     usage={"input_tokens": tokens, "output_tokens": 0})}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def openai_chat(req, tokens):
    base = {"id": "chatcmpl-mock-" + uuid.uuid4().hex[:12], "created": int(time.time()), "model": req.get("model") or "mock"}
    usage = {"prompt_tokens": tokens, "completion_tokens": 1, "total_tokens": tokens + 1}
    if not req.get("stream"):
        return reply(200, dict(base, object="chat.completion", usage=usage, choices=[
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]))

    def chunk(delta, finish=None):
        return (None, dict(base, object="chat.completion.chunk", choices=[{"index": 0, "delta": delta, "finish_reason": finish}]))

    events = [chunk({"role": "assistant", "content": ""}), chunk({"content": "ok"}), chunk({}, "stop")]
    if (req.get("stream_options") or {}).get("include_usage"):
        events.append((None, dict(base, object="chat.completion.chunk", choices=[], usage=usage)))
    return sse(events + [(None, "[DONE]")])


def openai_responses(req, tokens):
    rid, mid = "resp_mock_" + uuid.uuid4().hex[:12], "msg_mock_" + uuid.uuid4().hex[:12]
    usage = {"input_tokens": tokens, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}, "output_tokens": 1,
             "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": tokens + 1}
    part = {"type": "output_text", "text": "ok", "annotations": []}
    item = {"type": "message", "id": mid, "status": "completed", "role": "assistant", "content": [part]}
    resp = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed",
            "model": req.get("model") or "mock", "output": [item], "usage": usage,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
    if not req.get("stream"):
        return reply(200, resp)
    empty = dict(item, status="in_progress", content=[])
    seq = iter(range(1000))

    def ev(name, **kw):
        return (name, dict(type=name, sequence_number=next(seq), **kw))

    return sse([
        ev("response.created", response=dict(resp, status="in_progress", output=[], usage=None)),
        ev("response.output_item.added", output_index=0, item=empty),
        ev("response.content_part.added", item_id=mid, output_index=0, content_index=0, part=dict(part, text="")),
        ev("response.output_text.delta", item_id=mid, output_index=0, content_index=0, delta="ok", logprobs=[]),
        ev("response.output_text.done", item_id=mid, output_index=0, content_index=0, text="ok", logprobs=[]),
        ev("response.content_part.done", item_id=mid, output_index=0, content_index=0, part=part),
        ev("response.output_item.done", output_index=0, item=item),
        ev("response.completed", response=resp),
    ])


def handler(event, context):
    path = event.get("rawPath", "")
    if not path.startswith(f"/{TOKEN}/"):
        return not_found("not found")
    path = path[len(TOKEN) + 1:]
    http = event.get("requestContext", {}).get("http", {})
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", "replace")
    try:
        req = json.loads(body) if body else {}
    except ValueError:
        req = {}
    req = req if isinstance(req, dict) else {}
    msgs = req.get("messages") if isinstance(req.get("messages"), list) else req.get("input")
    print(json.dumps({"mock": "request", "method": http.get("method"),
                      "path": path, "query": event.get("rawQueryString"), "source_ip": http.get("sourceIp"),
                      "headers": {k: mask(v) for k, v in sorted(headers.items())}, "leaks": leaks(headers, body),
                      "body_keys": sorted(req), "model": req.get("model"), "metadata": req.get("metadata"), "user": req.get("user"),
                      "stream": req.get("stream"), "body_sha256": hashlib.sha256(body.encode()).hexdigest()[:16],
                      "tools": [tool_name(t) for t in req["tools"] if isinstance(t, dict)] if isinstance(req.get("tools"), list) else None,
                      "tool_choice": req.get("tool_choice"),
                      "messages": [blocks(m) for m in msgs or [] if isinstance(m, dict)] if isinstance(msgs, list) else None}))

    m = TOKENS_RE.search(body)
    tokens = int(m.group(1)) if m else 10
    if path.endswith("/messages/count_tokens"):
        return reply(200, {"input_tokens": 10})
    if path.endswith("/messages"):
        return anthropic_messages(req, tokens)
    if path.endswith("/chat/completions"):
        return openai_chat(req, tokens)
    if path.endswith("/responses"):
        return openai_responses(req, tokens)
    if path.endswith("/models") and http.get("method") == "GET":
        return reply(200, {"object": "list", "data": [
            {"id": i, "object": "model", "created": 0, "owned_by": "mock"} for i in ("mock", "mock-large")]})
    return not_found(f"mock: {path}")
