# EAR modeling tool

A local AI agent gives the tool a goal and a set of source documents. The
worker uses Qwen2.5-3B-Instruct through llama.cpp to produce components,
relationships, assumptions, and open questions. Each component and relationship
must cite a source ID and an exact excerpt. The worker checks that IDs are
unique, relationship endpoints exist, and each excerpt occurs in its source.
Small models copy imperfectly. Qwen2.5-3B re-spaces code (Ghidra's
`int argc,char **argv` comes back as `int argc, char **argv`), and when it
stitches several lines of decompiled code together it invents plausible lines
in between. Before the check, a quote that matches its source apart from
whitespace, at exactly one place, is replaced by the source's own text.
Otherwise a multi-line quote is cut down to its longest line (12+ characters)
that really occurs in the source. Stored excerpts are always exact. Review
whether the cited text supports the interpretation.

## Run locally

From the repository root, install Python 3.12 dependencies in the module.

```bash
cd modeling
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-agent.txt
```

Start a local llama.cpp server with the quantized model. The first start
downloads about 2.1 GB. This example works on CPU or supported GPU hardware.

```bash
llama-server -hf Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M \
  --alias ear-model --host 127.0.0.1 --port 8080 --ctx-size 16384 --parallel 1
```

In another terminal, from the same module directory, run the sample.

```bash
.venv/bin/python design.py examples/task.json
```

For an existing compatible model server, set `EAR_MODEL_URL` to its base URL
and `EAR_MODEL_NAME` to the served model name, and `EAR_MODEL_KEY` if the
server requires a bearer key (`--api-key`). It must support
`/v1/chat/completions` with JSON Schema output. Keep the local server on
loopback when working with private evidence.

## Connect the agent

Register this stdio MCP server in your agent's MCP configuration. Substitute
absolute paths to the checkout and virtual environment. Windows virtual
environments use `Scripts/python.exe`.

```json
{
  "mcpServers": {
    "ear-modeling": {
      "command": "/absolute/path/ear/modeling/.venv/bin/python",
      "args": ["/absolute/path/ear/modeling/server.py"]
    }
  }
}
```

| Tool | Input | Return |
| --- | --- | --- |
| `model_design_local` | `goal` and inline `sources` as in `examples/task.json` | Validated design in the same call |
| `model_design` | `goal` and `evidence_key`, e.g. `ear-modeling/evidence/capture.json` | GitHub run ID, run URL, and private request ID |
| `model_design_status` | `run_id` | Run state and conclusion, plus the design after success |

One task accepts 1–20 sources with distinct IDs. The normalized JSON for the
goal and sources must fit within 8 KiB UTF-8. Preselect relevant excerpts for
larger datasets. Invalid model output gets one correction attempt. Local
inference needs no GitHub or B2 credentials.

## Configure GitHub execution

1. Put the workflow and its module on the repository's default branch and
   enable Actions. A dispatch creates one workflow run. Pushes and pull
   requests do not trigger modeling work.
2. By default the job runs on GitHub's free `ubuntu-latest` runner (free and
   unlimited for public repositories). `worker.sh` picks `cpu-worker.sh`
   there: the same pinned GGUF on a digest-pinned llama.cpp CPU server, with
   the model kept in `actions/cache`. To use a GPU instead, choose a Linux x64
   runner with Docker, a compatible NVIDIA driver and the NVIDIA Container
   Toolkit and set the Actions repository variable `EAR_MODELING_GPU_RUNNER`
   to its label; `worker.sh` then runs `gpu-worker.sh`. Hosted GPU larger
   runners require an organization on GitHub Team or Enterprise Cloud. For a
   personal repository, register a dedicated, preferably ephemeral,
   self-hosted GPU runner. Configure self-hosted capacity for trusted code only.
3. Use a private Backblaze B2 bucket and bucket-scoped application keys.
   Configure the settings below. Supply the local settings to the MCP server
   through the agent's environment. Keep credentials out of version control.
4. Add the evidence JSON under `ear-modeling/evidence/` in the bucket. The
   file contains a single `sources` array. To upload the bundled sample with
   configured local credentials, run the following from this directory.

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from design import Task
from jobs import Store

sample = Task.model_validate_json(Path("examples/task.json").read_bytes())
Store().write("ear-modeling/evidence/capture.json", sample.model_dump_json(exclude={"goal"}).encode())
PY
```

| Setting | Local agent | GitHub job |
| --- | --- | --- |
| `EAR_B2_BUCKET`, `EAR_B2_REGION` | Environment (region as `us-east-005` or the endpoint `s3.us-east-005.backblazeb2.com`) | Actions secrets with the same names |
| `EAR_B2_KEY_ID`, `EAR_B2_APPLICATION_KEY` | Read source and result objects, write request snapshots | Actions secrets for a worker key that reads requests and writes results |
| `EAR_GITHUB_TOKEN` | Fine-grained token for this repository with Actions write access | Unused |
| `EAR_GITHUB_REPO` | Defaults to `Kaycee7000/ear` | Unused |
| `EAR_GITHUB_REF` | Defaults to `main`. Set to the default branch if renamed | Only the default branch runs the GPU job |

Call `model_design` with the sample goal and the uploaded key. Poll the
returned run ID with `model_design_status`. A terminal failure includes the
GitHub run URL. The dispatch endpoint uses GitHub REST API version
`2026-03-10` to obtain the exact run ID. After an ambiguous network response,
look up the displayed request ID in the Actions run titles before resubmitting.

The worker receives only the request ID through GitHub. It reads the frozen
goal and evidence from B2 and downloads a checksum-verified GGUF and a
digest-pinned CUDA server. It saves the validated result privately at
`ear-modeling/results/<run_id>/<attempt>/design.json`. Polling a rerun reads
the current attempt. There are no public design artifacts. The runner has a
25-minute timeout. Set B2 lifecycle rules for requests and results according
to your retention needs. Allow a fresh runner time to download the image and
weights. Use hosted Actions jobs for software development work associated
with the repository.

## Evidence from binaries and model checks

The `r2-ghidra-ci/extract` job writes evidence for this tool under
`ear-modeling/evidence/r2/`, so `model_design` can model a binary; the
`pipeline/` tools chain the two automatically. To check that a model can do
this job, `make_review_suite.py` generates `model-review/suites/ear-design.yaml`
from this module (same instruction, same schema, same citation rules through
model-review's `ear_design` check). Regenerate it after changing `design.py` or
`examples/`, then review a model on GitHub's free runner:

```bash
.venv/bin/python make_review_suite.py
model-review review --model hf.co/Qwen/Qwen2.5-3B-Instruct-GGUF:Q4_K_M --suite ear-design
```

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite exercises source and graph validation, schema retries, private object
handling, dispatch, polling and reruns with simulated external services. The
local sample uses an actual model. GPU execution additionally requires the
configured runner and B2 credentials.

[llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server)
· [GitHub GPU runner eligibility](https://docs.github.com/en/actions/concepts/runners/larger-runners)
· [GitHub Actions terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features)
· [Backblaze B2 Python SDK setup](https://www.backblaze.com/docs/cloud-storage-use-the-aws-sdk-for-python-with-backblaze-b2)
