"""Explicit opt-in. Dispatch at most one new job and verify its B2 download."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from ear_image.agent import invoke
from ear_image.cli import fetch


@unittest.skipUnless(
    os.getenv("EAR_IMAGE_LIVE_TEST") == "1", "requires explicit opt-in, GitHub and B2 credentials"
)
class Live(unittest.IsolatedAsyncioTestCase):
    async def test_one_remote_image(self):
        reply = await invoke(
            "generate_image",
            prompt="A blue ceramic cup on a plain cream background.",
            seed=43,
            request_id="integration-" + str(uuid4()),
        )
        self.assertIn("job_id", reply, msg=reply.get("error_code"))
        job = reply["job_id"]
        print(f"Live job {job}", flush=True)
        end = time.monotonic() + 35 * 60
        while time.monotonic() < end:
            reply = await invoke("get_image_result", job_id=job, wait_seconds=15)
            if reply["status"] in ("failed", "completed"):
                break
            await asyncio.sleep(
                min(max(1, reply["retry_after_seconds"]), max(0, end - time.monotonic()))
            )
        self.assertEqual(reply["status"], "completed", msg=reply.get("error_code"))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "output.png"
            fetch(reply, dest)
            self.assertEqual(dest.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual((reply["width"], reply["height"], reply["seed"]), (512, 512, 43))
