from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from .contract import Issue


def retry_delay(headers):
    value = headers.get("retry-after")
    now = datetime.now(UTC).timestamp()
    try:
        if value and not value.isdecimal():
            return max(1, int(parsedate_to_datetime(value).timestamp() - now) + 1)
        if value:
            return max(1, int(value))
        if headers.get("x-ratelimit-remaining") == "0":
            return max(1, int(headers["x-ratelimit-reset"]) - int(now) + 1)
    except (ValueError, KeyError, OverflowError, TypeError):
        pass
    return 60


class GitHub:
    def __init__(self, cfg, token, transport=None):
        self.cfg = cfg
        self.http = httpx.AsyncClient(
            base_url=f"https://api.github.com/repos/{cfg.repo}/actions/",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            timeout=httpx.Timeout(6, connect=3),
            transport=transport,
            follow_redirects=False,
        )

    async def call(self, method, path, **kwargs):
        try:
            res = await self.http.request(method, path, **kwargs)
        except httpx.TransportError:
            raise Issue("remote_unavailable", 15) from None
        if res.status_code == 429 or (
            res.status_code == 403
            and (
                "retry-after" in res.headers
                or res.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in res.text[:1000].lower()
            )
        ):
            raise Issue("rate_limited", retry_delay(res.headers))
        if res.status_code >= 500 or res.status_code == 408:
            raise Issue("remote_unavailable", 15)
        if res.status_code == 404 and method == "GET" and path.startswith("runs/"):
            return None
        if res.is_error:
            raise Issue("dispatch_rejected" if method == "POST" else "github_access")
        try:
            return res.json()
        except ValueError:
            # Includes legacy 204 or incomplete success replies. Reconcile before any new POST.
            raise Issue("remote_unavailable", 15) from None

    def mapping(self, run_id, api, html):
        if (
            type(run_id) is not int
            or run_id < 1
            or not isinstance(api, str)
            or api.casefold()
            != f"https://api.github.com/repos/{self.cfg.repo}/actions/runs/{run_id}".casefold()
            or not isinstance(html, str)
            or html.casefold() != f"https://github.com/{self.cfg.repo}/actions/runs/{run_id}".casefold()
        ):
            raise Issue("remote_unavailable", 15)
        return dict(run_id=run_id, run_url=api, html_url=html)

    async def dispatch(self, row):
        data = await self.call(
            "POST",
            f"workflows/{quote(self.cfg.workflow)}/dispatches",
            json={
                "ref": self.cfg.ref,
                "inputs": {"job_id": row["job_id"], "request_sha256": row["request_sha256"]},
            },
        )
        if not isinstance(data, dict):
            raise Issue("remote_unavailable", 15)
        return self.mapping(data.get("workflow_run_id"), data.get("run_url"), data.get("html_url"))

    def matches(self, run, row):
        return (
            run.get("display_title") == "ear-document-" + row["job_id"]
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == self.cfg.ref
            and run.get("path") == ".github/workflows/" + self.cfg.workflow
        )

    async def reconcile(self, row):
        def date(seconds):
            return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            "event": "workflow_dispatch",
            "branch": self.cfg.ref,
            "per_page": 100,
            "created": date(row["dispatch_at"] - 120) + ".." + date(row["dispatch_at"] + 3600),
        }
        found = {}
        for page in range(1, 11):
            data = await self.call(
                "GET", f"workflows/{quote(self.cfg.workflow)}/runs", params={**params, "page": page}
            )
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("workflow_runs"), list)
                or type(data.get("total_count")) is not int
            ):
                raise Issue("remote_unavailable", 15)
            runs = data["workflow_runs"]
            for run in runs:
                if not isinstance(run, dict):
                    raise Issue("remote_unavailable", 15)
                if self.matches(run, row):
                    mapping = self.mapping(run.get("id"), run.get("url"), run.get("html_url"))
                    found[mapping["run_id"]] = mapping
            if len(found) > 1:
                raise Issue("multiple_runs")
            if len(runs) < 100 or page * 100 >= data["total_count"]:
                return next(iter(found.values()), None)
        raise Issue("remote_unavailable", 60)  # A partial search cannot establish absence.

    async def get(self, row):
        run = await self.call("GET", f"runs/{row['run_id']}")
        if run is not None and (not isinstance(run, dict) or not isinstance(run.get("status"), str)):
            raise Issue("remote_unavailable", 15)
        if run is not None and (
            not self.matches(run, row)
            or run.get("id") != row["run_id"]
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] < 1
        ):
            raise Issue("github_access")
        return run
