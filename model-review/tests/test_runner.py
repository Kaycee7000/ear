import json

from conftest import SUITES_DIR
from model_review import runner
from model_review.suite import load_suite, parse_suite

SMOKE = SUITES_DIR / "smoke.yaml"


def quiet(*_a, **_k):
    pass


def test_smoke_passes_on_good_mock(mock_model):
    results = runner.run_suite(load_suite(SMOKE), mock_model("good"), "mock", log=quiet)
    assert results["verdict"] == "PASS"
    assert results["summary"]["passed"] == results["summary"]["total"] == 6
    assert results["summary"]["latency_ms"]["p95"] is not None
    assert results["summary"]["tokens_per_sec"] > 0


def test_smoke_fails_on_bad_mock(mock_model):
    results = runner.run_suite(load_suite(SMOKE), mock_model("bad"), "mock", log=quiet)
    assert results["verdict"] == "FAIL"
    assert results["summary"]["errors"] == 0
    failed = [c for c in results["cases"] if not c["passed"]]
    assert failed and all(c["output"] == "banana" for c in failed)


def test_unreachable_endpoint_is_error_not_fail():
    suite = parse_suite({"warmup": False, "defaults": {"timeout_s": 2},
                         "cases": [{"id": "a", "prompt": "hi", "checks": [{"min_length": 1}]}]})
    results = runner.run_suite(suite, "http://127.0.0.1:9/v1", "m", log=quiet)
    assert results["verdict"] == "ERROR"
    assert results["cases"][0]["error"]


def test_latency_gate(mock_model):
    suite = parse_suite({"warmup": False, "gates": {"max_p95_latency_ms": 50},
                         "cases": [{"id": "a", "prompt": "hello", "checks": [{"min_length": 1}]}]})
    slow = runner.run_suite(suite, mock_model("good", delay_ms=150), "mock", log=quiet)
    assert slow["verdict"] == "FAIL"
    assert [g["name"] for g in slow["gates"] if not g["passed"]] == ["max_p95_latency_ms"]


def test_think_blocks_are_stripped(monkeypatch):
    monkeypatch.setattr(runner, "chat", lambda *a, **k: ("<think>hmm, 41?</think>\n42", {}))
    suite = parse_suite({"warmup": False,
                         "cases": [{"id": "a", "prompt": "17+25", "checks": [{"equals": "42"}]}]})
    assert runner.run_suite(suite, "http://x/v1", "m", log=quiet)["verdict"] == "PASS"


def test_percentile():
    assert runner.percentile([], 95) is None
    assert runner.percentile([5], 50) == 5
    assert runner.percentile(list(range(1, 101)), 95) == 95


def test_main_writes_outputs(mock_model, tmp_path, monkeypatch):
    gh_out = tmp_path / "gh_output"
    gh_summary = tmp_path / "gh_summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(gh_summary))
    out = tmp_path / "out"
    code = runner.main(["--suite", "smoke", "--suites-dir", str(SUITES_DIR), "--model", "mock",
                        "--base-url", mock_model("good"), "--backend", "mock",
                        "--request-id", "abc123", "--out", str(out)])
    assert code == 0
    results = json.loads((out / "results.json").read_text())
    assert results["schema"] == "model-review/v1" and results["request_id"] == "abc123"
    assert "verdict=PASS" in gh_out.read_text()
    assert "Model review: PASS" in gh_summary.read_text()


def test_main_bad_suite_still_explains_itself(tmp_path, monkeypatch):
    gh_out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    assert runner.main(["--suite", "no-such-suite", "--suites-dir", str(SUITES_DIR), "--model", "m",
                        "--base-url", "http://x/v1", "--out", str(tmp_path)]) == 2
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["verdict"] == "ERROR" and results["stage"] == "suite"
    assert "available" in results["error"]
    assert "verdict=ERROR" in gh_out.read_text()


def test_main_rejects_path_as_suite_name(tmp_path):
    assert runner.main(["--suite", "../etc", "--suites-dir", str(SUITES_DIR), "--model", "m",
                        "--base-url", "http://x/v1", "--out", str(tmp_path)]) == 2


def test_backend_script_failure_writes_results(tmp_path):
    import os
    import shutil
    import subprocess
    if not shutil.which("bash"):
        import pytest
        pytest.skip("bash not available")
    script = SUITES_DIR.parent / "scripts" / "start_backend.sh"
    env = {**os.environ, "BACKEND": "endpoint", "MODEL": "m", "RUNNER_TEMP": str(tmp_path)}
    env.pop("ENDPOINT", None)
    env.pop("GITHUB_ENV", None)
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode == 1
    results = json.loads((tmp_path / "model-review" / "results.json").read_text())
    assert results["verdict"] == "ERROR" and results["stage"] == "backend"
    assert "endpoint input" in results["error"]
