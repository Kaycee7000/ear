# CPU image generation for EAR

The local agent submits one 512×512 image request and persists its UUID and chosen seed in SQLite before contacting GitHub. A disposable Python container on a standard hosted Linux CPU runner generates the image, uploads the PNG to a private Backblaze B2 bucket, then writes its result manifest. The agent can shut down after dispatch. On a later call it verifies the run, attempt, manifest and PNG metadata, and signs a fresh B2 GET URL. The frontend downloads directly from B2.

Everything for this module lives in `image-generation/` except the dispatch entry point at `.github/workflows/generate-image.yml`. The other EAR tools use the same stdio MCP registration pattern. The CLI and Python API work independently of MCP.

## 1. Configure B2

Create an **allPrivate** bucket and note the region in its S3 endpoint, for example `us-west-004` in `https://s3.us-west-004.backblazeb2.com`. Create two application keys restricted to that bucket and the file-name prefix `images/`. Configure the module's prefix as `images` without the trailing slash.

| Key | Capabilities | Location |
| --- | --- | --- |
| Worker | `writeFiles` for single-part PutObject | Actions secrets |
| Agent | `readFiles` for GetObject, HeadObject and signing GETs | Local agent environment |

The B2 application-key API supports selecting individual capabilities, if a console preset grants more. The clients address the configured bucket directly, without listing buckets or keys or applying object ACLs. Use a prefix dedicated to this installation. Keep current PNGs and their JSON metadata indefinitely by default. To expire content, opt into a B2 lifecycle rule for this prefix and account for previous object versions. A URL's expiry controls access through that URL and has no effect on storage retention. For browser JavaScript fetching bytes across origins, configure bucket CORS for the frontend origin and GET. A normal direct download link can be opened without exposing credentials.

[Backblaze key permissions](https://www.backblaze.com/docs/cloud-storage-map-iam-policies-and-s3-acls-to-backblaze-b2-access-controls)

## 2. Configure the repository

Merge the workflow onto the default branch, enable Actions, and allow the pinned official checkout and cache actions if your repository has an allowlist. Use a trusted branch and restrict who can modify its workflow. GitHub's `Run workflow` UI becomes available when the default branch contains this workflow. The agent normally supplies all inputs. A repository-scoped fine-grained GitHub token on the **agent** needs **Actions: write** to dispatch and inspect runs. The job's own token has only **Contents: read**.

| Actions secret | Value |
| --- | --- |
| `EAR_IMAGE_B2_WRITE_KEY_ID`, `EAR_IMAGE_B2_WRITE_KEY` | Dedicated B2 upload key ID and application key |
| `EAR_IMAGE_HF_TOKEN` | Optional read token for a model requiring authenticated access. The pinned SD-Turbo revision was publicly accessible at implementation time. |

| Actions repository variable | Value or default |
| --- | --- |
| `EAR_IMAGE_B2_REGION`, `EAR_IMAGE_B2_BUCKET` | Required region and private bucket |
| `EAR_IMAGE_PREFIX` | `images` |
| `EAR_IMAGE_TRUSTED_REF` | Default branch name. Use a branch name such as `main`. |
| `EAR_IMAGE_JOB_MINUTES` | Integer 1–30, default 30 for the whole job |
| `EAR_IMAGE_MODEL_ID`, `EAR_IMAGE_MODEL_REVISION` | `stabilityai/sd-turbo` and `b261bac6fd2cf515557d5d0707481eafa0485ec2`. Overrides must identify a compatible StableDiffusionPipeline repository and a full, immutable commit. |
| `EAR_IMAGE_CACHE` | `false`. Set to `true` to restore/save the selected model component files and their download metadata. The maximum archive input is 6 GiB per model/revision. Old revisions are subject to GitHub's cache quota and eviction. |

The runner reads prompt data from GitHub's event JSON. The run title contains the UUID. The worker suppresses third-party inference output, including token-truncation messages, and writes only resource measurements, timings and sanitized errors to the job log. GitHub receives dispatch inputs, including the prompt. Treat workflow metadata in a public repo as potentially visible. Keep sensitive prompts out of this workflow.

## 3. Install the local coordinator

From the repository root, on a machine with Python 3.12 or later:

```sh
cd image-generation
python3 -m venv .venv
.venv/bin/pip install '.[mcp]'
cp .env.example .env
# Fill the private values in .env on this machine, then export them.
set -a
. ./.env
set +a
```

Install `.` alone for CLI/Python. Neither install includes PyTorch, Diffusers, Transformers, model weights or Pillow. The worker has its own Linux x86-64 CPython 3.12 dependency lock. Its PyTorch wheel is pinned by URL and SHA-256.

| Local setting | Meaning |
| --- | --- |
| `EAR_IMAGE_GITHUB_TOKEN` | Repository-scoped agent token |
| `EAR_IMAGE_B2_READ_KEY_ID`, `EAR_IMAGE_B2_READ_KEY` | B2 read/signing key ID and application key |
| `EAR_IMAGE_B2_BUCKET`, `EAR_IMAGE_B2_REGION`, `EAR_IMAGE_PREFIX` | Must match the worker |
| `EAR_IMAGE_REPO`, `EAR_IMAGE_REF`, `EAR_IMAGE_WORKFLOW` | Defaults to `Kaycee7000/ear`, `main`, `generate-image.yml` |
| `EAR_IMAGE_DB` | Defaults to `$XDG_STATE_HOME/ear-image/jobs.sqlite3` or `~/.local/state/ear-image/jobs.sqlite3`. Give all agent/CLI processes for this installation the same persistent local path. |
| `EAR_IMAGE_ACTIVE_LIMIT`, `EAR_IMAGE_DAILY_LIMIT` | Defaults 1 active and 10 admissions per UTC day, configurable within 1–5 and 1–50 |
| `EAR_IMAGE_MAX_WAIT`, `EAR_IMAGE_TOOL_DEADLINE` | Default 20-second polling budget and 30-second runtime deadline. Set the deadline to the actual client tool timeout or lower. One second is reserved for returning the result. Client cancellation interrupts local polling. |
| `EAR_IMAGE_URL_TTL` | Signed URL lifetime, default 3600 seconds, range 60–86400 |

The SQLite file contains prompt text, seeds, idempotency keys, request fingerprints, timestamps, GitHub mappings and previously verified manifests. It is created with owner-only permissions. Keep this file private and back it up to resume jobs from another installation. Scope cleanup or backup retention separately for local prompts and B2 images.

## 4. Submit, resume and fetch

```sh
.venv/bin/ear-image submit 'A red canoe on a misty lake' --seed 19 --request-id canoe-001
.venv/bin/ear-image status JOB_UUID --wait-seconds 15
.venv/bin/ear-image fetch JOB_UUID canoe.png --wait-seconds 15
```

Substitute the UUID returned by submit. It is safe to close the agent after GitHub accepts the job. A pending fetch prints status and leaves the destination alone. A successful fetch checks the byte count and SHA-256 before replacing the destination. The status command emits the signed URL on completion, so handle its output as a temporary bearer credential.

For a Python agent:

```python
from ear_image import generate_image, get_image_result

async def submit():
    return await generate_image("A red canoe on a misty lake", seed=19, request_id="canoe-001")

async def resume(job_id):
    return await get_image_result(job_id, wait_seconds=10)
```

Register the installed stdio executable in a compatible MCP host. Pass the local settings to its process through your host's secret/environment configuration. A GUI host may need explicit environment settings even if your terminal has them.

```json
{
  "mcpServers": {
    "ear-image": {
      "command": "/absolute/path/to/ear/image-generation/.venv/bin/ear-image-mcp",
      "args": []
    }
  }
}
```

Function-calling agents can register `ear_image/tool_schema.json` and map the two function names to the Python functions above. Both interfaces return JSON. A frontend can display `image_url`; an agent's ability to see the image depends on its host's image-input support.

## Job protocol

| Status | Meaning |
| --- | --- |
| `submitting` | The request was persisted. GitHub's acknowledgement is unknown or pending reconciliation. |
| `queued`, `running` | Known workflow run, awaiting completion |
| `checking` | Remote state/outputs could not yet be reverified. Honor `retry_after_seconds`. |
| `completed` | Matching result manifest and PNG metadata have been verified. The response contains an agent-signed URL. |
| `busy`, `limit_reached` | Local admission was refused before creating the job. Retry after the indicated interval. |
| `failed` | See the stable `error_code` and sanitized `message`. Includes input/idempotency errors, cancelled/timed-out runs, worker failures and missing or corrupt outputs. An explicit GitHub rerun can still be resolved with the original job ID. |

Pending responses include `job_id`, `retry_after_seconds` and the run ID when known. Completed responses include `object_key`, `image_url`, UTC `expires_at`, MIME type, byte size, SHA-256, dimensions, seed, model ID/revision, run ID, attempt and measured timings/resources. A failure before admission has no job ID. The client never stores signed URLs.

When supplied, an idempotency key covers the exact prompt, the caller's seed argument (including omitted/null), and deployment target. An omitted seed is randomly chosen and persisted once. SQLite serializes admission across processes. A new submission checks stale active records when capacity is full. Manual GitHub dispatches, manual reruns and separate databases bypass the local budget. There is no in-memory queue and the workflow sets no concurrency group. GitHub's default concurrency-group behavior can replace a pending run; admission here does not depend on that queue.

The client uses the `2026-03-10` dispatch contract and parses `workflow_run_id`, `run_url` and `html_url`. After a lost response (including an unexpected 204) it searches workflow-scoped pages by exact run title, event, branch and workflow path. It never retries an ambiguous POST. An incomplete search or rate limit defers checking. If no match is confirmed after ten minutes and a complete search, `dispatch_unconfirmed` releases the slot. Later checks can recover a late-appearing run. Inspect the run title before starting a genuinely new request. This includes the narrow crash window after committing SQLite and before sending the POST.

Each attempt writes `<prefix>/<job_id>/<run_id>/<attempt>/output.png` first and `result.json` last. A failed step writes `error.json` when possible. A hard kill, cancellation or bootstrap failure can leave only GitHub's conclusion, or an uncommitted PNG. A subsequent attempt uses a different path. The coordinator checks the latest GitHub attempt on lookup. If GitHub has removed the run, a previously verified manifest in SQLite can still provide a fresh URL after checking its PNG in B2. Losing both the run metadata and the local mapping before the first verification can require manual recovery from B2.

[GitHub dispatch API](https://docs.github.com/en/rest/actions/workflows?apiVersion=2026-03-10#create-a-workflow-dispatch-event) · [Concurrency behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

## Worker resources, costs and terms

The job uses the official digest-pinned Debian-based Python image with its default root user, writable container filesystem, and POSIX shell. It measures Linux/cgroup RAM, CPU quota and affinity, free disk and peak process RSS. The result records dependency install/download, model download/load, inference and PNG upload seconds. The manifest upload follows the timed PNG upload. The model download selects only the needed configs, tokenizer and float32 safetensors, about 5.16 GB for the default revision. Generation uses CPU inference mode, batch one, one step and zero guidance. Model setup never runs on the agent.

A conservative preflight requires 9 GiB of available RAM plus enough free disk for the uncached weights and 2 GiB of headroom. Public and private standard runners can have different limits. An 8 GB runner fails the preflight with `insufficient_resources`. Successful performance and peak usage on your specific runner require a live measurement. There is no fallback to paid runners or GPUs.

Use hosted Actions for development/testing work associated with this repository. GitHub's terms address disproportionate use and serverless application workloads. Single-user use is not a blanket exemption. Standard public jobs and private plan allowances are billed differently. Check your Actions and B2 storage, request and egress allowances. SD-Turbo's pinned revision is subject to the Stability AI Community License, including its commercial-use terms.

[GitHub terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions) · [Runner limits](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) · [Billing](https://docs.github.com/en/actions/concepts/billing-and-usage) · [Pinned model license](https://huggingface.co/stabilityai/sd-turbo/blob/b261bac6fd2cf515557d5d0707481eafa0485ec2/LICENSE.md) · [B2 pricing](https://www.backblaze.com/cloud-storage/pricing)

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Offline tests mock GitHub/B2 and generate synthetic PNG fixtures without running inference. The real PNG decoder test additionally needs Pillow from the worker requirements in a separate test environment. The opt-in test below submits **one real, billable-by-plan job**, polls it and downloads and verifies its B2 PNG. It requires the merged default-branch workflow and configured credentials; it keeps the B2 output.

```sh
EAR_IMAGE_LIVE_TEST=1 .venv/bin/python -m unittest discover -s tests -p test_live.py -v
```
