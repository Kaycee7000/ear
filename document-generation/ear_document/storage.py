import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import boto3
import httpx
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from .contract import MIME, Issue, Manifest, request_key


def base_key(prefix, job, run_id, attempt):
    return f"{prefix}/results/{job}/{run_id}/{attempt}"


class B2:
    def __init__(self, region, bucket, key, secret):
        if not key or not secret:
            raise Issue("configuration")
        self.bucket, self.region = bucket, region
        self.credentials = Credentials(key, secret)
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://s3.{region}.backblazeb2.com",
            region_name=region,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=3,
                read_timeout=6,
                retries={"total_max_attempts": 1},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    async def stage(self, prefix, row):
        key = request_key(prefix, row["job_id"])
        data = row["request_json"].encode()
        url = f"{self.s3.meta.endpoint_url}/{self.bucket}/{quote(key, safe='/')}"
        req = AWSRequest(method="PUT", url=url, data=data, headers={"Content-Type": "application/json"})
        S3SigV4Auth(self.credentials, "s3", self.region).add_auth(req)
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                res = await client.put(url, content=data, headers=dict(req.headers))
            if res.status_code == 429 or res.status_code >= 500:
                raise Issue("remote_unavailable", 15)
            if not res.is_success:
                raise Issue("staging_rejected")
        except httpx.TransportError:
            raise Issue("remote_unavailable", 15) from None

    async def _read(self, method, key):
        # Header-sign metadata requests with the AWS SDK. Async transport lets the
        # agent cancel a slow poll at its deadline without waiting for a DNS/SDK thread.
        url = f"{self.s3.meta.endpoint_url}/{self.bucket}/{quote(key, safe='/')}"
        req = AWSRequest(method=method, url=url)
        S3SigV4Auth(self.credentials, "s3", self.region).add_auth(req)
        try:
            async with httpx.AsyncClient(timeout=6) as client:
                async with client.stream(method, url, headers=dict(req.headers)) as res:
                    if res.status_code == 404:
                        raise Issue("output_missing")
                    if res.status_code == 429 or res.status_code >= 500:
                        raise Issue("remote_unavailable", 15)
                    if res.status_code != 200:
                        raise Issue("storage_access")
                    if method == "HEAD":
                        return res.headers
                    data = bytearray()
                    async for chunk in res.aiter_bytes(chunk_size=8192):
                        data.extend(chunk)
                        if len(data) > 65536:
                            raise Issue("output_invalid")
                    return json.loads(data)
        except (ValueError, UnicodeError):
            raise Issue("output_invalid") from None
        except httpx.TransportError:
            raise Issue("remote_unavailable", 15) from None

    def put(self, key, body, kind, metadata=None):
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=body, ContentType=kind, Metadata=metadata or {}
            )
        except (BotoCoreError, ClientError):
            raise Issue("upload_failed") from None

    async def verify(self, row, prefix, cached=None):
        base = base_key(prefix, row["job_id"], row["run_id"], row["attempt"])
        try:
            data = cached if cached is not None else await self._read("GET", base + "/result.json")
            manifest = Manifest.model_validate(data)
        except (ValidationError, ValueError):
            raise Issue("output_invalid") from None
        if (
            manifest.job_id,
            manifest.workflow_run_id,
            manifest.run_attempt,
            manifest.request_sha256,
            manifest.format,
        ) != (
            row["job_id"],
            row["run_id"],
            row["attempt"],
            row["request_sha256"],
            row["format"],
        ) or manifest.object_key != f"{base}/output.{manifest.format}":
            raise Issue("output_invalid")
        head = await self._read("HEAD", manifest.object_key)
        if (
            head.get("content-type") != MIME[manifest.format]
            or manifest.mime_type != MIME[manifest.format]
            or head.get("content-length") != str(manifest.byte_size)
            or head.get("x-amz-meta-sha256") != manifest.sha256
        ):
            raise Issue("output_invalid")
        return manifest.model_dump()

    async def worker_error(self, row, prefix):
        try:
            data = await self._read(
                "GET", base_key(prefix, row["job_id"], row["run_id"], row["attempt"]) + "/error.json"
            )
            if (
                isinstance(data, dict)
                and data.get("job_id") == row["job_id"]
                and data.get("workflow_run_id") == row["run_id"]
                and data.get("run_attempt") == row["attempt"]
                and isinstance(data.get("error_code"), str)
                and data["error_code"] in WORKER_ERRORS
            ):
                return data["error_code"]
        except Issue:
            pass
        return "worker_failed"

    def sign(self, key, ttl):
        now = datetime.now(UTC)
        url = self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl, HttpMethod="GET"
        )
        return url, (now + timedelta(seconds=ttl)).isoformat()


WORKER_ERRORS = {
    "invalid_worker_input",
    "request_missing",
    "render_failed",
    "invalid_output",
    "upload_failed",
    "configuration",
    "worker_failed",
    "storage_access",
    "remote_unavailable",
}
