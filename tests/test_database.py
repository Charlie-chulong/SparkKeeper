from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from spark_keeper.database import (
    Database,
    DatabaseCompatibilityError,
    DatabaseInitializationError,
)
from spark_keeper.models import AttemptStatus, BatchMode, BatchStatus, FriendCandidate, MessageKind
from spark_keeper.mutex import AlreadyRunningError, WindowsTaskMutex


def candidate(key: str, name: str = "测试好友") -> FriendCandidate:
    return FriendCandidate(
        stable_key=key,
        display_name=name,
        douyin_id=f"id-{key}",
        evidence={"identity_strength": "strong"},
    )


def setup_target(database: Database):
    account = database.save_account("account-key", "测试账号")
    target = database.add_target(candidate("friend-key"), "测试好友")
    database.save_plan(enabled=False, send_time="09:00", message_text="测试消息", confirmed=True)
    return account, target


def test_initialize_is_idempotent(tmp_path) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    first = Database(path)
    second = Database(path)
    assert first.get_plan().send_time == "09:00"
    assert second.get_meta("schema_version") == "3"
    assert (first.get_plan().delay_min_seconds, first.get_plan().delay_max_seconds) == (3, 8)
    assert not (tmp_path / "backups").exists()


def test_v1_database_migrates_inter_target_delay_defaults(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO app_meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE plan (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                send_time TEXT NOT NULL DEFAULT '09:00',
                message_text TEXT NOT NULL DEFAULT '',
                confirmed_at TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO plan(singleton, enabled, send_time, message_text, confirmed_at, updated_at)
                VALUES (1, 0, '09:00', '旧计划', NULL, '2026-09-04T08:00:00+08:00');
            """
        )

    database = Database(path)

    plan = database.get_plan()
    assert database.get_meta("schema_version") == "3"
    assert plan.message_text == "旧计划"
    assert plan.message_kind is MessageKind.TEXT
    assert (plan.delay_min_seconds, plan.delay_max_seconds) == (3, 8)


def test_plan_inter_target_delay_persists_and_validates(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    plan = database.save_plan(
        enabled=False,
        send_time="09:00",
        message_text="测试消息",
        confirmed=True,
        delay_min_seconds=4,
        delay_max_seconds=11,
    )
    assert (plan.delay_min_seconds, plan.delay_max_seconds) == (4, 11)

    for minimum, maximum in ((-1, 8), (9, 8), (3, 121), (True, 8)):
        with pytest.raises((TypeError, ValueError), match="好友间等待时间"):
            database.save_plan(
                enabled=False,
                send_time="09:00",
                message_text="测试消息",
                confirmed=True,
                delay_min_seconds=minimum,
                delay_max_seconds=maximum,
            )


def test_success_creates_daily_guard_and_blocks_duplicate(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    first_batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    first = database.reserve_attempt(
        batch_id=first_batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        manual_override=False,
    )
    assert first.allowed
    database.mark_attempt_triggered(first.attempt_id)
    database.finish_attempt(first.attempt_id, AttemptStatus.SUCCESS)
    database.finish_batch(first_batch, BatchStatus.SUCCESS)

    second_batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    second = database.reserve_attempt(
        batch_id=second_batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="另一条文本",
        manual_override=False,
    )
    assert not second.allowed
    assert second.conflicting_attempt_id == first.attempt_id
    assert database.get_attempt(second.attempt_id)["status"] == AttemptStatus.DUPLICATE.value


def test_manual_override_is_audited_without_removing_original_guard(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    first_batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    first = database.reserve_attempt(
        batch_id=first_batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        manual_override=False,
    )
    database.mark_attempt_triggered(first.attempt_id)
    database.finish_attempt(first.attempt_id, AttemptStatus.SUCCESS)

    override_batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    override = database.reserve_attempt(
        batch_id=override_batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        manual_override=True,
    )
    assert override.allowed
    assert override.conflicting_attempt_id == first.attempt_id
    row = database.get_attempt(override.attempt_id)
    assert row["manual_override"] == 1
    assert row["override_of"] == first.attempt_id
    database.mark_attempt_triggered(override.attempt_id)
    database.finish_attempt(override.attempt_id, AttemptStatus.FAILED)
    assert database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_explicit_failure_releases_new_guard(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        manual_override=False,
    )
    database.mark_attempt_triggered(reservation.attempt_id)
    database.finish_attempt(reservation.attempt_id, AttemptStatus.FAILED)
    assert not database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_recover_inflight_fails_closed_as_unknown(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.SCHEDULED, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        manual_override=False,
    )
    assert database.recover_inflight_if_idle() == 1
    assert database.get_attempt(reservation.attempt_id)["status"] == AttemptStatus.UNKNOWN.value
    assert database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_pending_action_is_deduplicated(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    first = database.create_pending_action("login_required", "login:today", {"reason": "expired"})
    second = database.create_pending_action("login_required", "login:today", {"reason": "again"})
    assert first == second
    assert len(database.list_pending_actions()) == 1


def test_clear_history_preserves_current_day_guard(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        manual_override=False,
    )
    database.mark_attempt_triggered(reservation.attempt_id)
    database.finish_attempt(reservation.attempt_id, AttemptStatus.SUCCESS)
    database.clear_history_preserving_today(date(2026, 9, 4))
    assert database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_account_switch_disables_targets_and_schedule(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    _, target = setup_target(database)
    database.save_plan(enabled=True, send_time="09:00", message_text="测试消息", confirmed=True)
    database.clear_account()

    database.save_account("different-account", "另一个账号")

    assert not database.get_target(target.id).enabled
    assert not database.get_plan().enabled


def test_add_targets_beyond_five_preserves_enabled_states(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    targets = [
        database.add_target(candidate(f"friend-{index}", f"好友{index}"), f"好友{index}")
        for index in range(7)
    ]

    assert all(target.enabled for target in targets)
    assert database.list_targets(enabled_only=True) == targets

    database.set_target_enabled(targets[0].id, False)
    added = database.add_target(candidate("friend-7", "好友7"), "好友7")

    assert added.enabled
    assert not database.get_target(targets[0].id).enabled
    assert database.list_targets(enabled_only=True) == [*targets[1:], added]
    assert len(database.list_targets()) == 8


def test_reenable_target_beyond_five_preserves_existing_records(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    targets = [
        database.add_target(candidate(f"friend-{index}", f"好友{index}"), f"好友{index}")
        for index in range(7)
    ]
    database.set_target_enabled(targets[0].id, False)
    assert len(database.list_targets(enabled_only=True)) == 6

    database.set_target_enabled(targets[0].id, True)

    assert database.list_targets(enabled_only=True) == targets
    assert database.list_targets() == targets


@pytest.mark.parametrize("enabled", [True, False])
def test_reconfirm_stable_key_beyond_five_updates_existing_target(tmp_path, enabled) -> None:
    database = Database(tmp_path / "state.sqlite3")
    targets = [
        database.add_target(candidate(f"friend-{index}", f"好友{index}"), f"好友{index}")
        for index in range(7)
    ]
    target = targets[0]
    database.set_target_enabled(target.id, enabled)
    assert len(database.list_targets(enabled_only=True)) > 5

    confirmed = database.add_target(candidate(target.stable_key, "更新后的好友"), "更新后的搜索词")

    assert confirmed.id == target.id
    assert confirmed.stable_key == target.stable_key
    assert confirmed.display_name == "更新后的好友"
    assert confirmed.search_query == "更新后的搜索词"
    assert confirmed.enabled
    assert database.get_target(target.id) == confirmed
    assert database.list_targets() == [confirmed, *targets[1:]]
    assert database.list_targets(enabled_only=True) == [confirmed, *targets[1:]]


def test_pre_send_failure_is_visible_in_history_without_guard(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)

    attempt_id = database.record_terminal_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="测试消息",
        status=AttemptStatus.FAILED,
    )

    assert database.get_attempt(attempt_id)["status"] == AttemptStatus.FAILED.value
    assert not database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_bulk_enable_disable_changes_only_selected_targets(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    targets = [database.add_target(candidate(f"friend-{i}"), "好友") for i in range(4)]
    selected = [targets[0].id, targets[2].id, targets[0].id]
    database.set_targets_enabled(selected, False)
    assert [target.enabled for target in database.list_targets()] == [False, True, False, True]
    database.set_targets_enabled(iter(selected), True)
    assert database.list_targets() == targets


def test_bulk_delete_keeps_history_targets_and_removes_only_unreferenced_targets(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, historical = setup_target(database)
    removable = database.add_target(candidate("removable"), "好友")
    untouched = database.add_target(candidate("untouched"), "好友")
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    attempt = database.record_terminal_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=historical.id,
        run_date="2026-09-08",
        message_text="测试文本",
        status=AttemptStatus.FAILED,
    )
    database.delete_targets([removable.id, historical.id, removable.id])
    assert database.get_target(removable.id) is None
    assert database.get_target(historical.id).enabled is False
    assert database.get_target(untouched.id) == untouched
    assert database.get_attempt(attempt)["target_id"] == historical.id


@pytest.mark.parametrize("operation", ["enable", "disable", "delete"])
def test_bulk_target_missing_member_rolls_back_entire_group(tmp_path, operation) -> None:
    database = Database(tmp_path / "state.sqlite3")
    targets = [database.add_target(candidate(f"friend-{i}"), "好友") for i in range(2)]
    if operation == "enable":
        database.set_targets_enabled([target.id for target in targets], False)
    before = database.list_targets()
    with pytest.raises(KeyError):
        if operation == "delete":
            database.delete_targets([targets[0].id, 999999, targets[1].id])
        else:
            database.set_targets_enabled(
                [targets[0].id, 999999, targets[1].id], operation == "enable"
            )
    assert database.list_targets() == before


@pytest.mark.parametrize("operation", ["enable", "disable", "delete"])
def test_bulk_target_database_failure_rolls_back_entire_group(tmp_path, operation) -> None:
    database = Database(tmp_path / "state.sqlite3")
    targets = [database.add_target(candidate(f"friend-{i}"), "好友") for i in range(2)]
    if operation == "enable":
        database.set_targets_enabled([target.id for target in targets], False)
    before = database.list_targets()
    trigger_operation = "DELETE" if operation == "delete" else "UPDATE"
    with database.connect(immediate=True) as connection:
        connection.execute(
            f"CREATE TRIGGER fail_second BEFORE {trigger_operation} ON targets "
            f"WHEN OLD.id = {targets[1].id} "
            "BEGIN SELECT RAISE(ABORT, 'fixture failure on second target'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        if operation == "delete":
            database.delete_targets([target.id for target in targets])
        else:
            database.set_targets_enabled([target.id for target in targets], operation == "enable")
    assert database.list_targets() == before


def test_v2_migration_preserves_plan_history_guard_and_legacy_snapshot(tmp_path) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    database = Database(path)
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="旧历史正文",
        manual_override=False,
    )
    database.mark_attempt_triggered(reservation.attempt_id)
    database.finish_attempt(reservation.attempt_id, AttemptStatus.SUCCESS)
    database.create_pending_action(
        "missed_schedule", "legacy", {"message_text": "旧快照", "target_ids": [target.id]}
    )
    previous = database.get_plan()
    with database.connect(immediate=True) as connection:
        connection.execute("ALTER TABLE plan DROP COLUMN message_kind")
        connection.execute("ALTER TABLE send_attempts DROP COLUMN message_kind")
        connection.execute("UPDATE app_meta SET value = '2' WHERE key = 'schema_version'")

    migrated = Database(path)
    reopened = Database(path)

    assert migrated.get_plan() == previous
    assert reopened.get_meta("schema_version") == "3"
    history = migrated.list_history()[0]
    assert (history["message_kind"], history["message_text"]) == ("text", "旧历史正文")
    assert history["id"] == reservation.attempt_id
    assert migrated.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")
    assert migrated.list_pending_actions()[0]["payload"] == {
        "message_text": "旧快照", "target_ids": [target.id], "message_kind": "text"
    }
    backups = list((path.parent.parent / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "2"
        assert backup.execute(
            "SELECT message_text FROM send_attempts WHERE id = ?", (reservation.attempt_id,)
        ).fetchone()[0] == "旧历史正文"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("kind", list(MessageKind))
def test_message_kind_plan_and_attempt_history_round_trip(tmp_path, kind) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    expected_text = " 原始文本 " if kind is MessageKind.TEXT else ""
    plan = database.save_plan(
        enabled=True, send_time="09:00", message_text=" 原始文本 ", confirmed=True,
        message_kind=kind,
    )
    assert plan.message_kind is kind
    assert plan.message_text == expected_text
    assert plan.confirmed_at
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch, account_key=account.platform_user_id, target_id=target.id,
        run_date="2026-09-04", message_text=" 原始文本 ", manual_override=False,
        message_kind=kind,
    )
    database.mark_attempt_triggered(reservation.attempt_id)
    database.finish_attempt(reservation.attempt_id, AttemptStatus.SUCCESS)
    terminal = database.record_terminal_attempt(
        batch_id=batch, account_key=account.platform_user_id, target_id=target.id,
        run_date="2026-09-05", message_text=" 原始文本 ", status=AttemptStatus.FAILED,
        message_kind=kind,
    )
    assert {row["id"] for row in database.list_history()} == {reservation.attempt_id, terminal}
    for row in database.list_history():
        assert (row["message_kind"], row["message_text"]) == (kind.value, expected_text)


@pytest.mark.parametrize("first_kind", list(MessageKind))
def test_daily_guard_spans_kinds_and_preserves_manual_override_audit(tmp_path, first_kind) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    other_kind = (
        MessageKind.SPARK_STICKER if first_kind is MessageKind.TEXT else MessageKind.TEXT
    )
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    common = {
        "batch_id": batch, "account_key": account.platform_user_id, "target_id": target.id,
        "run_date": "2026-09-04", "message_text": "文本",
    }
    first = database.reserve_attempt(**common, message_kind=first_kind, manual_override=False)
    database.mark_attempt_triggered(first.attempt_id)
    database.finish_attempt(first.attempt_id, AttemptStatus.SUCCESS)
    duplicate = database.reserve_attempt(**common, message_kind=other_kind, manual_override=False)
    assert not duplicate.allowed
    assert duplicate.conflicting_attempt_id == first.attempt_id
    assert database.get_attempt(duplicate.attempt_id)["message_kind"] == other_kind.value
    override = database.reserve_attempt(**common, message_kind=other_kind, manual_override=True)
    assert override.allowed
    row = database.get_attempt(override.attempt_id)
    assert row["manual_override"] == 1
    assert row["override_of"] == first.attempt_id
    assert row["message_kind"] == other_kind.value
    database.finish_attempt(override.attempt_id, AttemptStatus.FAILED)
    assert database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_plan_kind_validation_and_database_failure_leave_previous_plan_intact(tmp_path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    previous = database.save_plan(
        enabled=True, send_time="09:00", message_text="", confirmed=True,
        message_kind=MessageKind.SPARK_STICKER,
    )
    for kind, text in (("unsupported", "文本"), (MessageKind.TEXT, " ")):
        with pytest.raises(ValueError):
            database.save_plan(
                enabled=True, send_time="10:00", message_text=text, confirmed=True,
                message_kind=kind,
            )
        assert database.get_plan() == previous
    with database.connect(immediate=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_plan BEFORE UPDATE ON plan "
            "BEGIN SELECT RAISE(ABORT, 'plan write failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="plan write failed"):
        database.save_plan(
            enabled=True, send_time="10:00", message_text="新文本", confirmed=True,
            message_kind=MessageKind.TEXT,
        )
    assert database.get_plan() == previous


@pytest.mark.parametrize("method", ["reserve_attempt", "record_terminal_attempt"])
def test_unknown_attempt_kind_is_rejected_before_writing(tmp_path, method) -> None:
    database = Database(tmp_path / "state.sqlite3")
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.MANUAL, target_count=1)
    options = (
        {"manual_override": False} if method == "reserve_attempt"
        else {"status": AttemptStatus.FAILED}
    )
    with pytest.raises(ValueError):
        getattr(database, method)(
            batch_id=batch, account_key=account.platform_user_id, target_id=target.id,
            run_date="2026-09-04", message_text="文本", message_kind="unsupported", **options,
        )
    assert database.count_rows("send_attempts") == 0
    assert database.count_rows("daily_send_guards") == 0


def create_legacy_database(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO app_meta VALUES ('schema_version', '1');
            CREATE TABLE plan(
                singleton INTEGER PRIMARY KEY, enabled INTEGER NOT NULL,
                send_time TEXT NOT NULL, message_text TEXT NOT NULL,
                confirmed_at TEXT, updated_at TEXT NOT NULL
            );
            INSERT INTO plan VALUES (1, 0, '09:00', '保留计划', NULL, 'legacy');
            """
        )


def test_future_schema_rejected_without_any_database_write(tmp_path) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    create_legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE app_meta SET value = '999' WHERE key = 'schema_version'")
    before = {item.name: item.read_bytes() for item in path.parent.iterdir()}

    with pytest.raises(DatabaseCompatibilityError, match="999.*3"):
        Database(path)

    assert {item.name: item.read_bytes() for item in path.parent.iterdir()} == before
    assert not (tmp_path / "backups").exists()


def test_future_schema_in_wal_rejected_without_changing_persistent_data(tmp_path) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    Database(path)
    keeper = sqlite3.connect(path)
    try:
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        keeper.execute("UPDATE app_meta SET value = '999' WHERE key = 'schema_version'")
        keeper.commit()
        wal = path.with_name(path.name + "-wal")
        before = (path.read_bytes(), wal.read_bytes())
        with pytest.raises(DatabaseCompatibilityError, match="999.*3"):
            Database(path)
        # SQLite read-only WAL readers may update volatile SHM reader marks,
        # but neither the database nor its committed WAL may be modified.
        assert (path.read_bytes(), wal.read_bytes()) == before
        assert not (tmp_path / "backups").exists()
    finally:
        keeper.close()


def test_backup_failure_stops_before_schema_changes(tmp_path) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    create_legacy_database(path)
    (tmp_path / "backups").write_text("阻止创建备份目录", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(DatabaseInitializationError, match="备份失败"):
        Database(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "1"


def test_migration_failure_rolls_back_all_ddl_and_keeps_backup(tmp_path, monkeypatch) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    create_legacy_database(path)
    before = path.read_bytes()
    migrate = Database._migrate_schema

    def fail_after_migration(connection):
        migrate(connection)
        connection.execute("CREATE TABLE partial_migration(value TEXT)")
        raise sqlite3.OperationalError("离线迁移故障")

    monkeypatch.setattr(Database, "_migrate_schema", staticmethod(fail_after_migration))
    with pytest.raises(DatabaseInitializationError, match="事务已回滚.*升级前备份"):
        Database(path)

    assert path.read_bytes() == before
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(path) as original, sqlite3.connect(backups[0]) as backup:
        assert list(original.iterdump()) == list(backup.iterdump())
        assert original.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('partial_migration', 'send_attempts')"
        ).fetchall() == []


def test_migration_backup_includes_committed_wal_and_is_not_reused(tmp_path) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    database = Database(path)
    keeper = sqlite3.connect(path)
    try:
        keeper.execute("PRAGMA wal_autocheckpoint = 0")
        keeper.execute("UPDATE app_meta SET value = '2' WHERE key = 'schema_version'")
        keeper.execute("UPDATE plan SET message_text = '仅在WAL中的计划'")
        keeper.commit()
        assert path.with_name(path.name + "-wal").stat().st_size > 0

        migrated = Database(path)
        backups = list((tmp_path / "backups").glob("*.sqlite3"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("SELECT message_text FROM plan").fetchone()[0] == "仅在WAL中的计划"
            assert backup.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            ).fetchone()[0] == "2"
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        migrated.save_plan(
            enabled=False, send_time="10:30", message_text="升级后新业务", confirmed=True
        )
        reopened = Database(path)
        assert reopened.get_plan().message_text == "升级后新业务"
        assert list((tmp_path / "backups").glob("*.sqlite3")) == backups
        assert database.path == reopened.path
    finally:
        keeper.close()


def test_recovery_leaves_active_send_and_guard_unchanged(tmp_path) -> None:
    database = Database(tmp_path / "data" / "state.sqlite3")
    account, target = setup_target(database)
    batch = database.start_batch(BatchMode.SCHEDULED, target_count=1)
    reservation = database.reserve_attempt(
        batch_id=batch,
        account_key=account.platform_user_id,
        target_id=target.id,
        run_date="2026-09-04",
        message_text="离线记录，不发送",
        manual_override=False,
    )
    before = database.get_attempt(reservation.attempt_id)
    with database.connect() as connection:
        guard = dict(connection.execute("SELECT * FROM daily_send_guards").fetchone())

    with WindowsTaskMutex():
        assert database.recover_inflight_if_idle() == 0
        assert database.get_attempt(reservation.attempt_id) == before
        with database.connect() as connection:
            assert dict(connection.execute("SELECT * FROM daily_send_guards").fetchone()) == guard

    assert database.recover_inflight_if_idle() == 1
    assert database.get_attempt(reservation.attempt_id)["status"] == AttemptStatus.UNKNOWN.value
    assert database.has_daily_guard(account.platform_user_id, target.id, "2026-09-04")


def test_maintenance_lock_normalizes_aliases_without_holding_automation_lock(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "data" / "state.sqlite3"
    entered = False
    initialize = Database._initialize_locked

    def initialize_with_competitor(database):
        nonlocal entered
        if not entered:
            entered = True
            with (
                WindowsTaskMutex(),
                pytest.raises(DatabaseInitializationError, match="正在初始化或升级"),
            ):
                Database(path.parent / ".." / "data" / path.name.upper())
        initialize(database)

    monkeypatch.setattr(Database, "_initialize_locked", initialize_with_competitor)
    database = Database(path)
    with WindowsTaskMutex(), pytest.raises(AlreadyRunningError), WindowsTaskMutex():
        pass
    assert database.get_meta("schema_version") == "3"
