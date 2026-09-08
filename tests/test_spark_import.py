from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from spark_keeper.automation.spark_scan import _contact
from spark_keeper.database import Database
from spark_keeper.models import (
    FriendCandidate,
    SparkContact,
    SparkScanResult,
    SparkScanStatus,
    SparkState,
)


def contact(identity: str, name: str = "同名好友") -> SparkContact:
    profile = f"https://www.douyin.com/user/{identity}"
    return SparkContact(
        candidate=FriendCandidate(
            stable_key=hashlib.sha256(f"profile:{profile}".encode()).hexdigest(),
            display_name=name,
            profile_url=profile,
            evidence={"identity_strength": "strong", "chat_type": "single"},
        ),
        spark_state=SparkState.ACTIVE,
        reason="专用标识明确显示有效火花",
    )


def scan_result(database: Database, *contacts: SparkContact) -> SparkScanResult:
    account = database.get_account()
    assert account is not None
    return SparkScanResult(
        account_key=account.platform_user_id,
        account_logged_in_at=account.logged_in_at,
        scanned_at="2026-09-07T09:00:00+08:00",
        status=SparkScanStatus.COMPLETE,
        contacts=contacts,
        scanned_count=len(contacts),
    )


@pytest.fixture
def database(tmp_path) -> Database:
    result = Database(tmp_path / "state.sqlite3")
    result.save_account("scan-account", "测试账号")
    return result


def test_import_same_name_distinct_identities_disabled_and_idempotent(database) -> None:
    first, second = contact("first"), contact("second")
    scan = scan_result(database, first, second)
    keys = [first.candidate.stable_key, second.candidate.stable_key]

    assert database.import_spark_contacts(scan, keys + keys) == (2, 0)
    targets = database.list_targets()
    assert len(targets) == 2
    assert not any(target.enabled for target in targets)
    assert {target.stable_key for target in targets} == set(keys)
    assert all(
        target.evidence["spark_scan"]["observed_at"] == scan.scanned_at for target in targets
    )
    assert database.import_spark_contacts(scan, keys) == (0, 2)
    assert database.list_targets() == targets
    assert database.count_rows("send_attempts") == 0
    assert database.count_rows("batch_runs") == 0


@pytest.mark.parametrize("source", ["badge", "metadata"])
def test_all_spark_states_of_reliable_single_chats_import_on_selection(database, source) -> None:
    contacts = []
    for state, badge in [
        (2, "灰色有效火花"),
        (3, "火花恢复中"),
        (4, "火花已熄灭"),
        (9, ""),
    ]:
        contacts.append(
            _contact(
                {
                    "name": "同名好友",
                    "profiles": [f"https://www.douyin.com/user/state-{state}"],
                    "ids": [],
                    "dataId": "",
                    "types": ["single"],
                    "badges": [badge] if source == "badge" else [],
                    "flame": (
                        {"state": state, "start": 1799999000, "end": 1800001000}
                        if source == "metadata"
                        else None
                    ),
                    "flameVisible": source == "metadata",
                },
                state,
            )
        )
    assert [item.spark_state for item in contacts] == [
        SparkState.ACTIVE,
        SparkState.RECOVER,
        SparkState.INACTIVE,
        SparkState.UNKNOWN,
    ]
    assert all(item.importable for item in contacts)
    unselected = contact("unselected")
    scan = scan_result(database, *contacts, unselected)
    keys = [item.candidate.stable_key for item in contacts]
    assert database.import_spark_contacts(scan, keys) == (4, 0)
    targets = database.list_targets()
    assert len(targets) == 4
    assert {target.stable_key for target in targets} == set(keys)
    assert not any(target.enabled for target in targets)
    assert {target.evidence["spark_scan"]["state"] for target in targets} == {
        state.value for state in SparkState
    }
    assert database.import_spark_contacts(scan, keys) == (0, 4)
    assert database.list_targets() == targets
    assert database.count_rows("send_attempts") == 0
    assert database.count_rows("batch_runs") == 0


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("state", list(SparkState))
def test_import_preserves_existing_record_including_legacy_identity_key(
    database, enabled, state
) -> None:
    found = replace(contact("existing", "当前名称"), spark_state=state)
    legacy = replace(found.candidate, stable_key="legacy-key", display_name="原有名称")
    saved = database.add_target(legacy, "原有搜索词")
    database.set_target_enabled(saved.id, enabled)
    before = database.get_target(saved.id)

    assert database.import_spark_contacts(
        scan_result(database, found), [found.candidate.stable_key]
    ) == (0, 1)
    assert database.get_target(saved.id) == before
    assert len(database.list_targets()) == 1


@pytest.mark.parametrize("change", ["logout", "other-account", "new-session"])
def test_import_rejects_changed_account_or_login(database, change) -> None:
    found = contact("friend")
    scan = scan_result(database, found)
    if change == "logout":
        database.clear_account()
    elif change == "other-account":
        database.save_account("different-account", "另一个账号")
    else:
        with database.connect(immediate=True) as connection:
            connection.execute("UPDATE account SET logged_in_at = ?", ("new-login-session",))

    with pytest.raises(ValueError, match="重新扫描"):
        database.import_spark_contacts(scan, [found.candidate.stable_key])
    assert database.list_targets() == []


@pytest.mark.parametrize("state", list(SparkState))
@pytest.mark.parametrize(
    "unsafe",
    [
        "group",
        "group-type",
        "weak",
        "unknown-type",
        "identity-conflict",
        "missing-key",
        "missing-identity",
        "foreign-profile",
        "self-profile",
        "invalid-id",
    ],
)
def test_import_rejects_unsafe_identity_selection_atomically(database, unsafe, state) -> None:
    first = contact("good")
    other = replace(contact("unsafe"), spark_state=state)
    candidate = other.candidate
    evidence = dict(candidate.evidence)
    if unsafe == "group":
        other = replace(other, is_group=True)
    elif unsafe == "group-type":
        evidence["chat_type"] = "group"
    elif unsafe == "weak":
        evidence["identity_strength"] = "weak"
    elif unsafe == "unknown-type":
        evidence["chat_type"] = "unknown"
    elif unsafe == "identity-conflict":
        evidence["identity_conflict"] = True
    elif unsafe == "missing-key":
        candidate = replace(candidate, stable_key="")
    elif unsafe == "missing-identity":
        candidate = replace(candidate, profile_url="")
    elif unsafe == "foreign-profile":
        candidate = replace(candidate, profile_url="https://evil.example/user/unsafe")
    elif unsafe == "self-profile":
        candidate = replace(candidate, profile_url="https://www.douyin.com/user/self")
    else:
        candidate = replace(candidate, profile_url="", douyin_id="invalid id")
    other = replace(other, candidate=replace(candidate, evidence=evidence))
    assert not other.importable

    with pytest.raises(ValueError, match="不能导入"):
        database.import_spark_contacts(
            scan_result(database, first, other),
            [first.candidate.stable_key, other.candidate.stable_key],
        )
    assert database.list_targets() == []


def test_import_rejects_keys_not_in_scan(database) -> None:
    found = contact("friend")
    with pytest.raises(ValueError, match="不能导入"):
        database.import_spark_contacts(scan_result(database, found), ["not-scanned"])
    assert database.list_targets() == []


def test_import_rolls_back_insert_when_later_identity_is_ambiguous(database) -> None:
    first, ambiguous = contact("new"), contact("ambiguous")
    database.add_target(replace(ambiguous.candidate, stable_key="old-a"), "旧记录甲")
    database.add_target(replace(ambiguous.candidate, stable_key="old-b"), "旧记录乙")
    before = database.list_targets()

    with pytest.raises(ValueError, match="冲突"):
        database.import_spark_contacts(
            scan_result(database, first, ambiguous),
            [first.candidate.stable_key, ambiguous.candidate.stable_key],
        )
    assert database.list_targets() == before
    assert not any(
        event["category"] == "spark_contacts_imported" for event in database.list_events()
    )


def test_import_rejects_conflicting_identities_of_same_key(database) -> None:
    found = contact("friend")
    uncertain = replace(
        found,
        candidate=replace(found.candidate, profile_url="https://www.douyin.com/user/other"),
    )
    with pytest.raises(ValueError, match="冲突"):
        database.import_spark_contacts(
            scan_result(database, found, uncertain), [found.candidate.stable_key]
        )
    assert database.list_targets() == []


@pytest.mark.parametrize("first_state", list(SparkState))
@pytest.mark.parametrize("second_state", list(SparkState))
def test_duplicate_reliable_identity_imports_despite_spark_state_changes(
    database, first_state, second_state
) -> None:
    first = replace(contact("friend"), spark_state=first_state)
    second = replace(first, spark_state=second_state, reason="另一条火花状态观察")
    scan = scan_result(database, first, second)
    keys = [first.candidate.stable_key]

    assert database.import_spark_contacts(scan, keys) == (1, 0)
    targets = database.list_targets()
    assert len(targets) == 1
    assert not targets[0].enabled
    expected = first_state if first_state is second_state else SparkState.UNKNOWN
    assert targets[0].evidence["spark_scan"]["state"] == expected.value
    assert database.import_spark_contacts(scan, keys) == (0, 1)
    assert database.list_targets() == targets


@pytest.mark.parametrize("unsafe", ["weak", "unknown-type", "group", "identity-conflict", "id"])
@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_unreliable_or_conflicting_identity_rejects_entire_selection(
    database, unsafe, reverse
) -> None:
    first = contact("friend")
    second = replace(first, spark_state=SparkState.UNKNOWN)
    evidence = dict(first.candidate.evidence)
    if unsafe == "weak":
        evidence["identity_strength"] = "weak"
    elif unsafe == "unknown-type":
        evidence["chat_type"] = "unknown"
    elif unsafe == "identity-conflict":
        evidence["identity_conflict"] = True
    elif unsafe == "group":
        second = replace(second, is_group=True)
    else:
        first = replace(first, candidate=replace(first.candidate, douyin_id="first_id"))
        second = replace(second, candidate=replace(second.candidate, douyin_id="other_id"))
    second = replace(second, candidate=replace(second.candidate, evidence=evidence))
    observations = [first, second]
    if reverse:
        observations.reverse()
    other = contact("otherwise-safe")

    with pytest.raises(ValueError, match="冲突"):
        database.import_spark_contacts(
            scan_result(database, other, *observations),
            [other.candidate.stable_key, first.candidate.stable_key],
        )
    assert database.list_targets() == []
