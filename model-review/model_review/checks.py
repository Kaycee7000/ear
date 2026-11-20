"""Assertions a suite can make about one model reply.

A check is written in YAML as a one-key mapping, optionally with modifiers:

    - contains: "42"
    - not_contains: ["as an AI", "I cannot"]
      ignore_case: true
    - json_schema: {type: object, required: [action]}
      strict: true

See CHECK_KINDS for the full list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

try:  # jsonschema is a declared dependency, but keep import errors readable
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

CHECK_KINDS = {
    "contains": "every given string appears in the reply",
    "contains_any": "at least one of the given strings appears",
    "not_contains": "none of the given strings appear",
    "regex": "the regular expression matches somewhere in the reply",
    "equals": "the trimmed reply equals the string",
    "one_of": "the trimmed reply equals one of the strings",
    "json_valid": "the reply parses as JSON (a ```json fence is allowed unless strict)",
    "json_schema": "the reply parses as JSON and validates against the schema",
    "min_length": "the trimmed reply has at least N characters",
    "max_length": "the trimmed reply has at most N characters",
    "max_latency_ms": "the request finished within N milliseconds",
    "ear_design": "the reply is an EAR design (modeling tool) whose IDs, relationships and exact-quote "
                  "citations are valid for the task JSON in the last user message",
}
MODIFIERS = {"ignore_case", "strict"}


class CheckError(ValueError):
    """A check in a suite file is malformed."""


@dataclass
class Check:
    kind: str
    arg: Any
    ignore_case: bool = False
    strict: bool = False

    @property
    def label(self) -> str:
        arg = self.arg
        if self.kind == "json_schema":
            arg = "<schema>"
        elif isinstance(arg, (list, dict)):
            arg = json.dumps(arg)
        text = f"{self.kind}: {arg}"
        if self.ignore_case:
            text += " (ignore case)"
        if self.strict:
            text += " (strict)"
        return text


@dataclass
class CheckResult:
    check: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.check, "passed": self.passed, "detail": self.detail}


def _as_str_list(kind: str, value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    raise CheckError(f"{kind} needs a string or a non-empty list of strings")


def parse_check(spec: Any) -> Check:
    if not isinstance(spec, dict):
        raise CheckError(f"each check must be a mapping, got {spec!r}")
    kinds = [k for k in spec if k in CHECK_KINDS]
    unknown = [k for k in spec if k not in CHECK_KINDS and k not in MODIFIERS]
    if unknown:
        raise CheckError(f"unknown check key(s) {unknown}; known checks: {sorted(CHECK_KINDS)}")
    if len(kinds) != 1:
        raise CheckError(f"each check needs exactly one of {sorted(CHECK_KINDS)}, got {kinds or 'none'}")
    kind = kinds[0]
    arg = spec[kind]
    check = Check(kind, arg, bool(spec.get("ignore_case", False)), bool(spec.get("strict", False)))

    if kind in ("contains", "not_contains", "contains_any", "one_of"):
        check.arg = _as_str_list(kind, arg)
    elif kind in ("regex", "equals"):
        if not isinstance(arg, str):
            raise CheckError(f"{kind} needs a string")
        if kind == "regex":
            try:
                re.compile(arg)
            except re.error as exc:
                raise CheckError(f"invalid regex {arg!r}: {exc}") from exc
    elif kind in ("json_valid", "ear_design"):
        if arg is not True:
            raise CheckError(f"{kind} must be `true`")
    elif kind == "json_schema":
        if not isinstance(arg, dict):
            raise CheckError("json_schema needs a JSON Schema mapping")
        if jsonschema is None:
            raise CheckError("json_schema needs the `jsonschema` package")
        try:
            jsonschema.validators.validator_for(arg).check_schema(arg)
        except jsonschema.SchemaError as exc:
            raise CheckError(f"invalid JSON schema: {exc.message}") from exc
    elif kind in ("min_length", "max_length", "max_latency_ms"):
        if isinstance(arg, bool) or not isinstance(arg, (int, float)) or arg < 0:
            raise CheckError(f"{kind} needs a non-negative number")
    return check


_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", re.S)


def extract_json(text: str, strict: bool = False) -> Any:
    """Parse the reply as JSON. Unless strict, a single surrounding ``` fence is allowed."""
    body = text.strip()
    if not strict:
        match = _FENCE.match(body)
        if match:
            body = match.group(1).strip()
    return json.loads(body)


def _fold(s: str, ignore_case: bool) -> str:
    return s.casefold() if ignore_case else s


_EAR_ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_EAR_LIMITS = {"components": (1, 16), "relationships": (0, 24), "assumptions": (0, 12), "open_questions": (0, 12)}


def _unspaced_once(quote: str, text: str) -> bool:
    target, flat = "".join(quote.split()), "".join(text.split())
    at = flat.find(target)
    return bool(target) and at >= 0 and flat.find(target, at + 1) < 0


def _respaced_excerpt(quote: str, text: str) -> bool:
    """modeling/design.py's exact_quote accepts the quote: it copies `text` apart from whitespace at exactly one
    place, or it is a multi-line quote with a real line of 12+ characters (the tool then stores that exact text)."""
    if _unspaced_once(quote, text):
        return True
    return any(len(line.strip()) >= 12 and (line.strip() in text or _unspaced_once(line, text))
               for line in quote.splitlines())


def ear_design_problem(reply: Any, sources: dict[str, str]) -> str | None:
    """Same rules as modeling/design.py (Design schema + check_against). Returns the first problem, or None."""
    if not isinstance(reply, dict):
        return "the reply is not a JSON object"
    extra = set(reply) - set(_EAR_LIMITS)
    if extra:
        return f"unexpected keys {sorted(extra)}"
    for key, (low, high) in _EAR_LIMITS.items():
        value = reply.get(key)
        if not isinstance(value, list) or not low <= len(value) <= high:
            return f"{key} must be a list of {low}-{high} items"
    for key in ("assumptions", "open_questions"):
        if any(not isinstance(t, str) or not 1 <= len(t.strip()) <= 600 for t in reply[key]):
            return f"{key} must hold strings of 1-600 characters"

    def cited(item: dict, where: str) -> str | None:
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4:
            return f"{where}: evidence must be a list of 1-4 citations"
        for cite in evidence:
            if not isinstance(cite, dict) or set(cite) != {"source_id", "quote"}:
                return f"{where}: each citation needs exactly source_id and quote"
            sid, quote = cite["source_id"], cite["quote"]
            if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 160:
                return f"{where}: quote must be 1-160 characters"
            if sid not in sources:
                return f"{where}: unknown source_id {sid!r}"
            if quote.strip() not in sources[sid] and not _respaced_excerpt(quote, sources[sid]):
                return f"{where}: quote is not an excerpt of source {sid!r}, even ignoring spacing: {quote.strip()[:80]!r}"
        return None

    ids = []
    for i, comp in enumerate(reply["components"]):
        where = f"components[{i}]"
        if not isinstance(comp, dict) or set(comp) != {"id", "purpose", "evidence"}:
            return f"{where} needs exactly id, purpose and evidence"
        if not isinstance(comp["id"], str) or not _EAR_ID.match(comp["id"].strip()):
            return f"{where}: id must match {_EAR_ID.pattern}"
        if not isinstance(comp["purpose"], str) or not 1 <= len(comp["purpose"].strip()) <= 600:
            return f"{where}: purpose must be 1-600 characters"
        ids.append(comp["id"].strip())
        problem = cited(comp, where)
        if problem:
            return problem
    if len(set(ids)) != len(ids):
        return "component IDs are not unique"
    for i, rel in enumerate(reply["relationships"]):
        where = f"relationships[{i}]"
        if not isinstance(rel, dict) or set(rel) != {"source", "target", "description", "evidence"}:
            return f"{where} needs exactly source, target, description and evidence"
        if not isinstance(rel["source"], str) or not isinstance(rel["target"], str) \
                or rel["source"].strip() not in ids or rel["target"].strip() not in ids:
            return f"{where}: source and target must be existing component IDs"
        if not isinstance(rel["description"], str) or not 1 <= len(rel["description"].strip()) <= 600:
            return f"{where}: description must be 1-600 characters"
        problem = cited(rel, where)
        if problem:
            return problem
    return None


def _task_sources(messages: list[dict] | None) -> dict[str, str] | None:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            try:
                task = json.loads(message.get("content") or "")
                return {s["id"]: s["text"].strip() for s in task["sources"]}
            except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
                return None
    return None


def run_check(check: Check, output: str, latency_ms: float | None,
              messages: list[dict] | None = None) -> CheckResult:
    label = check.label
    ic = check.ignore_case
    text = output.strip()
    hay = _fold(output, ic)

    if check.kind == "contains":
        missing = [s for s in check.arg if _fold(s, ic) not in hay]
        return CheckResult(label, not missing, f"missing {missing}" if missing else "")
    if check.kind == "contains_any":
        ok = any(_fold(s, ic) in hay for s in check.arg)
        return CheckResult(label, ok, "" if ok else "none of the strings found")
    if check.kind == "not_contains":
        found = [s for s in check.arg if _fold(s, ic) in hay]
        return CheckResult(label, not found, f"found {found}" if found else "")
    if check.kind == "regex":
        flags = re.IGNORECASE if ic else 0
        ok = re.search(check.arg, output, flags) is not None
        return CheckResult(label, ok, "" if ok else "no match")
    if check.kind == "equals":
        ok = _fold(text, ic) == _fold(check.arg.strip(), ic)
        return CheckResult(label, ok, "" if ok else f"got {text[:120]!r}")
    if check.kind == "one_of":
        ok = _fold(text, ic) in {_fold(s.strip(), ic) for s in check.arg}
        return CheckResult(label, ok, "" if ok else f"got {text[:120]!r}")
    if check.kind in ("json_valid", "json_schema"):
        try:
            data = extract_json(output, check.strict)
        except (json.JSONDecodeError, ValueError) as exc:
            return CheckResult(label, False, f"not valid JSON: {exc}")
        if check.kind == "json_valid":
            return CheckResult(label, True)
        validator = jsonschema.validators.validator_for(check.arg)(check.arg)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            where = "/".join(str(p) for p in first.path) or "<root>"
            more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
            return CheckResult(label, False, f"{where}: {first.message}{more}")
        return CheckResult(label, True)
    if check.kind == "min_length":
        ok = len(text) >= check.arg
        return CheckResult(label, ok, "" if ok else f"length {len(text)}")
    if check.kind == "max_length":
        ok = len(text) <= check.arg
        return CheckResult(label, ok, "" if ok else f"length {len(text)}")
    if check.kind == "ear_design":
        sources = _task_sources(messages)
        if sources is None:
            return CheckResult(label, False, "the last user message is not a modeling task JSON with sources")
        try:
            data = extract_json(output, check.strict)
        except (json.JSONDecodeError, ValueError) as exc:
            return CheckResult(label, False, f"not valid JSON: {exc}")
        problem = ear_design_problem(data, sources)
        return CheckResult(label, problem is None, problem or "")
    if check.kind == "max_latency_ms":
        if latency_ms is None:
            return CheckResult(label, False, "no latency recorded")
        ok = latency_ms <= check.arg
        return CheckResult(label, ok, "" if ok else f"took {latency_ms:.0f} ms")
    raise CheckError(f"unhandled check kind {check.kind}")  # pragma: no cover
