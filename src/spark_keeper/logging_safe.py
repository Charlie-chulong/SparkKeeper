from __future__ import annotations

import hashlib
import traceback
from urllib.parse import urlsplit, urlunsplit

from .models import Target


def target_label(target: Target) -> str:
    return f"{target.display_name}（ID: {target.id}）"


def safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def stable_digest(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def format_error(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))
