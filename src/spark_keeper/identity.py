from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def canonical_profile_url(value: str) -> str:
    """Return the stable Douyin profile URL, never a search or self URL."""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or parsed.netloc.lower() not in {
            "douyin.com",
            "www.douyin.com",
        }:
            return ""
        match = re.fullmatch(r"/user/([A-Za-z0-9_-]+)/?", parsed.path)
        if match is None or match[1].lower() == "self":
            return ""
        return f"https://www.douyin.com/user/{match[1]}"
    except (AttributeError, ValueError):
        return ""


def _conversation_digest(evidence: Mapping[str, Any]) -> str:
    digest = evidence.get("conversation_digest")
    if (
        evidence.get("identity_source") == "sdk_single_conversation"
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-fA-F]{64}", digest)
    ):
        return digest.lower()
    return ""


def identity_key(profile_url: str, evidence: Mapping[str, Any]) -> str:
    profile = canonical_profile_url(profile_url)
    if profile:
        return hashlib.sha256(f"profile:{profile}".encode()).hexdigest()
    digest = _conversation_digest(evidence)
    return f"sdk-conversation:{digest}" if digest else ""


def identities_match(
    left_profile: str,
    left_evidence: Mapping[str, Any],
    right_profile: str,
    right_evidence: Mapping[str, Any],
) -> bool:
    left = canonical_profile_url(left_profile)
    right = canonical_profile_url(right_profile)
    if left and right:
        return left == right
    digest = _conversation_digest(left_evidence)
    return bool(digest and digest == _conversation_digest(right_evidence))
