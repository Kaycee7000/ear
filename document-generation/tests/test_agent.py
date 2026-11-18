import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from ear_document.agent import Agent
from ear_document.config import Settings
from ear_document.contract import Issue, Submit, utc
from ear_document.github import GitHub
from ear_document.storage import B2, base_key
from ear_document.store import Store


def outline(title="Shift handoff"):
    return {"title": title, "sections": [{"heading": "Next steps", "bullets": ["Review the report."]}]}


class FakeGit:
    def __init__(self):
        self.dispatch = AsyncMock(
            return_value={"run_id": 42, "run_url": "api-url", "html_url": "web-url"}
        )
        self.reconcile = AsyncMock(return_value=None)
        self.get = AsyncMock(
            return_value={"status": "in_progress", "run_attempt": 1, "conclusion": None}
        )


class Jobs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Settings(
            Path(self.temp.name) / "jobs.sqlite3",
            "owner/repo",
            "main",
            "generate-document.yml",
            "us-west-004",
            "test-bucket",
            "documents",
            1,
            10,
            3,
            8,
            3600,
        )
        self.db = Store(self.cfg)
        self.gh = FakeGit()
        self.objects = {}
        self.b2 = B2(self.cfg.region, self.cfg.bucket, "test", "test")
        self.head = {
            "content-length": "99",
            "content-type": "application/pdf",
            "x-amz-meta-sha256": "b" * 64,
        }

        async def read(method, key):
            if method == "HEAD":
                return self.head
            return self.objects[key] if key in self.objects else self.missing()

        self.b2._read = AsyncMock(side_effect=read)
        self.requests = AsyncMock()
        self.agent = Agent(self.cfg, self.db, self.gh, self.b2, self.requests)

    @staticmethod
    def missing():
        raise Issue("output_missing")

    async def create(self, **kw):
        res = await self.agent.generate_document(outline(), "pdf", **kw)
        return res["job_id"]

    def finish(self, job, attempt=1, conclusion="success"):
        self.gh.get.return_value = {
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": attempt,
        }
        base = base_key("documents", job, 42, attempt)
        self.objects[base + "/result.json"] = dict(
            version=1,
            job_id=job,
            workflow_run_id=42,
            run_attempt=attempt,
            object_key=base + "/output.pdf",
            byte_size=99,
            mime_type="application/pdf",
            sha256="b" * 64,
            format="pdf",
            section_count=1,
            page_count=1,
            request_sha256=self.db.get(job)["request_sha256"],
            started_at=utc(),
            uploaded_at=utc(),
        )
        return base

    async def test_input_and_shell_text(self):
        for data, fmt, kwargs in [
            ({}, "docx", {}),
            (outline(""), "pdf", {}),
            (outline("a\x00"), "pptx", {}),
            (outline(), "../etc", {}),
            (outline(), "pdf", {"wait_seconds": -1}),
            (outline(), "docx", {"request_id": "../x"}),
            ({**outline(), "command": "ls"}, "pdf", {}),
            ({"title": "title", "sections": [{"heading": "empty"}]}, "pptx", {}),
        ]:
            self.assertEqual(
                (await self.agent.generate_document(data, fmt, **kwargs))["error_code"], "invalid_input"
            )
        self.assertEqual(
            (await self.agent.get_document_result("../tmp"))["error_code"], "invalid_input"
        )
        self.assertEqual(
            (await self.agent.get_document_result(str(uuid4())))["error_code"], "unknown_job"
        )
        text = '$(touch /tmp/file) ${{ secrets.EXAMPLE }} <a> & "quoted"'
        res = await self.agent.generate_document(outline(text), "pdf")
        self.assertEqual(
            json.loads(self.db.get(res["job_id"])["request_json"])["outline"]["title"], text
        )

    async def test_idempotent_restart_and_conflict(self):
        job = await self.create(request_id="req")
        restart = Agent(self.cfg, Store(self.cfg), self.gh, self.b2, self.requests)
        result = await restart.generate_document(outline(), "pdf", request_id="req")
        self.assertEqual(job, result["job_id"])
        for data, fmt in [(outline(), "pptx"), (outline("Changed"), "pdf")]:
            self.assertEqual(
                (await restart.generate_document(data, fmt, request_id="req"))["error_code"],
                "idempotency_conflict",
            )
        self.gh.dispatch.assert_awaited_once()

    async def test_ambiguous_post_recovered_after_restart(self):
        self.gh.dispatch.side_effect = Issue("remote_unavailable")
        job = await self.create(request_id="lost")
        self.assertIsNone(self.db.get(job)["run_id"])
        self.db.update(self.db.get(job), next_check=0)
        self.gh.reconcile.return_value = {"run_id": 42, "run_url": "api-url", "html_url": "web-url"}
        self.finish(job)
        result = await Agent(
            self.cfg, Store(self.cfg), self.gh, self.b2, self.requests
        ).generate_document(outline(), "pdf", request_id="lost")
        self.assertEqual((result["status"], result["workflow_run_id"]), ("completed", 42))
        self.gh.dispatch.assert_awaited_once()

    async def test_unconfirmed_dispatch_releases_slot_after_complete_search(self):
        self.gh.dispatch.side_effect = Issue("remote_unavailable")
        job = await self.create()
        with self.db.connection() as db:
            db.execute(
                "UPDATE jobs SET dispatch_at=dispatch_at-601, next_check=0 WHERE job_id=?", (job,)
            )
        self.assertEqual(
            (await self.agent.get_document_result(job))["error_code"], "dispatch_unconfirmed"
        )
        self.gh.reconcile.assert_awaited_once()
        self.gh.dispatch.assert_awaited_once()

    async def test_cap_and_busy_reclamation(self):
        job = await self.create()
        self.assertEqual((await self.agent.generate_document(outline("More"), "pdf"))["status"], "busy")
        self.finish(job, conclusion="cancelled")
        self.db.update(self.db.get(job), next_check=0)
        cap = replace(self.cfg, daily=1)
        result = await Agent(cap, Store(cap), self.gh, self.b2, self.requests).generate_document(
            outline("More"), "pdf"
        )
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(self.db.get(job)["error"], "cancelled")

    async def test_transactional_concurrent_admission(self):
        def reserve(i):
            try:
                return self.db.reserve(Submit(outline=outline(str(i)), format="pdf"))[1]
            except Issue as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=6) as pool:
            states = list(pool.map(reserve, range(6)))
        self.assertEqual(states.count(True), 1)
        self.assertEqual(states.count("busy"), 5)

    async def test_wait_deadline_does_not_cancel_job(self):
        job = await self.create()

        async def slow(row):
            await asyncio.sleep(10)

        self.gh.get.side_effect = slow
        start = time.monotonic()
        result = await self.agent.get_document_result(job, wait_seconds=1)
        self.assertLess(time.monotonic() - start, 1.8)
        self.assertIn(result["status"], ("queued", "running"))
        self.gh.get.side_effect = None
        self.finish(job)
        self.assertEqual((await self.agent.get_document_result(job))["status"], "completed")

    async def test_failure_cancel_timeout_and_sanitization(self):
        job = await self.create()
        for conclusion, code in [
            ("cancelled", "cancelled"),
            ("timed_out", "timed_out"),
            ("failure", "worker_failed"),
        ]:
            base = self.finish(job, conclusion=conclusion)
            self.objects[base + "/error.json"] = {
                "error_code": "token=secret",
                "job_id": job,
                "workflow_run_id": 42,
                "run_attempt": 1,
                "message": "private prompt",
            }
            reply = await self.agent.get_document_result(job)
            self.assertEqual(reply["error_code"], code)
            self.assertNotIn("secret", str(reply))
        self.objects[base + "/error.json"]["error_code"] = "invalid_output"
        self.assertEqual((await self.agent.get_document_result(job))["error_code"], "invalid_output")
        self.assertFalse(any(call.args[0] == "HEAD" for call in self.b2._read.await_args_list))

    async def test_missing_mismatched_and_corrupt_outputs(self):
        job = await self.create()
        base = self.finish(job)
        good = self.objects.pop(base + "/result.json")
        self.assertEqual((await self.agent.get_document_result(job))["error_code"], "output_missing")
        for changes in [
            {"run_attempt": 2},
            {"job_id": str(uuid4())},
            {"object_key": "other/output.pdf"},
            {"format": "pptx"},
            {"sha256": "a" * 64},
            {"request_sha256": "c" * 64},
            {"byte_size": 100},
            {"page_count": -1},
        ]:
            self.objects[base + "/result.json"] = {**good, **changes}
            self.assertEqual(
                (await self.agent.get_document_result(job))["error_code"], "output_invalid"
            )
        self.objects[base + "/result.json"] = good
        self.b2._read.side_effect = Issue("output_missing")
        self.assertEqual((await self.agent.get_document_result(job))["error_code"], "output_missing")

    async def test_rerun_and_fresh_urls_even_after_history_expires(self):
        job = await self.create()
        self.finish(job)
        with patch.object(
            self.b2, "sign", side_effect=[("first", "expired"), ("second", "fresh"), ("third", "fresh")]
        ):
            original = await self.agent.get_document_result(job)
            self.assertEqual(original["download_url"], "first")
            self.assertNotIn('"download_url"', self.db.get(job)["manifest"])
            self.gh.get.return_value = None
            self.assertEqual((await self.agent.get_document_result(job))["download_url"], "second")
            self.gh.get.return_value = {"status": "in_progress", "run_attempt": 2, "conclusion": None}
            self.assertNotIn("download_url", await self.agent.get_document_result(job))
            self.db.update(self.db.get(job), next_check=0)
            self.finish(job, 2)
            reply = await self.agent.get_document_result(job)
            self.assertEqual((reply["run_attempt"], reply["download_url"]), (2, "third"))
            self.assertNotEqual(original["object_key"], reply["object_key"])
        self.gh.dispatch.assert_awaited_once()

    async def test_explicit_dispatch_throttle_survives_restart(self):
        self.gh.dispatch.side_effect = Issue("rate_limited", 60)
        denied = await self.agent.generate_document(outline("First"), "pdf")
        self.assertEqual(denied["error_code"], "rate_limited")
        agent = Agent(self.cfg, Store(self.cfg), self.gh, self.b2, self.requests)
        denied = await agent.generate_document(outline("Second"), "pdf")
        self.assertEqual(denied["error_code"], "rate_limited")
        self.assertGreaterEqual(denied["retry_after_seconds"], 59)
        self.gh.dispatch.assert_awaited_once()

    async def test_rerun_seen_before_slow_storage_invalidates_old_result(self):
        job = await self.create()
        self.finish(job)
        self.assertEqual((await self.agent.get_document_result(job))["status"], "completed")
        self.finish(job, 2)
        self.b2._read.side_effect = Issue("remote_unavailable", 15)
        self.assertNotIn("download_url", await self.agent.get_document_result(job))
        saved = self.db.get(job)
        self.assertEqual(saved["attempt"], 2)
        self.assertIsNone(saved["manifest"])

    async def test_rate_limit_respected_across_calls(self):
        job = await self.create()
        self.gh.get.side_effect = Issue("rate_limited", 120)
        for _ in range(2):
            result = await self.agent.get_document_result(job)
            self.assertGreaterEqual(result["retry_after_seconds"], 119)
        self.gh.get.assert_awaited_once()

    async def test_staging_recovery_and_dispatch_claim(self):
        reserved, _ = self.db.reserve(Submit(outline=outline(), format="pdf"))
        job = reserved["job_id"]
        self.requests.stage.side_effect = Issue("remote_unavailable", 5)
        self.assertEqual((await self.agent.get_document_result(job))["status"], "staging")
        self.gh.dispatch.assert_not_awaited()
        self.requests.stage.side_effect = None
        self.db.update(self.db.get(job), next_check=0)
        await asyncio.gather(self.agent.get_document_result(job), self.agent.get_document_result(job))
        self.gh.dispatch.assert_awaited_once()
        self.assertEqual(self.db.get(job)["run_id"], 42)

    async def test_bad_staging_key_releases_slot(self):
        self.requests.stage.side_effect = Issue("staging_rejected")
        reply = await self.agent.generate_document(outline(), "docx")
        self.assertEqual((reply["status"], reply["error_code"]), ("failed", "staging_rejected"))
        self.assertFalse(self.db.active_jobs())
        self.gh.dispatch.assert_not_awaited()


class GitHubWire(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Mock(repo="o/r", ref="main", workflow="generate-document.yml")
        self.row = {"job_id": str(uuid4()), "dispatch_at": time.time(), "request_sha256": "a" * 64}

    def wire(self, handler):
        gh = GitHub(self.cfg, "placeholder", httpx.MockTransport(handler))
        self.addAsyncCleanup(gh.http.aclose)
        return gh

    def mapping(self, i):
        return {
            "workflow_run_id": i,
            "run_url": f"https://api.github.com/repos/o/r/actions/runs/{i}",
            "html_url": f"https://github.com/o/r/actions/runs/{i}",
        }

    async def test_2026_dispatch_and_ambiguous_responses(self):
        def handle(req):
            self.assertEqual(req.headers["X-GitHub-Api-Version"], "2026-03-10")
            self.assertEqual(
                json.loads(req.content)["inputs"],
                {"job_id": self.row["job_id"], "request_sha256": "a" * 64},
            )
            return httpx.Response(200, json=self.mapping(91))

        self.assertEqual((await self.wire(handle).dispatch(self.row))["run_id"], 91)
        for response in [
            httpx.Response(204),
            httpx.Response(200, json={"id": 91}),
            httpx.Response(502),
        ]:
            with self.assertRaisesRegex(Issue, "temporarily"):
                await self.wire(lambda _: response).dispatch(self.row)

    async def test_exact_paginated_recovery(self):
        def row(i):
            fields = self.mapping(i)
            return {
                "id": i,
                "url": fields["run_url"],
                "html_url": fields["html_url"],
                "display_title": "ear-document-" + self.row["job_id"],
                "event": "workflow_dispatch",
                "head_branch": "main",
                "path": ".github/workflows/generate-document.yml",
            }

        def handle(req):
            self.assertIn("/workflows/generate-document.yml/runs", req.url.path)
            first = [{**row(1), "display_title": "wrong"}] * 98 + [
                {**row(2), "head_branch": "other"},
                {**row(3), "path": "other"},
            ]
            runs = first if req.url.params["page"] == "1" else [row(99)]
            return httpx.Response(200, json={"workflow_runs": runs, "total_count": 101})

        self.assertEqual((await self.wire(handle).reconcile(self.row))["run_id"], 99)
        with self.assertRaises(Issue) as err:
            await self.wire(
                lambda _: httpx.Response(
                    200, json={"workflow_runs": [row(5), row(6)], "total_count": 2}
                )
            ).reconcile(self.row)
        self.assertEqual(err.exception.code, "multiple_runs")

    async def test_rate_limit_header(self):
        with self.assertRaises(Issue) as err:
            await self.wire(lambda _: httpx.Response(429, headers={"Retry-After": "99"})).dispatch(
                self.row
            )
        self.assertEqual((err.exception.code, err.exception.delay), ("rate_limited", 99))
