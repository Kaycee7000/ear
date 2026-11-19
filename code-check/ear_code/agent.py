"""Code checks reuse EAR's SQLite admission, dispatch reconciliation and bounded polling."""

import asyncio
import json
import math
import sqlite3
import time
from typing import Any

from ear_document.agent import Agent, problem
from ear_document.contract import MESSAGES, Issue
from ear_document.github import GitHub
from ear_document.storage import B2, base_key
from pydantic import ValidationError

from .plan import ERRORS, MIME, Hash, Key, Lookup, Manifest, Plan, Project, Request, Size, Source, Submit, Upload, Wait
from .settings import config, credentials, storage, value

MESSAGES_CODE = {
    **MESSAGES,
    **ERRORS,
    "invalid_input": "Check the source handle, project name and plan.",
    "busy": "The active code-check limit was reached.",
    "worker_failed": "The code-check workflow ended before publishing a verified result.",
}


def clean(reply):
    if code := reply.get("error_code"):
        reply["message"] = MESSAGES_CODE.get(code, "Code check failed.")
    return reply


class CodeGitHub(GitHub):
    def matches(self, run, row):
        return (
            run.get("display_title") == "ear-code-" + row["job_id"]
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == self.cfg.ref
            and run.get("path") == ".github/workflows/" + self.cfg.workflow
        )


class CodeB2(B2):
    async def verify(self, row, prefix, cached=None):
        base = base_key(prefix, row["job_id"], row["run_id"], row["attempt"])
        try:
            data = cached if cached is not None else await self._read("GET", base + "/result.json")
            manifest = Manifest.model_validate(data)
            req = Request.model_validate_json(row["request_json"])
        except ValueError:
            raise Issue("output_invalid") from None
        if (
            manifest.job_id,
            manifest.workflow_run_id,
            manifest.run_attempt,
            manifest.request_sha256,
            manifest.mode,
            manifest.project,
        ) != (
            row["job_id"],
            row["run_id"],
            row["attempt"],
            row["request_sha256"],
            req.plan.mode,
            req.project,
        ):
            raise Issue("output_invalid")
        files = [manifest.report] + manifest.artifacts + [s.log for s in manifest.steps if s.log]
        for item in files:
            if not item.object_key.startswith(base + "/"):
                raise Issue("output_invalid")
        # The report and every artifact must exist with the recorded size and checksum.
        for item in [manifest.report] + manifest.artifacts:
            head = await self._read("HEAD", item.object_key)
            if (
                head.get("content-length") != str(item.byte_size)
                or head.get("x-amz-meta-sha256") != item.sha256
            ):
                raise Issue("output_invalid")
        return manifest.model_dump()

    async def worker_error(self, row, prefix):
        try:
            obj = await self._read(
                "GET", base_key(prefix, row["job_id"], row["run_id"], row["attempt"]) + "/error.json"
            )
            if (
                isinstance(obj, dict)
                and (obj.get("job_id"), obj.get("workflow_run_id"), obj.get("run_attempt"))
                == (row["job_id"], row["run_id"], row["attempt"])
                and obj.get("error_code") in ERRORS
            ):
                return obj["error_code"]
        except Issue:
            pass
        return "worker_failed"


class CodeAgent(Agent):
    async def start(self, row):
        if self.requests is None:
            self.requests = B2(self.cfg.region, self.cfg.bucket, *credentials("INPUT_WRITE"))
        return await super().start(row)

    def render(self, row):
        if row["status"] == "failed":
            code = row["error"]
            return {
                k: v
                for k, v in dict(
                    status="failed",
                    job_id=row["job_id"],
                    error_code=code,
                    message=MESSAGES_CODE.get(code, "Code check failed."),
                    workflow_run_id=row["run_id"],
                    run_attempt=row["attempt"],
                ).items()
                if v is not None
            }
        if row["status"] != "completed":
            return clean(super().render(row))
        doc = json.loads(row["manifest"])
        for item in [doc["report"], *doc["artifacts"], *[s["log"] for s in doc["steps"] if s.get("log")]]:
            url, expires = self.b2.sign(item["object_key"], self.cfg.ttl)
            item.update(download_url=url, expires_at=expires)
        return {"status": "completed", **doc}

    async def check(self, job_id):
        row = self.store.get(job_id)
        if row["target"] != self.cfg.target:
            raise Issue("configuration")
        if row["status"] == "failed" and row["next_check"] > time.time():
            return {
                "status": "checking",
                "job_id": job_id,
                "workflow_run_id": row["run_id"],
                "retry_after_seconds": max(1, math.ceil(row["next_check"] - time.time())),
            }
        return clean(await super().check(job_id))

    async def submit(self, req):
        row = None
        end = self.deadline(req.wait_seconds)
        try:
            async with asyncio.timeout(end - time.monotonic()):
                try:
                    row, new = self.store.reserve(req)
                except Issue as exc:
                    if exc.code != "busy":
                        raise
                    for job in self.store.active_jobs():
                        await self.check(job)
                    row, new = self.store.reserve(req)
                if new or row["status"] == "staging":
                    reply = await self.check(row["job_id"])
                    if not req.wait_seconds or reply["status"] == "failed":
                        return reply
                return await self.watch(row["job_id"], min(req.wait_seconds, self.cfg.wait), end)
        except TimeoutError:
            return self.after_timeout(row["job_id"]) if row else clean(problem("busy"))


async def invoke(request):
    cfg = config()
    if not (token := value("GITHUB_TOKEN")):
        raise Issue("configuration")
    outputs = CodeB2(cfg.region, cfg.bucket, *credentials("RESULT_READ"))
    inbox = (
        B2(cfg.region, cfg.bucket, *credentials("INPUT_WRITE")) if isinstance(request, Submit) else None
    )
    gh = CodeGitHub(cfg, token)
    try:
        from ear_document.store import Store

        agent = CodeAgent(cfg, Store(cfg), gh, outputs, inbox)
        if isinstance(request, Submit):
            return await agent.submit(request)
        return await agent.get_document_result(request.job_id, request.wait_seconds)
    finally:
        await gh.http.aclose()


async def _call(model, kwargs):
    try:
        return clean(await invoke(model.model_validate(kwargs)))
    except ValidationError:
        return clean(problem("invalid_input"))
    except Issue as exc:
        reply = clean(problem(exc.code))
        if exc.delay:
            reply["retry_after_seconds"] = exc.delay
        return reply
    except (OSError, sqlite3.Error):
        return clean(problem("configuration"))


async def run_code_check(
    source: Source, project: Project, plan: Plan, wait_seconds: Wait = 0, request_id: Key | None = None
) -> dict[str, Any]:
    """Review a project's diff with the coding model, or build and test it in a disposable container and explain a failure."""
    return await _call(
        Submit,
        dict(source=source, project=project, plan=plan, wait_seconds=wait_seconds, request_id=request_id),
    )


async def get_code_result(job_id: str, wait_seconds: Wait = 0) -> dict[str, Any]:
    """Resume a code check: steps, review or failure analysis, and fresh B2 links to the report, logs and artifacts."""
    return await _call(Lookup, dict(job_id=job_id, wait_seconds=wait_seconds))


def prepare_source_upload(sha256: Hash, byte_size: Size) -> dict[str, Any]:
    """Prepare a ten-minute direct B2 PUT for a project archive (.tar.gz) with a known checksum and size."""
    try:
        req = Upload(sha256=sha256, byte_size=byte_size)
        region, bucket, prefix = storage()
        inbox = B2(region, bucket, *credentials("INPUT_WRITE"))
        handle = req.sha256 + ".tar.gz"
        url = inbox.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": f"{prefix}/requests/sources/{handle}",
                "ContentLength": req.byte_size,
                "ContentType": MIME["gz"],
            },
            ExpiresIn=600,
            HttpMethod="PUT",
        )
        return {
            "status": "upload_ready",
            "handle": handle,
            "upload_url": url,
            "method": "PUT",
            "content_type": MIME["gz"],
            "byte_size": req.byte_size,
            "expires_in_seconds": 600,
        }
    except ValidationError:
        return clean(problem("invalid_input"))
    except Issue as exc:
        return clean(problem(exc.code))
