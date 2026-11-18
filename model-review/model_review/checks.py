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
    elif kind == "json_valid":
        if arg is not True:
            raise CheckError("json_valid must be `true`")
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


def run_check(check: Check, output: str, latency_ms: float | None) -> CheckResult:
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
    if check.kind == "max_latency_ms":
        if latency_ms is None:
            return CheckResult(label, False, "no latency recorded")
        ok = latency_ms <= check.arg
        return CheckResult(label, ok, "" if ok else f"took {latency_ms:.0f} ms")
    raise CheckError(f"unhandled check kind {check.kind}")  # pragma: no cover
