# ear

Tools an AI agent can run on GitHub Actions. Each works on its own; together they form one pipeline:

```
binary ──► r2-ghidra-ci/extract ──► evidence ──► modeling ──► cited design
            (radare2 + Ghidra,        (B2)        (Qwen2.5-3B,
             no network)                           free CPU runner)
                                                       ▲
                    model-review (suite ear-design) ───┘ checks that a model can do the modeling step
```

| Folder | What it does | Workflow |
|---|---|---|
| `r2-ghidra-ci/` | Toolbox image (radare2, r2ghidra, binutils, strace, pytest) and `extract/`, which turns a binary into evidence: file facts, imports, strings and decompiled functions | `r2-ghidra-image.yml` (image), `ear-extract.yml` |
| `modeling/` | Turns evidence into a design whose every component and relationship quotes the evidence exactly | `ear-model-design.yml` |
| `model-review/` | Tests a model with a suite and returns PASS/FAIL; the `ear-design` suite tests the modeling step | `model-review.yml` |
| `pipeline/` | Agent tools that chain extraction and modeling: `analyze_binary`, `get_analysis`, `get_function_code` | uses the two above |

Everything private (binaries, evidence, designs) goes through the private Backblaze B2 bucket; workflows
receive only request IDs, so nothing private appears in this public repository's logs or artifacts.

Setup for the pipeline: Actions secrets `EAR_B2_BUCKET`, `EAR_B2_REGION`, `EAR_B2_KEY_ID`,
`EAR_B2_APPLICATION_KEY`, and a fine-grained token for the agent with Actions: Read and write and Contents: Read.
See each folder's README for details.
