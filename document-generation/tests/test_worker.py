import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from ear_document.agent import Agent
from ear_document.cli import fetch
from ear_document.config import Settings
from ear_document.contract import MAX_REQUEST_BYTES, Dispatch, Issue, Request, digest
from ear_document.storage import B2
from ear_document.store import Store

ROOT = Path(__file__).resolve().parents[1]
OUTLINE = json.loads((ROOT / "examples/outline.json").read_text())
RENDERERS = all(find_spec(pkg) for pkg in ("docx", "pptx", "pypdf", "PIL"))
if RENDERERS:
    sys.path.insert(0, str(ROOT / "worker"))
    import run as worker
    from docx import Document
    from pptx import Presentation
    from pypdf import PdfReader
    from render import render, validate


@unittest.skipUnless(RENDERERS, "requires the separate worker dependencies")
class Worker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.reader, self.writer = Mock(), Mock()
        self.reader.bucket = "private-test"
        self.dispatch = Dispatch(job_id=str(uuid4()), request_sha256="0" * 64)

    def read(self, data):
        self.reader.s3.get_object.return_value = {"Body": io.BytesIO(data)}

    @unittest.skipUnless(shutil.which("soffice"), "requires LibreOffice Writer")
    async def test_three_formats_through_submission_worker_restart_signing_download(self):
        cfg = Settings(
            self.path / "jobs.sqlite",
            "o/r",
            "main",
            "generate-document.yml",
            "us-west-004",
            "private-test",
            "documents",
            1,
            10,
            3,
            10,
            3600,
        )
        for fmt in ("pdf", "docx", "pptx"):
            with self.subTest(fmt=fmt):
                remote = {}
                requests, github = AsyncMock(), AsyncMock()
                github.dispatch.return_value = {"run_id": 18, "run_url": "api", "html_url": "web"}
                b2 = B2(cfg.region, cfg.bucket, "test", "test")
                agent = Agent(cfg, Store(cfg), github, b2, requests)
                submit = await agent.generate_document(OUTLINE, fmt)
                row = agent.store.get(submit["job_id"])
                self.assertEqual(submit["status"], "queued")
                requests.stage.assert_awaited_once()
                self.read(requests.stage.await_args.args[1]["request_json"].encode())
                github.dispatch.assert_awaited_once()
                dispatch = Dispatch(job_id=row["job_id"], request_sha256=row["request_sha256"])

                def save(key, body, mime, metadata=None):
                    remote[key] = (body, mime, metadata)

                self.writer.put.side_effect = save
                self.assertEqual(
                    worker.execute(dispatch, 18, 1, self.reader, self.writer, "documents", self.path), 0
                )
                self.assertEqual(
                    [key.rsplit("/", 1)[-1] for key in remote], [f"output.{fmt}", "result.json"]
                )
                manifest = list(remote.values())[-1][0]
                self.assertNotIn(OUTLINE["title"].encode(), manifest)

                async def read(method, key):
                    data, mime, meta = remote[key]
                    if method == "HEAD":
                        return {
                            "content-type": mime,
                            "content-length": str(len(data)),
                            "x-amz-meta-sha256": meta["sha256"],
                        }
                    return json.loads(data)

                b2._read = read
                github.get.return_value = {
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "success",
                }
                resumed = Agent(cfg, Store(cfg), github, b2, None)
                reply = await resumed.get_document_result(row["job_id"])
                self.assertEqual((reply["status"], reply["format"]), ("completed", fmt))
                self.assertIn("X-Amz-Signature", reply["download_url"])
                downloaded = self.path / f"downloaded.{fmt}"
                with patch("ear_document.cli.httpx.stream") as stream:
                    stream.return_value.__enter__.return_value = httpx.Response(
                        200,
                        content=remote[reply["object_key"]][0],
                        request=httpx.Request("GET", reply["download_url"]),
                    )
                    fetch(reply, downloaded)
                if fmt == "pdf":
                    contents = " ".join(page.extract_text() for page in PdfReader(downloaded).pages)
                    self.assertGreater(reply["page_count"], 0)
                elif fmt == "docx":
                    contents = " ".join(p.text for p in Document(downloaded).paragraphs)
                    self.assertIsNone(reply["page_count"])
                else:
                    deck = Presentation(downloaded)
                    self.assertEqual(len(deck.slides), reply["page_count"])
                    contents = " ".join(
                        s.text for page in deck.slides for s in page.shapes if s.has_text_frame
                    )
                for line in [
                    OUTLINE["title"],
                    OUTLINE["author"],
                    OUTLINE["subtitle"],
                    *[
                        text
                        for section in OUTLINE["sections"]
                        for text in [
                            section["heading"],
                            *section.get("paragraphs", []),
                            *section.get("bullets", []),
                        ]
                    ],
                ]:
                    self.assertIn(line, contents)

    def test_invalid_request_never_reaches_renderer(self):
        for data, checksum in [
            (b"{}", "0" * 64),
            (b"{}", digest(b"{}")),
            (b"a" * (MAX_REQUEST_BYTES + 1), "0" * 64),
        ]:
            self.read(data)
            d = self.dispatch.model_copy(update={"request_sha256": checksum})
            with patch("run.render") as renderer, contextlib.redirect_stderr(io.StringIO()):
                code = worker.execute(d, 1, 1, self.reader, self.writer, "documents", self.path)
                renderer.assert_not_called()
            self.assertEqual(code, 1)
            key, raw, mime = self.writer.put.call_args.args
            self.assertTrue(key.endswith("/error.json"))
            self.assertEqual(json.loads(raw)["error_code"], "invalid_worker_input")

    def test_upload_or_completion_write_failure(self):
        task = Request(outline=OUTLINE, format="docx")
        data = task.payload_json().encode()
        for failure_at in (1, 2):
            writes = []

            def put(key, *args):
                writes.append((key, args))
                if len(writes) == failure_at:
                    raise Issue("upload_failed")

            self.read(data)
            self.writer.put.side_effect = put
            d = self.dispatch.model_copy(update={"request_sha256": digest(data)})
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    worker.execute(d, 2, 3, self.reader, self.writer, "documents", self.path), 1
                )
            self.assertTrue(writes[-1][0].endswith("/2/3/error.json"))
            self.assertEqual(json.loads(writes[-1][1][0])["error_code"], "upload_failed")
            self.assertEqual(len(writes), failure_at + 1)

    def test_long_deck_continuation_and_literal_xml(self):
        title = "<tag> & $(echo text) ${{ example }}"
        first = "W" * 250 + " " + "dense sentence " * 50
        outline = {
            "title": title,
            "sections": [{"heading": "Details", "bullets": [first] * 11 + ["LAST ITEM"]}],
        }
        task = Request(outline=outline, format="pptx")
        path = render(task, self.path)
        count = validate(path, task)
        self.assertGreater(count, 3)
        deck = Presentation(path)
        self.assertEqual(deck.slides[0].shapes[1].text, title)
        text = " ".join(s.text for page in deck.slides for s in page.shapes if s.has_text_frame)
        self.assertEqual(text.count("dense"), 550)
        self.assertIn("LAST ITEM", text)
        for slide in deck.slides:
            for shape in slide.shapes:
                self.assertLessEqual(shape.top + shape.height, deck.slide_height)
        path.write_bytes(b"bad file")
        with self.assertRaisesRegex(Issue, "validate"):
            validate(path, task)

    def test_full_cover_fields_do_not_overlap(self):
        task = Request(
            outline={**OUTLINE, "title": "W" * 96, "subtitle": "界" * 180, "author": "M" * 96},
            format="pptx",
        )
        deck = Presentation(render(task, self.path))
        title, subtitle, author = list(deck.slides[0].shapes)[1:]
        self.assertLess(title.top + title.height, subtitle.top)
        self.assertLess(subtitle.top + subtitle.height, author.top)
        self.assertLess(author.top + author.height, deck.slide_height)
