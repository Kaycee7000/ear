"""Shared fixtures: a mock model server and a fake GitHub REST API."""

from __future__ import annotations

import io
import json
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from model_review.mock_server import make_server  # noqa: E402

SUITES_DIR = ROOT / "suites"
TOKEN = "test-token"


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def mock_model():
    """Factory: mock_model(mode='good') -> base_url."""
    servers = []

    def start(mode: str = "good", delay_ms: float = 0) -> str:
        srv = _serve(make_server(0, mode, delay_ms))
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}/v1"

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


class FakeGitHub:
    """Just enough of the GitHub REST API for the client: dispatch, runs, jobs, artifacts, contents."""

    def __init__(self):
        self.return_details = True   # False -> dispatch answers 204 like the pre-2026 API
        self.results: dict | None = None  # results.json to serve as the artifact; None = no artifact
        self.polls_until_done = 2
        self.conclusion = "success"
        self.dispatches: list[dict] = []
        self.blob_auth_headers: list[str | None] = []
        self.run: dict | None = None
        self._run_gets = 0
        self.server = _serve(ThreadingHTTPServer(("127.0.0.1", 0), self._handler()))
        port = self.server.server_address[1]
        self.api_url = f"http://127.0.0.1:{port}"
        self.blob_url = f"http://localhost:{port}/blob/results.zip"  # different host on purpose

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("results.json", json.dumps(self.results))
            zf.writestr("report.md", "# report")
        return buf.getvalue()

    def _handler(self):
        gh = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload=None, raw: bytes | None = None, headers=None):
                data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self):
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    self._send(401, {"message": "Bad credentials"})
                    return False
                return True

            def do_GET(self):  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/blob/results.zip":
                    gh.blob_auth_headers.append(self.headers.get("Authorization"))
                    return self._send(200, raw=gh._zip())
                if not self._authed():
                    return
                if path == "/repos/o/r":
                    return self._send(200, {"default_branch": "main"})
                if path == "/repos/o/r/actions/workflows/model-review.yml/runs":
                    runs = [gh.run] if gh.run else []
                    return self._send(200, {"total_count": len(runs), "workflow_runs": runs})
                if gh.run and path == f"/repos/o/r/actions/runs/{gh.run['id']}":
                    gh._run_gets += 1
                    if gh._run_gets >= gh.polls_until_done:
                        gh.run.update(status="completed", conclusion=gh.conclusion)
                    else:
                        gh.run.update(status="in_progress")
                    return self._send(200, gh.run)
                if gh.run and path == f"/repos/o/r/actions/runs/{gh.run['id']}/artifacts":
                    arts = []
                    if gh.results is not None:
                        arts.append({"id": 5, "name": "model-review-" + gh.dispatches[-1]["inputs"]["request_id"],
                                     "expired": False,
                                     "archive_download_url": f"{gh.api_url}/repos/o/r/actions/artifacts/5/zip"})
                    return self._send(200, {"total_count": len(arts), "artifacts": arts})
                if path == "/repos/o/r/actions/artifacts/5/zip":
                    return self._send(302, headers={"Location": gh.blob_url})
                if gh.run and path == f"/repos/o/r/actions/runs/{gh.run['id']}/jobs":
                    return self._send(200, {"jobs": [{"name": "review", "conclusion": "failure", "steps": [
                        {"name": "Set up job", "conclusion": "success"},
                        {"name": "Start model backend", "conclusion": "failure"},
                    ]}]})
                if path == "/repos/o/r/contents/model-review/suites":
                    return self._send(200, [
                        {"name": "smoke.yaml", "path": "model-review/suites/smoke.yaml", "type": "file"},
                        {"name": "README.md", "path": "model-review/suites/README.md", "type": "file"},
                    ])
                self._send(404, {"message": "Not Found"})

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return
                path = urlsplit(self.path).path
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if path == "/repos/o/r/actions/workflows/model-review.yml/dispatches":
                    gh.dispatches.append(body)
                    rid = body["inputs"]["request_id"]
                    gh.run = {"id": 1001, "status": "queued", "conclusion": None,
                              "html_url": "https://github.com/o/r/actions/runs/1001",
                              "display_title": f"model-review {body['inputs']['model']} / "
                                               f"{body['inputs']['suite']} [{rid}]"}
                    gh._run_gets = 0
                    if gh.return_details and body.get("return_run_details"):
                        return self._send(200, {"workflow_run_id": 1001,
                                                "run_url": f"{gh.api_url}/repos/o/r/actions/runs/1001",
                                                "html_url": "https://github.com/o/r/actions/runs/1001"})
                    return self._send(204)
                self._send(404, {"message": "Not Found"})

        return H


@pytest.fixture
def fake_github():
    gh = FakeGitHub()
    yield gh
    gh.close()
