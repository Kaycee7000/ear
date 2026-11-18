import pytest

from model_review.checks import CheckError, extract_json, parse_check, run_check


def check(spec, output, latency=10.0):
    return run_check(parse_check(spec), output, latency)


@pytest.mark.parametrize("spec,output,expected", [
    ({"contains": "42"}, "the answer is 42", True),
    ({"contains": ["a", "z"]}, "abc", False),
    ({"contains": "PARIS", "ignore_case": True}, "paris", True),
    ({"contains_any": ["hi", "hello"]}, "hello there", True),
    ({"contains_any": ["hi", "hello"]}, "good day", False),
    ({"not_contains": ["as an AI"]}, "Sure!", True),
    ({"not_contains": "sorry", "ignore_case": True}, "Sorry, no", False),
    ({"regex": r"^\W*ok\W*$", "ignore_case": True}, "Ok.", True),
    ({"regex": r"\b42\b"}, "4242", False),
    ({"equals": "OK"}, "  OK \n", True),
    ({"equals": "OK"}, "OK!", False),
    ({"one_of": ["yes", "no"], "ignore_case": True}, "No", True),
    ({"min_length": 3}, "ab", False),
    ({"max_length": 3}, "abc", True),
])
def test_text_checks(spec, output, expected):
    assert check(spec, output).passed is expected


def test_json_checks_allow_fence_unless_strict():
    fenced = '```json\n{"a": 1}\n```'
    assert check({"json_valid": True}, fenced).passed
    assert not check({"json_valid": True, "strict": True}, fenced).passed
    assert check({"json_valid": True, "strict": True}, '{"a": 1}').passed
    assert not check({"json_valid": True}, 'Here you go: {"a": 1}').passed


def test_json_schema_reports_where_it_failed():
    schema = {"type": "object", "required": ["action"],
              "properties": {"action": {"enum": ["pass", "shoot"]}}}
    assert check({"json_schema": schema}, '{"action": "shoot"}').passed
    result = check({"json_schema": schema}, '{"action": "dance"}')
    assert not result.passed and "action" in result.detail


def test_latency_check():
    assert check({"max_latency_ms": 100}, "x", latency=99).passed
    assert not check({"max_latency_ms": 100}, "x", latency=101).passed
    assert not run_check(parse_check({"max_latency_ms": 100}), "x", None).passed


@pytest.mark.parametrize("bad", [
    "contains",
    {"contains": "a", "regex": "b"},
    {"nope": 1},
    {"contains": []},
    {"regex": "("},
    {"json_valid": False},
    {"json_schema": {"type": "not-a-type"}},
    {"max_length": -1},
    {"max_latency_ms": True},
])
def test_parse_errors(bad):
    with pytest.raises(CheckError):
        parse_check(bad)


def test_extract_json_plain_fence():
    assert extract_json("```\n[1, 2]\n```") == [1, 2]
