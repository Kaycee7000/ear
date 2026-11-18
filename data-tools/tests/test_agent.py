import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from ear_document.contract import Issue
from ear_document.store import Store
from pydantic import ValidationError

from ear_data import prepare_dataset_upload, run_data_job
from ear_data.agent import DataAgent, DataB2, DataGitHub
from ear_data.plan import MAX_FILE, MIME, Request, Submit
from ear_data.settings import config

HANDLE = "a" * 64 + ".csv"
PLAN = {"sql": "SELECT * FROM sales", "export": "csv", "chart": {"kind": "line", "x": "x", "y": "y"}}


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "EAR_DATA_DB": str(Path(self.tmp.name) / "jobs.sqlite"),
                "EAR_B2_REGION": "us-west-004",
                "EAR_B2_BUCKET": "private-bucket",
                "EAR_B2_KEY_ID": "test",
                "EAR_B2_APPLICATION_KEY": "test",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        self.cfg = config()
        self.st = Store(self.cfg)
        self.gh = AsyncMock()
        self.gh.dispatch.return_value = {"run_id": 11, "run_url": "api", "html_url": "web"}
        self.gh.get.return_value = {"status": "in_progress", "run_attempt": 1}
        self.inbox = AsyncMock()
        self.b2 = DataB2(self.cfg.region, self.cfg.bucket, "test", "test")
        self.agent = DataAgent(self.cfg, self.st, self.gh, self.b2, self.inbox)

    def req(self, **kw):
        return Submit(datasets={"sales": HANDLE}, plan=PLAN, **kw)

    async def test_schema_and_scoped_blob_upload(self):
        for names in (
            {"../bad": HANDLE},
            {"sales": "/etc/file.csv"},
            {"t": "https://example.org/file.csv"},
        ):
            self.assertEqual((await run_data_job(names, PLAN))["error_code"], "invalid_input")
        for plan in (
            {"profile": "other"},
            {"sql": "select 1", "profile": "sales"},
            {"shell": "echo"},
            {"profile": "sales", "export": "zip"},
        ):
            with self.assertRaises(ValidationError):
                Request(datasets={"sales": HANDLE}, plan=plan)
        for size in (0, MAX_FILE + 1):
            self.assertEqual(prepare_dataset_upload("a" * 64, "csv", size)["status"], "failed")
        link = prepare_dataset_upload("a" * 64, "csv", 100)
        self.assertIn("/private-bucket/data/requests/datasets/" + HANDLE, link["upload_url"])
        self.assertIn("content-length", link["upload_url"])
        self.assertEqual(link["method"], "PUT")

    async def test_cli_upload_transmits_original_bytes(self):
        import hashlib

        from ear_data.cli import upload_file

        path = Path(self.tmp.name) / "data.csv"
        body = b"id,qty\n1,7\n"
        path.write_bytes(body)

        def put(url, *, content, headers, timeout):
            self.assertEqual(b"".join(content), body)
            self.assertEqual(headers["content-length"], str(len(body)))
            self.assertEqual(headers["content-type"], "text/csv")
            return httpx.Response(200, request=httpx.Request("PUT", url))

        with patch("ear_data.cli.httpx.put", side_effect=put):
            self.assertEqual(upload_file(path)["handle"], hashlib.sha256(body).hexdigest() + ".csv")

    async def test_restart_idempotency_reconciliation_limits(self):
        self.gh.dispatch.side_effect = Issue("remote_unavailable")
        first = await self.agent.submit(self.req(request_id="a"))
        job = first["job_id"]
        self.st.update(self.st.get(job), next_check=0)
        self.gh.reconcile.return_value = {"run_id": 11, "run_url": "api", "html_url": "web"}
        resumed = DataAgent(self.cfg, Store(self.cfg), self.gh, self.b2, self.inbox)
        self.assertEqual((await resumed.submit(self.req(request_id="a")))["status"], "running")
        self.gh.dispatch.assert_awaited_once()
        self.assertEqual(self.st.get(job)["run_id"], 11)
        with self.assertRaises(Issue) as conflict:
            await resumed.submit(
                Submit(datasets={"sales": HANDLE}, plan={"profile": "sales"}, request_id="a")
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        with self.assertRaises(Issue) as busy:
            await resumed.submit(self.req())
        self.assertEqual(busy.exception.code, "busy")
        self.st.update(self.st.get(job), next_check=0)
        self.gh.get.return_value = {"status": "completed", "conclusion": "cancelled", "run_attempt": 1}
        self.assertEqual((await resumed.check(job))["error_code"], "cancelled")
        with self.assertRaises(Issue) as limit:
            Store(replace(self.cfg, daily=1)).reserve(self.req())
        self.assertEqual(limit.exception.code, "daily_limit")

    async def test_manifest_all_artifacts_rerun_and_fresh_links(self):
        job = (await self.agent.submit(self.req()))["job_id"]
        base = f"data/results/{job}/11/1"

        def art(name, mime):
            return {
                "object_key": base + "/" + name,
                "mime_type": mime,
                "byte_size": 12,
                "sha256": "b" * 64,
            }

        m = {
            "job_id": job,
            "request_sha256": self.st.get(job)["request_sha256"],
            "version": 1,
            "workflow_run_id": 11,
            "run_attempt": 1,
            "format": "csv",
            "rows": 1,
            "columns": 2,
            "completed_at": "2026-11-18T00:00:00+00:00",
            "table": art("table.csv", MIME["csv"]),
            "chart": art("chart.png", "image/png"),
        }

        async def read(method, key):
            if method == "GET":
                return m
            return {
                "content-type": "image/png" if key.endswith(".png") else MIME["csv"],
                "content-length": "12",
                "x-amz-meta-sha256": "b" * 64,
            }

        self.b2._read = AsyncMock(side_effect=read)
        self.gh.get.return_value = {"status": "completed", "conclusion": "success", "run_attempt": 1}
        with patch.object(
            self.b2,
            "sign",
            side_effect=[("one", "expiry"), ("png1", "expiry"), ("two", "fresh"), ("png2", "fresh")],
        ):
            self.assertEqual((await self.agent.check(job))["table"]["download_url"], "one")
            self.assertEqual((await self.agent.check(job))["chart"]["download_url"], "png2")
        self.assertNotIn("download_url", self.st.get(job)["manifest"])
        self.gh.get.return_value["run_attempt"] = 2
        self.assertEqual((await self.agent.check(job))["error_code"], "output_invalid")
        self.b2._read.side_effect = Issue("output_missing")
        self.assertEqual((await self.agent.check(job))["error_code"], "output_missing")

    async def test_dispatch_wire_private_identity(self):
        cfg = self.cfg
        job = str(uuid4())

        def handler(req):
            self.assertEqual(
                json.loads(req.content)["inputs"], {"job_id": job, "request_sha256": "a" * 64}
            )
            return httpx.Response(
                200,
                json={
                    "workflow_run_id": 11,
                    "run_url": f"https://api.github.com/repos/{cfg.repo}/actions/runs/11",
                    "html_url": f"https://github.com/{cfg.repo}/actions/runs/11",
                },
            )

        gh = DataGitHub(cfg, "test", httpx.MockTransport(handler))
        self.addAsyncCleanup(gh.http.aclose)
        self.assertEqual((await gh.dispatch({"job_id": job, "request_sha256": "a" * 64}))["run_id"], 11)
        run = {
            "event": "workflow_dispatch",
            "head_branch": "main",
            "display_title": "ear-data-" + job,
            "path": ".github/workflows/data-tools.yml",
        }
        self.assertTrue(gh.matches(run, {"job_id": job}))
        self.assertFalse(gh.matches({**run, "display_title": "Build data toolbox"}, {"job_id": job}))

    async def test_wait_timeout_and_sanitized_worker_error(self):
        job = (await self.agent.submit(self.req()))["job_id"]

        async def slow(_):
            await asyncio.sleep(10)

        self.gh.get.side_effect = slow
        start = time.monotonic()
        self.assertEqual((await self.agent.get_document_result(job, 1))["status"], "queued")
        self.assertLess(time.monotonic() - start, 2)
        self.gh.get.side_effect = None
        self.gh.get.return_value = {"status": "completed", "conclusion": "failure", "run_attempt": 1}
        self.b2._read = AsyncMock(
            return_value={
                "job_id": job,
                "workflow_run_id": 11,
                "run_attempt": 1,
                "request_sha256": self.st.get(job)["request_sha256"],
                "error_code": "query_invalid",
                "raw": "private",
            }
        )
        reply = await self.agent.check(job)
        self.assertEqual(reply["error_code"], "query_invalid")
        self.assertNotIn("private", str(reply))
