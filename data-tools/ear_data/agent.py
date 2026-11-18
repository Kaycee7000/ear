"""Use EAR's existing SQLite admission, dispatch reconciliation and bounded polling."""

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
from ear_document.store import Store
from pydantic import ValidationError

from .plan import (
    ERRORS,
    MIME,
    Alias,
    Handle,
    Hash,
    InFormat,
    Key,
    Lookup,
    Manifest,
    Plan,
    Request,
    Size,
    Submit,
    Upload,
    Wait,
)
from .settings import config, credentials, storage, value

MESSAGES_DATA = {
    **MESSAGES,
    **ERRORS,
    "invalid_input": "Check the dataset handles, plan and identifiers.",
    "busy": "The active data-job limit was reached.",
    "worker_failed": "The data workflow ended before publishing a verified result.",
}


def clean(reply):
    if code := reply.get("error_code"):
        reply["message"] = MESSAGES_DATA.get(code, "Data job failed.")
    return reply


class DataGitHub(GitHub):
    def matches(self, run, row):
        return (
            run.get("display_title") == "ear-data-" + row["job_id"]
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == self.cfg.ref
            and run.get("path") == ".github/workflows/" + self.cfg.workflow
        )


class DataB2(B2):
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
            manifest.format,
        ) != (
            row["job_id"],
            row["run_id"],
            row["attempt"],
            row["request_sha256"],
            row["format"],
        ) or bool(manifest.chart) != bool(req.plan.chart):
            raise Issue("output_invalid")
        artifacts = [(manifest.table, "table." + manifest.format, MIME[manifest.format])]
        if manifest.chart:
            artifacts.append((manifest.chart, "chart.png", MIME["png"]))
        for item, filename, mime in artifacts:
            if item.object_key != base + "/" + filename or item.mime_type != mime:
                raise Issue("output_invalid")
            head = await self._read("HEAD", item.object_key)
            if (
                head.get("content-type") != mime
                or head.get("content-length") != str(item.byte_size)
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
                and (
                    obj.get("job_id"),
                    obj.get("workflow_run_id"),
                    obj.get("run_attempt"),
                    obj.get("request_sha256"),
                )
                == (row["job_id"], row["run_id"], row["attempt"], row["request_sha256"])
                and isinstance(obj.get("error_code"), str)
                and obj["error_code"] in ERRORS
            ):
                return obj["error_code"]
        except Issue:
            pass
        return "worker_failed"


class DataAgent(Agent):
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
                    message=MESSAGES_DATA[code],
                    workflow_run_id=row["run_id"],
                    run_attempt=row["attempt"],
                ).items()
                if v is not None
            }
        if row["status"] != "completed":
            return clean(super().render(row))
        doc = json.loads(row["manifest"])
        for name in ("table", "chart"):
            if item := doc.get(name):
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
    outputs = DataB2(cfg.region, cfg.bucket, *credentials("RESULT_READ"))
    # Lookup may resume a crash before the private request was staged.
    inbox = (
        B2(cfg.region, cfg.bucket, *credentials("INPUT_WRITE")) if isinstance(request, Submit) else None
    )
    gh = DataGitHub(cfg, token)
    try:
        agent = DataAgent(cfg, Store(cfg), gh, outputs, inbox)
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


async def run_data_job(
    datasets: dict[Alias, Handle], plan: Plan, wait_seconds: Wait = 0, request_id: Key | None = None
) -> dict[str, Any]:
    """Analyze uploaded datasets with a read-only SQL SELECT or column profile. Export a table and an optional chart."""
    return await _call(
        Submit, dict(datasets=datasets, plan=plan, wait_seconds=wait_seconds, request_id=request_id)
    )


async def get_data_result(job_id: str, wait_seconds: Wait = 0) -> dict[str, Any]:
    """Resume a data job, verify its exports and issue fresh B2 URLs."""
    return await _call(Lookup, dict(job_id=job_id, wait_seconds=wait_seconds))


def prepare_dataset_upload(sha256: Hash, format: InFormat, byte_size: Size) -> dict[str, Any]:
    """Prepare a ten-minute direct B2 PUT for a known file checksum and byte size. The frontend uploads the raw file."""
    try:
        req = Upload(sha256=sha256, format=format, byte_size=byte_size)
        region, bucket, prefix = storage()
        inbox = B2(region, bucket, *credentials("INPUT_WRITE"))
        handle = req.sha256 + "." + req.format
        mime = MIME[req.format]
        url = inbox.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": f"{prefix}/requests/datasets/{handle}",
                "ContentLength": req.byte_size,
                "ContentType": mime,
            },
            ExpiresIn=600,
            HttpMethod="PUT",
        )
        return {
            "status": "upload_ready",
            "handle": handle,
            "upload_url": url,
            "method": "PUT",
            "content_type": mime,
            "byte_size": req.byte_size,
            "expires_in_seconds": 600,
        }
    except ValidationError:
        return clean(problem("invalid_input"))
    except Issue as exc:
        return clean(problem(exc.code))
