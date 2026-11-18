"""Private extraction job: a request ID in, the extraction and modeling evidence out, both in private B2.

Run by .github/workflows/ear-extract.yml on a GitHub-hosted runner. It reads ear-extract/requests/<id>.json,
fetches the binary (an https URL, or an object the owner uploaded under ear-extract/binaries/), runs
ear_extract.py inside the r2-ghidra-ci toolbox image with no network access, and writes:

  ear-extract/results/<run_id>/<attempt>/extraction.json   everything extracted (decompiled code included)
  ear-modeling/evidence/r2/<run_id>-<attempt>.json          {"sources": [...]}, an evidence key for modeling

The public job log only shows counts, never the request, the binary's contents or the evidence.
Uses the modeling tool's B2 settings: EAR_B2_BUCKET, EAR_B2_REGION, EAR_B2_KEY_ID, EAR_B2_APPLICATION_KEY.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "modeling"))

import ear_extract  # noqa: E402
from jobs import Store, required  # noqa: E402

IMAGE = os.getenv("EAR_R2_IMAGE", "ghcr.io/kaycee7000/ear/r2-ghidra-ci:latest")
MAX_BINARY_BYTES = 50 << 20
MAX_EXTRACTION_BYTES = 2 << 20
BINARY_KEY = re.compile(r"ear-extract/binaries/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,200}(/[A-Za-z0-9_.-]{1,200}){0,4}")


def request_key(request_id: str) -> str:
    return f"ear-extract/requests/{request_id}.json"


def result_key(run_id: int, attempt: int, name: str) -> str:
    if run_id < 1 or attempt < 1:
        raise ValueError("Expected a positive run ID and attempt.")
    return f"ear-extract/results/{run_id}/{attempt}/{name}"


def evidence_key(run_id: int, attempt: int) -> str:
    return f"ear-modeling/evidence/r2/{run_id}-{attempt}.json"


def run_toolbox(workdir: Path, goal: str, functions: str) -> None:
    """ear_extract.py inside the toolbox image: no network, no capabilities, the binary is never executed."""
    command = ["docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--memory", "6g", "--pids-limit", "256",
               "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
               "-v", f"{workdir}:/work", "-v", f"{HERE}:/x:ro", IMAGE,
               "python3", "/x/ear_extract.py", "--file", "/work/binary", "--goal", goal,
               "--functions", functions, "--out", "/work/out"]
    subprocess.run(command, capture_output=True, timeout=1200)  # output stays out of the public log


def work(request_id: str, run_id: int, attempt: int, store: Store | None = None) -> dict:
    if UUID(request_id).hex != request_id:
        raise ValueError("Invalid request ID.")
    store = store or Store()
    request = json.loads(store.read(request_key(request_id)))
    source = request.get("source") or {}
    with tempfile.TemporaryDirectory(prefix="ear-extract-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        binary = workdir / "binary"
        if source.get("url"):
            ear_extract.fetch(source["url"], binary, MAX_BINARY_BYTES)
        elif source.get("key"):
            if not BINARY_KEY.fullmatch(source["key"]) or ".." in source["key"].split("/"):
                raise ear_extract.ExtractError("binary_key must be an object under ear-extract/binaries/")
            binary.write_bytes(store.read(source["key"], max_bytes=MAX_BINARY_BYTES))
        else:
            raise ear_extract.ExtractError("the request names no binary")
        run_toolbox(workdir, request.get("goal") or "", request.get("functions") or "")
        out = workdir / "out"
        extraction = json.loads((out / "extraction.json").read_text()) if (out / "extraction.json").exists() else {
            "error": "the toolbox container produced no output"}
        if extraction.get("error"):
            raise ear_extract.ExtractError(extraction["error"])
        evidence = json.loads((out / "evidence.json").read_text())
    store.write(result_key(run_id, attempt, "extraction.json"), json.dumps(extraction).encode(),
                max_bytes=MAX_EXTRACTION_BYTES)
    store.write(evidence_key(run_id, attempt), json.dumps(evidence, ensure_ascii=False).encode())
    return extraction


def main() -> int:
    run_id, attempt = int(required("GITHUB_RUN_ID")), int(required("GITHUB_RUN_ATTEMPT"))
    try:
        extraction = work(required("EAR_REQUEST_ID"), run_id, attempt)
    except Exception as exc:
        # Only extractor messages are fixed, content-free texts; anything else is reported by type alone.
        message = str(exc) if isinstance(exc, ear_extract.ExtractError) else ""
        try:
            Store().write(result_key(run_id, attempt, "error.json"),
                          json.dumps({"error": type(exc).__name__, "message": message}).encode())
        except Exception:
            pass
        print(f"Extraction failed ({type(exc).__name__}). Details are in the private result.", file=sys.stderr)
        return 1
    print(f"Extracted {extraction['functions']['count']} functions, decompiled {len(extraction['decompiled'])}, "
          f"{len(extraction.get('evidence_sources', []))} evidence sources.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
