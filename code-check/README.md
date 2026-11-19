# EAR code check

Review a change before it is committed, or build and test a project, on a disposable GitHub CPU runner. A
coding model on the same runner reviews the diff, or explains why a build or test failed and how to fix it.
The project, logs, report and built files stay in private Backblaze B2; the agent gets a job ID, the outcome,
the review or failure analysis and B2 object keys, never the files themselves.

| Mode | What runs | What comes back |
| --- | --- | --- |
| `review` | Qwen2.5-Coder-7B-Instruct (Q4_K_M, llama.cpp CPU server) reads the file list and the diff | `approved` or `changes_requested`, a summary and up to 12 findings (severity, file, line, problem, fix) |
| `test` | `setup`, `build`, `test` shell commands in the toolbox container, in that order, stopping at the first failure | `passed` or `failed`; each step's exit code, time and log; on failure, the model's analysis (what failed, why, fix steps, files to look at); on success, up to 8 built files (`artifacts` globs; a folder becomes a `.zip`) |

The toolbox (`Dockerfile`) matches the agent's workstation: Python 3.12, Node 26.13.1, uv 0.13.9, git and a
C/C++ toolchain. Pushes to `main` that change this folder build it, test it offline with the example project
and publish it to `ghcr.io/kaycee7000/ear/code-check:tree-<git tree>`; jobs pull that image and build the
same Dockerfile if it is missing. Pull requests also run the coding model on the example project and print
its analysis and review, with timings, in the job summary.

## Isolation

Each phase runs in its own container with only what it needs (see the table at the top of `runner.py`). The
project's own commands run with no credentials and no GitHub token, as the runner's unprivileged user, with a
read-only root filesystem, 6 GiB of memory, 3 CPUs and a 15-minute limit per step. Only `setup` (installing
dependencies) has network access, unless the plan sets `"network": "all"`. The B2 read key is only in the
container that fetches the request; the write key is only in the one that publishes the results. The model
reads the diff or the end of the failing log as data; its answer is only text in the report.

Workflow inputs are the job UUID and the request checksum. The project, commands, logs and findings never
appear in workflow inputs, run names or logs.

## Use

```sh
python3 -m venv .venv
.venv/bin/pip install ./document-generation './code-check[mcp]'
# Same settings as the other EAR tools: EAR_GITHUB_TOKEN (Actions read/write), EAR_B2_BUCKET,
# EAR_B2_REGION, EAR_B2_KEY_ID, EAR_B2_APPLICATION_KEY.
.venv/bin/ear-code check path/to/project code-check/examples/review-plan.json
.venv/bin/ear-code check path/to/project code-check/examples/test-plan.json --wait-seconds 20
.venv/bin/ear-code status JOB_UUID --wait-seconds 20
.venv/bin/ear-code fetch JOB_UUID report.md
.venv/bin/ear-code fetch JOB_UUID dist.zip --artifact dist.zip
```

`check` packs the folder with `ear_code/pack.py`: git's tracked and untracked (not ignored) files, or everything
except dependency and build folders, plus `.ear/diff.patch`, the change to review (the working tree against
`--base`, default `HEAD`; the last commit when nothing is uncommitted). The archive is reproducible, so the same
files give the same handle. `pack.py` uses only the standard library and can be copied to the machine that
holds the project.

Agent functions (also registered by `ear-code-mcp`, schemas in `ear_code/tool_schema.json`):

- `prepare_source_upload(sha256, byte_size)`: a ten-minute B2 PUT for the archive; returns its `handle`.
- `run_code_check(source, project, plan, wait_seconds=0, request_id=None)`: starts the job.
- `get_code_result(job_id, wait_seconds=0)`: status, then the manifest with fresh download links.

Plans:

```json
{"mode": "review", "focus": "Apply a discount code at checkout."}
{"mode": "test", "steps": {"setup": "npm ci", "build": "npm run build", "test": "npm test",
  "artifacts": ["dist"], "network": "setup"}, "analyze": true}
```

## Storage

`code/requests/<job>.json` and `code/requests/sources/<sha256>.tar.gz` hold the request and the project;
`code/results/<job>/<run>/<attempt>/` holds `report.md`, `logs/<step>.log`, `artifacts/<name>` and, written
last, `result.json`. Failed workers write `error.json` with a fixed error code. Dedicated keys can replace the
shared ones: `EAR_CODE_INPUT_WRITE_KEY_ID/KEY` and `EAR_CODE_RESULT_READ_KEY_ID/KEY` locally,
`EAR_CODE_INPUT_READ_KEY_ID/KEY` and `EAR_CODE_RESULT_WRITE_KEY_ID/KEY` as Actions secrets. The model can be
changed with the Actions variables `EAR_CODE_MODEL_URL` (a huggingface.co GGUF), `EAR_CODE_MODEL_SHA256` and
`EAR_CODE_MODEL_NAME`.

GitHub-hosted runners are for building and testing software; check that your use fits GitHub's Actions terms
and your plan's minutes.

## Tests

```sh
cd code-check && ../.venv/bin/python -m unittest discover -s tests -v      # offline, no Docker
python3 selftest.py IMAGE [--model]                                         # real containers (and model)
```
