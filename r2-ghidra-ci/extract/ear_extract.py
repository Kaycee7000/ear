#!/usr/bin/env python3
"""Extract evidence from a binary for the EAR modeling tool, using the r2-ghidra-ci toolbox image.

Runs inside ghcr.io/kaycee7000/ear/r2-ghidra-ci (Python standard library + radare2 + r2ghidra):

    python3 ear_extract.py --url https://example.org/tool --goal "How does it store data?" --out out/
    python3 ear_extract.py --file ./target_bin --functions main,parse_args --out out/

Writes three files to --out:
  extraction.json  everything extracted: file info, sections, imports, exports, strings, function list,
                   and the decompiled code of the selected functions (r2ghidra `pdg`, falling back to `pdc`)
  evidence.json    {"sources": [...]} in the modeling tool's format, trimmed to fit its 8 KiB task limit
  task.json        {"sources": [...], "goal": "..."}, ready for modeling/design.py

Static analysis only: the binary is never executed. Text from the caller (function names) is only matched
against radare2's function list; commands sent to radare2 use addresses, never caller text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SCHEMA = "ear-extract/v1"
TASK_LIMIT = 8192          # modeling/design.py: goal + sources must fit in 8192 UTF-8 bytes
TASK_MARGIN = 96           # headroom for serializer differences
SOURCE_LIMIT = 6000        # modeling/design.py: max characters per source text
MAX_SOURCES = 20           # modeling/design.py: 1-20 sources
MIN_FUNCTION_CHARS = 300   # don't add a decompiled function shorter than this after trimming
FUNCTION_CAP = 2000        # evidence characters per decompiled function, so several functions fit
QUOTE_LIMIT = 160          # modeling/design.py: citation quotes are 1-160 characters
FACT_LINE = 150            # one-line fact sources stay under QUOTE_LIMIT, so a model can quote one whole
DEFAULT_GOAL = ("Explain how this program is built: its main components, the data it handles, "
                "and how control and data flow between them, using only the extracted evidence.")
ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
# Evidence keeps what says something about the program; extraction.json keeps everything.
CRT_SYMBOL = re.compile(r"^(_ITM_\w+|__cxa_finalize|__cxa_atexit|__gmon_start__|__libc_start_main|"
                        r"__stack_chk_fail|_Jv_RegisterClasses|_IO_stdin_used|__TMC_END__|__dso_handle|"
                        r"__bss_start|__data_start|_edata|_end|_init|_fini|_start)$")


class ExtractError(RuntimeError):
    pass


# ---------- fetching ----------

def fetch(url: str, dest: Path, max_bytes: int) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise ExtractError("binary_url must be an https:// URL")
    request = urllib.request.Request(url, headers={"User-Agent": "ear-extract"})
    with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as out:
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ExtractError(f"binary is larger than {max_bytes // (1 << 20)} MB")
            out.write(chunk)
    if total == 0:
        raise ExtractError("downloaded file is empty")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------- radare2 over a pipe (the r2pipe protocol: `r2 -q0` ends every reply with a NUL byte) ----------

class R2:
    def __init__(self, path: Path, timeout: float = 120):
        self.proc = subprocess.Popen(
            ["r2", "-q0", "-e", "scr.color=0", "-e", "scr.interactive=false", "-e", "bin.relocs.apply=true",
             "-e", "scr.utf8=false", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._read(timeout)  # banner / init

    def _read(self, timeout: float) -> str:
        fd = self.proc.stdout.fileno()
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ExtractError(f"radare2 did not answer within {timeout:.0f} s")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                self.close()
                raise ExtractError("radare2 exited unexpectedly")
            buf.extend(chunk)
            if buf.endswith(b"\x00"):
                return buf[:-1].decode("utf-8", "replace")

    def cmd(self, command: str, timeout: float = 120) -> str:
        self.proc.stdin.write(command.encode() + b"\n")
        self.proc.stdin.flush()
        return self._read(timeout)

    def cmdj(self, command: str, timeout: float = 120):
        text = self.cmd(command, timeout).strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q!!\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()


# ---------- shaping radare2 output ----------

def _addr(fn: dict) -> int | None:
    for key in ("addr", "offset", "minaddr"):
        if isinstance(fn.get(key), int):
            return fn[key]
    return None


def _size(fn: dict) -> int:
    for key in ("realsz", "size"):
        if isinstance(fn.get(key), int):
            return fn[key]
    return 0


def is_import(name: str) -> bool:
    return name.startswith(("sym.imp.", "imp.", "loc.imp.")) or ".imp." in name


BOILERPLATE = re.compile(r"(^|\.)(_init|_fini|_start|frame_dummy|register_tm_clones|deregister_tm_clones|"
                         r"__do_global_dtors_aux|__libc_csu_\w+|fini0|init0)$")


def pick_functions(functions: list[dict], wanted: list[str], limit: int) -> list[dict]:
    """Requested names first, then main (or the entry point if there is no main), then the largest
    functions. Compiler start-up boilerplate is skipped unless asked for by name."""
    candidates = [f for f in functions if isinstance(f.get("name"), str) and _addr(f) is not None
                  and not is_import(f["name"])]
    has_main = any(f["name"] in ("main", "sym.main", "dbg.main") for f in candidates)
    chosen: list[dict] = []

    def add(fn):
        if fn not in chosen and len(chosen) < limit:
            chosen.append(fn)

    def matches(fn, name):
        n = fn["name"]
        return n == name or n.endswith("." + name)

    for name in wanted:
        for fn in candidates:
            if matches(fn, name):
                add(fn)
                break
    for fn in candidates:
        if matches(fn, "main") or (not has_main and fn["name"] == "entry0"):
            add(fn)
            break
    for fn in sorted(candidates, key=_size, reverse=True):
        if _size(fn) >= 16 and fn["name"] != "entry0" and not BOILERPLATE.search(fn["name"]):
            add(fn)
    return chosen


_PRINTABLE = re.compile(r"^[\x20-\x7e]+$")


def interesting_strings(entries: list[dict], limit: int = 300) -> list[str]:
    seen, ranked = set(), []
    for entry in entries or []:
        s = entry.get("string") if isinstance(entry, dict) else None
        if not isinstance(s, str):
            continue
        s = s.strip()
        if not 4 <= len(s) <= 160 or s in seen or not _PRINTABLE.match(s):
            continue
        letters = sum(c.isalnum() for c in s)
        if letters < len(s) * 0.5:
            continue
        seen.add(s)
        score = (("/" in s) + ("http" in s) + ("%" in s) * 2 + (" " in s) * 2 + (".so" in s) + ("=" in s)
                 + ("error" in s.lower()) * 2)
        ranked.append((-score, len(ranked), s))
    return [s for _, _, s in sorted(ranked)[:limit]]


def tidy_code(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r", "").split("\n")]
    out, blank = [], 0
    for line in lines:
        blank = blank + 1 if not line else 0
        if blank <= 1:
            out.append(line)
    return "\n".join(out).strip()


_DECLARATION = re.compile(r"^\s+[A-Za-z_][\w \t*]*?[\s*]+[A-Za-z_]\w*(\s*\[\d*\])*;$")


def without_declarations(code: str) -> str:
    """Drop the block of local variable declarations Ghidra puts at the top of each function body.
    They take up much of the evidence budget and say little about behaviour."""
    out, in_header = [], False
    for line in code.split("\n"):
        if line.strip() == "{" and not out[-1:] == ["{"]:
            in_header = True
            out.append(line)
            continue
        if in_header:
            if _DECLARATION.match(line) and "=" not in line and "(" not in line:
                continue
            if not line.strip():
                continue
            in_header = False
        out.append(line)
    return "\n".join(out)


def trim(text: str, max_chars: int) -> str:
    """Cut at a line boundary (or, in one-line lists, at a separator) rather than mid-item."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in ("\n", " | ", "; ", ", "):
        if sep in cut[max_chars // 2:]:
            return cut[:cut.rfind(sep)].rstrip()
    return cut.rstrip()


def source_id(prefix: str, name: str, used: set[str]) -> str:
    base = (prefix + ID_RE.sub("-", name).strip("-"))[:44].rstrip("-") or prefix.rstrip("-")
    sid, n = base, 2
    while sid in used:
        sid = f"{base[:44]}-{n}"
        n += 1
    used.add(sid)
    return sid


def describe(info: dict) -> tuple[str, str]:
    """File facts as two short lines of phrases (what the file is; how it was built). Models paraphrase
    `key: value` lists when quoting them, and try to quote a fact line whole, so each line stays under
    the quote limit."""
    what, build = [], []
    if info.get("file"):
        what.append(f"file {info['file']}" + (f" ({info['size_bytes']} bytes)" if info.get("size_bytes") else ""))
    kind = " ".join(str(x) for x in (info.get("format"), info.get("arch")) if x)
    if kind:
        what.append(kind + (f" {info['bits']}-bit" if info.get("bits") else "") + (f" for {info['os']}" if info.get("os") else ""))
    if info.get("language"):
        what.append(f"language {info['language']}")
    if info.get("compiler"):
        build.append(f"built with {info['compiler']}")
    if info.get("stripped") is not None:
        build.append("stripped of symbols" if info["stripped"] else "symbols not stripped")
    if info.get("static") is not None:
        build.append("statically linked" if info["static"] else "dynamically linked")
    return trim("; ".join(what), FACT_LINE), trim("; ".join(build), FACT_LINE)


def task_bytes(goal: str, sources: list[dict]) -> int:
    return len(json.dumps({"sources": sources, "goal": goal}, ensure_ascii=False, separators=(",", ":")).encode())


def build_sources(extraction: dict, goal: str) -> list[dict]:
    """Turn the extraction into modeling sources that fit the task limit."""
    budget = TASK_LIMIT - TASK_MARGIN
    used: set[str] = set()
    sources: list[dict] = []

    def try_add(sid_prefix: str, name: str, text: str, cap: int, min_chars: int = 12) -> bool:
        text = trim(text, min(cap, SOURCE_LIMIT))
        if len(sources) >= MAX_SOURCES or len(text) < min_chars:
            return False
        sid = source_id(sid_prefix, name, used)
        entry = {"id": sid, "text": text}
        while task_bytes(goal, sources + [entry]) > budget:
            room = len(entry["text"]) - (task_bytes(goal, sources + [entry]) - budget) - 8
            if room < min_chars:
                used.discard(sid)
                return False
            entry["text"] = trim(entry["text"], room)
        sources.append(entry)
        return True

    # One line per source where possible: small models mangle quotes that span lines.
    what, build = describe(extraction.get("info", {}))
    try_add("", "binary", what, FACT_LINE, 1)
    try_add("", "build", build, FACT_LINE, 1)
    # Bare lists, no "Imported functions:" label (the source ID says it): models quote the label plus
    # one item ("Imported functions: getenv"), which is not an exact excerpt unless it is the first item.
    imports = [n for n in extraction.get("imports") or [] if not CRT_SYMBOL.match(n)]
    if imports:
        try_add("", "imports", ", ".join(imports), 700, 1)
    exports = [n for n in extraction.get("exports") or [] if not n.startswith("_") and not CRT_SYMBOL.match(n)]
    if exports and len(exports) <= 400:
        try_add("", "exports", ", ".join(exports), 500, 1)
    if extraction.get("strings"):
        try_add("", "strings", " | ".join(extraction["strings"]), 1200, 1)
    for fn in extraction.get("decompiled") or []:
        header = f"Decompiled {fn['name']} at {fn['addr']} ({fn['method']}, local declarations omitted):\n"
        try_add("fn-", fn["name"], header + without_declarations(fn["code"]), FUNCTION_CAP, MIN_FUNCTION_CHARS)
    if not sources:
        raise ExtractError("nothing could be extracted from this file")
    return sources


# ---------- main extraction ----------

def extract(path: Path, wanted: list[str], max_functions: int, log=print) -> dict:
    size = path.stat().st_size
    warnings: list[str] = []
    r2 = R2(path)
    try:
        info_raw = r2.cmdj("ij") or {}
        core, binfo = info_raw.get("core", {}), info_raw.get("bin", {})
        if not binfo or binfo.get("arch") in (None, "", "any"):
            warnings.append("radare2 did not recognise an executable format; results may be thin")
        sections = [{"name": s.get("name"), "size": s.get("size"), "perm": s.get("perm")}
                    for s in (r2.cmdj("iSj") or []) if isinstance(s, dict) and s.get("name")]
        imports = sorted({i["name"] for i in (r2.cmdj("iij") or []) if isinstance(i, dict) and i.get("name")})
        exports = sorted({e["name"] for e in (r2.cmdj("iEj") or []) if isinstance(e, dict) and e.get("name")})
        strings = interesting_strings(r2.cmdj("izj") or [])
        level = "aaa" if size <= 4 << 20 else "aa"
        started = time.monotonic()
        r2.cmd(level, timeout=600)
        log(f"analysis ({level}) took {time.monotonic() - started:.0f} s")
        functions = [f for f in (r2.cmdj("aflj", timeout=120) or []) if isinstance(f, dict)]
        decompiled = []
        for fn in pick_functions(functions, wanted, max_functions):
            addr = _addr(fn)
            code, method = "", ""
            for method in ("pdg", "pdc"):
                try:
                    code = tidy_code(r2.cmd(f"{method} @ {addr:#x}", timeout=180))
                except ExtractError as exc:
                    warnings.append(f"{fn['name']}: {method} failed ({exc})")
                    r2 = R2(path)
                    r2.cmd(level, timeout=600)
                    code = ""
                if code and "Cannot find function" not in code and not code.startswith("ERROR"):
                    break
                code = ""
            if code:
                decompiled.append({"name": fn["name"], "addr": f"{addr:#x}", "size": _size(fn), "method": method,
                                   "code": code[:20000]})
        missing = [w for w in wanted if not any(d["name"] == w or d["name"].endswith("." + w) for d in decompiled)]
        if missing:
            warnings.append("requested functions not found or not decompiled: " + ", ".join(missing))
    finally:
        r2.close()
    info = {
        "file": path.name, "size_bytes": size, "sha256": sha256(path),
        "format": binfo.get("bintype") or core.get("format"), "class": binfo.get("class"),
        "arch": binfo.get("arch"), "bits": binfo.get("bits"), "machine": binfo.get("machine"),
        "os": binfo.get("os"), "language": binfo.get("lang"), "compiler": binfo.get("compiler"),
        "stripped": binfo.get("stripped"), "static": binfo.get("static"), "endian": binfo.get("endian"),
    }
    top = sorted((f for f in functions if not is_import(str(f.get("name", "")))), key=_size, reverse=True)[:50]
    return {
        "schema": SCHEMA, "info": info, "sections": sections, "imports": imports, "exports": exports[:2000],
        "strings": strings,
        "functions": {"count": len(functions),
                      "largest": [{"name": f.get("name"), "addr": f"{_addr(f):#x}", "size": _size(f)} for f in top
                                  if _addr(f) is not None]},
        "decompiled": decompiled, "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="https:// URL of the binary")
    src.add_argument("--file", type=Path, help="local path of the binary")
    parser.add_argument("--goal", default="", help="what the design should explain (max 1000 characters)")
    parser.add_argument("--functions", default="", help="comma-separated function names to decompile first")
    parser.add_argument("--max-functions", type=int, default=8)
    parser.add_argument("--max-mb", type=int, default=50, help="download size limit in MB")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    goal = " ".join(args.goal.split()) or DEFAULT_GOAL
    wanted = [w for w in (x.strip() for x in args.functions.split(",")) if re.fullmatch(r"[A-Za-z0-9_.$@]{1,128}", w)]
    try:
        if len(goal) > 1000:
            raise ExtractError("goal must be at most 1000 characters")
        if args.url:
            path = args.out.parent / "ear-binary"
            fetch(args.url, path, args.max_mb << 20)
        else:
            path = args.file
        extraction = extract(path, wanted, max(1, min(args.max_functions, 16)))
        extraction["source"] = {"url": args.url} if args.url else {"file": str(args.file)}
        sources = build_sources(extraction, goal)
        extraction["evidence_sources"] = [s["id"] for s in sources]
    except (ExtractError, OSError, urllib.error.URLError) as exc:
        (args.out / "extraction.json").write_text(json.dumps({"schema": SCHEMA, "error": str(exc)}, indent=1))
        print(f"extraction failed: {exc}", file=sys.stderr)
        return 1
    (args.out / "extraction.json").write_text(json.dumps(extraction, indent=1))
    (args.out / "evidence.json").write_text(json.dumps({"sources": sources}, indent=1, ensure_ascii=False))
    (args.out / "task.json").write_text(json.dumps({"sources": sources, "goal": goal}, ensure_ascii=False))
    print(f"{extraction['info']['format']} {extraction['info']['arch']}-{extraction['info']['bits']}: "
          f"{extraction['functions']['count']} functions, {len(extraction['decompiled'])} decompiled, "
          f"{len(sources)} evidence sources, task {task_bytes(goal, sources)} bytes")
    for warning in extraction["warnings"]:
        print("warning:", warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
