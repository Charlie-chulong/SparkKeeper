from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any

from .identity import identity_key


class ThemeMode(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class MessageKind(str, Enum):
    TEXT = "text"
    SPARK_STICKER = "spark_sticker"


class BatchMode(StrEnum):
    VALIDATION = "validation"
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    MISSED = "missed"


class BatchStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class AttemptStatus(StrEnum):
    SENDING = "sending"
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"


class ErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    HUMAN_VERIFICATION_REQUIRED = "human_verification_required"
    PAGE_NOT_READY = "page_not_ready"
    PAGE_STRUCTURE_CHANGED = "page_structure_changed"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_AMBIGUOUS = "target_ambiguous"
    TARGET_IDENTITY_MISMATCH = "target_identity_mismatch"
    COMPOSER_UNAVAILABLE = "composer_unavailable"
    SEND_FAILED = "send_failed"
    SEND_UNKNOWN = "send_unknown"
    ALREADY_RUNNING = "already_running"
    DUPLICATE_BLOCKED = "duplicate_blocked"
    LOGIN_STATE_UNAVAILABLE = "login_state_unavailable"
    CONFIGURATION_INVALID = "configuration_invalid"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class Account:
    platform_user_id: str
    display_name: str
    logged_in_at: str


@dataclass(frozen=True, slots=True)
class FriendCandidate:
    stable_key: str
    display_name: str
    profile_url: str = ""
    avatar_url: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


class SparkState(StrEnum):
    ACTIVE = "active"
    RECOVER = "recover"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class SparkScanStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SparkContact:
    candidate: FriendCandidate
    spark_state: SparkState
    reason: str
    is_group: bool = False

    @property
    def importable(self) -> bool:
        candidate = self.candidate
        return (
            not self.is_group
            and candidate.evidence.get("chat_type") == "single"
            and candidate.evidence.get("identity_strength") == "strong"
            and not candidate.evidence.get("identity_conflict")
            and bool(candidate.stable_key)
            and bool(identity_key(candidate.profile_url, candidate.evidence))
        )


@dataclass(frozen=True, slots=True)
class SparkScanResult:
    account_key: str
    account_logged_in_at: str
    scanned_at: str
    status: SparkScanStatus
    contacts: tuple[SparkContact, ...]
    scanned_count: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Target:
    id: int
    stable_key: str
    display_name: str
    profile_url: str
    avatar_url: str
    search_query: str
    evidence: dict[str, Any]
    enabled: bool
    confirmed_at: str


@dataclass(frozen=True, slots=True)
class Plan:
    enabled: bool
    send_time: str
    message_text: str
    confirmed_at: str | None
    updated_at: str
    delay_min_seconds: int = 3
    delay_max_seconds: int = 8
    message_kind: MessageKind = MessageKind.TEXT


@dataclass(frozen=True, slots=True)
class TargetResult:
    target_id: int
    target_alias: str
    status: AttemptStatus
    error_code: ErrorCode | None = None
    detail: str = ""
    attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: str
    mode: BatchMode
    status: BatchStatus
    results: tuple[TargetResult, ...]
    error_code: ErrorCode | None = None

    @property
    def counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in AttemptStatus}
        for result in self.results:
            counts[result.status.value] += 1
        return counts
