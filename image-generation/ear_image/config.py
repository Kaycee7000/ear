import os
import re
from dataclasses import dataclass
from pathlib import Path

from .contract import Issue


def setting(name, default=""):
    return os.environ.get("EAR_IMAGE_" + name, default)


def number(name, default, low, high):
    try:
        value = int(setting(name, str(default)))
        if low <= value <= high:
            return value
    except ValueError:
        pass
    raise Issue("configuration")


@dataclass(frozen=True)
class Settings:
    db: Path
    repo: str
    ref: str
    workflow: str
    region: str
    bucket: str
    prefix: str
    active: int
    daily: int
    wait: int
    deadline: int
    ttl: int

    @classmethod
    def load(cls):
        repo = setting("REPO", "Kaycee7000/ear")
        ref = setting("REF", "main")
        workflow = setting("WORKFLOW", "generate-image.yml")
        region, bucket, prefix = storage_settings()
        for value, pattern in [
            (repo, r"[\w.-]+/[\w.-]+"),
            (ref, r"[\w./-]+"),
            (workflow, r"[\w-]+\.ya?ml"),
        ]:
            if not re.fullmatch(pattern, value, re.ASCII) or ".." in value:
                raise Issue("configuration")
        state = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
        return cls(
            Path(setting("DB", str(state / "ear-image/jobs.sqlite3"))).expanduser(),
            repo,
            ref,
            workflow,
            region,
            bucket,
            prefix,
            number("ACTIVE_LIMIT", 1, 1, 5),
            number("DAILY_LIMIT", 10, 1, 50),
            number("MAX_WAIT", 20, 0, 300),
            number("TOOL_DEADLINE", 30, 3, 360),
            number("URL_TTL", 3600, 60, 86400),
        )

    @property
    def target(self):
        return "|".join((self.repo, self.ref, self.workflow, self.region, self.bucket, self.prefix))


def storage_settings():
    region, bucket, prefix = setting("B2_REGION"), setting("B2_BUCKET"), setting("PREFIX", "images")
    if (
        not re.fullmatch(r"[a-z0-9-]+", region)
        or not re.fullmatch(r"[A-Za-z0-9-]{6,63}", bucket)
        or not re.fullmatch(r"[\w-]+(?:/[\w-]+)*", prefix, re.ASCII)
        or len(prefix) > 180
    ):
        raise Issue("configuration")
    return region, bucket, prefix
