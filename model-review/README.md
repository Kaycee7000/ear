# model-review

Use GitHub Actions as an API that tests and validates an AI model. Your agent makes one
call (`review_model`). Behind that call, a GitHub Actions run serves the model, runs a
test suite against it and publishes a verdict: **PASS**, **FAIL** or **ERROR**.

```
agent ──review_model()──► GitHub REST API: workflow dispatch (returns run_id)
                               │
                               ▼
                 Actions runner (GitHub CPU / GPU, or your own PC)
                 1. start backend: Ollama pull+serve | existing endpoint | mock
                 2. run suite: suites/<name>.yaml → checks per reply
                 3. upload artifact: results.json + report.md (+ job summary)
                               │
agent ◄──verdict + failed cases── client polls the run, downloads results.json
```

Each review is one workflow run, so you get a log, a summary page and a 14-day artifact
for every review. Because it's a job queue and not a live server, a review takes minutes
(runner start-up + model download). Use it to check checkpoints, not for inference.

## Layout

Everything is in this folder, except one file that GitHub requires to be at the repo root:

| Path | What it is |
|---|---|
| `../.github/workflows/model-review.yml` | Entry point. Only `workflow_dispatch`, so it never runs on push or PR |
| `action.yml` | The steps: install harness, start backend, run suite, upload results, enforce verdict |
| `scripts/start_backend.sh` | Starts or reuses Ollama, checks an existing endpoint, or starts the mock |
| `model_review/runner.py` | Runs a suite against any OpenAI-compatible `/chat/completions` endpoint |
| `model_review/checks.py`, `suite.py` | Check types and suite loading/validation |
| `model_review/client.py` | Agent-side API: dispatch → poll → fetch `results.json`; also a CLI |
| `model_review/tools.py`, `tool_schema.json` | The same calls as function-calling tools |
| `model_review/mcp_server.py` | The same tools over MCP (stdio) |
| `model_review/mock_server.py` | Fake model for testing the pipeline itself |
| `suites/` | Test suites (`smoke.yaml`, `json-actions.example.yaml`) |
| `tests/` | Offline tests: mock model + fake GitHub API |

## Setup

1. **Merge to `main`.** GitHub only lets you dispatch a workflow whose file is on the
   default branch.
2. **Create a token** for the agent: a fine-grained personal access token limited to this
   repository, with **Actions: Read and write** and **Contents: Read**.
3. **Install the client** on the machine where your agent runs:
   ```bash
   pip install "./model-review[mcp]"      # from a clone of this repo
   export MODEL_REVIEW_TOKEN=github_pat_...
   ```
4. **Test the pipeline** with the mock model (no download, about a minute):
   ```bash
   model-review review --model mock --backend mock     # expect "state": "passed"
   ```
5. **Review a real model**:
   ```bash
   model-review review --model qwen2.5:0.5b --suite smoke
   ```

Other commands: `model-review review ... --no-wait` (returns the run id right away),
`model-review status <run_id> --wait 600`, `model-review suites`.
Exit codes: 0 passed or still pending, 1 failed, 2 error.

## Connecting your agent

**MCP** (agents that support MCP servers):
```json
{
  "mcpServers": {
    "model-review": {
      "command": "model-review-mcp",
      "env": {"MODEL_REVIEW_TOKEN": "github_pat_...", "MODEL_REVIEW_REPO": "Kaycee7000/ear"}
    }
  }
}
```

**Function calling** (local agents on Ollama, llama.cpp, vLLM, LM Studio...):
```python
import json
from model_review.tools import TOOL_SCHEMAS, call_tool

resp = llm.chat.completions.create(model=..., messages=messages, tools=TOOL_SCHEMAS)
for tc in resp.choices[0].message.tool_calls or []:
    result = call_tool(tc.function.name, tc.function.arguments)
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
```

Tools: `review_model(model, suite, backend, endpoint, runner, wait_seconds)`,
`get_review(run_id, wait_seconds)`, `list_suites()`. `review_model` waits 120 s by
default. If the run hasn't finished by then, it returns `state: queued|running` with a
`run_id`, and the agent calls `get_review` later. This keeps each tool call short, which
matters because many agent frameworks time out long tool calls.

A finished review returns a compact result (the full detail stays in the artifact):
```json
{
  "state": "failed", "verdict": "FAIL", "run_id": 1234, "run_url": "https://github.com/...",
  "message": "FAIL: 4/6 cases passed (threshold 0.8)",
  "summary": {"total": 6, "passed": 4, "pass_rate": 0.6667, "latency_ms": {"p50": 812, "p95": 2140}, "tokens_per_sec": 18.4},
  "failed_cases": [{"id": "json_object", "failed_checks": ["json_schema: <schema>: not valid JSON: ..."], "output": "Sure! Here's..."}]
}
```
`state` is `passed` / `failed` when the model was tested and `error` when the run broke
before it could test. In the `error` case, `message` says why: an unknown suite (with the
list of available ones), a failed `ollama pull`, an unreachable endpoint, or, if the run
died without writing anything, the names of the failed steps.

## Where it runs

Choose a runner with `runner` (a label, or a JSON list such as `["self-hosted","gpu"]`):

| Runner | Cost | Notes |
|---|---|---|
| `ubuntu-latest` (default) | Free, because this repo is public | CPU only, so keep to small quantized models. Good for format and behaviour checks |
| GitHub GPU larger runner (T4) | Billed per minute; included minutes don't apply | Only for **organizations** on GitHub Team or Enterprise Cloud. This repo belongs to a personal account, so it would have to move to an org first. Create the runner in a runner group and pass its name |
| Self-hosted (your PC) | Free in Actions | Uses your real GPU, so latency numbers mean something. If Ollama is already running there, it is reused; LM Studio, llama.cpp or vLLM servers work with `backend=endpoint`, `endpoint=http://127.0.0.1:<port>/v1` |

> **Self-hosted runner on a public repo:** GitHub recommends self-hosted runners only on
> private repositories, because a pull request from a fork can try to run code on them.
> If you attach your PC to this repo, give it a unique label (e.g. `kaycee-gpu`). In
> Settings → Actions, require approval for workflows from outside contributors, and run
> the runner as a low-privilege user. Or keep the self-hosted setup in a private repo.

## Backends

| `backend` | `model` means | What the runner does |
|---|---|---|
| `ollama` | Ollama tag (`qwen2.5:0.5b`) or `hf.co/<user>/<repo>:<quant>` (GGUF on Hugging Face) | Reuses a running Ollama, or installs it (Linux), then `ollama pull` and serve |
| `endpoint` | Model name the server expects | Checks `<endpoint>/models` answers, then tests against it. Secret `MODEL_REVIEW_API_KEY` is sent as a bearer token if set |
| `mock` | Anything | Starts the built-in fake model, which tests the pipeline only |

Each run downloads the model again on GitHub-hosted runners, and you pay for those minutes
on billed runners. Self-hosted runners keep the model locally.

## Writing suites

A suite is a YAML file in `suites/`; its file name is the `suite` input.

```yaml
name: my-model
description: What this suite proves.
pass_threshold: 0.9            # fraction of cases that must pass (default 1.0)
gates:                         # optional
  max_p95_latency_ms: 3000
  min_tokens_per_sec: 10
warmup: true                   # untimed first request so model-load time isn't counted
defaults:
  system: "You are ..."
  params: {temperature: 0, max_tokens: 200, seed: 7}   # passed through to /chat/completions
  strip_think: true            # drop <think>...</think> before checking
  timeout_s: 300
x-shared: &schema {type: object}   # x-* keys are free for YAML anchors
cases:
  - id: unique-id
    prompt: "..."              # or messages: [{role: user, content: ...}, ...]
    params: {max_tokens: 20}   # merged over defaults
    checks:
      - contains: "42"
```

| Check | Passes when |
|---|---|
| `contains: str \| [str]` | every string appears |
| `contains_any: [str]` | at least one appears |
| `not_contains: str \| [str]` | none appear |
| `regex: pattern` | the pattern matches somewhere (Python `re.search`) |
| `equals: str` / `one_of: [str]` | the trimmed reply is exactly that / one of those |
| `json_valid: true` | the reply is JSON (a single ``` fence is allowed unless `strict: true`) |
| `json_schema: {...}` | JSON that validates against the schema (`strict: true` = no fence allowed) |
| `min_length: n` / `max_length: n` | trimmed reply length in characters |
| `max_latency_ms: n` | that request took at most n ms |
| `ear_design: true` | the reply is a design for the EAR modeling tool whose IDs, relationships and exact-quote citations are valid for the task JSON in the last user message (suite `ear-design`, generated by `modeling/make_review_suite.py`) |

Add `ignore_case: true` to text checks. A case passes when all its checks pass. The
verdict is **PASS** when every gate passes, **ERROR** when no case got an answer at all,
and **FAIL** otherwise. `suites/json-actions.example.yaml` shows format, judgment and
latency tests for a model that outputs structured actions. Copy it for your own model.

Check a suite locally against any running server before pushing it:
```bash
python -m model_review.runner --suite-file suites/my-model.yaml \
  --model qwen2.5:0.5b --base-url http://127.0.0.1:11434/v1 --out /tmp/review
```

## Development

```bash
cd model-review
pip install -e ".[mcp,test]"
pytest            # offline: mock model server + fake GitHub API, no token needed
```
