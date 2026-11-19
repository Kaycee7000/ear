import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from resources import GIB, peak, snapshot

from ear_image.config import number, setting, storage_settings
from ear_image.contract import (
    MODEL,
    REVISION,
    Issue,
    Manifest,
    ModelSpec,
    WorkerInput,
    canonical_id,
    utc,
)
from ear_image.storage import B2, WORKER_ERRORS, base_key

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / ".model-cache"


def positive(text):
    if not text.isascii() or not text.isdecimal() or len(text) > 20 or int(text) < 1:
        raise ValueError("Expected positive ID.")
    return int(text)


def validated_png(path):
    from PIL import Image

    try:
        if not 0 < path.stat().st_size <= 8 * 1024**2:
            raise ValueError
        with Image.open(path) as img:
            if img.format != "PNG" or img.size != (512, 512) or img.n_frames != 1:
                raise ValueError
            img.verify()
        with Image.open(path) as img:
            img.load()
        return path.read_bytes()
    except (OSError, ValueError, SyntaxError):
        raise Issue("invalid_png") from None


def publish(b2, base, folder, identity, task, spec, started, timings, resources):
    data = validated_png(folder / "output.png")
    digest = hashlib.sha256(data).hexdigest()
    key = base + "/output.png"
    try:
        start = time.monotonic()
        b2.put(key, data, "image/png", {"sha256": digest})
        timings["png_upload"] = time.monotonic() - start
        manifest = Manifest(
            **identity,
            **spec.model_dump(),
            version=1,
            object_key=key,
            byte_size=len(data),
            sha256=digest,
            width=512,
            height=512,
            seed=task.seed,
            mime_type="image/png",
            started_at=started,
            uploaded_at=utc(),
            timings=timings,
            resources=resources,
        )
        b2.put(base + "/result.json", manifest.model_dump_json().encode(), "application/json")
    except Issue:
        raise Issue("upload_failed") from None
    return manifest


def execute(folder, raw, b2, prefix, identity, started):
    base = base_key(prefix, identity["job_id"], identity["workflow_run_id"], identity["run_attempt"])
    code = "invalid_worker_input"
    resources, timings = {}, {}
    try:
        seed = raw["seed"]
        if not isinstance(seed, str) or not seed.isascii() or not seed.isdecimal() or len(seed) > 10:
            raise ValueError
        task = WorkerInput.model_validate({**raw, "seed": int(seed)})
        code = "configuration"
        spec = ModelSpec(
            model_id=setting("MODEL_ID", MODEL), model_revision=setting("MODEL_REVISION", REVISION)
        )
        number("JOB_MINUTES", 30, 1, 30)
        code = "insufficient_resources"
        resources = snapshot(folder)
        CACHE.mkdir(exist_ok=True)
        restored = sum(p.stat().st_size for p in CACHE.rglob("*") if p.is_file())
        if (
            restored > 6 * GIB
            or resources["ram_available_bytes"] < 9 * GIB
            or resources["disk_free_bytes"] < max(0, 6 * GIB - restored) + 2 * GIB
        ):
            raise Issue(code)
        print(json.dumps({"resources": resources}), flush=True)
        (folder / "input.json").write_text(task.model_dump_json())
        (folder / "model.json").write_text(spec.model_dump_json())
        env = {
            k: v
            for k, v in os.environ.items()
            if not (
                k.startswith("EAR_IMAGE_B2_")
                or k in ("GITHUB_TOKEN", "EAR_IMAGE_GITHUB_TOKEN", "HF_TOKEN")
            )
        }
        env.update(
            PIP_NO_CACHE_DIR="1",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            HF_HUB_DISABLE_XET="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            CUDA_VISIBLE_DEVICES="",
            TOKENIZERS_PARALLELISM="false",
        )
        code = "dependencies_failed"
        start = time.monotonic()
        # The prebuilt worker image (Dockerfile) already holds this lock; pip would still fetch the URL-pinned
        # torch wheel again, so it is only run outside that image.
        if os.environ.get("EAR_IMAGE_PREBUILT") != "1":
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.returncode:
                raise Issue("insufficient_resources" if proc.returncode in (-9, 137) else code)
        timings["dependencies"] = time.monotonic() - start + float(os.environ["BOOTSTRAP_SECONDS"])
        code = "model_failed"
        (folder / "stage").write_text(code)
        if os.environ.get("HF_TOKEN"):
            env["HF_TOKEN"] = os.environ["HF_TOKEN"]
        proc = subprocess.run(
            [
                sys.executable,
                str(HERE / "infer.py"),
                str(folder),
                str(CACHE),
                str(resources["cpu_threads"]),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode:
            reason = (folder / "stage").read_text()
            if proc.returncode in (-9, 137):
                reason = "insufficient_resources"
            raise Issue(reason if reason in WORKER_ERRORS else "worker_failed")
        timings.update(json.loads((folder / "timings.json").read_text()))
        resources.update(peak())
        code = "upload_failed"
        manifest = publish(b2, base, folder, identity, task, spec, started, timings, resources)
        print(
            json.dumps({"timings_seconds": manifest.timings.model_dump(), "resources": resources}),
            flush=True,
        )
        return 0
    except Exception as exc:
        if isinstance(exc, Issue):
            code = exc.code
        code = code if code in WORKER_ERRORS else "worker_failed"
        error = {
            **identity,
            "error_code": code,
            "timestamp": utc(),
            "resources": {**resources, **peak()},
        }
        try:
            b2.put(base + "/error.json", json.dumps(error).encode(), "application/json")
        except Exception:
            pass
        print(json.dumps(error), flush=True)
        return 1


def main():
    try:
        started = utc()
        raw = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["inputs"]
        identity = {
            "job_id": canonical_id(raw["job_id"]),
            "workflow_run_id": positive(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": positive(os.environ["GITHUB_RUN_ATTEMPT"]),
        }
        region, bucket, prefix = storage_settings()
        b2 = B2(region, bucket, setting("B2_WRITE_KEY_ID"), setting("B2_WRITE_KEY"))
        with tempfile.TemporaryDirectory() as name:
            return execute(Path(name), raw, b2, prefix, identity, started)
    except Exception:
        print("Invalid worker identity or storage configuration.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
