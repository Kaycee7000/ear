"""Run a suite against an OpenAI-compatible chat endpoint and write a verdict.

Used inside the GitHub Action, and works the same on your own machine:

    python -m model_review.runner --suite smoke --suites-dir suites \\
        --model qwen2.5:0.5b --base-url http://127.0.0.1:11434/v1 --out ./out

Writes <out>/results.json (machine-readable) and <out>/report.md (for people).
In Actions it also writes the report to the job summary and sets the
`verdict` and `pass_rate` step outputs.

Exit codes: 0 = suite ran (whatever the verdict), 2 = could not run (bad suite or arguments).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .checks import run_check
from .suite import Suite, SuiteError, load_suite, resolve_suite

RESULTS_SCHEMA = "model-review/v1"
MAX_STORED_OUTPUT = 4000
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class ModelCallError(RuntimeError):
    pass


def chat(base_url: str, model: str, messages: list[dict], params: dict,
         timeout_s: float, api_key: str | None = None) -> tuple[str, dict]:
    """One non-streaming chat completion. Returns (reply text, usage dict)."""
    body = {"model": model, "messages": messages, "stream": False, **params}
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ModelCallError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ModelCallError(f"request failed: {getattr(exc, 'reason', exc)}") from exc
    except json.JSONDecodeError as exc:
        raise ModelCallError(f"endpoint returned non-JSON: {exc}") from exc
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelCallError(f"unexpected response shape: {str(payload)[:300]}") from exc
    text = message.get("content") or ""
    return text, payload.get("usage") or {}


def _call_with_retry(base_url, model, messages, params, timeout_s, api_key):
    try:
        return chat(base_url, model, messages, params, timeout_s, api_key)
    except ModelCallError as exc:
        # Retry once for connection-level failures; HTTP errors are real answers.
        if str(exc).startswith("HTTP "):
            raise
        time.sleep(2)
        return chat(base_url, model, messages, params, timeout_s, api_key)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _gpu_name() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _environment() -> dict:
    env = {
        "runner_os": os.environ.get("RUNNER_OS"),
        "runner_name": os.environ.get("RUNNER_NAME"),
        "gpu": _gpu_name(),
        "harness_version": __version__,
    }
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        env["github_run_id"] = run_id
        env["github_run_url"] = f"{server}/{repo}/actions/runs/{run_id}"
    return env


def run_suite(suite: Suite, base_url: str, model: str, api_key: str | None = None,
              backend: str = "", request_id: str = "", log=print) -> dict:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()

    if suite.warmup:
        log("warmup request (not scored)...")
        try:
            _call_with_retry(base_url, model, [{"role": "user", "content": "Hi"}],
                             {"max_tokens": 8}, suite.timeout_s, api_key)
        except ModelCallError as exc:
            log(f"  warmup failed: {exc}")

    cases_out = []
    for case in suite.cases:
        record = {"id": case.id, "passed": False, "error": None, "latency_ms": None,
                  "completion_tokens": None, "output": "", "checks": []}
        start = time.monotonic()
        try:
            text, usage = _call_with_retry(base_url, model, case.messages, case.params,
                                           suite.timeout_s, api_key)
        except ModelCallError as exc:
            record["error"] = str(exc)
            log(f"  ERROR {case.id}: {exc}")
            cases_out.append(record)
            continue
        latency_ms = (time.monotonic() - start) * 1000
        if suite.strip_think:
            text = _THINK.sub("", text).strip()
        results = [run_check(c, text, latency_ms, case.messages) for c in case.checks]
        record.update(
            passed=all(r.passed for r in results),
            latency_ms=round(latency_ms, 1),
            completion_tokens=usage.get("completion_tokens"),
            output=text[:MAX_STORED_OUTPUT],
            checks=[r.to_dict() for r in results],
        )
        log(f"  {'pass' if record['passed'] else 'FAIL'} {case.id} ({latency_ms:.0f} ms)")
        cases_out.append(record)

    total = len(cases_out)
    passed = sum(1 for c in cases_out if c["passed"])
    errors = sum(1 for c in cases_out if c["error"])
    latencies = [c["latency_ms"] for c in cases_out if c["latency_ms"] is not None]
    timed = [c for c in cases_out if c["completion_tokens"] and c["latency_ms"]]
    tokens = sum(c["completion_tokens"] for c in timed)
    seconds = sum(c["latency_ms"] for c in timed) / 1000
    tps = round(tokens / seconds, 2) if seconds > 0 else None
    pass_rate = passed / total if total else 0.0
    p95 = percentile(latencies, 95)

    gates = [{
        "name": "pass_rate",
        "passed": pass_rate >= suite.pass_threshold,
        "detail": f"{pass_rate:.2f} vs threshold {suite.pass_threshold:.2f} ({passed}/{total} cases)",
    }]
    if "max_p95_latency_ms" in suite.gates:
        limit = suite.gates["max_p95_latency_ms"]
        gates.append({"name": "max_p95_latency_ms", "passed": p95 is not None and p95 <= limit,
                      "detail": f"p95 {p95 if p95 is None else round(p95)} ms vs limit {limit} ms"})
    if "min_tokens_per_sec" in suite.gates:
        limit = suite.gates["min_tokens_per_sec"]
        gates.append({"name": "min_tokens_per_sec", "passed": tps is not None and tps >= limit,
                      "detail": f"{tps} tok/s vs minimum {limit} tok/s"})

    if total and errors == total:
        verdict = "ERROR"  # never got a single answer: infrastructure, not model quality
    elif all(g["passed"] for g in gates):
        verdict = "PASS"
    else:
        verdict = "FAIL"

    return {
        "schema": RESULTS_SCHEMA,
        "verdict": verdict,
        "model": model,
        "backend": backend,
        "suite": suite.name,
        "suite_description": suite.description,
        "request_id": request_id or None,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.monotonic() - t0, 1),
        "environment": _environment(),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed - errors,
            "errors": errors,
            "pass_rate": round(pass_rate, 4),
            "pass_threshold": suite.pass_threshold,
            "latency_ms": {
                "p50": percentile(latencies, 50),
                "p95": p95,
                "max": max(latencies) if latencies else None,
            },
            "tokens_per_sec": tps,
            "completion_tokens": tokens or None,
        },
        "gates": gates,
        "cases": cases_out,
    }


def error_results(error: str, stage: str, model: str = "", backend: str = "", suite: str | None = None,
                  request_id: str = "") -> dict:
    """results.json for a run that broke before the model could be tested."""
    return {
        "schema": RESULTS_SCHEMA,
        "verdict": "ERROR",
        "error": error,
        "stage": stage,
        "model": model,
        "backend": backend,
        "suite": suite,
        "request_id": request_id or None,
        "environment": _environment(),
        "summary": {},
        "gates": [],
        "cases": [],
    }


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(results: dict) -> str:
    s = results["summary"]
    lat = s["latency_ms"]
    icon = {"PASS": "✅", "FAIL": "❌", "ERROR": "⚠️"}.get(results["verdict"], "")
    lines = [
        f"## {icon} Model review: {results['verdict']}",
        "",
        f"**Model** `{results['model']}` · **Suite** `{results['suite']}` · **Backend** `{results['backend'] or '-'}`",
        "",
        "| Passed | Failed | Errors | Pass rate | p50 latency | p95 latency | Tokens/s |",
        "|---|---|---|---|---|---|---|",
        f"| {s['passed']}/{s['total']} | {s['failed']} | {s['errors']} | {s['pass_rate']:.0%} "
        f"(needs {s['pass_threshold']:.0%}) | {lat['p50'] or '-'} ms | {lat['p95'] or '-'} ms | "
        f"{s['tokens_per_sec'] or '-'} |",
        "",
        "**Gates**",
        "",
    ]
    for g in results["gates"]:
        lines.append(f"- {'✅' if g['passed'] else '❌'} `{g['name']}`: {g['detail']}")
    bad = [c for c in results["cases"] if not c["passed"]]
    if bad:
        lines += ["", "**Failed cases**", ""]
        for c in bad:
            if c["error"]:
                lines.append(f"- `{c['id']}`: error: {_md_escape(c['error'][:300])}")
                continue
            reasons = "; ".join(
                f"{x['check']} ({x['detail']})" if x["detail"] else x["check"]
                for x in c["checks"] if not x["passed"]
            )
            lines.append(f"- `{c['id']}`: {_md_escape(reasons)}")
            lines.append(f"  - output: `{_md_escape(c['output'][:300])}`")
    lines += ["", "<details><summary>All cases</summary>", "",
              "| Case | Result | Latency (ms) | Tokens |", "|---|---|---|---|"]
    for c in results["cases"]:
        result = "error" if c["error"] else ("pass" if c["passed"] else "fail")
        lines.append(f"| `{c['id']}` | {result} | {c['latency_ms'] or '-'} | {c['completion_tokens'] or '-'} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def _append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--suite", help="suite name, looked up in --suites-dir")
    which.add_argument("--suite-file", help="path to a suite YAML file (local use)")
    parser.add_argument("--suites-dir", default=str(Path(__file__).resolve().parent.parent / "suites"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:11434/v1")
    parser.add_argument("--api-key", default=os.environ.get("MODEL_REVIEW_API_KEY") or None)
    parser.add_argument("--backend", default="")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--out", default="model-review-out")
    parser.add_argument("--private", action="store_true",
                        help="private suite: keep prompts and replies out of the job summary (results stay in --out)")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        path = Path(args.suite_file) if args.suite_file else resolve_suite(args.suite, args.suites_dir)
        suite = load_suite(path)
    except SuiteError as exc:
        print(f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"error: {exc}", file=sys.stderr)
        # Still leave a results.json so the agent sees *why* instead of just "the run failed".
        (out / "results.json").write_text(json.dumps(error_results(
            str(exc), "suite", args.model, args.backend, args.suite or args.suite_file, args.request_id),
            indent=2), encoding="utf-8")
        _append(os.environ.get("GITHUB_OUTPUT"), "verdict=ERROR\npass_rate=0\n")
        return 2

    print(f"Reviewing {args.model} with suite '{suite.name}' ({len(suite.cases)} cases) at {args.base_url}")
    results = run_suite(suite, args.base_url, args.model, args.api_key, args.backend, args.request_id)

    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    report = render_markdown(results)
    (out / "report.md").write_text(report, encoding="utf-8")

    s = results["summary"]
    if args.private:
        # A public repo's job summary is visible to everyone: only the outcome goes there.
        _append(os.environ.get("GITHUB_STEP_SUMMARY"),
                f"## model-review: private suite\n\n**{results['verdict']}**: {s['passed']}/{s['total']} cases passed "
                f"({s['errors']} errors). Prompts, replies and the report stay in the private bucket.\n")
    else:
        _append(os.environ.get("GITHUB_STEP_SUMMARY"), report)
    _append(os.environ.get("GITHUB_OUTPUT"),
            f"verdict={results['verdict']}\npass_rate={results['summary']['pass_rate']}\n")

    print(f"Verdict: {results['verdict']}  ({s['passed']}/{s['total']} passed, {s['errors']} errors)")
    print(f"Results written to {out / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
