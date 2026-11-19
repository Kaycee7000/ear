"""Networked I/O phases. The containers that run project code never receive these credentials.

    python -m worker.transfer stage     request + project archive from B2 -> /input (checksums verified)
    python -m worker.transfer publish   logs, report, artifacts -> B2, then result.json last
    python -m worker.transfer error     error.json with a fixed error code
"""

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

from ear_code.plan import (
    ERRORS,
    MAX_ARTIFACT,
    MAX_LOG,
    MAX_MANIFEST,
    MAX_REQUEST,
    MAX_SOURCE,
    MIME,
    Artifact,
    CodeError,
    Dispatch,
    Manifest,
    Request,
    Summary,
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
            read_timeout=30,
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
                raise CodeError("limit_exceeded")
            while chunk := stream.read(1024**2):
                total += len(chunk)
                if total > cap:
                    raise CodeError("limit_exceeded")
                digest.update(chunk)
                out.write(chunk)
        if total != reply["ContentLength"] or digest.hexdigest() != checksum:
            raise CodeError("source_invalid")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        raise CodeError(
            "source_missing" if code in ("404", "NoSuchKey", "NotFound") else "storage_access"
        ) from None


def stage(s3, bucket, prefix, dispatch, folder):
    path = folder / "request.json"
    download(s3, bucket, f"{prefix}/requests/{dispatch.job_id}.json", path, MAX_REQUEST, dispatch.request_sha256)
    try:
        req = Request.model_validate_json(path.read_bytes())
    except ValueError:
        raise CodeError("invalid_worker_input") from None
    download(
        s3,
        bucket,
        f"{prefix}/requests/sources/{req.source}",
        folder / "source.tar.gz",
        MAX_SOURCE,
        req.source.split(".")[0],
    )


@contextmanager
def local_file(path, cap):
    # The offline phases have exited. Accept regular, bounded files only.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise CodeError("worker_failed")
        yield file, info.st_size


def upload(s3, bucket, key, path, name, mime, cap=MAX_ARTIFACT):
    with local_file(path, cap) as (file, size):
        checksum = hashlib.file_digest(file, "sha256").hexdigest()
        file.seek(0)
        s3.put_object(
            Bucket=bucket,
            Key=key,
            ContentType=mime,
            ContentLength=size,
            Metadata={"sha256": checksum},
            Body=file,
        )
    return Artifact(object_key=key, name=name, mime_type=mime, byte_size=size, sha256=checksum)


def guess(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "zip": "application/zip",
        "apk": "application/vnd.android.package-archive",
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "html": "text/html; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
        "md": MIME["md"],
        "json": MIME["json"],
        "csv": "text/csv",
        "gz": MIME["gz"],
        "tgz": MIME["gz"],
        "whl": "application/zip",
        "mp3": "audio/mpeg",
    }.get(ext, MIME["bin"])


def publish(s3, bucket, base, dispatch, run, attempt, inputs, output):
    raw = (inputs / "request.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != dispatch.request_sha256:
        raise CodeError("invalid_worker_input")
    req = Request.model_validate_json(raw)
    with local_file(output / "summary.json", MAX_MANIFEST) as (file, _):
        summary = Summary.model_validate_json(file.read())
    if summary.mode != req.plan.mode:
        raise CodeError("worker_failed")
    steps = []
    for step in summary.steps:
        log = output / "logs" / f"{step.name}.log"
        if log.exists():
            step = step.model_copy(
                update={"log": upload(s3, bucket, f"{base}/logs/{step.name}.log", log, f"{step.name}.log", MIME["log"], MAX_LOG)}
            )
        steps.append(step)
    artifacts = [
        upload(s3, bucket, f"{base}/artifacts/{name}", output / "artifacts" / name, name, guess(name))
        for name in summary.artifacts
    ]
    report = upload(s3, bucket, f"{base}/report.md", output / "report.md", "report.md", MIME["md"], MAX_LOG)
    result = Manifest(
        **dispatch.model_dump(),
        version=1,
        workflow_run_id=run,
        run_attempt=attempt,
        mode=summary.mode,
        outcome=summary.outcome,
        project=req.project,
        steps=steps,
        review=summary.review,
        analysis=summary.analysis,
        model=summary.model,
        artifacts=artifacts,
        report=report,
        source_files=summary.source_files,
        completed_at=utc(),
    )
    body = result.model_dump_json().encode()
    if len(body) > MAX_MANIFEST:
        raise CodeError("worker_failed")
    s3.put_object(Bucket=bucket, Key=base + "/result.json", ContentType="application/json", Body=body)


def fail(s3, bucket, base, dispatch, run, attempt, code):
    error = {
        **dispatch.model_dump(),
        "workflow_run_id": run,
        "run_attempt": attempt,
        "error_code": code if code in ERRORS else "worker_failed",
    }
    s3.put_object(Bucket=bucket, Key=base + "/error.json", ContentType="application/json", Body=json.dumps(error).encode())


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
        code = exc.code if isinstance(exc, CodeError) else "worker_failed"
        if isinstance(exc, (ClientError, BotoCoreError)):
            code = "storage_access" if mode == "stage" else "upload_failed"
        # Only the parent runner reads this fixed vocabulary; discard SDK/parser messages.
        print(code)
        return 1


if __name__ == "__main__":
    sys.exit(main())
