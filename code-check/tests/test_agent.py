import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ear_document.contract import Issue
from ear_document.store import Store
from pydantic import ValidationError

from ear_code import prepare_source_upload, run_code_check
from ear_code.agent import CodeAgent, CodeB2
from ear_code.plan import MAX_SOURCE, Manifest, Plan, Request, Submit, Summary
from ear_code.settings import config

SOURCE = "b" * 64 + ".tar.gz"
TEST = {"mode": "test", "steps": {"setup": "npm ci", "test": "npm test", "artifacts": ["dist"]}}


def artifact(key, name="report.md"):
    return {"object_key": key, "name": name, "mime_type": "text/markdown; charset=utf-8", "byte_size": 10,
            "sha256": "c" * 64}


class PlanTests(unittest.TestCase):
    def test_plan_rules(self):
        Plan.model_validate(TEST)
        Plan.model_validate({"mode": "review", "focus": "Add a discount code."})
        for bad in (
            {"mode": "test"},
            {"mode": "review", "steps": {"test": "npm test"}},
            {"mode": "test", "steps": {"setup": "npm ci"}},
            {"mode": "test", "steps": {"test": "npm test", "artifacts": ["../secrets"]}},
            {"mode": "test", "steps": {"test": "npm test", "artifacts": ["/etc/passwd"]}},
            {"mode": "test", "steps": {"test": "npm test\x00rm"}},
            {"mode": "test", "steps": {"test": "npm test", "network": "everything"}},
            {"mode": "deploy"},
        ):
            with self.assertRaises(ValidationError, msg=json.dumps(bad)):
                Plan.model_validate(bad)
        with self.assertRaises(ValidationError):
            Request(source="../x.tar.gz", project="p", plan=TEST)
        with self.assertRaises(ValidationError):
            Request(source=SOURCE, project="../p", plan=TEST)

    def test_summary_and_manifest_shapes(self):
        summary = Summary.model_validate({
            "mode": "test", "outcome": "failed", "source_files": 4, "artifacts": [],
            "steps": [{"name": "test", "command": "npm test", "exit_code": 1, "seconds": 2.5, "tail": "fail"}],
            "analysis": {"summary": "s", "what_failed": "w", "why": "y", "fix": ["f"], "files": ["a.js"]},
            "model": {"model": "m", "seconds": 1.0, "prompt_tokens": 3, "output_tokens": 4, "note": None},
        })
        self.assertEqual(summary.steps[0].exit_code, 1)
        base = "code/results/j/1/1"
        manifest = {"job_id": "1b4e28ba-2fa1-41d2-883f-0016d3cca427", "request_sha256": "a" * 64, "version": 1,
                    "workflow_run_id": 1, "run_attempt": 1, "mode": "review", "outcome": "passed", "project": "p",
                    "report": artifact(base + "/report.md"), "source_files": 1,
                    "completed_at": "2026-11-19T00:00:00+00:00"}
        with self.assertRaises(ValidationError):
            Manifest.model_validate(manifest)  # a review cannot "pass"
        Manifest.model_validate({**manifest, "outcome": "approved"})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "EAR_CODE_DB": str(Path(self.tmp.name) / "jobs.sqlite"),
            "EAR_B2_REGION": "us-east-005", "EAR_B2_BUCKET": "private-bucket",
            "EAR_B2_KEY_ID": "test", "EAR_B2_APPLICATION_KEY": "test",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.cfg = config()
        self.store = Store(self.cfg)
        self.gh = AsyncMock()
        self.gh.dispatch.return_value = {"run_id": 11, "run_url": "api", "html_url": "web"}
        self.gh.get.return_value = {"status": "in_progress", "run_attempt": 1}
        self.inbox = AsyncMock()
        self.b2 = CodeB2(self.cfg.region, self.cfg.bucket, "test", "test")
        self.agent = CodeAgent(self.cfg, self.store, self.gh, self.b2, self.inbox)

    async def test_upload_link_is_scoped_to_the_archive(self):
        for size in (0, MAX_SOURCE + 1):
            self.assertEqual(prepare_source_upload("a" * 64, size)["status"], "failed")
        link = prepare_source_upload("a" * 64, 100)
        self.assertIn("/private-bucket/code/requests/sources/" + "a" * 64 + ".tar.gz", link["upload_url"])
        self.assertEqual((link["handle"], link["content_type"]), ("a" * 64 + ".tar.gz", "application/gzip"))
        self.assertEqual((await run_code_check(SOURCE, "p", {"mode": "deploy"}))["error_code"], "invalid_input")

    async def test_submit_dispatches_once_and_renders_links(self):
        reply = await self.agent.submit(Submit(source=SOURCE, project="cart", plan=TEST, request_id="r1"))
        self.assertIn(reply["status"], ("queued", "running"))
        again = await self.agent.submit(Submit(source=SOURCE, project="cart", plan=TEST, request_id="r1"))
        self.assertEqual(again["job_id"], reply["job_id"])
        self.gh.dispatch.assert_awaited_once()
        with self.assertRaises(Issue):
            await self.agent.submit(Submit(source=SOURCE, project="cart", plan={"mode": "review"}, request_id="r1"))
        row = self.store.get(reply["job_id"])
        base = f"code/results/{row['job_id']}/11/1"
        doc = {"job_id": row["job_id"], "request_sha256": row["request_sha256"], "version": 1,
               "workflow_run_id": 11, "run_attempt": 1, "mode": "test", "outcome": "passed", "project": "cart",
               "steps": [{"name": "test", "command": "npm test", "exit_code": 0, "seconds": 1.0, "tail": "ok",
                          "log": artifact(base + "/logs/test.log", "test.log")}],
               "artifacts": [artifact(base + "/artifacts/dist.zip", "dist.zip")],
               "report": artifact(base + "/report.md"), "source_files": 3,
               "completed_at": "2026-11-19T00:00:00+00:00"}
        heads = {"content-length": "10", "x-amz-meta-sha256": "c" * 64}
        self.gh.get.return_value = {"status": "completed", "conclusion": "success", "run_attempt": 1}
        with patch.object(CodeB2, "_read", AsyncMock(side_effect=lambda m, k: doc if m == "GET" else heads)):
            self.store.update(row, next_check=0)
            done = await self.agent.check(row["job_id"])
        self.assertEqual(done["status"], "completed")
        self.assertIn("download_url", done["report"])
        self.assertIn("download_url", done["artifacts"][0])
        self.assertIn("download_url", done["steps"][0]["log"])

    async def test_verify_rejects_foreign_keys(self):
        reply = await self.agent.submit(Submit(source=SOURCE, project="cart", plan=TEST))
        row = {**self.store.get(reply["job_id"]), "run_id": 11, "attempt": 1}
        doc = {"job_id": row["job_id"], "request_sha256": row["request_sha256"], "version": 1,
               "workflow_run_id": 11, "run_attempt": 1, "mode": "test", "outcome": "passed", "project": "cart",
               "report": artifact("documents/results/other/report.md"), "source_files": 1,
               "completed_at": "2026-11-19T00:00:00+00:00"}
        with self.assertRaises(Issue):
            await self.b2.verify(row, self.cfg.prefix, doc)


if __name__ == "__main__":
    unittest.main()
