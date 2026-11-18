"""Shared wire types. No inference libraries or implicit environment reads."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

MODEL = "stabilityai/sd-turbo"
REVISION = "b261bac6fd2cf515557d5d0707481eafa0485ec2"
MAX_SEED = 2**32 - 1
Seed = Annotated[int, Field(strict=True, ge=0, le=MAX_SEED)]
Positive = Annotated[int, Field(strict=True, gt=0)]
Wait = Annotated[int, Field(strict=True, ge=0, le=86400)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")]
ModelID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+$", max_length=150)]
Revision = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Duration = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def utc():
    return datetime.now(UTC).isoformat()


def canonical_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).version != 4:
        raise ValueError("Expected a canonical UUIDv4.")
    return value


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Request(Record):
    prompt: Annotated[str, Field(min_length=1, max_length=1000)]
    seed: Seed | None = None

    @field_validator("prompt")
    @classmethod
    def plain_text(cls, text):
        if not text.strip() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
            raise ValueError("Expected nonempty prompt text.")
        text.encode("utf-8")
        return text


class Submit(Request):
    wait_seconds: Wait = 0
    request_id: Key | None = None


class Lookup(Record):
    job_id: str
    wait_seconds: Wait = 0
    _validate_job = field_validator("job_id")(canonical_id)


class WorkerInput(Request):
    job_id: str
    seed: Seed
    _validate_job = field_validator("job_id")(canonical_id)


class ModelSpec(Record):
    model_id: ModelID = MODEL
    model_revision: Revision = REVISION


class Timings(Record):
    dependencies: Duration
    model_download: Duration
    model_load: Duration
    inference: Duration
    png_upload: Duration


class Resources(Record):
    ram_limit_bytes: Positive
    ram_available_bytes: Positive
    cpu_millicores: Positive
    cpu_threads: Positive
    disk_total_bytes: Positive
    disk_free_bytes: Positive
    peak_worker_rss_bytes: Positive
    peak_cgroup_memory_bytes: Positive | None = None


class Manifest(Record):
    model_id: ModelID
    model_revision: Revision
    version: Literal[1]
    job_id: str
    workflow_run_id: Positive
    run_attempt: Positive
    object_key: str
    mime_type: Literal["image/png"]
    byte_size: Annotated[int, Field(gt=0, le=8 * 1024**2)]
    sha256: Digest
    width: Literal[512]
    height: Literal[512]
    seed: Seed
    started_at: str
    uploaded_at: str
    timings: Timings
    resources: Resources
    _validate_job = field_validator("job_id")(canonical_id)

    @field_validator("started_at", "uploaded_at")
    @classmethod
    def utc_timestamp(cls, value):
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() != UTC.utcoffset(None):
            raise ValueError("Expected UTC timestamp.")
        return value


MESSAGES = {
    "invalid_input": "Check the prompt, seed, wait interval or identifier.",
    "idempotency_conflict": "This request ID was used with different parameters.",
    "busy": "Active generation limit reached.",
    "daily_limit": "Daily submission limit reached.",
    "unknown_job": "The local store has no such job.",
    "configuration": "Check trusted agent or worker configuration.",
    "dispatch_rejected": "GitHub rejected the workflow dispatch.",
    "rate_limited": "GitHub rate-limited the request.",
    "dispatch_unconfirmed": "No matching dispatch was confirmed in the recovery window.",
    "multiple_runs": "More than one workflow run matched this job ID.",
    "remote_unavailable": "The remote service is temporarily unavailable.",
    "run_missing": "GitHub no longer exposes the mapped run and no verified result was recorded.",
    "github_access": "GitHub run metadata is unavailable with this configuration.",
    "cancelled": "GitHub cancelled the generation.",
    "timed_out": "The generation exceeded its workflow time limit.",
    "worker_failed": "The workflow ended before publishing a verified result.",
    "invalid_worker_input": "The worker rejected its dispatch inputs.",
    "insufficient_resources": "The CPU runner has insufficient memory or disk.",
    "dependencies_failed": "The worker dependency installation failed.",
    "model_failed": "The worker could not download or load the pinned model.",
    "inference_failed": "CPU image generation failed.",
    "invalid_png": "The worker could not validate the generated PNG.",
    "upload_failed": "The worker could not publish its output to B2.",
    "output_missing": "A required B2 output is missing.",
    "output_invalid": "The stored output failed identity or integrity checks.",
    "storage_access": "B2 rejected the storage request.",
}


class Issue(Exception):
    def __init__(self, code, delay=0):
        self.code, self.delay = code, delay
        super().__init__(MESSAGES[code])


def failure(code, **fields):
    return dict(status="failed", error_code=code, message=MESSAGES[code], **fields)
