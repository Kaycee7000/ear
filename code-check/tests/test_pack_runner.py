import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import runner  # noqa: E402
from ear_code.pack import PackError, pack  # noqa: E402


def git(folder, *args):
    subprocess.run(["git", "-C", str(folder), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid"})


def members(path):
    with tarfile.open(path, "r:gz") as tar:
        return {m.name: tar.extractfile(m).read() if m.isfile() else None for m in tar}


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "proj"
        (self.root / "src").mkdir(parents=True)
        (self.root / "node_modules" / "x").mkdir(parents=True)
        (self.root / "src" / "a.js").write_text("export const a = 1;\n")
        (self.root / "node_modules" / "x" / "i.js").write_text("ignored")

    def test_plain_folder_is_reproducible_and_skips_dependencies(self):
        out1, out2 = Path(self.tmp.name) / "1.tgz", Path(self.tmp.name) / "2.tgz"
        first, second = pack(self.root, out1), pack(self.root, out2)
        self.assertEqual(first["sha256"], second["sha256"])
        names = members(out1)
        self.assertIn("src/a.js", names)
        self.assertNotIn("node_modules/x/i.js", names)
        self.assertIn("+export const a = 1;", names[".ear/diff.patch"].decode())
        self.assertIn("not a git repository", names[".ear/diff_of.txt"].decode())

    def test_git_diff_covers_uncommitted_and_new_files(self):
        git(self.root, "init", "-q")
        (self.root / ".gitignore").write_text("node_modules/\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "one")
        (self.root / "src" / "a.js").write_text("export const a = 2;\n")
        (self.root / "src" / "b.js").write_text("export const b = 3;\n")
        info = pack(self.root, Path(self.tmp.name) / "g.tgz")
        diff = members(info["path"])[".ear/diff.patch"].decode()
        self.assertIn("-export const a = 1;", diff)
        self.assertIn("+export const b = 3;", diff)
        self.assertIn("uncommitted", info["diff_of"])
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "two")
        info = pack(self.root, Path(self.tmp.name) / "h.tgz")
        self.assertIn("last commit", info["diff_of"])
        self.assertIn("+export const b = 3;", members(info["path"])[".ear/diff.patch"].decode())
        with self.assertRaises(PackError):
            pack(self.root, Path(self.tmp.name) / "i.tgz", "no-such-ref")


class RunnerHelperTests(unittest.TestCase):
    def test_clip_tail_strips_colour_and_keeps_the_end(self):
        data = b"".join(f"line {i}\n".encode() for i in range(100)) + b"\x1b[31mnot ok 1\x1b[0m\n"
        tail = runner.clip_tail(data)
        self.assertTrue(tail.endswith("not ok 1"))
        self.assertNotIn("line 10\n", tail)

    def test_clip_diff_keeps_whole_files_and_names_the_rest(self):
        diff = "diff --git a/a b/a\n" + "+x\n" * 10 + "diff --git a/b b/b\n" + "+y\n" * 5000
        clipped = runner.clip_diff(diff, 200)
        self.assertIn("diff --git a/a", clipped)
        self.assertIn("not shown", clipped)
        self.assertIn("a/b b/b", clipped)

    def test_pick_files_follows_relative_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "test").mkdir()
            (src / "src").mkdir()
            (src / "test" / "cart.test.js").write_text('import { total } from "../src/cart.js";\n')
            (src / "src" / "cart.js").write_text("export function total() {}\n")
            log = "not ok 1 - total\n  at file:///work/src/test/cart.test.js:7:10\n"
            picked = [name for name, _ in runner.pick_files(src, log)]
            self.assertEqual(picked, ["test/cart.test.js", "src/cart.js"])
            self.assertIsNone(runner.resolve_import(src, "test/cart.test.js", "../../etc/passwd"))

    def test_text_clips_and_report_renders(self):
        self.assertEqual(runner.text("a\x00b  c", 10), "ab c")
        self.assertTrue(runner.text("x" * 50, 10).endswith("…"))
        summary = {"outcome": "failed", "artifacts": [], "review": None, "model": None,
                   "steps": [{"name": "test", "command": "npm test", "exit_code": 1, "seconds": 1.0, "tail": "boom"}],
                   "analysis": {"summary": "s", "what_failed": "w", "why": "y", "fix": ["f"], "files": ["a"]}}
        body = runner.report({"project": "cart", "plan": {"mode": "test"}}, summary, {"files": 3})
        self.assertIn("## test: `npm test`", body)
        self.assertIn("**Why:** y", body)


if __name__ == "__main__":
    unittest.main()
