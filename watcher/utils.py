from __future__ import annotations

import hashlib
import re
from datetime import datetime


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_key(value: str) -> str:
    value = (value or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", value)


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

