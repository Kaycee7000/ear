"""Private request and result storage and GitHub workflow dispatch."""
import json
import os
import re
from contextlib import closing
from uuid import UUID, uuid4

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

from design import Design, Evidence, Task, synthesize

WORKFLOW = "ear-model-design.yml"
PREFIX = "ear-modeling/"
MAX_OBJECT_BYTES = 256 * 1024


def required(name: str) -> str:
    if not os.environ.get(name):
        raise ValueError(f"Set {name} before using remote modeling.")
    return os.environ[name]


class Store:
    def __init__(self):
        self.bucket = required("EAR_B2_BUCKET")
        region = required("EAR_B2_REGION")
        # Accept the S3 endpoint as Backblaze shows it (s3.us-east-005.backblazeb2.com) as well as the region.
        endpoint = re.fullmatch(r"(?:https://)?s3\.([a-z0-9-]+)\.backblazeb2\.com/?", region)
        region = endpoint.group(1) if endpoint else region
        if not re.fullmatch(r"[a-z0-9-]+", region):
            raise ValueError("Invalid B2 region.")
        self.client = boto3.client(
            "s3", endpoint_url=f"https://s3.{region}.backblazeb2.com", region_name=region,
            aws_access_key_id=required("EAR_B2_KEY_ID"),
            aws_secret_access_key=required("EAR_B2_APPLICATION_KEY"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=8, read_timeout=30,
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )

    def read(self, key: str, max_bytes: int = MAX_OBJECT_BYTES) -> bytes:
        result = self.client.get_object(Bucket=self.bucket, Key=key)
        with closing(result["Body"]) as body:
            if result["ContentLength"] > max_bytes:
                raise ValueError("Stored JSON exceeds the size limit.")
            data = body.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Stored JSON exceeds the size limit.")
        return data

    def write(self, key: str, obj: bytes, max_bytes: int = MAX_OBJECT_BYTES,
              content_type: str = "application/json") -> None:
        if len(obj) > max_bytes:
            raise ValueError("Result exceeds storage limit.")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=obj, ContentType=content_type)


def repo_api() -> str:
    repo = os.getenv("EAR_GITHUB_REPO", "Kaycee7000/ear")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("EAR_GITHUB_REPO must be owner/repo.")
    return f"https://api.github.com/repos/{repo}/actions"


def github(method: str, path: str, **kwargs) -> dict:
    response = httpx.request(method, repo_api() + path, timeout=20, headers={
        "Authorization": "Bearer " + required("EAR_GITHUB_TOKEN"),
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
    }, **kwargs)
    response.raise_for_status()
    return response.json()


def submit(goal: str, evidence_key: str) -> dict:
    evidence_path = r"ear-modeling/evidence/[A-Za-z0-9_-][A-Za-z0-9_./-]{0,240}\.json"
    if not re.fullmatch(evidence_path, evidence_key) or ".." in evidence_key.split("/"):
        raise ValueError("Use a JSON key under ear-modeling/evidence/.")
    required("EAR_GITHUB_TOKEN")
    store = Store()
    evidence = Evidence.model_validate_json(store.read(evidence_key))
    task = Task(goal=goal, sources=evidence.sources)
    request_id = uuid4().hex
    # Freeze the evidence now so reruns reference the same source text.
    store.write(f"{PREFIX}requests/{request_id}.json", task.model_dump_json().encode())
    try:
        dispatched = github("POST", f"/workflows/{WORKFLOW}/dispatches", json={
            "ref": os.getenv("EAR_GITHUB_REF", "main"), "inputs": {"request_id": request_id},
        })
        return {"request_id": request_id, "run_id": dispatched["workflow_run_id"],
                "run_url": dispatched["html_url"]}
    except (httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
            raise
        raise RuntimeError(f"Dispatch outcome unknown. Check Actions for ear-modeling-{request_id} "
                           "before submitting again.") from exc


def result_key(run_id: int, attempt: int) -> str:
    if run_id < 1 or attempt < 1:
        raise ValueError("Expected a positive run ID and attempt.")
    return f"{PREFIX}results/{run_id}/{attempt}/design.json"


def status(run_id: int) -> dict:
    if run_id < 1:
        raise ValueError("Expected a positive run ID.")
    run = github("GET", f"/runs/{run_id}")
    if run["path"] != f".github/workflows/{WORKFLOW}":
        raise ValueError("Run belongs to another workflow.")
    state = {"run_id": run_id, "attempt": run["run_attempt"], "status": run["status"],
             "conclusion": run["conclusion"], "run_url": run["html_url"]}
    if run["status"] == "completed" and run["conclusion"] == "success":
        try:
            result = Store().read(result_key(run_id, run["run_attempt"]))
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"NoSuchKey", "404", "NotFound"}:
                raise
            state["result_error"] = "Design is missing or expired. Check the workflow run and B2 retention."
        else:
            state["design"] = Design.model_validate_json(result).model_dump()
    return state


def work(request_id: str, run_id: int, attempt: int) -> None:
    if UUID(request_id).hex != request_id:
        raise ValueError("Invalid request ID.")
    key = result_key(run_id, attempt)
    store = Store()
    task = Task.model_validate_json(store.read(f"{PREFIX}requests/{request_id}.json"))
    result = synthesize(task)
    store.write(key, result.model_dump_json().encode())


if __name__ == "__main__":
    import sys

    try:
        work(required("EAR_REQUEST_ID"), int(required("GITHUB_RUN_ID")), int(required("GITHUB_RUN_ATTEMPT")))
    except Exception as exc:
        # A public workflow log must never include evidence, model output or credentials.
        print(f"Modeling worker failed ({type(exc).__name__}). Check input, storage and model setup.",
              file=sys.stderr)
        sys.exit(1)
