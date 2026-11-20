"""Private suites: a suite kept in the EAR bucket, and its results kept there too.

A private suite can carry data that should not be public, such as a design built from a private binary. The
workflow reads it from the bucket (`suite_key`), the runner writes no prompts or replies to the job summary, and
results.json and report.md go back to the bucket instead of to a workflow artifact:

    model-review/suites/<name>.yaml          the suite (written by the agent's tools)
    model-review/results/<request_id>/       results.json and report.md

    python -m model_review.private fetch <suite_key> <dest.yaml>     (in the workflow)
    python -m model_review.private publish <request_id> <out_dir>    (in the workflow)

Settings come from EAR_B2_REGION (us-east-005 or its s3 endpoint), EAR_B2_BUCKET, EAR_B2_KEY_ID and
EAR_B2_APPLICATION_KEY. boto3 is imported only here, so public suites need no extra packages.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SUITE_KEY = re.compile(r"model-review/suites/[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.ya?ml")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
RESULTS_PREFIX = "model-review/results/"
MAX_SUITE_BYTES = 1024 * 1024
MAX_RESULTS_BYTES = 8 * 1024 * 1024


class PrivateError(RuntimeError):
    """The bucket is not configured, or the object is missing, too large or misnamed."""


def check_suite_key(key: str) -> str:
    if not SUITE_KEY.fullmatch(key or ""):
        raise PrivateError("suite_key must be an object under model-review/suites/ ending in .yaml")
    return key


def check_request_id(request_id: str) -> str:
    if not REQUEST_ID.fullmatch(request_id or ""):
        raise PrivateError("a private review needs a request id (letters, digits, - and _)")
    return request_id


def results_key(request_id: str, name: str = "results.json") -> str:
    return f"{RESULTS_PREFIX}{check_request_id(request_id)}/{name}"


def _bucket():
    names = ("EAR_B2_REGION", "EAR_B2_BUCKET", "EAR_B2_KEY_ID", "EAR_B2_APPLICATION_KEY")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise PrivateError("private suites need the bucket settings: " + ", ".join(missing))
    import boto3
    from botocore.config import Config

    region = os.environ["EAR_B2_REGION"].strip().removeprefix("https://").removeprefix("s3.")
    region = region.split(".backblazeb2.com")[0]
    client = boto3.client(
        "s3", endpoint_url=f"https://s3.{region}.backblazeb2.com", region_name=region,
        aws_access_key_id=os.environ["EAR_B2_KEY_ID"], aws_secret_access_key=os.environ["EAR_B2_APPLICATION_KEY"],
        config=Config(signature_version="s3v4", connect_timeout=20, read_timeout=60, retries={"max_attempts": 3}),
    )
    return client, os.environ["EAR_B2_BUCKET"]


def _read(client, bucket: str, key: str, limit: int) -> bytes:
    reply = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{limit}")
    with reply["Body"] as body:
        data = body.read(limit + 1)
    if len(data) > limit:
        raise PrivateError(f"{key} is larger than {limit} bytes")
    return data


def fetch_suite(key: str, dest: str | Path, client=None, bucket: str | None = None) -> Path:
    """Download a private suite to `dest` (the runner then reads it with --suite-file)."""
    check_suite_key(key)
    if client is None:
        client, bucket = _bucket()
    try:
        data = _read(client, bucket, key, MAX_SUITE_BYTES)
    except PrivateError:
        raise
    except Exception as exc:  # botocore errors: say which object, never the settings
        raise PrivateError(f"could not read {key}: {type(exc).__name__}") from None
    dest = Path(dest)
    dest.write_bytes(data)
    return dest


def publish_results(request_id: str, out_dir: str | Path, client=None, bucket: str | None = None) -> list[str]:
    """Upload results.json and report.md from the runner's output folder; returns the keys written."""
    check_request_id(request_id)
    if client is None:
        client, bucket = _bucket()
    written = []
    for name, mime in (("results.json", "application/json"), ("report.md", "text/markdown; charset=utf-8")):
        path = Path(out_dir) / name
        if path.is_file():
            key = results_key(request_id, name)
            client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes(), ContentType=mime)
            written.append(key)
    if not written:
        raise PrivateError(f"no results in {out_dir}")
    return written


def read_results(request_id: str, client=None, bucket: str | None = None) -> dict | None:
    """results.json of a private review, or None if it was never published."""
    if client is None:
        client, bucket = _bucket()
    try:
        return json.loads(_read(client, bucket, results_key(request_id), MAX_RESULTS_BYTES))
    except PrivateError:
        raise
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "InvalidRange"):
            return None
        raise PrivateError(f"could not read the private results: {type(exc).__name__}") from None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 3 and args[0] == "fetch":
            fetch_suite(args[1], args[2])
            print(f"Fetched the private suite ({Path(args[2]).stat().st_size} bytes)")
        elif len(args) == 3 and args[0] == "publish":
            keys = publish_results(args[1], args[2])
            print(f"Published {len(keys)} private result file(s) to the bucket")
        else:
            print("usage: python -m model_review.private fetch <suite_key> <dest> | publish <request_id> <out_dir>",
                  file=sys.stderr)
            return 2
    except PrivateError as exc:
        print(f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
