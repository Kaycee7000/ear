import asyncio

import pytest

from conftest import SUITES_DIR, TOKEN
from model_review import runner, tools
from model_review.client import ModelReviewClient, ReviewError
from model_review.suite import load_suite


@pytest.fixture
def smoke_results(mock_model):
    return runner.run_suite(load_suite(SUITES_DIR / "smoke.yaml"), mock_model("good"), "mock",
                            backend="mock", log=lambda *a: None)


def make_client(gh, token=TOKEN):
    return ModelReviewClient(repo="o/r", token=token, api_url=gh.api_url)


def test_review_end_to_end_with_run_details(fake_github, smoke_results):
    fake_github.results = smoke_results
    review = make_client(fake_github).review_model("mock", backend="mock", wait_s=30, poll_s=0.01)
    assert review["state"] == "passed" and review["verdict"] == "PASS"
    assert review["run_id"] == 1001 and review["failed_cases"] == []
    sent = fake_github.dispatches[-1]
    assert sent["ref"] == "main" and sent["return_run_details"] is True
    assert sent["inputs"]["backend"] == "mock" and len(sent["inputs"]["request_id"]) == 12
    # The GitHub token must not leak to the artifact blob host.
    assert fake_github.blob_auth_headers == [None]


def test_dispatch_without_run_details_finds_run_by_request_id(fake_github, smoke_results):
    fake_github.return_details = False
    fake_github.results = smoke_results
    started = make_client(fake_github).start_review("mock", backend="mock", runner=["self-hosted", "gpu"])
    assert started["run_id"] == 1001
    assert fake_github.dispatches[-1]["inputs"]["runner"] == '["self-hosted", "gpu"]'


def test_failed_model_reports_failed_cases(fake_github, mock_model):
    fake_github.results = runner.run_suite(load_suite(SUITES_DIR / "smoke.yaml"), mock_model("bad"),
                                           "mock", log=lambda *a: None)
    fake_github.conclusion = "failure"
    review = make_client(fake_github).review_model("mock", wait_s=30, poll_s=0.01)
    assert review["state"] == "failed"
    assert review["failed_cases"][0]["output"] == "banana"
    assert review["failed_cases"][0]["failed_checks"]


def test_run_without_results_is_error_with_failed_step(fake_github):
    fake_github.results = None
    fake_github.conclusion = "failure"
    review = make_client(fake_github).review_model("x", wait_s=30, poll_s=0.01)
    assert review["state"] == "error"
    assert review["failed_steps"] == ["Start model backend"]


def test_not_waiting_returns_run_id(fake_github):
    out = make_client(fake_github).review_model("x", wait_s=0)
    assert out["state"] == "queued" and out["run_id"] == 1001


def test_wait_timeout_returns_running_state(fake_github):
    fake_github.polls_until_done = 10_000
    out = make_client(fake_github).review_model("x", wait_s=0.05, poll_s=0.01)
    assert out["state"] in ("queued", "running") and out["run_id"] == 1001


def test_list_suites(fake_github):
    assert make_client(fake_github).list_suites() == [
        {"name": "smoke", "path": "model-review/suites/smoke.yaml"}]


def test_auth_errors(fake_github):
    with pytest.raises(ReviewError, match="token"):
        ModelReviewClient(repo="o/r", token="", api_url=fake_github.api_url).start_review("x")
    with pytest.raises(ReviewError, match="401"):
        make_client(fake_github, token="wrong").start_review("x")


def test_function_calling_dispatch(fake_github, smoke_results):
    fake_github.results = smoke_results
    tools.set_client(make_client(fake_github))
    try:
        out = tools.call_tool("review_model", '{"model": "mock", "backend": "mock", "wait_seconds": 0}')
        assert out["state"] == "queued"
        out = tools.call_tool("get_review", {"run_id": out["run_id"], "wait_seconds": 5})
        assert out["state"] == "passed"
        assert tools.call_tool("list_suites")["suites"][0]["name"] == "smoke"
        assert tools.call_tool("nope")["state"] == "error"
        assert "bad arguments" in tools.call_tool("get_review", {"oops": 1})["message"]
        assert "not valid JSON" in tools.call_tool("get_review", "{")["message"]
    finally:
        tools.set_client(None)


def test_tool_schema_matches_functions():
    import inspect
    for schema in tools.TOOL_SCHEMAS:
        fn = schema["function"]
        params = set(inspect.signature(tools.TOOLS[fn["name"]]).parameters)
        assert set(fn["parameters"]["properties"]) == params
        assert set(fn["parameters"].get("required", [])) <= params


def test_mcp_server_exposes_tools():
    pytest.importorskip("mcp")
    from model_review import mcp_server
    listed = asyncio.run(mcp_server.server.list_tools())
    names = {t.name for t in listed}
    assert names == {"review_model", "get_review", "list_suites"}


def test_setup_error_message_reaches_agent(fake_github):
    fake_github.results = runner.error_results("ollama pull 'nope' failed", "backend", "nope", "ollama")
    fake_github.conclusion = "failure"
    review = make_client(fake_github).review_model("nope", wait_s=30, poll_s=0.01)
    assert review["state"] == "error"
    assert "ollama pull 'nope' failed" in review["message"]
