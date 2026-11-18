"""Tests for b2_worker.py with B2 and the toolbox container simulated (needs modeling's requirements)."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("boto3")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import b2_worker  # noqa: E402
import ear_extract  # noqa: E402

REQUEST_ID = "0123456789abcdef0123456789abcdef"


class FakeStore:
    def __init__(self, objects):
        self.objects = dict(objects)

    def read(self, key, max_bytes=256 * 1024):
        data = self.objects[key]
        assert len(data) <= max_bytes
        return data

    def write(self, key, obj, max_bytes=256 * 1024, content_type="application/json"):
        assert len(obj) <= max_bytes
        self.objects[key] = obj


def fake_toolbox(workdir, goal, functions):
    """Stands in for `docker run ... ear_extract.py`: writes what the extractor would."""
    assert (workdir / "binary").read_bytes() == b"\x7fELF demo"
    out = workdir / "out"
    out.mkdir()
    sources = [{"id": "binary", "text": "format: elf"}]
    (out / "extraction.json").write_text(json.dumps({"schema": "ear-extract/v1", "functions": {"count": 2},
                                                     "decompiled": [], "evidence_sources": ["binary"],
                                                     "goal_seen": goal, "functions_seen": functions}))
    (out / "evidence.json").write_text(json.dumps({"sources": sources}))


def request(source):
    return {f"ear-extract/requests/{REQUEST_ID}.json":
            json.dumps({"source": source, "goal": "How?", "functions": "main", "design": True}).encode()}


def test_binary_from_bucket_to_private_results():
    store = FakeStore({**request({"key": "ear-extract/binaries/demo"}), "ear-extract/binaries/demo": b"\x7fELF demo"})
    with patch.object(b2_worker, "run_toolbox", fake_toolbox):
        b2_worker.work(REQUEST_ID, 42, 1, store)
    extraction = json.loads(store.objects["ear-extract/results/42/1/extraction.json"])
    assert extraction["goal_seen"] == "How?" and extraction["functions_seen"] == "main"
    assert json.loads(store.objects["ear-modeling/evidence/r2/42-1.json"]) == {"sources": [{"id": "binary", "text": "format: elf"}]}


def test_binary_from_url():
    store = FakeStore(request({"url": "https://example.org/demo"}))
    with patch.object(b2_worker, "run_toolbox", fake_toolbox), \
         patch.object(ear_extract, "fetch", lambda url, dest, limit: dest.write_bytes(b"\x7fELF demo")):
        b2_worker.work(REQUEST_ID, 42, 2, store)
    assert "ear-modeling/evidence/r2/42-2.json" in store.objects


@pytest.mark.parametrize("source", [{"key": "ear-modeling/evidence/a.json"}, {"key": "ear-extract/binaries/../x"}, {}])
def test_bad_sources_are_refused(source):
    with pytest.raises(ear_extract.ExtractError):
        b2_worker.work(REQUEST_ID, 42, 1, FakeStore(request(source)))


def test_extractor_error_is_raised():
    def failing(workdir, goal, functions):
        (workdir / "out").mkdir()
        (workdir / "out" / "extraction.json").write_text(json.dumps({"error": "nothing could be extracted"}))
    store = FakeStore({**request({"key": "ear-extract/binaries/demo"}), "ear-extract/binaries/demo": b"\x7fELF demo"})
    with patch.object(b2_worker, "run_toolbox", failing), pytest.raises(ear_extract.ExtractError, match="nothing"):
        b2_worker.work(REQUEST_ID, 42, 1, store)
    assert not any(k.startswith("ear-modeling/") for k in store.objects)


def test_request_ids_and_keys():
    with pytest.raises(ValueError):
        b2_worker.work("../../etc", 1, 1, FakeStore({}))
    assert b2_worker.evidence_key(9, 2) == "ear-modeling/evidence/r2/9-2.json"
