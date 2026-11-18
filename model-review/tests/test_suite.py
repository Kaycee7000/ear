import pytest

from conftest import SUITES_DIR
from model_review.suite import SuiteError, load_suite, parse_suite, resolve_suite


@pytest.mark.parametrize("path", sorted(SUITES_DIR.glob("*.y*ml")), ids=lambda p: p.name)
def test_shipped_suites_are_valid(path):
    suite = load_suite(path)
    assert suite.cases


def test_resolve_by_name_and_reject_paths():
    assert resolve_suite("smoke", SUITES_DIR).name == "smoke.yaml"
    for bad in ("../smoke", "/etc/passwd", "a/b", "", "..", "smoke/../../x"):
        with pytest.raises(SuiteError):
            resolve_suite(bad, SUITES_DIR)
    with pytest.raises(SuiteError, match="available"):
        resolve_suite("does-not-exist", SUITES_DIR)


def test_defaults_and_overrides():
    suite = parse_suite({
        "defaults": {"system": "be brief", "params": {"temperature": 0, "max_tokens": 50}},
        "cases": [
            {"id": "a", "prompt": "hi", "params": {"max_tokens": 5}, "checks": [{"min_length": 1}]},
            {"id": "b", "messages": [{"role": "system", "content": "own system"},
                                     {"role": "user", "content": "yo"}], "checks": [{"min_length": 1}]},
        ],
    })
    a, b = suite.cases
    assert a.messages == [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    assert a.params == {"temperature": 0, "max_tokens": 5}
    assert [m["content"] for m in b.messages] == ["own system", "yo"]  # case system replaces default
    assert suite.pass_threshold == 1.0


def test_all_problems_reported_together():
    with pytest.raises(SuiteError) as err:
        parse_suite({
            "pass_threshold": 2,
            "bogus": 1,
            "cases": [
                {"id": "a", "checks": [{"contains": "x"}]},             # no prompt
                {"id": "a", "prompt": "p", "checks": []},               # duplicate id, no checks
                {"id": "c", "prompt": "p", "params": {"model": "x"}, "checks": [{"regex": "("}]},
            ],
        })
    msg = str(err.value)
    for fragment in ("pass_threshold", "bogus", "exactly one of prompt", "duplicate case id",
                     "non-empty checks", "may not set 'model'", "invalid regex"):
        assert fragment in msg


def test_x_keys_are_allowed_for_anchors():
    suite = parse_suite({"x-schema": {"type": "object"},
                         "cases": [{"id": "a", "prompt": "p", "checks": [{"json_valid": True}]}]})
    assert suite.cases[0].id == "a"
