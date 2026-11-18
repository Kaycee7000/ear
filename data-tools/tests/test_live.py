"""Explicitly opt in to one genuine GitHub computation and private B2 round trip."""

import asyncio
import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from ear_document.cli import fetch

from ear_data import get_data_result, run_data_job
from ear_data.cli import upload_file


@unittest.skipUnless(
    os.environ.get("EAR_DATA_LIVE_TEST") == "1",
    "requires explicit opt-in and real GitHub/B2 credentials",
)
class Live(unittest.IsolatedAsyncioTestCase):
    async def test_one_live_job(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        dataset = upload_file(examples / "sales.csv")
        self.assertEqual(dataset["status"], "uploaded", msg=dataset.get("error_code"))
        plan = json.loads((examples / "revenue.json").read_text())
        plan["export"] = "csv"
        reply = await run_data_job(
            {"sales": dataset["handle"]}, plan, request_id="smoke-" + str(uuid4())
        )
        self.assertIn("job_id", reply, msg=reply.get("error_code"))
        job = reply["job_id"]
        print("Live data job", job, flush=True)
        end = time.monotonic() + 25 * 60
        while time.monotonic() < end:
            reply = await get_data_result(job, wait_seconds=15)
            if reply["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(min(reply["retry_after_seconds"], max(0, end - time.monotonic())))
        self.assertEqual(reply["status"], "completed", msg=reply.get("error_code"))
        with tempfile.TemporaryDirectory() as tmp:
            table, chart = Path(tmp) / "table.csv", Path(tmp) / "chart.png"
            fetch(reply["table"], table)
            fetch(reply["chart"], chart)
            with table.open() as f:
                self.assertEqual(
                    [(r["region"], float(r["revenue"])) for r in csv.DictReader(f)],
                    [("South", 27.5), ("West", 25.0)],
                )
            self.assertTrue(chart.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
