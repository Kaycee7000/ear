"""Tests for ear_extract.py. The last test needs radare2 (run it inside the r2-ghidra-ci image)."""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ear_extract as ex  # noqa: E402

ID = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def fake_extraction(code_lines=400, functions=12):
    code = "\n".join(f"    local_{i} = helper_{i}(param_1, \"value {i}\");" for i in range(code_lines))
    return {
        "info": {"file": "demo", "format": "elf64", "arch": "x86", "bits": 64, "stripped": False},
        "sections": [{"name": ".text", "size": 4096, "perm": "-r-x"}],
        "imports": [f"func{i}" for i in range(300)],
        "exports": [],
        "strings": [f"/etc/demo/config-{i}.conf" for i in range(200)],
        "decompiled": [{"name": f"sym.function_{i}", "addr": hex(0x1000 + i), "size": 500, "method": "pdg",
                        "code": "int function_%d(void)\n{\n%s\n}" % (i, code)} for i in range(functions)],
    }


def check_task(goal, sources):
    assert 1 <= len(sources) <= ex.MAX_SOURCES
    assert len({s["id"] for s in sources}) == len(sources)
    for s in sources:
        assert ID.match(s["id"]), s["id"]
        assert 1 <= len(s["text"]) <= ex.SOURCE_LIMIT
    assert ex.task_bytes(goal, sources) <= ex.TASK_LIMIT - ex.TASK_MARGIN


def test_sources_fit_the_modeling_limits():
    for goal in ("short goal", "g" * 1000):
        sources = ex.build_sources(fake_extraction(), goal)
        check_task(goal, sources)
        assert [s["id"] for s in sources][:4] == ["binary", "build", "imports", "strings"]
        assert any(s["id"].startswith("fn-") for s in sources)


def test_startup_symbols_and_noise_are_left_out_of_evidence():
    extraction = fake_extraction(functions=1)
    extraction["info"].update(sha256="ab" * 32, endian="little")
    extraction["sections"] = [{"name": ".text"}, {"name": ".note.gnu.property"}, {"name": ".rela.plt"}]
    extraction["imports"] = ["__libc_start_main", "_ITM_registerTMCloneTable", "fopen", "__gmon_start__"]
    extraction["exports"] = ["_IO_stdin_used", "__TMC_END__", "_end"]
    sources = {s["id"]: s["text"] for s in ex.build_sources(extraction, "goal")}
    assert sources["binary"] == "file demo; elf64 x86 64-bit" and sources["build"] == "symbols not stripped"
    facts = sources["binary"] + sources["build"]
    assert "sha256" not in facts and ".note" not in facts and ".text" not in facts
    assert "\n" not in facts and "\n" not in sources["strings"]
    assert sources["imports"] == "fopen"
    assert "exports" not in sources


def test_fact_lines_can_be_quoted_whole():
    info = {"file": "a-rather-long-program-name-" * 3, "size_bytes": 123456789, "format": "pe", "arch": "x86",
            "bits": 64, "os": "windows", "language": "c++",
            "compiler": "GCC: (GNU) 13.2.1 20231205 (Red Hat 13.2.1-6) with a long vendor suffix " * 2,
            "stripped": True, "static": True}
    extraction = fake_extraction(functions=1)
    extraction["info"] = info
    sources = {s["id"]: s["text"] for s in ex.build_sources(extraction, "goal")}
    for sid in ("binary", "build"):
        assert 1 <= len(sources[sid]) <= ex.FACT_LINE < ex.QUOTE_LIMIT and "\n" not in sources[sid]
    assert sources["binary"].startswith("file a-rather-long") and sources["build"].startswith("built with GCC")


def test_unicode_and_escapes_are_counted_in_bytes():
    extraction = fake_extraction()
    extraction["decompiled"][0]["code"] = "\n".join("    puts(\"héllo \\\"wörld\\\"\");\t// ✓" for _ in range(500))
    goal = "Erklären Sie das Programm ✓"
    check_task(goal, ex.build_sources(extraction, goal))


def test_function_ids_are_sanitized_and_unique():
    extraction = fake_extraction(code_lines=5, functions=0)
    extraction["decompiled"] = [{"name": n, "addr": "0x1", "size": 1, "method": "pdg", "code": "x = 1;\n" * 80}
                                for n in ("sym.std::vector<int>::push_back", "sym.std::vector<int>::push_back",
                                          "fcn.00401000")]
    ids = [s["id"] for s in ex.build_sources(extraction, "goal") if s["id"].startswith("fn-")]
    assert len(ids) == 3 and len(set(ids)) == 3 and all(ID.match(i) for i in ids)


def test_declarations_are_dropped_but_logic_kept():
    code = ("void main(int argc,char **argv)\n\n{\n    FILE *pFVar1;\n    uint32_t uVar2;\n    uchar auStack_48 [16];\n"
            "    char *pcVar3;\n\n    pFVar1 = fopen(*argv,\"r\");\n    uVar2 = 0;\n    if (argc < 2) {\n"
            "        usage(1);\n    }\n    return;\n}")
    out = ex.without_declarations(code)
    assert "FILE *pFVar1;" not in out and "auStack_48" not in out and "char *pcVar3;" not in out
    assert "pFVar1 = fopen(*argv,\"r\");" in out and "uVar2 = 0;" in out and "usage(1);" in out


def test_several_functions_fit():
    sources = ex.build_sources(fake_extraction(), "goal")
    assert sum(s["id"].startswith("fn-") for s in sources) >= 2


def test_trim_cuts_at_line_boundaries():
    text = "line one\nline two\nline three"
    assert ex.trim(text, 100) == text
    assert ex.trim(text, 20) == "line one\nline two"
    assert ex.trim("Imported functions: fopen, fclose, getenv, strcmp", 40) == "Imported functions: fopen, fclose"


def test_pick_functions_order():
    fns = [{"name": "sym.imp.printf", "addr": 1, "size": 9999},
           {"name": "sym.helper", "addr": 2, "size": 50},
           {"name": "main", "addr": 3, "size": 20},
           {"name": "sym.big", "addr": 4, "size": 800},
           {"name": "sym.parse_args", "addr": 5, "size": 30},
           {"name": "sym.tiny", "addr": 6, "size": 4}]
    names = [f["name"] for f in ex.pick_functions(fns, ["parse_args"], 4)]
    assert names == ["sym.parse_args", "main", "sym.big", "sym.helper"]
    boiler = [{"name": "entry0", "addr": 7, "size": 900}, {"name": "sym.register_tm_clones", "addr": 8, "size": 700},
              {"name": "entry.fini0", "addr": 9, "size": 600}]
    assert [f["name"] for f in ex.pick_functions(fns + boiler, [], 3)] == ["main", "sym.big", "sym.helper"]
    stripped = [{"name": "entry0", "addr": 7, "size": 40}, {"name": "fcn.00401000", "addr": 8, "size": 300}]
    assert [f["name"] for f in ex.pick_functions(stripped, [], 3)] == ["entry0", "fcn.00401000"]


def test_strings_are_filtered_and_ranked():
    entries = [{"string": s} for s in ["ab", "Usage: demo [options]", "%s: error %d", "\x01\x02junk", "zzzzzz",
                                       "/usr/lib/demo.so", "Usage: demo [options]", "!!!!@@@@####"]]
    out = ex.interesting_strings(entries)
    assert out[0] == "%s: error %d"
    assert "ab" not in out and "!!!!@@@@####" not in out and out.count("Usage: demo [options]") == 1


def test_bad_url_is_refused(tmp_path):
    with pytest.raises(ex.ExtractError):
        ex.fetch("http://example.org/a", tmp_path / "a", 10)
    with pytest.raises(ex.ExtractError):
        ex.fetch("file:///etc/passwd", tmp_path / "a", 10)


@pytest.mark.skipif(not shutil.which("r2"), reason="needs radare2 (run inside the r2-ghidra-ci image)")
def test_real_binary(tmp_path):
    target = Path(shutil.which("base64") or "/usr/bin/true")
    assert ex.main(["--file", str(target), "--functions", "main", "--out", str(tmp_path)]) == 0
    extraction = json.loads((tmp_path / "extraction.json").read_text())
    task = json.loads((tmp_path / "task.json").read_text())
    assert extraction["info"]["format"] and extraction["functions"]["count"] > 0
    assert extraction["decompiled"], extraction["warnings"]
    check_task(task["goal"], task["sources"])
