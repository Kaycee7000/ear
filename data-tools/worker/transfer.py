"""Networked I/O phases. The analysis container never receives these credentials."""

import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from ear_data.plan import (
    ERRORS,
    MAX_FILE,
    MAX_PLAN,
    MAX_TOTAL,
    MIME,
    Artifact,
    DataError,
    Dispatch,
    Manifest,
    Request,
    Shape,
    storage_location,
    utc,
)


def client(region):
    return boto3.client(
        "s3",
        endpoint_url=f"https://s3.{region}.backblazeb2.com",
        region_name=region,
        aws_access_key_id=os.environ["EAR_IO_KEY_ID"],
        aws_secret_access_key=os.environ["EAR_IO_KEY"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=20,
            retries={"total_max_attempts": 2},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def download(s3, bucket, key, path, cap, checksum):
    try:
        reply = s3.get_object(Bucket=bucket, Key=key)
        total, digest = 0, hashlib.sha256()
        with reply["Body"] as stream, path.open("wb") as out:
            if reply["ContentLength"] > cap:
                raise DataError("limit_exceeded")
            while chunk := stream.read(1024**2):
                total += len(chunk)
                if total > cap:
                    raise DataError("limit_exceeded")
                digest.update(chunk)
                out.write(chunk)
        if total != reply["ContentLength"] or digest.hexdigest() != checksum:
            raise DataError("dataset_invalid")
        return total
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        raise DataError(
            "dataset_missing" if code in ("404", "NoSuchKey", "NotFound") else "storage_access"
        ) from None


def stage(s3, bucket, prefix, dispatch, folder):
    key = f"{prefix}/requests/{dispatch.job_id}.json"
    path = folder / "request.json"
    download(s3, bucket, key, path, MAX_PLAN, dispatch.request_sha256)
    try:
        req = Request.model_validate_json(path.read_bytes())
    except ValueError:
        raise DataError("invalid_worker_input") from None
    remaining = MAX_TOTAL
    for handle in set(req.datasets.values()):
        remaining -= download(
            s3,
            bucket,
            f"{prefix}/requests/datasets/{handle}",
            folder / handle,
            min(MAX_FILE, remaining),
            handle.split(".")[0],
        )


@contextmanager
def local_file(path, cap):
    # The offline compute process has exited. Accept regular, bounded files only.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise DataError("analysis_failed")
        yield file, info.st_size


def upload(s3, bucket, key, path, fmt):
    with local_file(path, MAX_FILE) as (file, size):
        checksum = hashlib.file_digest(file, "sha256").hexdigest()
        file.seek(0)
        mime = MIME[fmt]
        s3.put_object(
            Bucket=bucket,
            Key=key,
            ContentType=mime,
            ContentLength=size,
            Metadata={"sha256": checksum},
            Body=file,
        )
    return Artifact(object_key=key, mime_type=mime, byte_size=size, sha256=checksum)


def publish(s3, bucket, base, dispatch, run, attempt, inputs, output):
    raw = (inputs / "request.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != dispatch.request_sha256:
        raise DataError("invalid_worker_input")
    req = Request.model_validate_json(raw)
    with local_file(output / "shape.json", 512) as (file, _):
        shape = Shape.model_validate_json(file.read())
    table = upload(
        s3, bucket, f"{base}/table.{req.format}", output / ("table." + req.format), req.format
    )
    if not table.byte_size and (req.format != "ndjson" or shape.rows):
        raise DataError("analysis_failed")
    chart = (
        upload(s3, bucket, base + "/chart.png", output / "chart.png", "png") if req.plan.chart else None
    )
    result = Manifest(
        **dispatch.model_dump(),
        **shape.model_dump(),
        version=1,
        workflow_run_id=run,
        run_attempt=attempt,
        format=req.format,
        table=table,
        chart=chart,
        completed_at=utc(),
    )
    s3.put_object(
        Bucket=bucket,
        Key=base + "/result.json",
        ContentType="application/json",
        Body=result.model_dump_json().encode(),
    )


def fail(s3, bucket, base, dispatch, run, attempt, code):
    error = {
        **dispatch.model_dump(),
        "workflow_run_id": run,
        "run_attempt": attempt,
        "error_code": code if code in ERRORS else "analysis_failed",
    }
    s3.put_object(
        Bucket=bucket,
        Key=base + "/error.json",
        ContentType="application/json",
        Body=json.dumps(error).encode(),
    )


def main():
    mode = sys.argv[1]
    try:
        dispatch = Dispatch(job_id=os.environ["JOB_ID"], request_sha256=os.environ["REQUEST_SHA"])
        run, attempt = int(os.environ["RUN_ID"]), int(os.environ["ATTEMPT"])
        if min(run, attempt) < 1:
            raise ValueError
        region, bucket, prefix = storage_location(
            os.environ["B2_REGION"], os.environ["B2_BUCKET"], os.environ["B2_PREFIX"]
        )
        s3 = client(region)
        base = f"{prefix}/results/{dispatch.job_id}/{run}/{attempt}"
        if mode == "stage":
            stage(s3, bucket, prefix, dispatch, Path("/input"))
        elif mode == "publish":
            publish(s3, bucket, base, dispatch, run, attempt, Path("/input"), Path("/output"))
        elif mode == "error":
            fail(s3, bucket, base, dispatch, run, attempt, os.environ["ERROR_CODE"])
        else:
            raise ValueError
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, DataError) else "analysis_failed"
        if isinstance(exc, (ClientError, BotoCoreError)):
            code = "storage_access" if mode == "stage" else "upload_failed"
        # Only the parent runner reads this fixed vocabulary; discard SDK/parser messages.
        print(code)
        return 1


if __name__ == "__main__":
    sys.exit(main())
