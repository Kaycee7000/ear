# Documents for EAR

Give the tool a finished outline and choose `pdf`, `docx` or `pptx`. Your agent writes the words. A disposable GitHub Actions CPU container lays them out and uploads the finished document to private Backblaze B2. The agent can go offline once GitHub accepts the dispatch. Later, the same SQLite job ID returns a fresh direct download URL.

| Format | Result |
| --- | --- |
| `docx` | Editable Word document with a title, optional subtitle and author, heading styles, paragraphs, bullet lists and page numbers |
| `pdf` | The Word layout rendered to a searchable PDF by headless LibreOffice Writer |
| `pptx` | Editable 16:9 PowerPoint deck with a title slide and content slides. Long sections flow onto additional slides with the section heading repeated. |

The outline, document title, file and signed URLs stay out of workflow inputs, run names and normal job logs. GitHub receives a UUID and a SHA-256 fingerprint. Public repositories can expose run metadata. The trusted runner reads the private outline during generation. The package lives in `document-generation/`, with a single Actions entry point in [generate-document.yml](../.github/workflows/generate-document.yml).

## Use the existing EAR configuration

1. Use a private B2 bucket. The agent and workflow must agree on the bucket, region and prefix (default `documents`). If you already use this repo's shared B2 credentials, confirm they can read and write under `documents/`. Keys restricted to a sibling tool's prefix cannot access this one.
2. In **Settings → Secrets and variables → Actions**, the workflow recognizes the existing secrets `EAR_B2_BUCKET`, `EAR_B2_REGION`, `EAR_B2_KEY_ID` and `EAR_B2_APPLICATION_KEY`. Dedicated keys are described below. Enable Actions and keep the workflow file on the default branch. Protect the trusted branch and restrict who can edit workflows with access to your secrets. The checkout token has `contents: read`.
3. On the agent, export the same four shared B2 settings (or the scoped alternatives), plus `EAR_GITHUB_TOKEN`. Use a fine-grained GitHub token restricted to this repository with **Actions read and write**. The region accepts a name such as `us-west-004` or its B2 S3 endpoint. Keep secrets in your environment or a private, ignored `.env`.

From a checkout with Python 3.12 or newer:

```sh
cd document-generation
python3 -m venv .venv
.venv/bin/pip install '.[mcp]'
# If using a private .env, copy .env.example, fill it locally, then export it:
# set -a; . ./.env; set +a

.venv/bin/ear-document submit examples/outline.json --format pdf --request-id handoff-pdf-01
.venv/bin/ear-document status JOB_UUID --wait-seconds 15
.venv/bin/ear-document fetch JOB_UUID handoff.pdf
```

Replace `JOB_UUID` with the submitted job ID. Use a distinct request ID for each desired format. For example, submit the same outline with `--format pptx --request-id handoff-slides-01`. Use `-` for JSON on stdin. A pending fetch leaves the destination alone. A completed fetch streams from B2 and checks the byte count and SHA-256 before replacing the destination. Installing `.` alone provides the CLI and Python functions. Both agent installations exclude the Office and PDF renderers.

## Agent tools

```python
from ear_document import generate_document, get_document_result


async def submit_brief():
    return await generate_document(
        outline={
            "title": "Operations brief",
            "sections": [
                {
                    "heading": "Next shift",
                    "paragraphs": ["Two incidents need follow-up."],
                    "bullets": ["Check the overnight outage log.", "Confirm the on-call owner."],
                }
            ],
        },
        format="pptx",
        request_id="ops-brief-001",
    )


async def resume(job_id):
    return await get_document_result(job_id, wait_seconds=10)
```

The outline has a required title and 1–40 sections. Subtitle and author are optional. Each section has a heading and at least one paragraph or bullet. Paragraphs precede bullets within a section. Text is literal and whitespace within each item is normalized. Headings and titles allow 96 characters, each body item allows 1,200, and the normalized request is limited to 48 KiB. Supply complete text. The worker uses fixed templates and installs no language model.

A compatible MCP host can register both tools with this stdio configuration. Give the child process the local settings through the host's environment configuration.

```json
{
  "mcpServers": {
    "ear-document": {
      "command": "/absolute/path/to/ear/document-generation/.venv/bin/ear-document-mcp",
      "args": []
    }
  }
}
```

Function-calling hosts can also register the [JSON schemas](ear_document/tool_schema.json) and map the function names to the Python API.

| Status | Agent behavior |
| --- | --- |
| `staging` | SQLite has the job. A later lookup can finish staging the private request and start the dispatch. |
| `submitting` | Dispatch was claimed. Check the job again to reconcile an uncertain response. |
| `queued`, `running`, `checking` | Keep the job ID and respect `retry_after_seconds`. `workflow_run_id` appears when known. |
| `completed` | Return or open `download_url`. The result includes UTC `expires_at`, format, MIME type, object key, byte size, SHA-256, section count, run ID and attempt. `page_count` is PDF pages or slides. Word page count is null because it depends on the viewing software. |
| `busy`, `limit_reached` | Admission was refused. Retry after the specified interval. |
| `failed` | Read the stable `error_code` and sanitized message. Examples include `idempotency_conflict`, `staging_rejected`, `cancelled`, `timed_out`, `upload_failed`, `output_missing` and `output_invalid`. |

Every result lookup signs a new URL after verifying completion. A URL is a temporary bearer credential. Share it only with the intended recipient. Direct navigation downloads the B2 object. For browser JavaScript fetching across origins, configure bucket CORS for the frontend's origin.

## Narrowly scoped credentials and settings

Use separate B2 application keys scoped to the private bucket and the exact file-name prefixes below. Replace `documents` if you change the common prefix. No role needs bucket listing, file deletion or object ACLs.

| Where | Key variables | B2 capability and prefix |
| --- | --- | --- |
| Agent environment | `EAR_DOCUMENT_B2_REQUEST_WRITE_KEY_ID`, `EAR_DOCUMENT_B2_REQUEST_WRITE_KEY` | `writeFiles` on `documents/requests/` |
| Agent environment | `EAR_DOCUMENT_B2_OUTPUT_READ_KEY_ID`, `EAR_DOCUMENT_B2_OUTPUT_READ_KEY` | `readFiles` on `documents/results/` for manifest, HEAD and signed downloads |
| Actions secrets | `EAR_DOCUMENT_B2_REQUEST_READ_KEY_ID`, `EAR_DOCUMENT_B2_REQUEST_READ_KEY` | `readFiles` on `documents/requests/` |
| Actions secrets | `EAR_DOCUMENT_B2_OUTPUT_WRITE_KEY_ID`, `EAR_DOCUMENT_B2_OUTPUT_WRITE_KEY` | `writeFiles` on `documents/results/` |

Set each dedicated ID/secret pair together. Shared credentials remain available as a compatibility option. Configure non-secret worker overrides as **Actions repository variables**: `EAR_DOCUMENT_B2_BUCKET`, `EAR_DOCUMENT_B2_REGION`, `EAR_DOCUMENT_PREFIX` and optional `EAR_DOCUMENT_TRUSTED_REF` (default branch by default). Set their matching values locally. The 15-minute workflow uses only manual dispatch on a standard Ubuntu CPU runner in a digest-pinned official Python container.

Local-only overrides are listed in [.env.example](.env.example). The important defaults are:

| Setting | Default |
| --- | --- |
| `EAR_DOCUMENT_GITHUB_TOKEN` | Falls back to local `EAR_GITHUB_TOKEN` |
| `EAR_DOCUMENT_REPO`, `EAR_DOCUMENT_REF`, `EAR_DOCUMENT_WORKFLOW` | `Kaycee7000/ear`, `main`, `generate-document.yml` |
| `EAR_DOCUMENT_DB` | `$XDG_STATE_HOME/ear-document/jobs.sqlite3` or `~/.local/state/ear-document/jobs.sqlite3` |
| `EAR_DOCUMENT_ACTIVE_LIMIT`, `EAR_DOCUMENT_DAILY_LIMIT` | One active job, ten admissions per UTC day, enforced transactionally per SQLite database |
| `EAR_DOCUMENT_MAX_WAIT`, `EAR_DOCUMENT_TOOL_DEADLINE` | 20 and 30 seconds. Set the deadline to your host's actual tool timeout or lower. Wait expiry leaves remote work running. |
| `EAR_DOCUMENT_URL_TTL` | 3,600 seconds, configurable from 60 to 86,400 |

## Storage and recovery

The coordinator saves the normalized outline, request ID, target, fingerprint and run mapping in an owner-only SQLite file. Use a stable path shared by the CLI and agent and back it up privately. Local admission does not cover manual dispatches or other databases.

1. The agent stages the normalized request at `documents/requests/<job_id>.json`. Concurrent retries can write identical bytes. A SQLite claim permits a single dispatching caller. Repeating the same idempotency key returns that job. A changed outline, format or deployment target returns a conflict.
2. The GitHub dispatch uses API version `2026-03-10`. After a lost acknowledgement, the coordinator searches the configured workflow by exact run name, branch and event. It never blindly repeats the POST. A complete search with no match after ten minutes releases the slot with `dispatch_unconfirmed`. Later lookups can identify a late run. This also covers a process crash between taking the dispatch claim and sending the POST.
3. The worker validates the request digest and schema, renders and reopens the file, then uploads `documents/results/<job_id>/<run_id>/<attempt>/output.<format>` and finally `result.json`. A worker error attempts to write `error.json`. GitHub's conclusion also covers cancellation or bootstrap failure before the worker starts. The job never publishes a GitHub artifact.
4. The coordinator checks the latest GitHub attempt and validates the matching manifest, expected path, content type, byte count and checksum metadata before signing. Reruns receive separate object keys. Once a result has been verified locally, it can be re-signed from B2 even after GitHub removes the run history. If GitHub purges the run before the first verification, recover the file directly from its B2 results prefix.

Documents and source outlines stay in B2 by default. You can configure separate lifecycle rules for `requests/` and `results/`; keep requests as long as you need reruns, and account for older B2 object versions. Expiring a signed URL does not delete the document.

GitHub-hosted Actions usage must relate to producing, testing, deploying or publishing the repository's software project. GitHub also restricts disproportionate workloads. Review applicability to your intended use and check plan limits for Actions minutes and B2 storage, requests and egress. [GitHub Actions terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions) · [GitHub dispatch](https://docs.github.com/en/rest/actions/workflows?apiVersion=2026-03-10#create-a-workflow-dispatch-event) · [Backblaze access controls](https://www.backblaze.com/docs/cloud-storage-map-iam-policies-and-s3-acls-to-backblaze-b2-access-controls) · [B2 pricing](https://www.backblaze.com/cloud-storage/pricing)

## Tests

```sh
# Agent-only environment, including stdio MCP registration
.venv/bin/python -m unittest discover -s tests -v

# Separate test environment for the real renderers (Debian / Ubuntu).
# Install libreoffice-writer and fonts-dejavu-core with the OS package manager first.
python3 -m venv .venv-worker
.venv-worker/bin/pip install '.[mcp]' -r worker/requirements.txt
.venv-worker/bin/python -m unittest discover -s tests -v
```

The offline suite mocks GitHub and B2. It exercises dispatch, restart, staging, concurrency, idempotency, cancellation, bounded polling, reruns and URL refresh. The worker suite renders and reopens all three formats and checks the downloaded bytes and outline content.

The following separate test explicitly submits **one real PDF job**, waits for the remote workflow and downloads and verifies its private B2 output. Configure the local credentials, workflow and bucket first. It retains the result and may incur provider charges.

```sh
EAR_DOCUMENT_LIVE_TEST=1 .venv/bin/python -m unittest discover -s tests -p test_live.py -v
```
