"""Load and validate suite files (YAML).

Shape:

    name: smoke
    description: Basic sanity checks any chat model should pass.
    pass_threshold: 0.8          # fraction of cases that must pass (default 1.0)
    gates:                       # optional extra pass conditions
      max_p95_latency_ms: 30000
      min_tokens_per_sec: 2
    warmup: true                 # one untimed request first, so model load time isn't counted
    defaults:
      system: "You are a helpful assistant."
      params: {temperature: 0, max_tokens: 256, seed: 7}
      strip_think: true          # drop <think>...</think> before checking
      timeout_s: 300
    cases:
      - id: arithmetic
        prompt: "What is 17 + 25? Reply with only the number."
        checks:
          - regex: '\\b42\\b'
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .checks import Check, CheckError, parse_check

SUITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
GATE_KEYS = {"max_p95_latency_ms", "min_tokens_per_sec"}
TOP_KEYS = {"name", "description", "pass_threshold", "gates", "warmup", "defaults", "cases"}
DEFAULT_KEYS = {"system", "params", "strip_think", "timeout_s"}
CASE_KEYS = {"id", "prompt", "messages", "system", "params", "checks", "description"}
ROLES = {"system", "user", "assistant"}


class SuiteError(ValueError):
    """The suite file is missing or malformed. The message lists every problem found."""


@dataclass
class Case:
    id: str
    messages: list[dict]
    params: dict
    checks: list[Check]
    description: str = ""


@dataclass
class Suite:
    name: str
    description: str
    cases: list[Case]
    pass_threshold: float = 1.0
    gates: dict = field(default_factory=dict)
    warmup: bool = True
    strip_think: bool = True
    timeout_s: float = 300.0
    path: str = ""


def resolve_suite(name: str, suites_dir: str | Path) -> Path:
    """Map a suite name to a file inside suites_dir. Names only, never paths."""
    if not SUITE_NAME_RE.match(name or "") or ".." in name:
        raise SuiteError(f"invalid suite name {name!r}: use letters, digits, '.', '_' or '-'")
    base = Path(suites_dir)
    for ext in (".yaml", ".yml"):
        candidate = base / f"{name}{ext}"
        if candidate.is_file():
            return candidate
    available = sorted(p.stem for p in base.glob("*.y*ml"))
    raise SuiteError(f"suite {name!r} not found in {base}; available: {available}")


def load_suite(path: str | Path) -> Suite:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SuiteError(f"suite file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SuiteError(f"{path}: invalid YAML: {exc}") from exc
    return parse_suite(raw, source=str(path), default_name=path.stem)


def parse_suite(raw: Any, source: str = "<suite>", default_name: str = "suite") -> Suite:
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise SuiteError(f"{source}: top level must be a mapping")

    for key in sorted(set(raw) - TOP_KEYS):
        if not str(key).startswith("x-"):  # x-* keys are free for YAML anchors / shared schemas
            problems.append(f"unknown top-level key {key!r} (prefix it with x- to keep shared definitions)")

    threshold = raw.get("pass_threshold", 1.0)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        problems.append("pass_threshold must be a number between 0 and 1")
        threshold = 1.0

    gates = raw.get("gates") or {}
    if not isinstance(gates, dict):
        problems.append("gates must be a mapping")
        gates = {}
    for key, value in gates.items():
        if key not in GATE_KEYS:
            problems.append(f"unknown gate {key!r}; known: {sorted(GATE_KEYS)}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            problems.append(f"gate {key} must be a non-negative number")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        problems.append("defaults must be a mapping")
        defaults = {}
    for key in sorted(set(defaults) - DEFAULT_KEYS):
        problems.append(f"unknown key in defaults: {key!r}")
    default_params = defaults.get("params") or {}
    if not isinstance(default_params, dict):
        problems.append("defaults.params must be a mapping")
        default_params = {}
    default_system = defaults.get("system")
    if default_system is not None and not isinstance(default_system, str):
        problems.append("defaults.system must be a string")
        default_system = None
    timeout_s = defaults.get("timeout_s", 300)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        problems.append("defaults.timeout_s must be a positive number")
        timeout_s = 300

    cases: list[Case] = []
    seen: set[str] = set()
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        problems.append("cases must be a non-empty list")
        raw_cases = []

    for index, rc in enumerate(raw_cases):
        where = f"cases[{index}]"
        if not isinstance(rc, dict):
            problems.append(f"{where} must be a mapping")
            continue
        cid = rc.get("id")
        if not isinstance(cid, str) or not cid.strip():
            problems.append(f"{where} needs a string id")
            cid = f"case-{index}"
        where = f"case {cid!r}"
        if cid in seen:
            problems.append(f"duplicate case id {cid!r}")
        seen.add(cid)
        for key in sorted(set(rc) - CASE_KEYS):
            problems.append(f"{where}: unknown key {key!r}")

        system = rc.get("system", default_system)
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        if ("prompt" in rc) == ("messages" in rc):
            problems.append(f"{where}: give exactly one of prompt or messages")
        elif "prompt" in rc:
            if not isinstance(rc["prompt"], str):
                problems.append(f"{where}: prompt must be a string")
            else:
                messages.append({"role": "user", "content": rc["prompt"]})
        else:
            msgs = rc["messages"]
            if not isinstance(msgs, list) or not msgs:
                problems.append(f"{where}: messages must be a non-empty list")
            else:
                for m in msgs:
                    if (
                        not isinstance(m, dict)
                        or m.get("role") not in ROLES
                        or not isinstance(m.get("content"), str)
                    ):
                        problems.append(f"{where}: each message needs role (system|user|assistant) and string content")
                        break
                else:
                    if any(m["role"] == "system" for m in msgs):
                        messages = [m for m in messages if m["role"] != "system"]
                    messages.extend({"role": m["role"], "content": m["content"]} for m in msgs)

        params = dict(default_params)
        case_params = rc.get("params") or {}
        if not isinstance(case_params, dict):
            problems.append(f"{where}: params must be a mapping")
        else:
            params.update(case_params)
        for reserved in ("model", "messages", "stream"):
            if reserved in params:
                problems.append(f"{where}: params may not set {reserved!r}")

        checks: list[Check] = []
        raw_checks = rc.get("checks")
        if not isinstance(raw_checks, list) or not raw_checks:
            problems.append(f"{where}: needs a non-empty checks list")
        else:
            for ci, spec in enumerate(raw_checks):
                try:
                    checks.append(parse_check(spec))
                except CheckError as exc:
                    problems.append(f"{where}: checks[{ci}]: {exc}")

        cases.append(Case(cid, messages, params, checks, str(rc.get("description", ""))))

    if problems:
        raise SuiteError(f"{source}: " + "; ".join(problems))

    return Suite(
        name=str(raw.get("name") or default_name),
        description=str(raw.get("description") or ""),
        cases=cases,
        pass_threshold=float(threshold),
        gates=dict(gates),
        warmup=bool(raw.get("warmup", True)),
        strip_think=bool(defaults.get("strip_think", True)),
        timeout_s=float(timeout_s),
        path=source,
    )
