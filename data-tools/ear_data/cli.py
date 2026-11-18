import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import httpx
from ear_document.cli import fetch

from .agent import get_data_result, prepare_dataset_upload, run_data_job
from .plan import MAX_FILE


def upload_file(path):
    with Path(path).open("rb") as file:
        size = os.fstat(file.fileno()).st_size
        if not 0 < size <= MAX_FILE:
            raise ValueError
        checksum = hashlib.file_digest(file, "sha256").hexdigest()
        ext = Path(path).suffix.lower().lstrip(".")
        req = prepare_dataset_upload(checksum, "ndjson" if ext == "jsonl" else ext, size)
        if req["status"] != "upload_ready":
            return req
        file.seek(0)
        response = httpx.put(
            req["upload_url"],
            content=iter(lambda: file.read(1024 * 1024), b""),
            headers={"content-type": req["content_type"], "content-length": str(size)},
            timeout=60,
        )
        response.raise_for_status()
        return {"status": "uploaded", "handle": req["handle"], "byte_size": size}


def main():
    parser = argparse.ArgumentParser(prog="ear-data")
    commands = parser.add_subparsers(required=True, dest="action")
    upload = commands.add_parser("upload")
    upload.add_argument("file")
    submit = commands.add_parser("submit")
    submit.add_argument("plan")
    submit.add_argument("--input", action="append", required=True, metavar="ALIAS=HANDLE")
    submit.add_argument("--request-id")
    status = commands.add_parser("status")
    download = commands.add_parser("fetch")
    for c in (status, download):
        c.add_argument("job_id")
    for c in (submit, status, download):
        c.add_argument("--wait-seconds", type=int, default=0)
    download.add_argument("destination")
    download.add_argument("--artifact", choices=("table", "chart"), default="table")
    args = parser.parse_args()
    try:
        if args.action == "upload":
            reply = upload_file(args.file)
        elif args.action == "submit":
            pairs = [entry.split("=", 1) for entry in args.input]
            datasets = dict(pairs)
            if len(datasets) != len(pairs):
                raise ValueError
            with open(args.plan, "rb") as f:
                raw = f.read(65537)
            if len(raw) > 65536:
                raise ValueError
            reply = asyncio.run(
                run_data_job(datasets, json.loads(raw), args.wait_seconds, args.request_id)
            )
        else:
            reply = asyncio.run(get_data_result(args.job_id, args.wait_seconds))
            if args.action == "fetch" and reply["status"] == "completed":
                if not reply.get(args.artifact):
                    raise ValueError
                fetch(reply[args.artifact], args.destination)
                reply = {"status": "downloaded", "file": args.destination, "job_id": args.job_id}
    except (OSError, ValueError, KeyError):
        reply = {
            "status": "failed",
            "error_code": "invalid_input",
            "message": "Check the file, format, aliases and artifact choice.",
        }
    except httpx.HTTPError:
        reply = {
            "status": "failed",
            "error_code": "transfer_failed",
            "message": "B2 file transfer failed.",
        }
    print(json.dumps(reply))
    return int(
        reply["status"] in ("failed", "busy", "limit_reached")
        or (args.action == "fetch" and reply["status"] != "downloaded")
    )


if __name__ == "__main__":
    sys.exit(main())
