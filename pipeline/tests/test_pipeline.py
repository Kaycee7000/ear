"""Offline tests for the EAR pipeline agent tools: B2 and GitHub are simulated, no token or network needed."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODELING = Path(__file__).resolve().parents[2] / "modeling"
os.environ.setdefault("EAR_MODELING_DIR", str(MODELING))
sys.path.insert(0, str(MODELING))

from botocore.exceptions import ClientError  # noqa: E402

from ear_pipeline import client  # noqa: E402

DESIGN = {"components": [{"id": "config", "purpose": "Find the notes folder.",
                          "evidence": [{"source_id": "fn-sym-load_config", "quote": "getenv"}]}],
          "relationships": [], "assumptions": [], "open_questions": []}
EXTRACTION = {"info": {"format": "elf"}, "imports": ["fopen"], "strings": [], "functions": {"count": 3, "largest": []},
              "decompiled": [{"name": "sym.load_config", "addr": "0x11a9", "method": "pdg", "code": "getenv(x);"}],
              "evidence_sources": ["binary", "fn-sym-load_config"], "warnings": []}


class FakeStore:
    objects: dict[str, bytes] = {}

    def read(self, key, max_bytes=256 * 1024):
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return self.objects[key]

    def write(self, key, obj, max_bytes=256 * 1024, content_type="application/json"):
        self.objects[key] = obj


@pytest.fixture
def world():
    FakeStore.objects = {}
    state = {"run": {"id": 501, "run_attempt": 1, "status": "queued", "conclusion": None,
                     "html_url": "https://github.com/o/r/actions/runs/501", "path": ".github/workflows/ear-extract.yml"},
             "dispatches": [], "submits": [], "modeling": {"status": "in_progress", "conclusion": None}}

    def github(method, path, **kwargs):
        if method == "POST":
            state["dispatches"].append((path, kwargs["json"]))
            rid = kwargs["json"]["inputs"]["request_id"]
            state["run"]["display_title"] = f"ear-extract-{rid}"
            return {"workflow_run_id": 501, "html_url": state["run"]["html_url"]}
        return dict(state["run"])

    def submit(goal, evidence_key):
        state["submits"].append((goal, evidence_key))
        return {"request_id": "x" * 32, "run_id": 777, "run_url": "https://github.com/o/r/actions/runs/777"}

    def status(run_id):
        out = {"run_id": run_id, "run_url": "u", **state["modeling"]}
        if state["modeling"]["status"] == "completed" and state["modeling"]["conclusion"] == "success":
            out["design"] = DESIGN
        return out

    with patch.dict(os.environ, {"EAR_GITHUB_TOKEN": "test"}), patch.object(client.jobs, "Store", FakeStore), \
         patch.object(client.jobs, "github", github), patch.object(client.jobs, "submit", submit), \
         patch.object(client.jobs, "status", status):
        yield state


def finish_extraction(state):
    state["run"].update(status="completed", conclusion="success")
    FakeStore.objects["ear-extract/results/501/1/extraction.json"] = json.dumps(EXTRACTION).encode()


def test_round_trip_extract_then_model_once(world):
    started = client.analyze(binary_url="https://example.org/notes", goal="How are notes stored?", functions="main")
    assert started == {"state": "extracting", "run_id": 501, "run_url": world["run"]["html_url"],
                       "request_id": started["request_id"]}
    path, body = world["dispatches"][0]
    assert path == "/workflows/ear-extract.yml/dispatches" and body["inputs"] == {"request_id": started["request_id"]}
    request = json.loads(FakeStore.objects[f"ear-extract/requests/{started['request_id']}.json"])
    assert request == {"source": {"url": "https://example.org/notes"}, "goal": "How are notes stored?",
                       "functions": "main", "design": True}
    assert "https://example.org" not in json.dumps(body)  # GitHub only sees the request ID
    assert client.status(501)["state"] == "extracting"
    finish_extraction(world)
    first = client.status(501)
    assert first["state"] == "designing" and first["evidence_key"] == "ear-modeling/evidence/r2/501-1.json"
    assert client.status(501)["state"] == "designing"
    assert world["submits"] == [("How are notes stored?", "ear-modeling/evidence/r2/501-1.json")]  # only once
    world["modeling"].update(status="completed", conclusion="success")
    done = client.status(501)
    assert done["state"] == "done" and done["design"] == DESIGN
    assert done["extraction"]["decompiled"] == ["sym.load_config"]


def test_evidence_only_and_function_code(world):
    client.analyze(binary_key="ear-extract/binaries/tools/notes", design=False)
    finish_extraction(world)
    result = client.status(501)
    assert result["state"] == "done" and "model_design" in result["message"] and not world["submits"]
    assert "getenv" in client.function_code(501, "load_config")["code"]
    assert "decompiled" in client.function_code(501, "missing")


def test_failures_are_reported(world):
    client.analyze(binary_url="https://example.org/big")
    world["run"].update(status="completed", conclusion="failure")
    FakeStore.objects["ear-extract/results/501/1/error.json"] = json.dumps(
        {"error": "ExtractError", "message": "binary is larger than 50 MB"}).encode()
    assert "50 MB" in client.status(501)["message"]
    world["run"].update(conclusion="success")
    finish_extraction(world)
    world["modeling"].update(status="completed", conclusion="failure")
    assert client.status(501)["state"] == "partial"


@pytest.mark.parametrize("kwargs", [
    {}, {"binary_url": "http://example.org/a"}, {"binary_url": "file:///etc/passwd"},
    {"binary_key": "ear-modeling/evidence/x.json"}, {"binary_key": "ear-extract/binaries/../requests/a.json"},
    {"binary_url": "https://example.org/a", "binary_key": "ear-extract/binaries/a"},
    {"binary_url": "https://example.org/a", "functions": "main; rm -rf /"},
    {"binary_url": "https://example.org/a", "goal": "g" * 1001},
])
def test_bad_requests_are_refused_before_anything_is_written(world, kwargs):
    with pytest.raises(ValueError):
        client.analyze(**kwargs)
    assert not world["dispatches"] and not FakeStore.objects


def test_a_run_github_does_not_list_yet_counts_as_starting(world):
    import httpx

    def answer(code):
        def github(method, path, **kwargs):
            request = httpx.Request(method, "https://api.github.com" + path)
            raise httpx.HTTPStatusError(f"{code}", request=request, response=httpx.Response(code, request=request))
        return github

    clock = [1000.0]
    client._unlisted.clear()
    with patch.object(client, "_now", lambda: clock[0]):
        with patch.object(client.jobs, "github", answer(404)):
            first = client.status(601)
            assert first["state"] == "extracting" and "not listed" in first["message"]
            clock[0] += client.UNLISTED_GRACE_S - 1
            assert client.status(601)["state"] == "extracting"
            clock[0] += 2
            with pytest.raises(ValueError, match="no run 601"):
                client.status(601)
        with patch.object(client.jobs, "github", answer(500)), pytest.raises(httpx.HTTPStatusError):
            client.status(602)                         # other errors still surface
    assert client.status(501)["state"] == "extracting"   # a listed run: the normal state


def test_other_workflows_are_refused(world):
    world["run"]["path"] = ".github/workflows/model-review.yml"
    with pytest.raises(ValueError):
        client.status(501)


def test_mcp_tools():
    pytest.importorskip("mcp")
    from ear_pipeline.mcp_server import server
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"analyze_binary", "get_analysis", "get_function_code"}
