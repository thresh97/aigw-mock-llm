"""Checks the handler locally, without AWS: python3 -m unittest discover tests

The SDK tests (skipped unless `pip install openai anthropic`) serve the handler over local HTTP, call it with the official
clients, and validate every response and stream event strictly against the SDKs' pydantic models.
"""

import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import get_args

os.environ.setdefault("TOKEN", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "handler"))
import handler as h  # noqa: E402

TOKEN = os.environ["TOKEN"]


def call(path, body=None, method="POST", token=TOKEN):
    ev = {"rawPath": f"/{token}{path}", "headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"},
          "body": json.dumps(body) if body is not None else "",
          "requestContext": {"http": {"method": method, "sourceIp": "127.0.0.1"}}}
    out = io.StringIO()
    with redirect_stdout(out):
        r = h.handler(ev, None)
    logs = [json.loads(line) for line in out.getvalue().splitlines()]
    return r, logs[0] if logs else None


def sse_events(body):
    """[(event name or None, data string)] from an SSE body."""
    events = []
    for chunk in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in chunk.split("\n"))
        events.append((fields.get("event"), fields["data"]))
    return events


class Handler(unittest.TestCase):
    def test_token_and_unknown_paths_404(self):
        self.assertEqual(call("/v1/messages", {}, token="wrong")[0]["statusCode"], 404)
        self.assertEqual(call("/v1/nope", {})[0]["statusCode"], 404)
        self.assertEqual(call("/v1/models", None, "POST")[0]["statusCode"], 404)

    def test_mock_tokens(self):
        r, _ = call("/v1/chat/completions", {"messages": [{"role": "user", "content": "MOCK_TOKENS=500"}]})
        self.assertEqual(json.loads(r["body"])["usage"]["prompt_tokens"], 500)
        r, _ = call("/v1/messages", {"messages": [{"role": "user", "content": "MOCK_TOKENS=7"}]})
        self.assertEqual(json.loads(r["body"])["usage"]["input_tokens"], 7)

    def test_chat_stream_usage_only_when_requested(self):
        for opts, want in [({}, 0), ({"stream_options": {"include_usage": True}}, 1)]:
            r, _ = call("/v1/chat/completions", dict(opts, stream=True, messages=[]))
            events = sse_events(r["body"])
            self.assertEqual(events[-1], (None, "[DONE]"))
            self.assertEqual(sum("usage" in json.loads(d) for _, d in events[:-1]), want)

    def test_log_line(self):
        _, log = call("/v1/chat/completions", {
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "messages": [{"role": "user", "content": "hi eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"},
                         {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "f"}}]},
                         {"role": "tool", "content": "x"}]})
        self.assertEqual(log["tools"], ["f"])
        self.assertEqual(log["messages"], ["user:text", "assistant:tool_use=f", "tool:text"])
        self.assertEqual(log["leaks"], ["<body>: JWT"])
        _, log = call("/v1/responses", {"input": [{"role": "user", "content": [{"type": "input_text", "text": "x"}]},
                                                  {"type": "function_call", "name": "f"},
                                                  {"type": "function_call_output", "output": "y"}]})
        self.assertEqual(log["messages"], ["user:input_text", "function_call:tool_use=f", "function_call_output:tool_result"])
        _, log = call("/v1/messages", {"tools": [{"name": "g"}], "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "name": "g"}]},
            {"role": "user", "content": [{"type": "tool_result"}]}]})
        self.assertEqual(log["tools"], ["g"])
        self.assertEqual(log["messages"], ["assistant:tool_use=g", "user:tool_result"])


class _Lambda(BaseHTTPRequestHandler):
    """Turns a local HTTP request into a Function URL event."""

    def _serve(self):
        path, _, query = self.path.partition("?")
        n = int(self.headers.get("content-length") or 0)
        ev = {"rawPath": path, "rawQueryString": query, "headers": dict(self.headers.items()),
              "body": self.rfile.read(n).decode() if n else "",
              "requestContext": {"http": {"method": self.command, "sourceIp": "127.0.0.1"}}}
        with redirect_stdout(io.StringIO()):
            r = h.handler(ev, None)
        body = r["body"].encode()
        self.send_response(r["statusCode"])
        for k, v in r["headers"].items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _serve

    def log_message(self, *args):
        pass


try:
    import anthropic
    import openai
    from pydantic import TypeAdapter
except ImportError:
    anthropic = None


@unittest.skipIf(anthropic is None, "pip install openai anthropic")
class SDK(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Lambda)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.server.server_port}/{TOKEN}"
        cls.oai = openai.OpenAI(base_url=f"{base}/v1", api_key="k", max_retries=0)
        cls.ant = anthropic.Anthropic(base_url=base, api_key="k", max_retries=0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def validate(self, model, data):
        return TypeAdapter(model).validate_python(data)

    def validate_event(self, union, data):
        """Against the union member whose `type` literal matches, so a failure names one model, not all of them."""
        members = {get_args(m.model_fields["type"].annotation)[0]: m for m in get_args(get_args(union)[0])}
        return self.validate(members[data["type"]], data)

    def raw_stream(self, path, body):
        r, _ = call(path, dict(body, stream=True))
        return [(e, d) for e, d in sse_events(r["body"]) if d != "[DONE]"]

    def test_anthropic(self):
        msg = self.ant.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(msg.content[0].text, "ok")
        self.validate(anthropic.types.Message, msg.to_dict())
        with self.ant.messages.stream(model="m", max_tokens=5, messages=[{"role": "user", "content": "hi"}]) as s:
            self.assertEqual(s.get_final_text(), "ok")
            self.assertEqual(s.get_final_message().stop_reason, "end_turn")
        for e, d in self.raw_stream("/v1/messages", {"messages": []}):
            self.assertEqual(self.validate_event(anthropic.types.RawMessageStreamEvent, json.loads(d)).type, e)
        n = self.ant.messages.count_tokens(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(n.input_tokens, 10)

    def test_openai_chat(self):
        c = self.oai.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(c.choices[0].message.content, "ok")
        self.validate(openai.types.chat.ChatCompletion, c.to_dict())
        with self.oai.chat.completions.stream(model="m", messages=[{"role": "user", "content": "hi"}],
                                              stream_options={"include_usage": True}) as s:
            final = s.get_final_completion()
        self.assertEqual(final.choices[0].message.content, "ok")
        self.assertEqual(final.usage.prompt_tokens, 10)
        for _, d in self.raw_stream("/v1/chat/completions", {"messages": [], "stream_options": {"include_usage": True}}):
            self.validate(openai.types.chat.ChatCompletionChunk, json.loads(d))

    def test_openai_responses(self):
        r = self.oai.responses.create(model="m", input="hi")
        self.assertEqual(r.output_text, "ok")
        self.validate(openai.types.responses.Response, r.to_dict())
        with self.oai.responses.stream(model="m", input="hi") as s:
            final = s.get_final_response()
        self.assertEqual(final.output_text, "ok")
        for e, d in self.raw_stream("/v1/responses", {"input": "hi"}):
            self.assertEqual(self.validate_event(openai.types.responses.ResponseStreamEvent, json.loads(d)).type, e)

    def test_openai_models(self):
        self.assertEqual([m.id for m in self.oai.models.list()], ["mock", "mock-large"])


if __name__ == "__main__":
    unittest.main()
