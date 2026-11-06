"""Checks the handler locally, without AWS: python3 -m unittest discover tests

The SDK tests (skipped unless `pip install openai anthropic`) serve the handler over local HTTP with handler/server.py,
call it with the official clients, and validate every response and stream event strictly against the SDKs' pydantic models.
"""

import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from unittest import mock
from typing import get_args

os.environ.setdefault("TOKEN", "t")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "handler"))
import handler as h  # noqa: E402
import server  # noqa: E402

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

    def test_error_from_model_name_or_prompt(self):
        cases = [("/v1/chat/completions", "gpt-4o-error-429", {"error": {"type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}}),
                 ("/v1/responses", "gpt-4o-mini-error-503", {"error": {"type": "server_error"}}),
                 ("/v1/messages", "claude-sonnet-4-5-error-529", {"type": "error", "error": {"type": "overloaded_error"}}),
                 ("/v1/messages/count_tokens", "claude-error-401", {"error": {"type": "authentication_error"}})]
        for path, model, want in cases:
            r, log = call(path, {"model": model, "messages": []})
            code = int(model[-3:])
            self.assertEqual(r["statusCode"], code)
            body = json.loads(r["body"])
            for k, v in want.items():
                self.assertEqual(body[k] if not isinstance(v, dict) else {x: body[k][x] for x in v}, v)
            self.assertEqual(log["simulate"], {"delay_s": 0, "error": code})
        r, _ = call("/v1/chat/completions", {"model": "gpt-4o", "messages": [{"role": "user", "content": "MOCK_ERROR=500"}]})
        self.assertEqual(r["statusCode"], 500)
        r, _ = call("/v1/chat/completions", {"model": "gpt-4o-error-429", "messages": [{"role": "user", "content": "MOCK_ERROR=502"}]})
        self.assertEqual(r["statusCode"], 502)  # the prompt wins
        for model in ("gpt-4o", "gpt-4-0613", "claude-3-5-sonnet-20241022", "terror-429", "x-error-200"):
            self.assertEqual(call("/v1/chat/completions", {"model": model, "messages": []})[0]["statusCode"], 200)

    def test_delay(self):
        with mock.patch.object(h.time, "sleep") as sleep:
            call("/v1/chat/completions", {"model": "m-delay-1500", "messages": []})
            sleep.assert_called_once_with(1.5)
            sleep.reset_mock()
            _, log = call("/v1/messages", {"model": "m", "messages": [{"role": "user", "content": "MOCK_DELAY=100-200"}]})
            (secs,), _ = sleep.call_args
            self.assertTrue(0.1 <= secs <= 0.2)
            self.assertEqual(log["simulate"]["delay_s"], secs)
            sleep.reset_mock()
            call("/v1/messages", {"model": "m-delay-300-100", "messages": []})  # reversed range
            self.assertTrue(0.1 <= sleep.call_args[0][0] <= 0.3)
            sleep.reset_mock()
            r, log = call("/v1/chat/completions", {"model": "m-delay-250-error-503", "messages": []})  # both: delay, then error
            sleep.assert_called_once_with(0.25)
            self.assertEqual(r["statusCode"], 503)
            sleep.reset_mock()
            call("/v1/chat/completions", {"model": "m", "messages": []})
            sleep.assert_not_called()

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


class Server(unittest.TestCase):
    def test_http(self):
        srv = server.serve(0, "127.0.0.1")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/healthz") as r:
                self.assertEqual((r.status, r.read()), (200, b"ok"))
            req = urllib.request.Request(f"{base}/{TOKEN}/v1/messages?x=1", data=b'{"model": "m", "stream": true}',
                                         headers={"x-probe": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"})
            out = io.StringIO()
            with redirect_stdout(out), urllib.request.urlopen(req) as r:
                self.assertEqual(r.headers["content-type"], "text/event-stream")
                self.assertIn(b"event: message_stop", r.read())
            log = json.loads(out.getvalue())
            self.assertEqual((log["path"], log["query"], log["source_ip"]), ("/v1/messages", "x=1", "127.0.0.1"))
            self.assertEqual(log["leaks"], ["x-probe: JWT"])
            with self.assertRaises(urllib.error.HTTPError) as e, redirect_stdout(io.StringIO()):
                urllib.request.urlopen(f"{base}/wrong/v1/models")
            self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


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
        cls.stdout, sys.stdout = sys.stdout, io.StringIO()  # the handler's log lines
        cls.server = server.serve(0, "127.0.0.1")
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.server.server_port}/{TOKEN}"
        cls.oai = openai.OpenAI(base_url=f"{base}/v1", api_key="k", max_retries=0)
        cls.ant = anthropic.Anthropic(base_url=base, api_key="k", max_retries=0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        sys.stdout = cls.stdout

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
