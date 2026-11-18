"""Explicit opt-in. Dispatch at most one new job and verify its B2 download."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from ear_document.agent import invoke
from ear_document.cli import fetch


@unittest.skipUnless(
    os.getenv("EAR_DOCUMENT_LIVE_TEST") == "1", "requires explicit opt-in, GitHub and B2 credentials"
)
class Live(unittest.IsolatedAsyncioTestCase):
    async def test_one_remote_pdf(self):
        reply = await invoke(
            "generate_document",
            outline={
                "title": "Workflow verification",
                "sections": [{"heading": "Result", "bullets": ["Private PDF delivery."]}],
            },
            format="pdf",
            request_id="integration-" + str(uuid4()),
        )
        self.assertIn("job_id", reply, msg=reply.get("error_code"))
        job = reply["job_id"]
        print(f"Live job {job}", flush=True)
        end = time.monotonic() + 35 * 60
        while time.monotonic() < end:
            reply = await invoke("get_document_result", job_id=job, wait_seconds=15)
            if reply["status"] in ("failed", "completed"):
                break
            await asyncio.sleep(
                min(max(1, reply["retry_after_seconds"]), max(0, end - time.monotonic()))
            )
        self.assertEqual(reply["status"], "completed", msg=reply.get("error_code"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "output.pdf"
            fetch(reply, dest)
            self.assertTrue(dest.read_bytes().startswith(b"%PDF-"))
            self.assertGreaterEqual(reply["page_count"], 1)
