import os
import resource
import shutil
from pathlib import Path

GIB = 1024**3
CGROUP = Path("/sys/fs/cgroup")


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def snapshot(path):
    info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    available = int(info["MemAvailable"].split()[0]) * 1024
    total = int(info["MemTotal"].split()[0]) * 1024
    limit, used = read(CGROUP / "memory.max"), read(CGROUP / "memory.current")
    if not limit:
        limit = read(CGROUP / "memory/memory.limit_in_bytes")
        used = read(CGROUP / "memory/memory.usage_in_bytes")
    stats = dict(line.split() for line in read(CGROUP / "memory.stat").splitlines())
    reclaimable = int(stats.get("inactive_file", "0"))
    if limit.isdigit() and used.isdigit():
        total = min(total, int(limit))
        available = min(available, max(0, int(limit) - int(used) + reclaimable))
    quota = read(CGROUP / "cpu.max").split()
    if not quota:
        quota = [read(CGROUP / "cpu/cpu.cfs_quota_us"), read(CGROUP / "cpu/cpu.cfs_period_us")]
    cores = len(os.sched_getaffinity(0))
    millicpus = cores * 1000
    if len(quota) == 2 and all(x.isdecimal() for x in quota) and int(quota[1]):
        millicpus = min(millicpus, max(1, int(quota[0]) * 1000 // int(quota[1])))
    disk = shutil.disk_usage(path)
    return {
        "ram_limit_bytes": total,
        "ram_available_bytes": available,
        "cpu_millicores": millicpus,
        "cpu_threads": max(1, min(cores, millicpus // 1000)),
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
    }


def peak():
    rss = max(
        resource.getrusage(kind).ru_maxrss for kind in (resource.RUSAGE_CHILDREN, resource.RUSAGE_SELF)
    )
    values = {"peak_worker_rss_bytes": int(rss * 1024)}
    container = read(CGROUP / "memory.peak")
    if container.isdecimal():
        values["peak_cgroup_memory_bytes"] = int(container)
    return values
