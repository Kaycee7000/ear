import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from botocore.stub import Stubber

from ear_image.cli import fetch
from ear_image.contract import Issue, ModelSpec, WorkerInput, utc
from ear_image.storage import B2, base_key

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import run as worker


def sample_png():
    """Small synthetic PNG fixture, assembled from pixels without an image model."""

    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    dims = struct.pack(">IIBBBBB", 512, 512, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", dims)
        + chunk(b"IDAT", zlib.compress(bytes(512 * (1 + 512 * 3))))
        + chunk(b"IEND", b"")
    )


class Storage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.b2 = B2("us-west-004", "test-bucket", "id", "secret")

    async def test_signed_header_reads_and_limits(self):
        original = httpx.AsyncClient

        async def check(req):
            self.assertEqual(req.url.host, "s3.us-west-004.backblazeb2.com")
            self.assertTrue(req.headers["Authorization"].startswith("AWS4-HMAC-SHA256"))
            self.assertFalse(req.url.query)
            return httpx.Response(200, content=b'{"version":1}')

        with patch(
            "ear_image.storage.httpx.AsyncClient",
            side_effect=lambda **kw: original(transport=httpx.MockTransport(check), **kw),
        ):
            self.assertEqual(await self.b2._read("GET", "images/key/result.json"), {"version": 1})
        for response, code in [
            (httpx.Response(200, content=b"x" * 65537), "output_invalid"),
            (httpx.Response(403), "storage_access"),
            (httpx.Response(404), "output_missing"),
        ]:
            with (
                patch(
                    "ear_image.storage.httpx.AsyncClient",
                    side_effect=lambda **kw: original(
                        transport=httpx.MockTransport(lambda req: response), **kw
                    ),
                ),
                self.assertRaises(Issue) as ctx,
            ):
                await self.b2._read("GET", "images/key/result.json")
            self.assertEqual(ctx.exception.code, code)

    def test_expired_signature_can_be_refreshed_offline(self):
        first = datetime(2026, 10, 1, tzinfo=UTC)
        key = "images/known/1/1/output.png"
        with patch("botocore.auth.get_current_datetime", return_value=first):
            old, _ = self.b2.sign(key, 3600)
        with patch("botocore.auth.get_current_datetime", return_value=first + timedelta(hours=2)):
            new, _ = self.b2.sign(key, 3600)
        self.assertNotEqual(old, new)
        self.assertEqual(urlsplit(old).path, urlsplit(new).path)
        self.assertEqual(parse_qs(urlsplit(new).query)["X-Amz-Expires"], ["3600"])

    def test_sdk_upload_has_expected_mime_checksum_and_no_acl(self):
        with Stubber(self.b2.s3) as stub:
            stub.add_response(
                "put_object",
                {},
                {
                    "Bucket": "test-bucket",
                    "Key": "images/output.png",
                    "Body": b"x",
                    "Metadata": {"sha256": "test"},
                    "ContentType": "image/png",
                },
            )
            self.b2.put("images/output.png", b"x", "image/png", {"sha256": "test"})
            stub.assert_no_pending_responses()

    def test_fetch_verifies_and_preserves_existing_file_on_failure(self):
        buf = sample_png()
        result = {
            "image_url": "https://test.invalid/download",
            "byte_size": len(buf),
            "sha256": hashlib.sha256(buf).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "image.png"
            for expected in [True, False]:
                response = httpx.Response(
                    200, content=buf, request=httpx.Request("GET", result["image_url"])
                )
                with patch("ear_image.cli.httpx.stream") as stream:
                    stream.return_value.__enter__.return_value = response
                    if expected:
                        fetch(result, target)
                    else:
                        result["sha256"] = "0" * 64
                        with self.assertRaises(ValueError):
                            fetch(result, target)
                    self.assertEqual(target.read_bytes(), buf)


class OfflineEndToEnd(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(__import__("importlib.util").util.find_spec("PIL"), "requires worker Pillow")
    async def test_submit_offline_worker_resume_and_download(self):
        from unittest.mock import AsyncMock
        from ear_image.agent import Agent
        from ear_image.config import Settings
        from ear_image.store import Store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            cfg = Settings(
                path / "jobs.sqlite",
                "o/r",
                "main",
                "generate-image.yml",
                "us-west-004",
                "test-bucket",
                "images",
                1,
                10,
                1,
                15,
                3600,
            )
            gh = Mock()
            gh.dispatch = AsyncMock(return_value={"run_id": 7, "run_url": "api", "html_url": "web"})
            b2 = B2(cfg.region, cfg.bucket, "test", "test")
            job = (await Agent(cfg, Store(cfg), gh, b2).generate_image("blue cup"))["job_id"]
            frozen = gh.dispatch.await_args.args[0]
            saved = {}

            def put(key, body, mime, metadata=None):
                saved[key] = (body, mime, metadata)

            writer = Mock()
            writer.put.side_effect = put
            data = sample_png()
            (path / "output.png").write_bytes(data)
            spec = ModelSpec()
            identity = {"job_id": job, "workflow_run_id": 7, "run_attempt": 1}
            base = base_key(cfg.prefix, job, 7, 1)
            resources = dict.fromkeys(
                (
                    "ram_limit_bytes",
                    "ram_available_bytes",
                    "cpu_threads",
                    "cpu_millicores",
                    "disk_free_bytes",
                    "disk_total_bytes",
                    "peak_worker_rss_bytes",
                ),
                100,
            )
            worker.publish(
                writer,
                base,
                path,
                identity,
                WorkerInput(job_id=job, seed=frozen["seed"], prompt=frozen["prompt"]),
                spec,
                utc(),
                {"dependencies": 1.0, "model_download": 2.0, "model_load": 3.0, "inference": 4.0},
                resources,
            )

            async def read(method, key):
                body, mime, metadata = saved[key]
                if method == "HEAD":
                    return {
                        "content-type": mime,
                        "content-length": str(len(body)),
                        "x-amz-meta-sha256": metadata["sha256"],
                    }
                return json.loads(body)

            b2._read = read
            gh.get = AsyncMock(
                return_value={"status": "completed", "conclusion": "success", "run_attempt": 1}
            )
            reply = await Agent(cfg, Store(cfg), gh, b2).get_image_result(job)
            self.assertEqual(reply["status"], "completed")
            self.assertEqual(urlsplit(reply["image_url"]).path, "/test-bucket/" + base + "/output.png")
            self.assertEqual(reply["sha256"], hashlib.sha256(data).hexdigest())
            with patch("ear_image.cli.httpx.stream") as stream:
                stream.return_value.__enter__.return_value = httpx.Response(
                    200, content=data, request=httpx.Request("GET", reply["image_url"])
                )
                fetch(reply, path / "fetched.png")
            self.assertEqual((path / "fetched.png").read_bytes(), data)
            gh.dispatch.assert_awaited_once()


class Worker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        job = str(uuid4())
        self.task = WorkerInput(job_id=job, seed=9, prompt="sample prompt")
        self.identity = {"job_id": job, "workflow_run_id": 8, "run_attempt": 2}
        self.base = base_key("images", job, 8, 2)
        self.resources = {
            "ram_limit_bytes": 12 * 2**30,
            "ram_available_bytes": 10 * 2**30,
            "disk_free_bytes": 10 * 2**30,
            "disk_total_bytes": 20 * 2**30,
            "cpu_millicores": 4000,
            "cpu_threads": 4,
            "peak_worker_rss_bytes": 100,
        }
        self.timings = dict(dependencies=2.0, model_download=1.0, model_load=3.0, inference=4.0)

    def test_png_precedes_manifest_and_upload_failures(self):
        data = sample_png()
        for fail_at in (0, 1, 2):
            writes = []

            def put(key, body, mime, metadata=None):
                writes.append((key, body, mime))
                if len(writes) == fail_at:
                    raise Issue("upload_failed")

            b2 = Mock()
            b2.put.side_effect = put
            with patch("run.validated_png", return_value=data):
                if fail_at:
                    with self.assertRaises(Issue):
                        worker.publish(
                            b2,
                            self.base,
                            self.folder,
                            self.identity,
                            self.task,
                            ModelSpec(),
                            utc(),
                            self.timings.copy(),
                            self.resources,
                        )
                else:
                    worker.publish(
                        b2,
                        self.base,
                        self.folder,
                        self.identity,
                        self.task,
                        ModelSpec(),
                        utc(),
                        self.timings.copy(),
                        self.resources,
                    )
            self.assertEqual(writes[0], (self.base + "/output.png", data, "image/png"))
            if fail_at == 1:
                self.assertEqual(len(writes), 1)
            else:
                key, body, mime = writes[1]
                self.assertEqual(key, self.base + "/result.json")
                self.assertEqual(json.loads(body)["sha256"], hashlib.sha256(data).hexdigest())
                self.assertNotIn(self.task.prompt, body.decode())

    def test_worker_validates_before_loading_and_reports_failures(self):
        raw = self.task.model_dump()
        raw["seed"] = str(raw["seed"])
        for change, code in [
            ({"seed": "-5"}, "invalid_worker_input"),
            ({"prompt": " "}, "invalid_worker_input"),
            ({}, "insufficient_resources"),
        ]:
            b2 = Mock()
            with (
                patch("run.CACHE", self.folder),
                patch("run.snapshot", return_value={**self.resources, "ram_available_bytes": 2**30}),
                patch("run.subprocess.run") as process,
                patch("builtins.print"),
            ):
                self.assertEqual(
                    worker.execute(self.folder, {**raw, **change}, b2, "images", self.identity, utc()), 1
                )
                process.assert_not_called()
            key, body, _ = b2.put.call_args.args
            self.assertEqual(key, self.base + "/error.json")
            self.assertEqual(json.loads(body)["error_code"], code)
            self.assertNotIn(self.task.prompt, body.decode())

    def test_worker_upload_error_writes_only_sanitized_error(self):
        raw = {**self.task.model_dump(), "seed": "9"}
        b2 = Mock()
        b2.put.side_effect = [Issue("upload_failed"), None]
        (self.folder / "timings.json").write_text(
            json.dumps({"model_download": 1, "model_load": 1, "inference": 1})
        )
        with (
            patch("run.CACHE", self.folder),
            patch("run.snapshot", return_value=self.resources.copy()),
            patch("run.subprocess.run", return_value=Mock(returncode=0)),
            patch("run.validated_png", return_value=sample_png()),
            patch.dict(os.environ, BOOTSTRAP_SECONDS="1"),
            patch("builtins.print"),
        ):
            self.assertEqual(worker.execute(self.folder, raw, b2, "images", self.identity, utc()), 1)
        self.assertEqual(
            [call.args[0] for call in b2.put.call_args_list],
            [self.base + "/output.png", self.base + "/error.json"],
        )
        self.assertEqual(json.loads(b2.put.call_args.args[1])["error_code"], "upload_failed")

    @unittest.skipUnless(__import__("importlib.util").util.find_spec("PIL"), "requires worker Pillow")
    def test_real_png_parser_and_corruption(self):
        path = self.folder / "output.png"
        data = sample_png()
        path.write_bytes(data)
        self.assertEqual(worker.validated_png(path), data)
        path.write_bytes(data[:-20])
        with self.assertRaises(Issue):
            worker.validated_png(path)
