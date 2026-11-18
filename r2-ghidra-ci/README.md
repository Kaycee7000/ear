# r2-ghidra-ci

Lightweight multi-arch (`linux/amd64`, `linux/arm64`) Docker image for GitHub Actions (`ghcr.io`), pre-installed with:

- **radare2** (`6.2.4`) — CLI disassembler, binary analyzer, and `r2` scripting
- **r2ghidra** (`6.2.4`) — Headless native Ghidra decompiler plugin (`pdg` / `pdgj` / `pdgx`)
- **binutils** — `objdump`, `readelf`, `nm`, `strings`, `strip`
- **pytest** — Python test runner for automated binary/RE test suites
- **strace** — System call tracer for dynamic analysis

## Monorepo Isolation

Everything for this image lives in `r2-ghidra-ci/`, except the thin workflow entry point at `.github/workflows/r2-ghidra-image.yml`. The workflow uses `sparse-checkout` and `paths` filtering so it only builds when `r2-ghidra-ci/**` or the workflow file itself changes.

## Setup for Agents & CI Workflows

### 1. Use in a GitHub Actions Job

Reference the published GHCR image directly in any workflow job so it starts in seconds:

```yaml
jobs:
  analyze:
    runs-on: ubuntu-latest
    container:
      image: ghcr.io/kaycee7000/ear/r2-ghidra-ci:latest
      options: --cap-add=SYS_PTRACE
    steps:
      - uses: actions/checkout@v4
      - name: Run binary analysis tests
        run: pytest -v
```

> **Note:** `--cap-add=SYS_PTRACE` is only required if your job runs `strace` or `r2 -d` (dynamic debugging). Static analysis and `r2ghidra` decompilation work without extra capabilities.

### 2. Run Locally (Docker)

```bash
# Pull from GHCR
docker pull ghcr.io/kaycee7000/ear/r2-ghidra-ci:latest

# Interactive shell with current directory mounted
docker run --rm -it --cap-add=SYS_PTRACE -v "$PWD:/workspace" ghcr.io/kaycee7000/ear/r2-ghidra-ci:latest
```

### 3. Headless Agent Commands

Agents can invoke `radare2`, `r2ghidra`, `binutils`, `strace`, and `pytest` non-interactively:

```bash
# Decompile a function with Ghidra (`pdg`) in headless mode
r2 -q -c "aaa; s main; pdg" ./target_bin

# Export Ghidra decompilation as structured JSON (`pdgj`) for agent parsing
r2 -q -c "aaa; s main; pdgj" ./target_bin

# Inspect ELF sections and symbols
readelf -Ws ./target_bin
objdump -d -M intel ./target_bin

# Trace syscalls during execution
strace -f -o trace.log ./target_bin
```

### 4. Example `pytest` Integration

```python
import json
import subprocess

def test_decompile_main():
    out = subprocess.check_output(
        ["r2", "-q", "-c", "aaa; s main; pdgj", "./target_bin"],
        text=True,
    )
    data = json.loads(out)
    assert "code" in data
    assert data["code"].strip() != ""
```
