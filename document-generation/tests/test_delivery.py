import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ear_document.cli import fetch
from ear_document.config import credential, storage_settings
from ear_document.contract import Request, digest, request_key
from ear_document.storage import B2

EXAMPLE = json.loads((Path(__file__).resolve().parents[1] / "examples/outline.json").read_text())


class Transfer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = B2("us-west-004", "private-bucket", "test", "test")

    async def test_private_staging_uses_signed_body(self):
        job = str(uuid4())
        payload = Request(outline=EXAMPLE, format="docx").payload_json()
        real_client = httpx.AsyncClient

        def capture(req):
            self.assertEqual(req.method, "PUT")
            self.assertEqual(req.url.path, "/private-bucket/" + request_key("docs", job))
            self.assertEqual(req.content, payload.encode())
            self.assertEqual(req.headers["x-amz-content-sha256"], digest(req.content))
            self.assertIn("AWS4-HMAC-SHA256", req.headers["authorization"])
            self.assertEqual(req.headers["content-type"], "application/json")
            return httpx.Response(200)

        with patch(
            "ear_document.storage.httpx.AsyncClient",
            side_effect=lambda **kw: real_client(transport=httpx.MockTransport(capture), **kw),
        ):
            await self.client.stage("docs", {"job_id": job, "request_json": payload})

    def test_expired_url_refresh(self):
        earlier = datetime(2026, 1, 1, tzinfo=UTC)
        key = "documents/results/job/1/1/output.pdf"
        with patch("botocore.auth.get_current_datetime", return_value=earlier):
            old, _ = self.client.sign(key, 3600)
        with patch("botocore.auth.get_current_datetime", return_value=earlier + timedelta(hours=2)):
            fresh, _ = self.client.sign(key, 3600)
        self.assertNotEqual(old, fresh)
        self.assertEqual(urlsplit(old).path, urlsplit(fresh).path)
        self.assertEqual(parse_qs(urlsplit(fresh).query)["X-Amz-Expires"], ["3600"])

    def test_mismatched_download_preserves_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "saved.pdf"
            path.write_bytes(b"keep")
            url = "https://example.invalid/document"
            result = {"download_url": url, "byte_size": 5, "sha256": digest(b"right")}
            with patch("ear_document.cli.httpx.stream") as stream:
                stream.return_value.__enter__.return_value = httpx.Response(
                    200, content=b"wrong", request=httpx.Request("GET", url)
                )
                with self.assertRaises(ValueError):
                    fetch(result, path)
            self.assertEqual(path.read_bytes(), b"keep")

    def test_limits_and_configuration(self):
        outline = {"title": "A", "sections": [{"heading": "H", "bullets": ["界" * 1000] * 20}]}
        with self.assertRaises(ValidationError):
            Request(outline=outline, format="pdf")
        with patch.dict(
            os.environ,
            {
                "EAR_B2_REGION": "https://s3.us-east-005.backblazeb2.com/",
                "EAR_B2_BUCKET": "test-bucket",
                "EAR_B2_KEY_ID": "shared",
                "EAR_B2_APPLICATION_KEY": "secret",
                "EAR_DOCUMENT_B2_OUTPUT_READ_KEY_ID": "dedicated",
                "EAR_DOCUMENT_B2_OUTPUT_READ_KEY": "read",
            },
            clear=True,
        ):
            self.assertEqual(storage_settings(), ("us-east-005", "test-bucket", "documents"))
            self.assertEqual(credential("REQUEST_WRITE"), ("shared", "secret"))
            self.assertEqual(credential("OUTPUT_READ"), ("dedicated", "read"))
