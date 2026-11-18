"""GitHub-hosted orchestration, stdlib only. Each phase gets separate mounts/credentials."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Fixed before crossing from an untrusted dataset processor into the credentialed publisher.
CODES = {
    "dataset_missing",
    "dataset_invalid",
    "query_invalid",
    "chart_invalid",
    "limit_exceeded",
    "analysis_failed",
    "invalid_worker_input",
    "upload_failed",
    "storage_access",
}


def command(image, mode, inputs, outputs, env_names=()):
    args = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--pids-limit",
        "128",
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--memory-swap",
        "4g",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=512m,mode=1777",
    ]
    if mode == "compute":
        args.extend(["--network", "none"])
    if mode in ("stage", "compute", "publish"):
        args.extend(
            [
                "--mount",
                f"type=bind,source={inputs},target=/input" + ("" if mode == "stage" else ",readonly"),
            ]
        )
    if mode in ("compute", "publish"):
        args.extend(
            [
                "--mount",
                f"type=bind,source={outputs},target=/output"
                + ("" if mode == "compute" else ",readonly"),
            ]
        )
    for key in env_names:
        args.extend(["--env", key])
    if mode == "compute":
        return [*args, image, "python", "-m", "worker.compute"]
    return [*args, image, "python", "-m", "worker.transfer", mode]


def execute(image, inputs, outputs, job):
    error = None
    for mode in ("stage", "compute", "publish", "error"):
        if (error is None and mode == "error") or (error is not None and mode != "error"):
            continue
        env = {}
        if mode != "compute":
            role = "READ" if mode == "stage" else "WRITE"
            env = {
                **job,
                "B2_BUCKET": os.environ["EAR_DATA_B2_BUCKET"],
                "B2_REGION": os.environ["EAR_DATA_B2_REGION"],
                "B2_PREFIX": os.getenv("EAR_DATA_PREFIX", "data"),
                "EAR_IO_KEY_ID": os.environ[f"EAR_DATA_{role}_KEY_ID"],
                "EAR_IO_KEY": os.environ[f"EAR_DATA_{role}_KEY"],
            }
            if mode == "error":
                env["ERROR_CODE"] = error
        # Kill and remove the named container on a client timeout, including an offline query.
        name = f"ear-data-{job['RUN_ID']}-{job['ATTEMPT']}-{mode}"
        args = command(image, mode, inputs, outputs, env)
        args[2:2] = ["--name", name]
        try:
            proc = subprocess.run(
                args,
                env={**os.environ, **env},
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=240 if mode == "compute" else 150,
            )
            if proc.returncode:
                code = proc.stdout.decode(errors="replace").strip() if mode != "compute" else ""
                if mode == "compute":
                    try:
                        # Never follow a symlink or block on a special file created by the compute phase.
                        import stat

                        with os.fdopen(
                            os.open(
                                outputs / "failure.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                            ),
                            "rb",
                        ) as info:
                            if stat.S_ISREG(os.fstat(info.fileno()).st_mode):
                                code = json.loads(info.read(256))["error_code"]
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                if not error:
                    error = (
                        code
                        if isinstance(code, str) and code in CODES
                        else ("limit_exceeded" if proc.returncode == 137 else "analysis_failed")
                    )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            error = error or "limit_exceeded"
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


def main():
    inputs = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["inputs"]
    job = {
        "JOB_ID": inputs["job_id"],
        "REQUEST_SHA": inputs["request_sha256"],
        "RUN_ID": os.environ["GITHUB_RUN_ID"],
        "ATTEMPT": os.environ["GITHUB_RUN_ATTEMPT"],
    }
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "source", Path(tmp) / "result"
        src.mkdir()
        out.mkdir()
        return execute(sys.argv[1], src, out, job)


if __name__ == "__main__":
    sys.exit(main())
