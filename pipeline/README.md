# EAR pipeline agent tools

One agent call runs the whole chain, privately:

1. `analyze_binary` writes a request to the private B2 bucket and starts `ear-extract.yml` with only its ID.
2. The extraction job fetches the binary (an https URL, or an object under `ear-extract/binaries/` in the bucket),
   runs `r2-ghidra-ci/extract/ear_extract.py` inside the toolbox image with no network, and stores
   `ear-extract/results/<run>/<attempt>/extraction.json` and the evidence `ear-modeling/evidence/r2/<run>-<attempt>.json`.
3. `get_analysis` sees the extraction finish and starts the modeling tool once with that evidence key
   (`modeling/jobs.py submit`, so `ear-model-design.yml` on the free CPU runner unless a GPU runner is set).
4. `get_analysis` then returns the design: components and relationships that each quote the evidence exactly.

| Tool | Input | Return |
|---|---|---|
| `analyze_binary` | `binary_url` or `binary_key`, optional `goal`, `functions` (e.g. `main,parse_config`), `design`, `wait_seconds` | run ID, then the same as `get_analysis` once it has waited |
| `get_analysis` | `run_id`, `wait_seconds` | `state` (`extracting`, `designing`, `done`, `partial`, `error`), extraction summary, `evidence_key`, design |
| `get_function_code` | `run_id`, `name` | one decompiled function (6000 characters max) |

Extraction takes a minute or two; the CPU design several more. The binary is never executed.

## Install for an agent

```bash
pip install -r modeling/requirements-agent.txt "./model-review[mcp]" ./pipeline
```

Register the stdio server (`ear-pipeline-mcp`) with these environment variables:

| Variable | Value |
|---|---|
| `EAR_MODELING_DIR` | absolute path of this repository's `modeling/` folder |
| `EAR_GITHUB_TOKEN` | fine-grained token for this repository: Actions read/write, Contents read |
| `EAR_B2_BUCKET`, `EAR_B2_REGION`, `EAR_B2_KEY_ID`, `EAR_B2_APPLICATION_KEY` | the private bucket (region as `us-east-005` or the `s3.us-east-005.backblazeb2.com` endpoint) |

The GitHub side needs the same four `EAR_B2_*` values as Actions secrets.

## Test

```bash
cd pipeline && python -m pytest        # simulated B2 and GitHub, no credentials
```
