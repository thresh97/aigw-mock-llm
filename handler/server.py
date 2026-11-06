"""Serves handler.py over plain HTTP, for running the mock as a container or locally instead of on Lambda.

python3 server.py (env TOKEN, optional LEAK_NEEDLES, PORT default 8080). Each request becomes a Function URL event, so
behavior and the log line are the same as on Lambda. GET /healthz answers 200 without the token, for container probes.
"""

import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import handler


class Lambda(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self):
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            return self._send(200, "text/plain", b"ok")
        n = int(self.headers.get("content-length") or 0)
        r = handler.handler({
            "rawPath": path, "rawQueryString": query, "headers": dict(self.headers.items()),
            "body": self.rfile.read(n).decode("utf-8", "replace") if n else "",
            "requestContext": {"http": {"method": self.command, "sourceIp": self.client_address[0]}}}, None)
        self._send(r["statusCode"], r["headers"]["content-type"], r["body"].encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _serve

    def log_message(self, *args):
        pass  # handler.py logs each request


def serve(port, host="0.0.0.0"):
    return ThreadingHTTPServer((host, port), Lambda)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 in a container gets no default SIGTERM handling
    srv = serve(int(os.environ.get("PORT", "8080")))
    print(f"mock listening on :{srv.server_port}", flush=True)
    srv.serve_forever()
