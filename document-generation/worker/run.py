"""One workflow attempt. Publish result.json only after validating and uploading the document."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from render import render, validate

from ear_document.config import credential, storage_settings
from ear_document.contract import (
    MAX_REQUEST_BYTES,
    MIME,
    Dispatch,
    Issue,
    Manifest,
    Request,
    digest,
    request_key,
    utc,
)
from ear_document.storage import B2, WORKER_ERRORS, base_key


def read_request(reader, prefix, dispatch):
    try:
        reply = reader.s3.get_object(Bucket=reader.bucket, Key=request_key(prefix, dispatch.job_id))
        with reply["Body"] as stream:
            data = stream.read(MAX_REQUEST_BYTES + 1)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            raise Issue("request_missing") from None
        raise Issue("storage_access") from None
    except BotoCoreError:
        raise Issue("remote_unavailable") from None
    if len(data) > MAX_REQUEST_BYTES or digest(data) != dispatch.request_sha256:
        raise Issue("invalid_worker_input")
    try:
        return Request.model_validate_json(data)
    except ValidationError:
        raise Issue("invalid_worker_input") from None


def execute(dispatch, run_id, attempt, reader, writer, prefix, folder):
    identity = dict(job_id=dispatch.job_id, workflow_run_id=run_id, run_attempt=attempt)
    started = utc()
    base = base_key(prefix, dispatch.job_id, run_id, attempt)
    code = "worker_failed"
    try:
        task = read_request(reader, prefix, dispatch)
        code = "render_failed"
        artifact = render(task, folder)
        pages = validate(artifact, task)
        data = artifact.read_bytes()
        key, checksum, mime = f"{base}/output.{task.format}", digest(data), MIME[task.format]
        writer.put(key, data, mime, {"sha256": checksum})
        result = Manifest(
            version=1,
            **identity,
            request_sha256=dispatch.request_sha256,
            format=task.format,
            object_key=key,
            mime_type=mime,
            byte_size=len(data),
            sha256=checksum,
            section_count=len(task.outline.sections),
            page_count=pages,
            started_at=started,
            uploaded_at=utc(),
        )
        writer.put(base + "/result.json", result.model_dump_json().encode(), "application/json")
    except Exception as exc:
        if isinstance(exc, Issue) and exc.code in WORKER_ERRORS:
            code = exc.code
        error = {**identity, "error_code": code, "timestamp": utc()}
        try:
            writer.put(base + "/error.json", json.dumps(error).encode(), "application/json")
        except Exception:
            pass
        print(code, file=sys.stderr)
        return 1
    return 0


def main():
    try:
        inputs = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["inputs"]
        dispatch = Dispatch.model_validate(inputs)
        run_id, attempt = (int(os.environ[k]) for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"))
        if min(run_id, attempt) < 1:
            raise ValueError
        region, bucket, prefix = storage_settings()
        reader = B2(region, bucket, *credential("REQUEST_READ"))
        writer = B2(region, bucket, *credential("OUTPUT_WRITE"))
        with TemporaryDirectory() as folder:
            return execute(dispatch, run_id, attempt, reader, writer, prefix, Path(folder))
    except Exception:
        print("Invalid worker identity or configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
