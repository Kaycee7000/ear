import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx

from .agent import invoke


def fetch(result, destination):
    """Stream only to a user-chosen CLI destination; verify before publishing the file."""
    target = Path(destination)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
        name = temp.name
    try:
        digest, size = hashlib.sha256(), 0
        with httpx.stream("GET", result["download_url"], timeout=30, follow_redirects=False) as resp:
            resp.raise_for_status()
            with open(name, "wb") as output:
                for part in resp.iter_bytes():
                    size += len(part)
                    if size > result["byte_size"]:
                        raise ValueError
                    digest.update(part)
                    output.write(part)
        if size != result["byte_size"] or digest.hexdigest() != result["sha256"]:
            raise ValueError
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(prog="ear-document")
    sub = parser.add_subparsers(required=True, dest="command")
    submit = sub.add_parser("submit")
    submit.add_argument("outline_file", help="UTF-8 JSON outline file; use - for stdin")
    submit.add_argument("--format", required=True, choices=("docx", "pdf", "pptx"))
    submit.add_argument("--request-id")
    status = sub.add_parser("status")
    status.add_argument("job_id")
    download = sub.add_parser("fetch")
    download.add_argument("job_id")
    download.add_argument("destination")
    for cmd in (submit, status, download):
        cmd.add_argument("--wait-seconds", type=int, default=0)
    args = vars(parser.parse_args())
    cmd, destination = args.pop("command"), args.pop("destination", None)
    if cmd == "submit":
        path = args.pop("outline_file")
        try:
            if path == "-":
                raw = sys.stdin.buffer.read(131073)
            else:
                with open(path, "rb") as source:
                    raw = source.read(131073)
            if len(raw) > 131072:
                raise ValueError
            args["outline"] = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError):
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error_code": "invalid_input",
                        "message": "Expected a UTF-8 JSON outline file under 128 KiB.",
                    }
                )
            )
            return 1
    result = asyncio.run(
        invoke("generate_document" if cmd == "submit" else "get_document_result", **args)
    )
    if cmd == "fetch" and result["status"] == "completed":
        try:
            fetch(result, destination)
        except (ValueError, OSError, httpx.HTTPError):
            print("Download or checksum validation failed.", file=sys.stderr)
            return 1
        print(json.dumps({"status": "completed", "job_id": result["job_id"], "file": destination}))
    else:
        print(json.dumps(result))
    return int(
        result["status"] in ("failed", "busy", "limit_reached")
        or (cmd == "fetch" and result["status"] != "completed")
    )


if __name__ == "__main__":
    sys.exit(main())
