"""Agent-side client: GitHub Actions as a model-review API.

    start_review()  -> POST workflow dispatch, returns the run id
    get_review()    -> run status, and once finished the verdict from results.json
    review_model()  -> both, waiting up to a time limit

Configuration (environment variables):
    MODEL_REVIEW_TOKEN   GitHub token (falls back to GITHUB_TOKEN, then GH_TOKEN).
                         Fine-grained PAT on this repo with "Actions: read and write".
    MODEL_REVIEW_REPO    owner/repo holding the workflow (default Kaycee7000/ear)
    MODEL_REVIEW_REF     branch to run on (default: the repo's default branch)
    MODEL_REVIEW_WORKFLOW  workflow file name (default model-review.yml)
    GITHUB_API_URL       API base (default https://api.github.com)

CLI:
    model-review review --model qwen2.5:0.5b --suite smoke
    model-review review --model mock --backend mock --no-wait
    model-review status 1234567890 --wait 600
    model-review suites
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

DEFAULT_REPO = "Kaycee7000/ear"
DEFAULT_WORKFLOW = "model-review.yml"
SUITES_PATH = "model-review/suites"
ARTIFACT_PREFIX = "model-review-"
MAX_OUTPUT_CHARS = 300
PRIVATE_SUITE = "private"   # the suite name a run shows when its suite came from the bucket (suite_key)
PRIVATE_TITLE = re.compile(r" / private \[([A-Za-z0-9][A-Za-z0-9_-]{0,63})\]$")

PENDING_STATUSES = {"queued", "requested", "waiting", "pending"}


class ReviewError(RuntimeError):
    """Something went wrong talking to GitHub (auth, missing workflow, network)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # surface 3xx as HTTPError so we can drop the auth header


class ModelReviewClient:
    def __init__(self, repo: str | None = None, token: str | None = None, ref: str | None = None,
                 workflow: str | None = None, api_url: str | None = None):
        self.repo = repo or os.environ.get("MODEL_REVIEW_REPO") or DEFAULT_REPO
        self.token = (token or os.environ.get("MODEL_REVIEW_TOKEN") or os.environ.get("GITHUB_TOKEN")
                      or os.environ.get("GH_TOKEN"))
        self._ref = ref or os.environ.get("MODEL_REVIEW_REF")
        self.workflow = workflow or os.environ.get("MODEL_REVIEW_WORKFLOW") or DEFAULT_WORKFLOW
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self._opener = urllib.request.build_opener(_NoRedirect)

    # ---------- HTTP ----------

    def _request(self, method: str, path: str, body: dict | None = None,
                 auth: bool = True, raw: bool = False):
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "model-review-client"}
        if auth:
            if not self.token:
                raise ReviewError("No GitHub token. Set MODEL_REVIEW_TOKEN (fine-grained PAT with Actions read/write).")
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=60) as resp:
                payload = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
            if location:
                # Artifact downloads redirect to blob storage: never send the GitHub token there.
                same_host = urllib.parse.urlsplit(location).netloc == urllib.parse.urlsplit(url).netloc
                return self._request("GET", location, auth=auth and same_host, raw=raw)
            detail = exc.read().decode("utf-8", "replace")[:400]
            hint = ""
            if exc.code == 401:
                hint = " (token missing or invalid)"
            elif exc.code in (403, 404):
                hint = (f" (check the token can use Actions on {self.repo}, and that "
                        f".github/workflows/{self.workflow} exists on the default branch)")
            raise ReviewError(f"GitHub {method} {url} -> HTTP {exc.code}{hint}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ReviewError(f"Could not reach {url}: {exc.reason}") from exc
        if raw:
            return status, payload
        return status, (json.loads(payload) if payload else None)

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self.repo}{suffix}"

    @property
    def ref(self) -> str:
        if not self._ref:
            _, info = self._request("GET", self._repo_path(""))
            self._ref = info.get("default_branch") or "main"
        return self._ref

    # ---------- API ----------

    def start_review(self, model: str, suite: str = "smoke", backend: str = "ollama",
                     endpoint: str | None = None, runner: str | list | None = None,
                     suite_key: str | None = None) -> dict:
        if not model:
            raise ReviewError("model is required")
        request_id = uuid.uuid4().hex[:12]
        if suite_key:
            from .private import PrivateError, check_suite_key
            try:
                check_suite_key(suite_key)
            except PrivateError as exc:
                raise ReviewError(str(exc)) from None
            suite = PRIVATE_SUITE
        elif suite == PRIVATE_SUITE:
            raise ReviewError("the suite name 'private' is reserved for suite_key reviews")
        inputs = {"model": model, "suite": suite, "backend": backend, "request_id": request_id}
        if suite_key:
            inputs["suite_key"] = suite_key
        if endpoint:
            inputs["endpoint"] = endpoint
        if runner:
            inputs["runner"] = json.dumps(runner) if isinstance(runner, list) else str(runner)
        status, payload = self._request(
            "POST", self._repo_path(f"/actions/workflows/{self.workflow}/dispatches"),
            {"ref": self.ref, "inputs": inputs, "return_run_details": True},
        )
        run_id = (payload or {}).get("workflow_run_id") if status == 200 else None
        html_url = (payload or {}).get("html_url")
        if not run_id:  # older API behaviour: 204 No Content, so find the run by request_id
            run = self._find_run(request_id)
            run_id, html_url = run["id"], run.get("html_url")
        return {"state": "queued", "run_id": run_id, "run_url": html_url, "request_id": request_id,
                "model": model, "suite": suite, "backend": backend}

    def _find_run(self, request_id: str, timeout_s: float = 90) -> dict:
        deadline = time.monotonic() + timeout_s
        path = self._repo_path(f"/actions/workflows/{self.workflow}/runs?event=workflow_dispatch&per_page=30")
        while time.monotonic() < deadline:
            _, payload = self._request("GET", path)
            for run in (payload or {}).get("workflow_runs", []):
                if request_id in (run.get("display_title") or ""):
                    return run
            time.sleep(3)
        raise ReviewError(f"Dispatched, but no run with request id {request_id} appeared within {timeout_s:.0f}s")

    def fetch_results(self, run_id: int) -> dict | None:
        _, payload = self._request("GET", self._repo_path(f"/actions/runs/{run_id}/artifacts?per_page=100"))
        for art in (payload or {}).get("artifacts", []):
            if art.get("name", "").startswith(ARTIFACT_PREFIX) and not art.get("expired"):
                _, blob = self._request("GET", art["archive_download_url"], raw=True)
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    names = [n for n in zf.namelist() if n.endswith("results.json")]
                    if names:
                        return json.loads(zf.read(names[0]))
        return None

    def _failed_steps(self, run_id: int) -> list[str]:
        try:
            _, payload = self._request("GET", self._repo_path(f"/actions/runs/{run_id}/jobs"))
        except ReviewError:
            return []
        failed = []
        for job in (payload or {}).get("jobs", []):
            for step in job.get("steps") or []:
                if step.get("conclusion") in ("failure", "cancelled", "timed_out"):
                    failed.append(step.get("name", "?"))
            if job.get("conclusion") in ("failure", "cancelled", "timed_out") and not failed:
                failed.append(f"job '{job.get('name')}' ({job.get('conclusion')})")
        return failed

    def get_review(self, run_id: int) -> dict:
        _, run = self._request("GET", self._repo_path(f"/actions/runs/{run_id}"))
        base = {"run_id": run_id, "run_url": run.get("html_url")}
        status = run.get("status")
        if status in PENDING_STATUSES:
            return {**base, "state": "queued",
                    "message": "Waiting for a runner. Call get_review again in a minute or two."}
        if status != "completed":
            return {**base, "state": "running",
                    "message": "The review is running. Call get_review again in a minute or two."}

        private = PRIVATE_TITLE.search(run.get("display_title") or "")
        if private:  # a private suite's results are in the bucket, never in an artifact
            from .private import PrivateError, read_results
            try:
                results = read_results(private.group(1))
            except PrivateError as exc:
                raise ReviewError(str(exc)) from None
        else:
            results = self.fetch_results(run_id)
        if results is None:
            steps = self._failed_steps(run_id)
            where = f" Failed at: {', '.join(steps)}." if steps else ""
            return {**base, "state": "error", "verdict": None, "conclusion": run.get("conclusion"),
                    "failed_steps": steps,
                    "message": f"The run finished ({run.get('conclusion')}) without results.{where} "
                               f"Open the run log: {run.get('html_url')}"}
        return {**base, **summarize_results(results)}

    def wait_for_review(self, run_id: int, timeout_s: float = 1800, poll_s: float = 15) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            review = self.get_review(run_id)
            if review["state"] not in ("queued", "running") or time.monotonic() >= deadline:
                return review
            time.sleep(max(1.0, min(poll_s, deadline - time.monotonic())))

    def review_model(self, model: str, suite: str = "smoke", backend: str = "ollama",
                     endpoint: str | None = None, runner: str | list | None = None,
                     wait_s: float = 1800, poll_s: float = 15, suite_key: str | None = None) -> dict:
        started = self.start_review(model, suite, backend, endpoint, runner, suite_key)
        if wait_s <= 0:
            return {**started, "message": "Review started. Call get_review with this run_id for the verdict."}
        review = self.wait_for_review(started["run_id"], wait_s, poll_s)
        return {"request_id": started["request_id"], **review}

    def list_suites(self) -> list[dict]:
        path = self._repo_path(f"/contents/{SUITES_PATH}?ref={urllib.parse.quote(self.ref)}")
        _, items = self._request("GET", path)
        suites = []
        for item in items or []:
            name = item.get("name", "")
            if item.get("type") == "file" and name.endswith((".yaml", ".yml")):
                suites.append({"name": name.rsplit(".", 1)[0], "path": item.get("path")})
        return suites


def summarize_results(results: dict) -> dict:
    """Compact, agent-friendly view of results.json (full detail stays in the artifact)."""
    verdict = results.get("verdict")
    state = {"PASS": "passed", "FAIL": "failed"}.get(verdict, "error")
    s = results.get("summary", {})
    failed_cases = []
    for case in results.get("cases", []):
        if case.get("passed"):
            continue
        entry = {"id": case.get("id")}
        if case.get("error"):
            entry["error"] = case["error"][:MAX_OUTPUT_CHARS]
        else:
            entry["failed_checks"] = [
                f"{c['check']}: {c['detail']}" if c.get("detail") else c["check"]
                for c in case.get("checks", []) if not c.get("passed")
            ]
            entry["output"] = (case.get("output") or "")[:MAX_OUTPUT_CHARS]
        failed_cases.append(entry)
    failed_gates = [g for g in results.get("gates", []) if not g.get("passed")]
    if results.get("error"):
        message = f"ERROR during {results.get('stage') or 'setup'}: {results['error']}"
    else:
        message = (f"{verdict}: {s.get('passed')}/{s.get('total')} cases passed "
                   f"(threshold {s.get('pass_threshold')})")
        if verdict == "ERROR":
            message += "; no case got an answer from the model endpoint"
    if failed_gates:
        message += "; failed gates: " + ", ".join(g["name"] for g in failed_gates)
    return {
        "state": state,
        "verdict": verdict,
        "model": results.get("model"),
        "suite": results.get("suite"),
        "backend": results.get("backend"),
        "summary": s,
        "gates": results.get("gates", []),
        "failed_cases": failed_cases,
        "error": results.get("error"),
        "environment": results.get("environment", {}),
        "message": message,
    }


def _exit_code(review: dict) -> int:
    return {"passed": 0, "queued": 0, "running": 0, "failed": 1}.get(review.get("state"), 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="model-review", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="owner/repo (default MODEL_REVIEW_REPO or Kaycee7000/ear)")
    parser.add_argument("--ref", help="branch to run the workflow from")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rv = sub.add_parser("review", help="start a review and (by default) wait for the verdict")
    rv.add_argument("--model", required=True)
    rv.add_argument("--suite", default="smoke")
    rv.add_argument("--suite-key", help="private suite in the EAR bucket (model-review/suites/...yaml)")
    rv.add_argument("--backend", default="ollama", choices=["ollama", "endpoint", "mock"])
    rv.add_argument("--endpoint")
    rv.add_argument("--runner", help='runner label or JSON list, e.g. \'["self-hosted","gpu"]\'')
    rv.add_argument("--wait", type=float, default=1800, help="seconds to wait (default 1800)")
    rv.add_argument("--no-wait", action="store_true", help="return right after dispatching")

    st = sub.add_parser("status", help="state / verdict of a run")
    st.add_argument("run_id", type=int)
    st.add_argument("--wait", type=float, default=0, help="seconds to wait for it to finish")

    sub.add_parser("suites", help="list suites available in the repo")

    args = parser.parse_args(argv)
    client = ModelReviewClient(repo=args.repo, ref=args.ref)
    try:
        if args.cmd == "review":
            out = client.review_model(args.model, args.suite, args.backend, args.endpoint,
                                      args.runner, wait_s=0 if args.no_wait else args.wait,
                                      suite_key=args.suite_key)
        elif args.cmd == "status":
            out = (client.wait_for_review(args.run_id, args.wait) if args.wait > 0
                   else client.get_review(args.run_id))
        else:
            out = {"suites": client.list_suites()}
    except ReviewError as exc:
        print(json.dumps({"state": "error", "message": str(exc)}, indent=2))
        return 2
    print(json.dumps(out, indent=2))
    return _exit_code(out) if args.cmd != "suites" else 0


if __name__ == "__main__":
    sys.exit(main())
