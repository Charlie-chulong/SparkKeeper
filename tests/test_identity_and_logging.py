from __future__ import annotations

from dataclasses import replace

import pytest

from spark_keeper.automation.douyin_chat import DouyinChatAdapter, _canonical_http_url
from spark_keeper.logging_safe import format_error, safe_url, target_label
from spark_keeper.models import Target


def test_candidate_prefers_canonical_profile_identity() -> None:
    candidate = DouyinChatAdapter._candidate_from_raw(
        {
            "name": " 测试好友 ",
            "profileUrl": "https://www.douyin.com/user/abc/?token=secret",
            "avatarUrl": "https://example.test/avatar.png?x=1",
            "lineCount": 2,
        }
    )
    assert candidate.display_name == "测试好友"
    assert candidate.profile_url == "https://www.douyin.com/user/abc"
    assert candidate.evidence["identity_strength"] == "strong"
    target = Target(
        id=1,
        stable_key=candidate.stable_key,
        display_name=candidate.display_name,
        profile_url=candidate.profile_url,
        avatar_url=candidate.avatar_url,
        search_query="测试好友",
        evidence=candidate.evidence,
        enabled=True,
        confirmed_at="now",
    )
    assert DouyinChatAdapter._identity_matches(candidate, target)


def test_safe_url_strips_query_and_fragment() -> None:
    assert (
        safe_url("https://www.douyin.com/chat?token=secret#part") == "https://www.douyin.com/chat"
    )
    assert _canonical_http_url("/user/abc/?token=secret") == "https://www.douyin.com/user/abc"


@pytest.mark.parametrize("explicit_cause", [False, True])
def test_error_details_preserve_traceback_chain_notes_and_original_text(explicit_cause) -> None:
    original = "storage_state Cookie Authorization diagnostic\n" + "完整错误" * 100
    try:
        try:
            raise ValueError(original)
        except ValueError as cause:
            cause.add_note("查找诊断：phase=search; candidates=0")
            error = RuntimeError("目标查找失败")
            error.add_note("search_query=真实好友名称")
            if explicit_cause:
                raise error from cause
            raise error
    except RuntimeError as exc:
        detail = format_error(exc)

    assert f"ValueError: {original}" in detail
    assert "RuntimeError: 目标查找失败" in detail
    assert "查找诊断：phase=search; candidates=0" in detail
    assert "search_query=真实好友名称" in detail
    assert "Traceback (most recent call last)" in detail
    assert "test_error_details_preserve_traceback_chain_notes_and_original_text" in detail
    assert ("direct cause" if explicit_cause else "During handling") in detail


def test_target_labels_preserve_names_and_distinguish_same_name_targets() -> None:
    first = Target(
        id=41,
        stable_key="friend-41",
        display_name="真实好友名称",
        profile_url="",
        avatar_url="",
        search_query="真实好友名称",
        evidence={},
        enabled=True,
        confirmed_at="now",
    )
    second = replace(first, id=42, stable_key="friend-42")

    assert target_label(first) == "真实好友名称（ID: 41）"
    assert target_label(second) == "真实好友名称（ID: 42）"
    assert target_label(first) != target_label(second)


@pytest.mark.parametrize(
    "raw",
    [
        {"douyinId": "obsolete-number"},
        {"dataId": "arbitrary-dom-identity"},
        {"avatarUrl": "https://example.test/same-avatar.png"},
        {"conversationId": "unverified-conversation", "conversationType": 2},
        {"conversationId": "unverified-conversation"},
    ],
)
def test_numbers_dom_ids_and_visual_identity_cannot_confirm(raw) -> None:
    candidate = DouyinChatAdapter._candidate_from_raw({"name": "同名好友", **raw})
    assert not candidate.stable_key
    assert candidate.evidence["identity_strength"] == "weak"
    assert "douyin_id" not in candidate.evidence
    assert "data_id_digest" not in candidate.evidence
    assert "obsolete-number" not in repr(candidate)
    target = Target(
        id=1,
        stable_key=candidate.stable_key,
        display_name=candidate.display_name,
        profile_url="",
        avatar_url=candidate.avatar_url,
        search_query=candidate.display_name,
        evidence=candidate.evidence,
        enabled=True,
        confirmed_at="fixture-time",
    )
    assert not DouyinChatAdapter._identity_matches(candidate, target)


def test_profiles_override_conversation_digest_and_stable_key_claims() -> None:
    candidate = DouyinChatAdapter._candidate_from_raw(
        {
            "name": "同名好友",
            "profileUrl": "https://www.douyin.com/user/current",
            "conversationId": "same-sdk-conversation",
            "conversationType": 1,
        }
    )
    target = Target(
        id=1,
        stable_key=candidate.stable_key,
        display_name=candidate.display_name,
        profile_url="https://www.douyin.com/user/different",
        avatar_url=candidate.avatar_url,
        search_query=candidate.display_name,
        evidence=candidate.evidence,
        enabled=True,
        confirmed_at="fixture-time",
    )
    assert not DouyinChatAdapter._identity_matches(candidate, target)
    assert DouyinChatAdapter._identity_matches(
        candidate, replace(target, profile_url="https://douyin.com/user/current/?tracking=ignored")
    )
    legacy = replace(
        target,
        profile_url="",
        evidence={
            "scan_conversation_digest": candidate.evidence["conversation_digest"],
            "identity_strength": "strong",
        },
    )
    assert not DouyinChatAdapter._identity_matches(candidate, legacy)
