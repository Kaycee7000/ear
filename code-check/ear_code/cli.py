import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
from ear_document.cli import fetch

from .agent import get_code_result, prepare_source_upload, run_code_check
from .pack import PackError, pack
from .plan import MAX_SOURCE


def upload_file(path):
    with Path(path).open("rb") as file:
        size = os.fstat(file.fileno()).st_size
        if not 0 < size <= MAX_SOURCE:
            raise ValueError
        checksum = hashlib.file_digest(file, "sha256").hexdigest()
        req = prepare_source_upload(checksum, size)
        if req["status"] != "upload_ready":
            return req
        file.seek(0)
        response = httpx.put(
            req["upload_url"],
            content=iter(lambda: file.read(1024 * 1024), b""),
            headers={"content-type": req["content_type"], "content-length": str(size)},
            timeout=120,
        )
        response.raise_for_status()
        return {"status": "uploaded", "source": req["handle"], "byte_size": size}


def main():
    parser = argparse.ArgumentParser(prog="ear-code")
    commands = parser.add_subparsers(required=True, dest="action")
    check = commands.add_parser("check", help="pack a project folder, upload it and start a review or test")
    check.add_argument("project")
    check.add_argument("plan", help="JSON plan file, for example examples/test-plan.json")
    check.add_argument("--base", help="git ref the review diff starts from (default: HEAD)")
    check.add_argument("--name", help="project name shown in the report (default: folder name)")
    check.add_argument("--request-id")
    status = commands.add_parser("status")
    download = commands.add_parser("fetch")
    for c in (status, download):
        c.add_argument("job_id")
    for c in (check, status, download):
        c.add_argument("--wait-seconds", type=int, default=0)
    download.add_argument("destination")
    download.add_argument("--artifact", default="report", help="report, or the name of a built file")
    args = parser.parse_args()
    try:
        if args.action == "check":
            with tempfile.TemporaryDirectory() as tmp:
                packed = pack(args.project, Path(tmp) / "source.tar.gz", args.base)
                sent = upload_file(packed["path"])
            if sent["status"] != "uploaded":
                reply = sent
            else:
                plan = json.loads(Path(args.plan).read_text())
                name = (args.name or Path(args.project).resolve().name)[:80]
                reply = asyncio.run(run_code_check(sent["source"], name, plan, args.wait_seconds, args.request_id))
        else:
            reply = asyncio.run(get_code_result(args.job_id, args.wait_seconds))
            if args.action == "fetch" and reply["status"] == "completed":
                items = {"report": reply["report"], **{a["name"]: a for a in reply["artifacts"]}}
                fetch(items[args.artifact], args.destination)
                reply = {"status": "downloaded", "file": args.destination, "job_id": args.job_id}
    except (OSError, ValueError, KeyError, PackError):
        reply = {"status": "failed", "error_code": "invalid_input",
                 "message": "Check the project folder, plan file and artifact name."}
    except httpx.HTTPError:
        reply = {"status": "failed", "error_code": "transfer_failed", "message": "B2 file transfer failed."}
    print(json.dumps(reply))
    return int(reply["status"] in ("failed", "busy", "limit_reached")
               or (args.action == "fetch" and reply["status"] != "downloaded"))


if __name__ == "__main__":
    sys.exit(main())
