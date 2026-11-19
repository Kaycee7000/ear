"""Contracts shared by the agent package and the worker image. Standard library and pydantic only."""

import json
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
    model_validator,
)

MAX_SOURCE = 64 * 1024**2  # compressed project archive
MAX_UNPACKED = 512 * 1024**2  # extracted project
MAX_MEMBERS = 20_000
MAX_REQUEST = 32 * 1024
MAX_ARTIFACT = 95 * 1024**2  # still fits one iMessage attachment
MAX_ARTIFACTS = 200 * 1024**2
MAX_LOG = 2 * 1024**2
MAX_MANIFEST = 60 * 1024  # the coordinator reads at most 64 KiB of JSON
STEP_NAMES = ("setup", "build", "test")

Mode = Literal["review", "test"]
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Source = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}\.tar\.gz$")]
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")]
Wait = Annotated[int, Field(ge=0, le=86400)]
Size = Annotated[int, Field(gt=0, le=MAX_SOURCE)]
Count = Annotated[int, Field(ge=1)]
Project = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")]
MIME = {
    "gz": "application/gzip",
    "json": "application/json",
    "md": "text/markdown; charset=utf-8",
    "log": "text/plain; charset=utf-8",
    "bin": "application/octet-stream",
}


def plain(limit):
    def check(value):
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            raise ValueError("Control characters are not allowed.")
        return value

    return Annotated[str, StringConstraints(min_length=1, max_length=limit), AfterValidator(check)]


def relative_glob(value):
    parts = value.split("/")
    if value.startswith("/") or ".." in parts or "" in parts:
        raise ValueError("Use a path pattern relative to the project folder.")
    return value


Command = plain(2000)
Glob = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_.*?\[\]/@+-]{1,200}$"), AfterValidator(relative_glob)
]


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


def uuid4_string(value):
    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("Expected a canonical UUIDv4.")
    return value


JobID = Annotated[str, AfterValidator(uuid4_string)]


class Steps(Record):
    """Shell commands run in order inside the toolbox container, from the project folder."""

    setup: Command | None = None  # install dependencies; always has network access
    build: Command | None = None
    test: Command | None = None
    network: Literal["setup", "all"] = "setup"  # "all" also gives build and test network access
    artifacts: list[Glob] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def runs_something(self):
        if not (self.build or self.test):
            raise ValueError("Give a build or test command.")
        return self


class Plan(Record):
    mode: Mode
    steps: Steps | None = None
    focus: plain(800) | None = None  # what the change is for, or what to check
    analyze: bool = True  # explain a failed step with the coding model

    @model_validator(mode="after")
    def matches_mode(self):
        if self.mode == "test" and self.steps is None:
            raise ValueError("A test needs steps.")
        if self.mode == "review" and self.steps is not None:
            raise ValueError("A review takes no steps.")
        return self


class Request(Record):
    source: Source
    project: Project
    plan: Plan

    @property
    def format(self):
        return self.plan.mode

    def payload_json(self):
        return json.dumps(
            self.model_dump(include={"source", "project", "plan"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @model_validator(mode="after")
    def fits(self):
        if len(self.payload_json().encode()) > MAX_REQUEST:
            raise ValueError("Request too large.")
        return self


class Submit(Request):
    wait_seconds: Wait = 0
    request_id: Key | None = None


class Lookup(Record):
    job_id: JobID
    wait_seconds: Wait = 0


class Upload(Record):
    sha256: Hash
    byte_size: Size


class Dispatch(Record):
    job_id: JobID
    request_sha256: Hash


class Artifact(Record):
    object_key: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    mime_type: str
    byte_size: Annotated[int, Field(ge=0, le=MAX_ARTIFACT)]
    sha256: Hash


class StepResult(Record):
    name: Literal["setup", "build", "test"]
    command: str
    exit_code: int | None  # None: stopped at the time limit
    seconds: float
    tail: Annotated[str, StringConstraints(max_length=2000)]
    log: Artifact | None = None


class Finding(Record):
    severity: Literal["blocker", "major", "minor", "nit"]
    file: Annotated[str, StringConstraints(max_length=200)]
    line: int | None = None
    problem: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    fix: Annotated[str, StringConstraints(max_length=500)]


class Review(Record):
    verdict: Literal["approve", "changes_requested"]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=800)]
    findings: list[Finding] = Field(default_factory=list, max_length=12)


class Analysis(Record):
    summary: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    what_failed: Annotated[str, StringConstraints(min_length=1, max_length=800)]
    why: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    fix: list[Annotated[str, StringConstraints(min_length=1, max_length=400)]] = Field(
        default_factory=list, max_length=6
    )
    files: list[Annotated[str, StringConstraints(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=6
    )


class ModelRun(Record):
    model: str
    seconds: float
    prompt_tokens: int
    output_tokens: int
    note: str | None = None  # set when the model could not answer


Outcome = Literal["passed", "failed", "approved", "changes_requested", "not_reviewed"]


class Summary(Record):
    """What the offline phases hand to the publisher (no storage keys yet)."""

    mode: Mode
    outcome: Outcome
    steps: list[StepResult] = Field(default_factory=list, max_length=3)
    review: Review | None = None
    analysis: Analysis | None = None
    model: ModelRun | None = None
    artifacts: list[str] = Field(default_factory=list, max_length=8)  # names under /output/artifacts
    source_files: int = 0


class Manifest(Dispatch):
    version: Literal[1]
    workflow_run_id: Count
    run_attempt: Count
    mode: Mode
    outcome: Outcome
    project: Project
    steps: list[StepResult] = Field(default_factory=list, max_length=3)
    review: Review | None = None
    analysis: Analysis | None = None
    model: ModelRun | None = None
    artifacts: list[Artifact] = Field(default_factory=list, max_length=8)
    report: Artifact
    source_files: int
    completed_at: str

    @model_validator(mode="after")
    def consistent(self):
        if datetime.fromisoformat(self.completed_at).utcoffset() != UTC.utcoffset(None):
            raise ValueError("Expected UTC completion time.")
        if self.mode == "review" and (self.steps or self.artifacts):
            raise ValueError("A review has no steps or artifacts.")
        if self.mode == "test" and self.outcome not in ("passed", "failed"):
            raise ValueError("Unexpected test outcome.")
        if self.mode == "review" and self.outcome not in ("approved", "changes_requested", "not_reviewed"):
            raise ValueError("Unexpected review outcome.")
        return self


def utc():
    return datetime.now(UTC).isoformat()


ERRORS = {
    "source_missing": "The project archive or private request was not found.",
    "source_invalid": "The project archive failed its checksum, size or path checks.",
    "invalid_worker_input": "The worker rejected its dispatch or private request.",
    "steps_failed": "The toolbox container could not run the steps.",
    "limit_exceeded": "The job exceeded a size or time limit.",
    "worker_failed": "The code-check worker could not finish.",
    "upload_failed": "B2 rejected the result upload.",
    "storage_access": "The worker could not access private storage.",
}


class CodeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


def storage_location(region, bucket, prefix):
    region = (
        region.removeprefix("https://")
        .removeprefix("s3.")
        .removesuffix("/")
        .removesuffix(".backblazeb2.com")
    )
    if (
        not re.fullmatch("[a-z0-9-]+", region)
        or not re.fullmatch("[A-Za-z0-9-]{6,63}", bucket)
        or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix)
        or len(prefix) > 150
    ):
        raise ValueError("Invalid storage configuration.")
    return region, bucket, prefix
