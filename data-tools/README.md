# EAR data tools

Upload a dataset once. Ask your agent to write an analysis plan. The agent returns a resumable job ID. GitHub runs DuckDB in a disposable CPU container and puts the resulting table and optional chart in private Backblaze B2. Once the dispatch is accepted, the local machine can go offline. A later status check issues fresh download links directly to B2.

**Example request** — “Total the sales by region, largest first. Give me an Excel workbook and a bar chart.” The included [sales dataset](examples/sales.csv) and [plan](examples/revenue.json) produce South 27.50 and West 25.00.

| Task | Installed in the image |
| --- | --- |
| Filter, join up to four datasets, clean columns, aggregate, use CTEs and window functions | DuckDB and SQLGlot |
| Read CSV, TSV, Parquet and newline-delimited JSON, move typed columns between readers and DuckDB | PyArrow |
| Read the first worksheet of an XLSX workbook, or write an editable XLSX table | openpyxl with defusedxml |
| Render bar, line and scatter charts | Matplotlib, NumPy and Pillow |
| Read and write private objects in the I/O phases | boto3 / botocore |

The [Dockerfile](Dockerfile) installs the [pinned worker wheels](worker/requirements.txt). On changes to this module on `main`, the [workflow](../.github/workflows/data-tools.yml) builds the image, runs an actual offline container smoke test and pushes it to `ghcr.io/kaycee7000/ear/data-tools` with a tag derived from the module's Git tree. Data jobs reuse that image. If the package is unavailable, the runner builds the same pinned Dockerfile for that job. The checkout pins the base image digest. No services, GPUs or data engines run on the agent.

## Setup

From the repository root, with Python 3.12 or later:

```sh
python3 -m venv data-tools/.venv
. data-tools/.venv/bin/activate
pip install ./document-generation './data-tools[mcp]'
cd data-tools
```

The first local package supplies EAR's existing SQLite store, workflow-dispatch client, polling and B2 transport. Installing it adds no Office or analysis engines. The MCP extra is optional for CLI/Python.

1. **B2**. Use an all-private bucket. With the default prefix, reusable datasets and private requests go under `data/requests/`; output tables, charts and JSON receipts go under `data/results/`. Keep results by default. B2 lifecycle rules can expire inputs or results independently. Reruns require their original inputs. Download URL expiration does not affect retention.
2. **Local agent environment**. Export `EAR_GITHUB_TOKEN` (fine-grained token for this repo with Actions read/write), `EAR_B2_BUCKET`, `EAR_B2_REGION`, `EAR_B2_KEY_ID` and `EAR_B2_APPLICATION_KEY`. The region accepts a name or its B2 S3 endpoint. Existing EAR credentials work if scoped to these prefixes. [.env.example](.env.example) lists local settings. To use it, copy it to private `.env`, edit, then source with export enabled.
3. **GitHub Actions**. Enable Actions, GHCR package publication and the manual workflow on the default branch. The worker supports the repository's existing `EAR_B2_BUCKET`, `EAR_B2_REGION`, `EAR_B2_KEY_ID` and `EAR_B2_APPLICATION_KEY` Actions secrets. For a separate bucket or prefix, set Actions **variables** `EAR_DATA_B2_REGION`, `EAR_DATA_B2_BUCKET`, `EAR_DATA_PREFIX` and matching local values. The default repo/ref are `Kaycee7000/ear` and `main`; local overrides are `EAR_DATA_REPO`, `EAR_DATA_REF`. Use the trusted default branch for dispatch. Only the build job needs package write permission.

Dedicated key pairs override the shared credential. Restrict keys to the bucket and file-name prefix shown. Each row gives the key ID variable followed by its application-key variable.

| Where | Key variables | B2 capability and prefix |
| --- | --- | --- |
| Local environment | `EAR_DATA_INPUT_WRITE_KEY_ID`, `EAR_DATA_INPUT_WRITE_KEY` | `writeFiles` on `data/requests/` |
| Local environment | `EAR_DATA_RESULT_READ_KEY_ID`, `EAR_DATA_RESULT_READ_KEY` | `readFiles` on `data/results/` |
| Actions secrets | `EAR_DATA_INPUT_READ_KEY_ID`, `EAR_DATA_INPUT_READ_KEY` | `readFiles` on `data/requests/` |
| Actions secrets | `EAR_DATA_RESULT_WRITE_KEY_ID`, `EAR_DATA_RESULT_WRITE_KEY` | `writeFiles` on `data/results/` |

## First job

```sh
HANDLE=$(ear-data upload examples/sales.csv | python -c 'import json, sys; print(json.load(sys.stdin)["handle"])')
ear-data submit examples/revenue.json --input "sales=$HANDLE" --request-id sales-regions-01
ear-data status JOB_UUID --wait-seconds 15
ear-data fetch JOB_UUID revenue.xlsx
ear-data fetch JOB_UUID revenue.png --artifact chart
```

Replace `JOB_UUID` with the returned UUID. The default submit returns after dispatch. A pending fetch leaves the destination alone. A successful fetch checks SHA-256 and byte count before replacing the file. For joins, pass another `--input alias=handle`. Input handles identify the bytes and format and can be reused across jobs. The CLI streams the data directly to B2.

## Agent and frontend interfaces

```python
from ear_data import run_data_job, get_data_result


async def submit(sales_handle):
    return await run_data_job(
        {"sales": sales_handle},
        {
            "sql": "SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region ORDER BY revenue DESC",
            "export": "xlsx",
            "chart": {"kind": "bar", "x": "region", "y": "revenue"},
        },
        request_id="sales-regions-01",
    )


async def resume(job_id):
    result = await get_data_result(job_id, wait_seconds=10)
    return result  # completed: table.download_url, optional chart.download_url, rows and columns
```

For a first look at an unfamiliar dataset use `{"profile":"sales","export":"csv"}`. DuckDB returns one statistics row per column, including type, count, range and estimated distinct count. A query plan takes one read-only DuckDB SELECT over registered aliases, including non-recursive CTEs, joins and set operations. The [function allowlist](worker/query.py) includes common date, string, numeric, aggregate and window functions. Use output aliases for duplicate column names. Each job produces one result table. A chart uses two columns of that table in the returned row order. Use a new job for an independent result. Natural-language interpretation belongs to the calling agent.

A frontend with the file, length and SHA-256 can call `prepare_dataset_upload(sha256, format, byte_size)`. PUT the raw file to the returned `upload_url` with its exact `content_type` and byte count, then pass the returned handle to `run_data_job`. The signature expires in ten minutes. A browser sending a `File` supplies Content-Length automatically. Configure the private bucket's CORS rules for the frontend origin and the required PUT/GET and Content-Type. Treat signed links as bearer credentials.

Register the stdio server with the same environment as the CLI, or map the [JSON schemas](ear_data/tool_schema.json) to the three Python functions.

```json
{"mcpServers":{"ear-data":{"command":"/absolute/path/to/ear/data-tools/.venv/bin/ear-data-mcp","args":[]}}}
```

## Formats and limits

- Inputs accept UTF-8 CSV/TSV with headers, Parquet, NDJSON (one JSON object per line), and the first worksheet of XLSX with a string header row. XLSX reads cached formula values. Columns need unique, nonempty names. CSV types are inferred; use Parquet or typed Excel cells for values such as identifiers with leading zeros.
- Export to CSV, Parquet (default), NDJSON, or XLSX. Excel text cells are written literally, including text beginning with an equals sign. CSV preserves raw text, which spreadsheet applications may interpret when opening the file. NDJSON dates/times use ISO strings and fixed-point decimals use strings to preserve precision.
- Up to four input aliases, 128 MiB per file, 256 MiB total input bytes, 512 MiB decoded tables, one million input rows per table and 256 columns. XLSX input additionally caps worksheet cells at one million. The result is capped at 250,000 rows, 192 MiB of in-memory Arrow output and 128 MiB per exported file. Charts support 1–500 rows with finite numeric Y values. Requests exceeding a cap fail without silently truncating the result.
- The analysis container has two CPU cores, 4 GiB RAM, a four-minute compute timeout, a read-only root filesystem and no network. The whole workflow has a 20-minute limit including image setup and transfers. Private inputs are mounted read-only during computation. The networked input and output containers get separate B2 credentials. Workflow inputs are the job UUID and request checksum. No private data, SQL or signed result URLs are written to normal workflow logs or GitHub artifacts.
- Submitted jobs use the shared persistent SQLite coordinator with one active job and ten admissions per UTC day by default. Set `EAR_DATA_DB` to a persistent local path for every agent process (default `~/.local/state/ear-data/jobs.sqlite3`, or under `XDG_STATE_HOME`). Other limits are in `.env.example`. Protect the database because it contains plans and dataset handles. A new request with the same idempotency key and different parameters is rejected. Manual GitHub dispatches and separate local databases are outside this admission limit.

## Job status and recovery

`staging`, `submitting`, `queued`, `running` and `checking` are resumable and include a job ID and suggested retry interval. After a lost GitHub acknowledgement the coordinator searches by exact run name, workflow and trusted ref. It never blindly repeats the POST. Caller wait expiry leaves the remote job running. A local restart during staging can finish the upload when polling resumes. Once GitHub accepts dispatch, the runner owns completion.

`completed` includes run ID, attempt, dimensions of the result table, and a descriptor for `table` and optional `chart`. Each descriptor contains a durable object key, MIME type, byte size, SHA-256, `download_url` and UTC `expires_at`. Default signed lifetime is one hour. The agent checks the result manifest and HEADs every required artifact before issuing URLs. A new lookup refreshes them. Each rerun uses its own `data/results/<job>/<run>/<attempt>/` directory. The result manifest is uploaded last. A job can leave partial blobs if upload fails; they do not constitute a completed job.

`busy` or `limit_reached` means local admission refused the request. `failed` returns a stable code such as `dataset_invalid`, `query_invalid`, `chart_invalid`, `limit_exceeded`, `cancelled`, `output_missing` or `upload_failed`. Hard cancellation may leave only the GitHub conclusion. A previously verified result remains retrievable from B2 after GitHub purges run history. For a run purged before first verification, use its B2 output prefix for manual recovery.

Hosted Actions usage is subject to GitHub's project-related-use terms, quotas and billing. GHCR and B2 storage, operations and transfer may incur charges. [GitHub terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions) · [GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) · [B2 pricing](https://www.backblaze.com/cloud-storage/pricing)

## Verification

From `data-tools/`:

```sh
python -m unittest discover -s tests -v
# Separate environment for the real engine and format tests
python3 -m venv .venv-worker
.venv-worker/bin/pip install ../document-generation '.[mcp]' -r worker/requirements.txt
.venv-worker/bin/python -m unittest discover -s tests -v
# Test the built image offline, using the same Docker restrictions as a real job
docker build -t ear-data-local .
python3 smoke_image.py ear-data-local
# Explicit opt-in: uploads the sample, dispatches one real job and verifies both B2 downloads
EAR_DATA_LIVE_TEST=1 python -m unittest discover -s tests -p test_live.py -v
```

The regular suite uses synthetic datasets with real parsers and mocked GitHub/B2 transports. The opt-in test requires configured GitHub and B2 accounts and keeps its small outputs.
