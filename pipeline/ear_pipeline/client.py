"""Agent-side API for the EAR pipeline: r2-ghidra-ci extraction -> modeling design, private in B2.

    analyze(binary_url | binary_key, goal, functions, design) -> extraction run (ear-extract.yml)
    status(run_id)  extraction state; once it succeeded (and a design was asked for) it starts the modeling run
                    once, with the extraction's evidence key, and then reports that run and finally the design
    function_code(run_id, name)  one decompiled function from the private extraction

Requests, binaries, evidence and designs stay in the private B2 bucket; GitHub only sees request IDs.
Settings are the modeling tool's: EAR_GITHUB_TOKEN (Actions read/write), EAR_GITHUB_REPO, EAR_GITHUB_REF and
EAR_B2_BUCKET / EAR_B2_REGION / EAR_B2_KEY_ID / EAR_B2_APPLICATION_KEY. EAR_MODELING_DIR points at modeling/.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid

_d = os.environ.get("EAR_MODELING_DIR")
if _d and _d not in sys.path:
    sys.path.insert(0, _d)

import httpx  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

import jobs  # noqa: E402  (modeling/jobs.py: Store, github, submit, status)

WORKFLOW = "ear-extract.yml"
MAX_EXTRACTION_BYTES = 2 << 20
MAX_CODE_CHARS = 6000
DEFAULT_GOAL = ("Explain how this program is built: its main components, the data it handles, "
                "and how control and data flow between them, using only the extracted evidence.")
BINARY_KEY = re.compile(r"ear-extract/binaries/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,200}(/[A-Za-z0-9_.-]{1,200}){0,4}")
_lock = threading.Lock()  # one modeling dispatch per extraction run, even with parallel status calls


def _missing(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}


def analyze(binary_url: str = "", binary_key: str = "", goal: str = "", functions: str = "",
            design: bool = True) -> dict:
    if bool(binary_url) == bool(binary_key):
        raise ValueError("Give exactly one of binary_url (https) or binary_key (ear-extract/binaries/...).")
    if binary_url:
        parts = urllib.parse.urlsplit(binary_url)
        if parts.scheme != "https" or not parts.netloc:
            raise ValueError("binary_url must be an https:// URL that GitHub's runner can download.")
    if binary_key and (not BINARY_KEY.fullmatch(binary_key) or ".." in binary_key.split("/")):
        raise ValueError("binary_key must be an object under ear-extract/binaries/ in the bucket.")
    goal = " ".join(goal.split()) or DEFAULT_GOAL
    if len(goal) > 1000:
        raise ValueError("goal must be at most 1000 characters.")
    names = [n.strip() for n in functions.split(",") if n.strip()]
    if any(not re.fullmatch(r"[A-Za-z0-9_.$@]{1,128}", n) for n in names):
        raise ValueError("functions must be a comma-separated list of function names.")
    jobs.required("EAR_GITHUB_TOKEN")
    request_id = uuid.uuid4().hex
    request = {"source": {"url": binary_url} if binary_url else {"key": binary_key}, "goal": goal,
               "functions": ",".join(names), "design": bool(design)}
    jobs.Store().write(f"ear-extract/requests/{request_id}.json", json.dumps(request).encode())
    try:
        run = jobs.github("POST", f"/workflows/{WORKFLOW}/dispatches", json={
            "ref": os.getenv("EAR_GITHUB_REF", "main"), "inputs": {"request_id": request_id}})
        return {"state": "extracting", "run_id": run["workflow_run_id"], "run_url": run["html_url"],
                "request_id": request_id}
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            raise
        raise RuntimeError(f"Dispatch outcome unknown. Check Actions for ear-extract-{request_id} "
                           "before starting again.") from exc


def _extraction_run(run_id: int) -> dict:
    if run_id < 1:
        raise ValueError("Expected a positive run ID.")
    run = jobs.github("GET", f"/runs/{run_id}")
    if run["path"] != f".github/workflows/{WORKFLOW}":
        raise ValueError("Run belongs to another workflow.")
    return run


def _prefix(run: dict) -> str:
    return f"ear-extract/results/{run['id']}/{run['run_attempt']}/"


UNLISTED_GRACE_S = 300
_unlisted: dict[int, float] = {}   # run ID -> when GitHub first answered 404 for it (this process)
_now = time.monotonic              # the tests replace this clock


def status(run_id: int) -> dict:
    try:
        run = _extraction_run(run_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        # GitHub can answer 404 for a few seconds after it starts a run (seen 2026-11-20, about 1 s after the
        # dispatch). Report the run as still starting, so a wait keeps polling; give up after a grace period.
        first = _unlisted.setdefault(run_id, _now())
        if _now() - first > UNLISTED_GRACE_S:
            raise ValueError(f"GitHub has no run {run_id}; check the run ID.") from None
        return {"run_id": run_id, "state": "extracting",
                "message": "GitHub has not listed this run yet (it can take a few seconds after the start). "
                           "Check again shortly."}
    _unlisted.pop(run_id, None)
    base = {"run_id": run_id, "run_url": run["html_url"]}
    if run["status"] != "completed":
        return {**base, "state": "extracting",
                "message": "Extraction is queued or running (about a minute or two). Check again shortly."}
    store = jobs.Store()
    if run["conclusion"] != "success":
        try:
            error = json.loads(store.read(_prefix(run) + "error.json"))
        except ClientError:
            error = {}
        reason = error.get("message") or error.get("error") or run["conclusion"]
        return {**base, "state": "error", "message": f"Extraction failed: {reason}. Run log: {run['html_url']}"}
    extraction = json.loads(store.read(_prefix(run) + "extraction.json", max_bytes=MAX_EXTRACTION_BYTES))
    key = f"ear-modeling/evidence/r2/{run['id']}-{run['run_attempt']}.json"
    result = {**base, "extraction": summarize(extraction), "evidence_key": key}
    request_id = (run.get("display_title") or "").removeprefix("ear-extract-")
    request = json.loads(store.read(f"ear-extract/requests/{request_id}.json"))
    if not request.get("design"):
        return {**result, "state": "done", "message": "Evidence extracted. Model it later with model_design "
                                                      f"(goal, evidence_key={key!r})."}
    marker = _prefix(run) + "design-run.json"
    with _lock:
        try:
            design_run = json.loads(store.read(marker))
        except ClientError as exc:
            if not _missing(exc):
                raise
            design_run = jobs.submit(request["goal"], key)
            store.write(marker, json.dumps(design_run).encode())
    modeling = jobs.status(int(design_run["run_id"]))
    result["design_run"] = {k: modeling.get(k) for k in ("run_id", "run_url", "status", "conclusion")}
    if modeling.get("design"):
        design = modeling["design"]
        return {**result, "state": "done", "design": design,
                "message": f"Design: {len(design['components'])} components, {len(design['relationships'])} "
                           "relationships, every one citing the extracted evidence."}
    if modeling["status"] == "completed":
        problem = modeling.get("result_error") or f"the modeling run ended with {modeling['conclusion']}"
        return {**result, "state": "partial", "message": f"Evidence extracted, but no design: {problem}."}
    return {**result, "state": "designing",
            "message": "Evidence extracted; the modeling run is building the design (several minutes on CPU)."}


def function_code(run_id: int, name: str) -> dict:
    run = _extraction_run(run_id)
    if run["status"] != "completed" or run["conclusion"] != "success":
        return {"error": "The extraction has not finished successfully."}
    extraction = json.loads(jobs.Store().read(_prefix(run) + "extraction.json", max_bytes=MAX_EXTRACTION_BYTES))
    for fn in extraction.get("decompiled", []):
        if fn["name"] == name or fn["name"].endswith("." + name):
            code = fn["code"]
            return {"name": fn["name"], "addr": fn["addr"], "method": fn["method"], "code": code[:MAX_CODE_CHARS],
                    "truncated": len(code) > MAX_CODE_CHARS}
    return {"error": f"{name!r} was not decompiled in run {run_id}.",
            "decompiled": [fn["name"] for fn in extraction.get("decompiled", [])]}


def summarize(extraction: dict) -> dict:
    functions = extraction.get("functions") or {}
    return {
        "info": extraction.get("info"),
        "imports": extraction.get("imports", [])[:60],
        "import_count": len(extraction.get("imports", [])),
        "strings": extraction.get("strings", [])[:30],
        "function_count": functions.get("count"),
        "largest_functions": [f["name"] for f in functions.get("largest", [])[:15]],
        "decompiled": [f["name"] for f in extraction.get("decompiled", [])],
        "evidence_sources": extraction.get("evidence_sources", []),
        "warnings": extraction.get("warnings", []),
    }
