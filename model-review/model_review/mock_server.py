"""A tiny OpenAI-compatible server for testing the pipeline without a real model.

    python -m model_review.mock_server --port 18080            # answers the smoke suite correctly
    python -m model_review.mock_server --port 18080 --mode bad # answers everything wrong

Use backend=mock in the workflow to check that dispatching, running, artifacts and
the agent client all work before spending minutes (or money) on a real model.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# (keywords that must all appear in the last user message, reply)
RULES = [
    (("17", "25"), "42"),
    (("exactly the word ok",), "OK"),
    (("json", "ada"), '```json\n{"name": "Ada", "age": 36}\n```'),
    (("favorite color",), "Teal"),
    (("capital of france",), "Paris"),
    (("hello",), "Hello! How can I help you today?"),
]


def reply_for(messages: list[dict], mode: str) -> str:
    if mode == "bad":
        return "banana"
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    text = str(last_user).casefold()
    for keywords, reply in RULES:
        if all(k in text for k in keywords):
            return reply
    return "I am a mock model."


class Handler(BaseHTTPRequestHandler):
    mode = "good"
    delay_s = 0.0

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "mock", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid JSON"}})
            return
        if self.delay_s:
            time.sleep(self.delay_s)
        messages = body.get("messages") or []
        reply = reply_for(messages, self.mode)
        prompt_words = sum(len(str(m.get("content", "")).split()) for m in messages)
        self._send(200, {
            "id": "mock-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": reply}}],
            "usage": {"prompt_tokens": prompt_words, "completion_tokens": len(reply.split()),
                      "total_tokens": prompt_words + len(reply.split())},
        })

    def log_message(self, fmt, *args):  # keep test output quiet
        pass


def make_server(port: int = 0, mode: str = "good", delay_ms: float = 0) -> ThreadingHTTPServer:
    handler = type("MockHandler", (Handler,), {"mode": mode, "delay_s": delay_ms / 1000})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--mode", choices=["good", "bad"], default=os.environ.get("MOCK_MODE", "good"))
    parser.add_argument("--delay-ms", type=float, default=float(os.environ.get("MOCK_DELAY_MS", 0)))
    args = parser.parse_args(argv)
    server = make_server(args.port, args.mode, args.delay_ms)
    print(f"mock model server ({args.mode}) on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
