"""Data contracts shared by the package and the image. No analysis imports."""

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

MAX_FILE = 128 * 1024**2
MAX_TOTAL = 256 * 1024**2
MAX_ROWS = 250_000
MAX_COLS = 256
MAX_PLAN = 32 * 1024
InFormat = Literal["csv", "tsv", "ndjson", "parquet", "xlsx"]
OutFormat = Literal["csv", "ndjson", "parquet", "xlsx"]
Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Handle = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}\.(csv|tsv|ndjson|parquet|xlsx)$")]
Alias = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
Column = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Wait = Annotated[int, Field(ge=0, le=86400)]
Key = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")]
Size = Annotated[int, Field(gt=0, le=MAX_FILE)]
Count = Annotated[int, Field(ge=1)]
MIME = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "ndjson": "application/x-ndjson",
    "parquet": "application/vnd.apache.parquet",
    "png": "image/png",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


def uuid4_string(value):
    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("Expected a canonical UUIDv4.")
    return value


JobID = Annotated[str, AfterValidator(uuid4_string)]


class Chart(Record):
    kind: Literal["bar", "line", "scatter"]
    x: Column
    y: Column
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None


class Plan(Record):
    sql: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12000)] | None
    ) = None
    profile: Alias | None = None
    export: OutFormat = "parquet"
    chart: Chart | None = None

    @model_validator(mode="after")
    def one_operation(self):
        if (self.sql is None) == (self.profile is None):
            raise ValueError("Choose one of sql or profile.")
        return self


class Request(Record):
    datasets: Annotated[dict[Alias, Handle], Field(min_length=1, max_length=4)]
    plan: Plan

    @property
    def format(self):
        return self.plan.export

    def payload_json(self):

        return json.dumps(
            self.model_dump(include={"datasets", "plan"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @model_validator(mode="after")
    def fits(self):
        if self.plan.profile and self.plan.profile not in self.datasets:
            raise ValueError("Unknown dataset alias.")
        if len(self.payload_json().encode()) > MAX_PLAN:
            raise ValueError("Plan too large.")
        return self


class Submit(Request):
    wait_seconds: Wait = 0
    request_id: Key | None = None


class Lookup(Record):
    job_id: JobID
    wait_seconds: Wait = 0


class Upload(Record):
    sha256: Hash
    format: InFormat
    byte_size: Size


class Dispatch(Record):
    job_id: JobID
    request_sha256: Hash


class Artifact(Record):
    object_key: str
    mime_type: str
    byte_size: Annotated[int, Field(ge=0, le=MAX_FILE)]
    sha256: Hash


class Shape(Record):
    rows: Annotated[int, Field(ge=0, le=MAX_ROWS)]
    columns: Annotated[int, Field(ge=1, le=MAX_COLS)]


class Manifest(Dispatch, Shape):
    version: Literal[1]
    workflow_run_id: Count
    run_attempt: Count
    format: OutFormat
    table: Artifact
    chart: Artifact | None
    completed_at: str

    @model_validator(mode="after")
    def valid_output(self):
        if datetime.fromisoformat(self.completed_at).utcoffset() != UTC.utcoffset(None):
            raise ValueError("Expected UTC completion time.")
        if not self.table.byte_size and (self.format != "ndjson" or self.rows):
            raise ValueError("Unexpected empty table file.")
        if self.chart and not self.chart.byte_size:
            raise ValueError("Empty chart file.")
        return self


def utc():
    return datetime.now(UTC).isoformat()


ERRORS = {
    "dataset_missing": "A staged dataset or private request was not found.",
    "dataset_invalid": "An input failed format, schema or checksum validation.",
    "query_invalid": "The query is unsupported or incompatible with the supplied columns and types.",
    "chart_invalid": "A chart needs existing columns, finite numeric Y values and 1–500 result rows.",
    "limit_exceeded": "The analysis exceeded a configured row, column, byte, time or memory limit.",
    "analysis_failed": "The data worker could not finish the analysis.",
    "invalid_worker_input": "The worker rejected its dispatch or private request.",
    "upload_failed": "B2 rejected the result upload.",
    "storage_access": "The worker could not access private storage.",
}


class DataError(Exception):
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
