"""Private suites: the key is dispatched, results come from the bucket, and nothing private reaches the summary."""
import io
import json

import pytest

from conftest import SUITES_DIR, TOKEN
from model_review import private, runner
from model_review.client import ModelReviewClient, ReviewError
from model_review.suite import load_suite

KEY = "model-review/suites/design-review-42.yaml"


class FakeBucket:
    """Just enough of boto3's S3 client for private.py."""

    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key, Range=None):  # noqa: N803
        if Key not in self.objects:
            error = Exception("missing")
            error.response = {"Error": {"Code": "NoSuchKey"}}
            raise error
        data = self.objects[Key]
        if Range:
            end = int(Range.split("-")[1])
            data = data[:end + 1]
        return {"Body": io.BytesIO(data)}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.objects[Key] = Body


@pytest.fixture
def smoke_results(mock_model):
    return runner.run_suite(load_suite(SUITES_DIR / "smoke.yaml"), mock_model("good"), "mock",
                            backend="mock", log=lambda *a: None)


def test_private_review_sends_key_and_reads_results_from_bucket(fake_github, smoke_results, monkeypatch):
    asked = []
    monkeypatch.setattr(private, "read_results", lambda request_id: asked.append(request_id) or smoke_results)
    fake_github.results = smoke_results   # an artifact exists, but a private run must not use it
    client = ModelReviewClient(repo="o/r", token=TOKEN, api_url=fake_github.api_url)
    review = client.review_model("mock", backend="mock", wait_s=30, poll_s=0.01, suite_key=KEY)
    sent = fake_github.dispatches[-1]["inputs"]
    assert sent["suite"] == "private" and sent["suite_key"] == KEY
    assert review["state"] == "passed" and review["verdict"] == "PASS"
    assert asked == [sent["request_id"]]
    assert fake_github.blob_auth_headers == []   # the artifact was never downloaded


def test_private_review_without_published_results_is_an_error(fake_github, monkeypatch):
    monkeypatch.setattr(private, "read_results", lambda request_id: None)
    client = ModelReviewClient(repo="o/r", token=TOKEN, api_url=fake_github.api_url)
    review = client.review_model("mock", backend="mock", wait_s=30, poll_s=0.01, suite_key=KEY)
    assert review["state"] == "error" and review["verdict"] is None


@pytest.mark.parametrize("key", ["../x.yaml", "model-review/suites/../secret.yaml", "model-review/results/a.yaml",
                                 "model-review/suites/a.json", ""])
def test_bad_suite_keys_are_refused(fake_github, key):
    client = ModelReviewClient(repo="o/r", token=TOKEN, api_url=fake_github.api_url)
    if key:
        with pytest.raises(ReviewError):
            client.start_review("mock", backend="mock", suite_key=key)
    with pytest.raises(ReviewError):   # 'private' only exists together with a key
        client.start_review("mock", suite="private", backend="mock")
    assert fake_github.dispatches == []


def test_fetch_publish_and_read_round_trip(tmp_path):
    bucket = FakeBucket()
    bucket.objects[KEY] = (SUITES_DIR / "smoke.yaml").read_bytes()
    dest = private.fetch_suite(KEY, tmp_path / "suite.yaml", client=bucket, bucket="b")
    assert load_suite(dest).name == "smoke"

    out = tmp_path / "out"
    out.mkdir()
    (out / "results.json").write_text(json.dumps({"verdict": "PASS", "summary": {"passed": 1}}))
    (out / "report.md").write_text("# report")
    keys = private.publish_results("abc123", out, client=bucket, bucket="b")
    assert keys == ["model-review/results/abc123/results.json", "model-review/results/abc123/report.md"]
    assert private.read_results("abc123", client=bucket, bucket="b")["verdict"] == "PASS"
    assert private.read_results("never", client=bucket, bucket="b") is None
    with pytest.raises(private.PrivateError):
        private.publish_results("bad/id", out, client=bucket, bucket="b")


def test_oversized_suite_is_refused(tmp_path, monkeypatch):
    bucket = FakeBucket()
    bucket.objects[KEY] = b"x" * (private.MAX_SUITE_BYTES + 10)
    with pytest.raises(private.PrivateError):
        private.fetch_suite(KEY, tmp_path / "suite.yaml", client=bucket, bucket="b")


def test_missing_settings_name_the_variables(monkeypatch):
    for name in ("EAR_B2_REGION", "EAR_B2_BUCKET", "EAR_B2_KEY_ID", "EAR_B2_APPLICATION_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(private.PrivateError, match="EAR_B2_BUCKET"):
        private.read_results("abc123")


def test_private_run_keeps_prompts_and_replies_out_of_the_summary(tmp_path, mock_model, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output.txt"))
    code = runner.main(["--suite-file", str(SUITES_DIR / "smoke.yaml"), "--private", "--model", "mock",
                        "--base-url", mock_model("good"), "--backend", "mock", "--out", str(tmp_path / "out")])
    assert code == 0
    text = summary.read_text()
    assert "private suite" in text and "PASS" in text
    prompts = [case.messages[-1]["content"] for case in load_suite(SUITES_DIR / "smoke.yaml").cases]
    assert not any(prompt[:30] in text for prompt in prompts)
    assert (tmp_path / "out" / "report.md").read_text().strip()   # the full report is still written locally
