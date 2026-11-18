import os
import re
from pathlib import Path

from ear_document.config import Settings
from ear_document.contract import Issue

from .plan import storage_location


def value(name, fallback=""):
    return os.getenv("EAR_DATA_" + name) or os.getenv("EAR_" + name, fallback)


def credentials(role):
    key, secret = os.getenv("EAR_DATA_" + role + "_KEY_ID"), os.getenv("EAR_DATA_" + role + "_KEY")
    if key or secret:
        return key, secret
    return value("B2_KEY_ID"), value("B2_APPLICATION_KEY")


def storage():
    try:
        return storage_location(
            value("B2_REGION"), value("B2_BUCKET"), os.getenv("EAR_DATA_PREFIX", "data")
        )
    except ValueError:
        raise Issue("configuration") from None


def config():
    def number(name, default, low, high):
        n = int(os.getenv("EAR_DATA_" + name, str(default)))
        if not low <= n <= high:
            raise ValueError
        return n

    region, bucket, prefix = storage()
    repo = os.getenv("EAR_DATA_REPO", "Kaycee7000/ear")
    ref = os.getenv("EAR_DATA_REF", "main")
    if (
        not re.fullmatch(r"[\w.-]+/[\w.-]+", repo, re.ASCII)
        or not re.fullmatch(r"[\w./-]+", ref, re.ASCII)
        or ".." in repo + ref
    ):
        raise Issue("configuration")
    state = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local/state"))
    try:
        return Settings(
            Path(os.getenv("EAR_DATA_DB", state / "ear-data/jobs.sqlite3")).expanduser(),
            repo,
            ref,
            "data-tools.yml",
            region,
            bucket,
            prefix,
            number("ACTIVE_LIMIT", 1, 1, 5),
            number("DAILY_LIMIT", 10, 1, 50),
            number("MAX_WAIT", 20, 0, 300),
            number("TOOL_DEADLINE", 30, 3, 360),
            number("URL_TTL", 3600, 60, 86400),
        )
    except ValueError:
        raise Issue("configuration") from None
