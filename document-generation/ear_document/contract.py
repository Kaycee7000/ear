"""Private request and result contracts shared by the coordinator and worker."""

import hashlib
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Format = Literal["pdf", "docx", "pptx"]
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
Wait = Annotated[int, Field(strict=True, ge=0, le=86400)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(strict=True, gt=0)]
MAX_REQUEST_BYTES = 48 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024**2


def utc():
    return datetime.now(UTC).isoformat()


def canonical_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value or UUID(value).version != 4:
        raise ValueError("Expected a canonical UUIDv4.")
    return value


def request_key(prefix, job):
    return f"{prefix}/requests/{canonical_id(job)}.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def line(value):
    if not value.strip() or re.search(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]", value
    ):
        raise ValueError("Expected plain text.")
    return " ".join(value.split())


Heading = Annotated[str, Field(min_length=1, max_length=96), AfterValidator(line)]
Text = Annotated[str, Field(min_length=1, max_length=1200), AfterValidator(line)]
Subtitle = Annotated[str, Field(min_length=1, max_length=180), AfterValidator(line)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Section(Record):
    heading: Heading
    paragraphs: list[Text] = Field(default_factory=list, max_length=24)
    bullets: list[Text] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def has_content(self):
        if not self.paragraphs and not self.bullets:
            raise ValueError("A section needs a paragraph or bullet.")
        return self


class Outline(Record):
    title: Heading
    subtitle: Subtitle | None = None
    author: Heading | None = None
    sections: list[Section] = Field(min_length=1, max_length=40)


class Request(Record):
    outline: Outline
    format: Format

    def payload_json(self):
        return self.model_dump_json(include={"outline", "format"})

    @model_validator(mode="after")
    def fits(self):
        if len(self.payload_json().encode()) > MAX_REQUEST_BYTES:
            raise ValueError("Outline exceeds the UTF-8 byte limit.")
        return self


class Submit(Request):
    wait_seconds: Wait = 0
    request_id: Key | None = None


class Lookup(Record):
    job_id: Annotated[str, AfterValidator(canonical_id)]
    wait_seconds: Wait = 0


class Dispatch(Record):
    job_id: Annotated[str, AfterValidator(canonical_id)]
    request_sha256: Digest


class Manifest(Dispatch):
    version: Literal[1]
    workflow_run_id: Positive
    run_attempt: Positive
    format: Format
    object_key: str
    mime_type: str
    byte_size: Annotated[int, Field(gt=0, le=MAX_ARTIFACT_BYTES)]
    sha256: Digest
    section_count: Annotated[int, Field(ge=1, le=40)]
    page_count: Positive | None
    started_at: str
    uploaded_at: str

    @field_validator("started_at", "uploaded_at")
    @classmethod
    def utc_timestamp(cls, value):
        if datetime.fromisoformat(value).utcoffset() != UTC.utcoffset(None):
            raise ValueError("Expected UTC.")
        return value

    @model_validator(mode="after")
    def content_type(self):
        if self.mime_type != MIME[self.format] or (self.page_count is None) != (self.format == "docx"):
            raise ValueError("Invalid document metadata.")
        return self


MESSAGES = {
    "invalid_input": "Check the outline, file format, wait interval or identifier.",
    "idempotency_conflict": "This request ID was used with different parameters.",
    "busy": "Active document limit reached.",
    "daily_limit": "Daily submission limit reached.",
    "unknown_job": "The local store has no such job.",
    "configuration": "Check trusted agent or worker configuration.",
    "dispatch_rejected": "GitHub rejected the workflow dispatch.",
    "rate_limited": "GitHub rate-limited the request.",
    "dispatch_unconfirmed": "No matching dispatch was confirmed in the recovery window.",
    "multiple_runs": "More than one workflow run matched this job ID.",
    "remote_unavailable": "The remote service is temporarily unavailable.",
    "run_missing": "The mapped run is unavailable and no verified output was recorded.",
    "github_access": "GitHub run metadata is unavailable with this configuration.",
    "cancelled": "GitHub cancelled the job.",
    "timed_out": "The workflow reached its time limit.",
    "worker_failed": "The workflow ended before publishing a verified document.",
    "invalid_worker_input": "The worker rejected its request or checksum.",
    "staging_rejected": "B2 rejected the private request upload. Check the request-writing key.",
    "request_missing": "The private outline is missing from B2.",
    "render_failed": "The document renderer could not complete.",
    "invalid_output": "The worker could not validate the generated file.",
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
