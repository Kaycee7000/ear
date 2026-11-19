"""Offline phases inside the toolbox container (no network, no credentials).

    python -m worker.files unpack    /input/source.tar.gz -> /work/src (+ /work/home), writes /output/unpack.json
    python -m worker.files collect   copies the plan's artifact globs from /work/src to /output/artifacts
"""

import json
import os
import shutil
import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from ear_code.plan import MAX_ARTIFACT, MAX_ARTIFACTS, MAX_MEMBERS, MAX_UNPACKED, CodeError, Request

INPUT, WORK, OUTPUT = Path("/input"), Path("/work"), Path("/output")


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or name.startswith("./.."):
        raise CodeError("source_invalid")
    return path


def unpack(archive, dest):
    """Extract regular files and folders only, within the size and count limits."""
    dest.mkdir(parents=True, exist_ok=True)
    files = total = links = 0
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = []
            for member in tar:
                if len(members) >= MAX_MEMBERS:
                    raise CodeError("limit_exceeded")
                safe_name(member.name)
                if member.issym() or member.islnk():
                    links += 1
                    continue
                if not (member.isfile() or member.isdir()):
                    raise CodeError("source_invalid")
                total += member.size
                if total > MAX_UNPACKED:
                    raise CodeError("limit_exceeded")
                files += member.isfile()
                members.append(member)
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, OSError, EOFError):
        raise CodeError("source_invalid") from None
    return {"files": files, "bytes": total, "links_skipped": links}


def collect(src, out, patterns):
    """Copy matched files (folders become .zip) into `out`. Returns the names, in order."""
    out.mkdir(parents=True, exist_ok=True)
    root = src.resolve()
    names, total = [], 0
    for pattern in patterns:
        for match in sorted(src.glob(pattern)):
            if len(names) >= 8:
                return names
            if match.is_symlink():
                continue
            real = match.resolve()
            if root not in real.parents:
                continue
            base = "__".join(real.relative_to(root).parts)
            if real.is_dir():
                name = base + ".zip"
                target = out / name
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in sorted(real.rglob("*")):
                        if path.is_symlink() or not path.is_file():
                            continue
                        archive.write(path, path.relative_to(real.parent).as_posix())
                        if target.stat().st_size > MAX_ARTIFACT:
                            break
            elif stat.S_ISREG(real.lstat().st_mode):
                name = base
                target = out / name
                shutil.copyfile(real, target, follow_symlinks=False)
            else:
                continue
            size = target.stat().st_size
            if name in names or size > MAX_ARTIFACT or total + size > MAX_ARTIFACTS:
                target.unlink()
                continue
            total += size
            names.append(name)
    return names


def main():
    mode = sys.argv[1]
    try:
        request = Request.model_validate_json((INPUT / "request.json").read_bytes())
        if mode == "unpack":
            info = unpack(INPUT / "source.tar.gz", WORK / "src")
            (WORK / "home").mkdir(exist_ok=True)
            (OUTPUT / "unpack.json").write_text(json.dumps(info))
        elif mode == "collect":
            patterns = request.plan.steps.artifacts if request.plan.steps else []
            names = collect(WORK / "src", OUTPUT / "artifacts", patterns)
            (OUTPUT / "artifacts.json").write_text(json.dumps(names))
        else:
            raise ValueError(mode)
        return 0
    except CodeError as exc:
        print(exc.code)
        return 1
    except Exception:
        print("worker_failed")
        return 1


if __name__ == "__main__":
    os.umask(0o022)
    sys.exit(main())
