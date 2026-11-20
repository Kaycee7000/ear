"""The ear_design check mirrors modeling/design.py's validation, so model-review can test modeling models."""
import copy
import json
from pathlib import Path

import pytest

from model_review.checks import parse_check, run_check
from model_review.suite import load_suite

TASK = {"goal": "Design the capture tool.", "sources": [
    {"id": "capture", "text": "The collector stores each captured page in the evidence bucket."},
    {"id": "index", "text": "The indexer reads pages from the evidence bucket and records their titles."}]}
MESSAGES = [{"role": "system", "content": "instructions"}, {"role": "user", "content": json.dumps(TASK)}]
QUOTE = {"source_id": "capture", "quote": "stores each captured page"}
GOOD = {
    "components": [{"id": "collector", "purpose": "Capture pages.", "evidence": [QUOTE]},
                   {"id": "indexer", "purpose": "Index pages.",
                    "evidence": [{"source_id": "index", "quote": "records their titles"}]}],
    "relationships": [{"source": "collector", "target": "indexer", "description": "Shares the bucket.",
                       "evidence": [QUOTE]}],
    "assumptions": [], "open_questions": ["Where is the bucket hosted?"],
}
CHECK = parse_check({"ear_design": True})


def result(reply, messages=MESSAGES):
    return run_check(CHECK, reply if isinstance(reply, str) else json.dumps(reply), 10.0, messages)


def test_valid_design_passes_even_in_a_fence():
    assert result(GOOD).passed
    assert result("```json\n" + json.dumps(GOOD) + "\n```").passed


@pytest.mark.parametrize("path,value,expect", [
    (("components", 0, "evidence", 0, "quote"), "invented text", "not an excerpt"),
    (("components", 0, "evidence", 0, "quote"), "records their titles", "not an excerpt"),   # wrong source
    (("components", 0, "evidence", 0, "source_id"), "missing", "unknown source_id"),
    (("components", 1, "id"), "collector", "not unique"),
    (("components", 0, "id"), "bad id!", "id must match"),
    (("relationships", 0, "target"), "nobody", "existing component IDs"),
    (("components", 0, "evidence"), [], "1-4 citations"),
])
def test_broken_designs_fail_with_a_reason(path, value, expect):
    reply = copy.deepcopy(GOOD)
    node = reply
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    check = result(reply)
    assert not check.passed and expect in check.detail


def test_respaced_quotes_pass_like_the_modeling_tool():
    # The modeling tool stores the source's exact text for a quote that differs only in spacing.
    reply = copy.deepcopy(GOOD)
    reply["components"][0]["evidence"][0]["quote"] = "stores  each\ncaptured page"
    assert result(reply).passed
    # Lines stitched with an invented one: passes because a real line of 12+ characters stands in.
    reply["components"][0]["evidence"][0]["quote"] = "The collector stores each\nan invented middle line"
    assert result(reply).passed
    reply["components"][0]["evidence"][0]["quote"] = "an invented line here\nand another invented one"
    assert not result(reply).passed
    # A re-spaced quote must match one place only.
    task = {"goal": "g", "sources": [{"id": "capture", "text": "a b. ab."}]}
    messages = [{"role": "system", "content": "x"}, {"role": "user", "content": json.dumps(task)}]
    ambiguous = {"components": [{"id": "c", "purpose": "p", "evidence": [{"source_id": "capture", "quote": "ab"}]}],
                 "relationships": [], "assumptions": [], "open_questions": []}
    assert result(ambiguous, messages).passed            # "ab" occurs exactly, so it is fine
    ambiguous["components"][0]["evidence"][0]["quote"] = "a  b."
    assert not result(ambiguous, messages).passed        # "ab." twice once spaces are gone: not one place


def test_extra_keys_and_bad_shapes_fail():
    assert "unexpected keys" in result({**GOOD, "notes": "x"}).detail
    assert "components must be a list" in result({**GOOD, "components": []}).detail
    assert not result("Sure! Here is the design").passed


def test_needs_the_task_in_the_prompt():
    check = result(GOOD, [{"role": "user", "content": "not json"}])
    assert not check.passed and "modeling task JSON" in check.detail


def test_generated_suite_loads():
    path = Path(__file__).resolve().parent.parent / "suites" / "ear-design.yaml"
    suite = load_suite(path)
    assert suite.cases and all(any(c.kind == "ear_design" for c in case.checks) for case in suite.cases)
    for case in suite.cases:
        schema = case.params["response_format"]["json_schema"]["schema"]
        assert "$ref" not in json.dumps(schema)  # inlined, so every backend's grammar converter can use it
        assert result(GOOD, case.messages).detail != "the last user message is not a modeling task JSON with sources"
