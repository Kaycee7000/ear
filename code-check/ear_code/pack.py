"""Pack a project folder into a code-check archive. Standard library only (Python 3.10+), so it can be copied
to any machine that holds the project and run there:

    python3 pack.py PROJECT_DIR OUT.tar.gz [--base GIT_REF]

The archive holds the project files (git's tracked and untracked-but-not-ignored files when the folder is a
git repository; otherwise everything except dependency and build folders) plus `.ear/diff.patch`, the change
to review: the working tree against GIT_REF (default HEAD), or the last commit when nothing is uncommitted.
The archive is reproducible: the same files give the same bytes and the same SHA-256.
Prints one JSON line: {"path", "sha256", "byte_size", "files", "diff_bytes", "diff_of"}.
"""

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

SKIP_DIRS = {
    ".git", ".ear", "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".next", ".nuxt", ".cache", "dist", "build", "target", "coverage", ".gradle",
}
MAX_FILE = 16 * 1024**2
MAX_TOTAL = 256 * 1024**2
MAX_COMPRESSED = 64 * 1024**2
MAX_FILES = 20_000
MAX_DIFF = 400 * 1024
MAX_NEW_FILE_DIFF = 200 * 1024


class PackError(Exception):
    pass


def git(root, *args, check=True):
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, timeout=60,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
    )
    if check and proc.returncode:
        raise PackError("git " + args[0] + " failed: " + proc.stderr.decode(errors="replace")[:300])
    return proc


def is_git(root):
    proc = git(root, "rev-parse", "--show-toplevel", check=False)
    return proc.returncode == 0 and Path(proc.stdout.decode().strip()).resolve() == root.resolve()


def listed(root):
    if is_git(root):
        out = git(root, "ls-files", "-co", "--exclude-standard", "-z").stdout.decode("utf-8", "surrogateescape")
        return sorted({n for n in out.split("\0") if n and n_ok(n)})
    names = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in files:
            names.append(str((Path(folder) / name).relative_to(root)).replace(os.sep, "/"))
    return sorted(names)


def new_file_diff(root, name):
    path = root / name
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > MAX_NEW_FILE_DIFF or b"\0" in data[:8192]:
        return f"diff --git a/{name} b/{name}\nnew file (binary or too large, not shown)\n"
    text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    body = "".join("+" + line + "\n" for line in lines)
    return f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def change(root, names, base):
    """The diff to review and a short description of what it covers."""
    if not is_git(root):
        return "".join(new_file_diff(root, n) for n in names), "all files (not a git repository)"
    has_head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    if not has_head:
        return "".join(new_file_diff(root, n) for n in names), "all files (no commits yet)"
    ref = base or "HEAD"
    if git(root, "rev-parse", "--verify", "-q", ref + "^{commit}", check=False).returncode:
        raise PackError(f"unknown git ref {ref!r}")
    tracked = git(root, "diff", "--no-color", "--no-ext-diff", "-M", ref, "--").stdout.decode("utf-8", "replace")
    untracked = git(root, "ls-files", "-o", "--exclude-standard", "-z").stdout.decode("utf-8", "surrogateescape")
    added = "".join(new_file_diff(root, n) for n in sorted(u for u in untracked.split("\0") if u and n_ok(u)))
    diff = tracked + added
    if diff.strip() or base:
        return diff, f"working tree against {ref}" + (" (uncommitted changes)" if not base else "")
    if git(root, "rev-parse", "--verify", "-q", "HEAD~1", check=False).returncode == 0:
        last = git(root, "diff", "--no-color", "--no-ext-diff", "-M", "HEAD~1", "HEAD", "--").stdout
        return last.decode("utf-8", "replace"), "the last commit (no uncommitted changes)"
    show = git(root, "show", "--no-color", "--format=", "HEAD").stdout.decode("utf-8", "replace")
    return show, "the only commit (no uncommitted changes)"


def n_ok(name):
    parts = Path(name).parts
    return parts[0] != ".ear" and not set(parts[:-1]) & SKIP_DIRS


def pack(root, out, base=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise PackError("project folder not found")
    names = listed(root)
    if not names:
        raise PackError("the project folder has no files")
    if len(names) > MAX_FILES:
        raise PackError(f"too many files ({len(names)} > {MAX_FILES})")
    diff, diff_of = change(root, names, base)
    diff_bytes = diff.encode("utf-8", "replace")
    if len(diff_bytes) > MAX_DIFF:
        diff_bytes = diff_bytes[:MAX_DIFF] + b"\n[diff truncated by pack.py]\n"
    raw = io.BytesIO()
    total, count = 0, 0
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime, info.uid, info.gid = len(data), mode, 0, 0, 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))

        for name in names:
            path = root / name
            if path.is_symlink() or not path.is_file():
                continue  # links and special files are left out
            size = path.stat().st_size
            if size > MAX_FILE:
                raise PackError(f"{name} is larger than {MAX_FILE // 1024**2} MiB")
            total += size
            if total > MAX_TOTAL:
                raise PackError("the project is larger than 256 MiB")
            add(name, path.read_bytes(), 0o755 if os.access(path, os.X_OK) else 0o644)
            count += 1
        add(".ear/diff.patch", diff_bytes)
        add(".ear/diff_of.txt", diff_of.encode())
    data = gzip.compress(raw.getvalue(), compresslevel=6, mtime=0)
    if len(data) > MAX_COMPRESSED:
        raise PackError("the compressed project is larger than 64 MiB")
    Path(out).write_bytes(data)
    return {"path": str(out), "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data),
            "files": count, "diff_bytes": len(diff_bytes), "diff_of": diff_of}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Pack a project for an EAR code check.")
    parser.add_argument("project")
    parser.add_argument("out")
    parser.add_argument("--base", help="git ref to diff against (default: HEAD)")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(pack(args.project, args.out, args.base)))
        return 0
    except (PackError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)[:300]}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
