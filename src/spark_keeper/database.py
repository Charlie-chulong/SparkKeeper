from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .models import (
    Account,
    AttemptStatus,
    BatchMode,
    BatchStatus,
    ErrorCode,
    FriendCandidate,
    MessageKind,
    Plan,
    SparkScanResult,
    SparkState,
    Target,
)
from .mutex import AlreadyRunningError, WindowsTaskMutex


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


@dataclass(frozen=True, slots=True)
class Reservation:
    attempt_id: str
    allowed: bool
    conflicting_attempt_id: str | None = None


class DatabaseCompatibilityError(RuntimeError):
    """The database requires a different application schema."""


class DatabaseInitializationError(RuntimeError):
    """Initialization stopped without applying a partial migration."""


class Database:
    SCHEMA_VERSION = 3

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self.initialize()

    @contextmanager
    def connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        identity = hashlib.sha256(os.path.normcase(str(self.path)).encode("utf-8")).hexdigest()
        try:
            with WindowsTaskMutex(name=rf"Local\SparkKeeperDatabaseMaintenance-{identity}"):
                self._initialize_locked()
        except AlreadyRunningError as exc:
            raise DatabaseInitializationError("数据库正在初始化或升级，请稍后重新启动。") from exc
        except (DatabaseCompatibilityError, DatabaseInitializationError):
            raise
        except Exception as exc:
            raise DatabaseInitializationError(f"数据库初始化失败，未继续运行：{exc}") from exc

    @classmethod
    def _schema_version(cls, connection: sqlite3.Connection) -> int | None:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            return None
        if "app_meta" not in tables:
            raise DatabaseCompatibilityError("数据库缺少版本标记，已拒绝修改。")
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key = 'schema_version'"
        ).fetchone()
        try:
            version = int(row[0]) if row is not None else 0
        except (TypeError, ValueError) as exc:
            raise DatabaseCompatibilityError("数据库版本标记无效，已拒绝修改。") from exc
        if version > cls.SCHEMA_VERSION:
            raise DatabaseCompatibilityError(
                f"数据库版本 {version} 高于本程序支持的版本 {cls.SCHEMA_VERSION}，"
                "请使用较新程序；已拒绝修改数据库，不会自动恢复旧备份。"
            )
        if version < 1:
            raise DatabaseCompatibilityError("数据库版本标记无效，已拒绝修改。")
        return version

    def _backup_before_migration(self, version: int) -> Path:
        directory = self.path.parent.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"{self.path.stem}-schema{version}-{stamp}-{uuid.uuid4().hex}.sqlite3"
        # A separate reader is necessary: backup() on our BEGIN IMMEDIATE
        # connection would wait on its own write transaction. That transaction
        # prevents other writers until the snapshot and migration both finish.
        source = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            destination = sqlite3.connect(path)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        return path

    def _initialize_locked(self) -> None:
        version: int | None = None
        if self.path.exists():
            # Read committed WAL too; immutable=1 would silently ignore it.
            # Read-only SQLite may update volatile SHM reader marks, never
            # application rows, schema, the main database, or committed WAL.
            preflight = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=10)
            try:
                version = self._schema_version(preflight)
            finally:
                preflight.close()
            if version == self.SCHEMA_VERSION:
                return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        backup: Path | None = None
        try:
            if version is None:
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            version = self._schema_version(connection)
            if version == self.SCHEMA_VERSION:
                connection.rollback()
                return
            if version is not None:
                try:
                    backup = self._backup_before_migration(version)
                except Exception as exc:
                    raise DatabaseInitializationError(
                        f"升级前数据库备份失败，未执行迁移，原库保留：{exc}"
                    ) from exc
            self._create_schema(connection)
            self._migrate_schema(connection)
            connection.commit()
        except BaseException as exc:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(exc, (DatabaseCompatibilityError, DatabaseInitializationError)):
                raise
            if not isinstance(exc, Exception):
                raise
            detail = f"；升级前备份：{backup}" if backup else ""
            raise DatabaseInitializationError(
                f"数据库初始化或迁移失败，事务已回滚，未继续运行{detail}：{exc}"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        # This fixed DDL contains no semicolons inside SQL literals. Execute
        # statements individually: executescript() would commit our transaction.
        schema = """

                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    platform_user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    logged_in_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stable_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    douyin_id TEXT NOT NULL DEFAULT '',
                    profile_url TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    search_query TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    confirmed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plan (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    send_time TEXT NOT NULL DEFAULT '09:00',
                    message_text TEXT NOT NULL DEFAULT '',
                    message_kind TEXT NOT NULL DEFAULT 'text',
                    confirmed_at TEXT,
                    updated_at TEXT NOT NULL,
                    delay_min_seconds INTEGER NOT NULL DEFAULT 3
                        CHECK (delay_min_seconds BETWEEN 0 AND 120),
                    delay_max_seconds INTEGER NOT NULL DEFAULT 8
                        CHECK (delay_max_seconds BETWEEN 0 AND 120),
                    CHECK (delay_min_seconds <= delay_max_seconds)
                );

                CREATE TABLE IF NOT EXISTS batch_runs (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    scheduled_for TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    target_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    unknown_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS send_attempts (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES batch_runs(id) ON DELETE CASCADE,
                    account_key TEXT NOT NULL,
                    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE RESTRICT,
                    run_date TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    message_kind TEXT NOT NULL DEFAULT 'text',
                    status TEXT NOT NULL,
                    manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
                    override_of TEXT,
                    started_at TEXT NOT NULL,
                    triggered_at TEXT,
                    finished_at TEXT,
                    error_code TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_attempts_date
                    ON send_attempts(run_date, target_id, status);
                CREATE INDEX IF NOT EXISTS idx_attempts_batch
                    ON send_attempts(batch_id);

                CREATE TABLE IF NOT EXISTS daily_send_guards (
                    account_key TEXT NOT NULL,
                    target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE RESTRICT,
                    run_date TEXT NOT NULL,
                    source_attempt_id TEXT NOT NULL REFERENCES send_attempts(id) ON DELETE RESTRICT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_key, target_id, run_date)
                );

                CREATE TABLE IF NOT EXISTS pending_actions (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    batch_id TEXT,
                    target_alias TEXT,
                    message TEXT NOT NULL
                );

                INSERT INTO app_meta(key, value) VALUES ('schema_version', '1')
                    ON CONFLICT(key) DO NOTHING;
                INSERT INTO plan(singleton, enabled, send_time, message_text, confirmed_at, updated_at)
                    VALUES (1, 0, '09:00', '', NULL, strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'))
                    ON CONFLICT(singleton) DO NOTHING;

                """
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    @classmethod
    def _migrate_schema(cls, connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(plan)").fetchall()}
        if "delay_min_seconds" not in columns:
            connection.execute(
                "ALTER TABLE plan ADD COLUMN delay_min_seconds INTEGER NOT NULL DEFAULT 3 "
                "CHECK (delay_min_seconds BETWEEN 0 AND 120)"
            )
        if "delay_max_seconds" not in columns:
            connection.execute(
                "ALTER TABLE plan ADD COLUMN delay_max_seconds INTEGER NOT NULL DEFAULT 8 "
                "CHECK (delay_max_seconds BETWEEN 0 AND 120)"
            )
        for table in ("plan", "send_attempts"):
            table_columns = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "message_kind" not in table_columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN message_kind TEXT NOT NULL DEFAULT 'text'"
                )
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(cls.SCHEMA_VERSION),),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect(immediate=True) as connection:
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def save_account(self, platform_user_id: str, display_name: str) -> Account:
        platform_user_id = platform_user_id.strip()
        display_name = display_name.strip()
        if not platform_user_id:
            raise ValueError("账号稳定标识不能为空")
        timestamp = now_iso()
        with self.connect(immediate=True) as connection:
            previous_row = connection.execute(
                "SELECT platform_user_id FROM account WHERE singleton = 1"
            ).fetchone()
            meta_row = connection.execute(
                "SELECT value FROM app_meta WHERE key = 'last_account_key'"
            ).fetchone()
            previous_key = (
                str(meta_row["value"])
                if meta_row
                else str(previous_row["platform_user_id"])
                if previous_row
                else ""
            )
            if previous_key and previous_key != platform_user_id:
                connection.execute("UPDATE targets SET enabled = 0")
                connection.execute(
                    "UPDATE plan SET enabled = 0, confirmed_at = NULL, updated_at = ? WHERE singleton = 1",
                    (timestamp,),
                )
            connection.execute(
                """
                INSERT INTO account(singleton, platform_user_id, display_name, logged_in_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    platform_user_id = excluded.platform_user_id,
                    display_name = excluded.display_name,
                    logged_in_at = excluded.logged_in_at
                """,
                (platform_user_id, display_name or "已登录账号", timestamp),
            )
            connection.execute(
                """
                INSERT INTO app_meta(key, value) VALUES ('last_account_key', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (platform_user_id,),
            )
        return Account(platform_user_id, display_name or "已登录账号", timestamp)

    def get_account(self) -> Account | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT platform_user_id, display_name, logged_in_at FROM account WHERE singleton = 1"
            ).fetchone()
        if not row:
            return None
        return Account(
            str(row["platform_user_id"]), str(row["display_name"]), str(row["logged_in_at"])
        )

    def clear_account(self) -> None:
        with self.connect(immediate=True) as connection:
            connection.execute("DELETE FROM account WHERE singleton = 1")

    def add_target(self, candidate: FriendCandidate, search_query: str) -> Target:
        if not candidate.stable_key.strip():
            raise ValueError("好友稳定标识不能为空")
        timestamp = now_iso()
        evidence = json.dumps(candidate.evidence, ensure_ascii=False, separators=(",", ":"))
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO targets(
                    stable_key, display_name, douyin_id, profile_url, avatar_url,
                    search_query, evidence_json, enabled, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(stable_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    douyin_id = excluded.douyin_id,
                    profile_url = excluded.profile_url,
                    avatar_url = excluded.avatar_url,
                    search_query = excluded.search_query,
                    evidence_json = excluded.evidence_json,
                    enabled = 1,
                    confirmed_at = excluded.confirmed_at
                """,
                (
                    candidate.stable_key,
                    candidate.display_name,
                    candidate.douyin_id,
                    candidate.profile_url,
                    candidate.avatar_url,
                    search_query.strip(),
                    evidence,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM targets WHERE stable_key = ?", (candidate.stable_key,)
            ).fetchone()
        assert row is not None
        return self._target_from_row(row)

    def import_spark_contacts(
        self, scan: SparkScanResult, selected_keys: Iterable[str]
    ) -> tuple[int, int]:
        """保存已确认的扫描选择；账号核验、去重和写入共用一次事务。"""
        selected = tuple(dict.fromkeys(selected_keys))
        if not selected:
            raise ValueError("请选择至少一位可导入好友")
        candidates = {}
        for contact in scan.contacts:
            key = contact.candidate.stable_key
            if key in candidates:
                previous = candidates[key]
                if (
                    not previous.importable
                    or not contact.importable
                    or previous.candidate.douyin_id != contact.candidate.douyin_id
                    or previous.candidate.profile_url != contact.candidate.profile_url
                ):
                    raise ValueError("扫描结果包含冲突或未确认身份，请重新扫描")
                if previous.spark_state != contact.spark_state:
                    contact = replace(
                        contact,
                        spark_state=SparkState.UNKNOWN,
                        reason="同一稳定身份的火花状态观察冲突，需人工确认",
                    )
            candidates[key] = contact
        if any(key not in candidates or not candidates[key].importable for key in selected):
            raise ValueError("所选好友不在扫描结果中，或单聊类型、稳定身份尚未确认，不能导入")

        added = existing = 0
        with self.connect(immediate=True) as connection:
            account = connection.execute(
                "SELECT platform_user_id, logged_in_at FROM account WHERE singleton = 1"
            ).fetchone()
            if (
                account is None
                or account["platform_user_id"] != scan.account_key
                or account["logged_in_at"] != scan.account_logged_in_at
            ):
                raise ValueError("登录账号或登录状态已变化，请重新扫描")
            for key in selected:
                candidate = candidates[key].candidate
                matches = connection.execute(
                    """
                    SELECT id, profile_url FROM targets
                    WHERE stable_key = ?
                       OR (? != '' AND profile_url = ?)
                       OR (? != '' AND douyin_id = ?)
                    """,
                    (
                        key,
                        candidate.profile_url,
                        candidate.profile_url,
                        candidate.douyin_id,
                        candidate.douyin_id,
                    ),
                ).fetchall()
                if len(matches) > 1 or (
                    matches
                    and candidate.profile_url
                    and matches[0]["profile_url"]
                    and matches[0]["profile_url"] != candidate.profile_url
                ):
                    raise ValueError("好友身份与已有记录冲突，本次导入未保存")
                if matches:
                    existing += 1
                    continue
                evidence = dict(candidate.evidence)
                evidence["spark_scan"] = {
                    "observed_at": scan.scanned_at,
                    "state": candidates[key].spark_state.value,
                }
                connection.execute(
                    """
                    INSERT INTO targets(
                        stable_key, display_name, douyin_id, profile_url, avatar_url,
                        search_query, evidence_json, enabled, confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        key,
                        candidate.display_name,
                        candidate.douyin_id,
                        candidate.profile_url,
                        candidate.avatar_url,
                        candidate.douyin_id or candidate.display_name,
                        json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
                        now_iso(),
                    ),
                )
                added += 1
            connection.execute(
                "INSERT INTO events(created_at, level, category, message) VALUES (?, ?, ?, ?)",
                (
                    now_iso(),
                    "INFO",
                    "spark_contacts_imported",
                    f"火花好友导入完成：新增 {added} 位（停用），已有 {existing} 位保持不变",
                ),
            )
        return added, existing

    def list_targets(self, *, enabled_only: bool = False) -> list[Target]:
        query = "SELECT * FROM targets"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._target_from_row(row) for row in rows]

    def get_target(self, target_id: int) -> Target | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return self._target_from_row(row) if row else None

    def set_target_enabled(self, target_id: int, enabled: bool) -> None:
        with self.connect(immediate=True) as connection:
            row = connection.execute("SELECT id FROM targets WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise KeyError(target_id)
            connection.execute(
                "UPDATE targets SET enabled = ? WHERE id = ?", (int(enabled), target_id)
            )

    def set_targets_enabled(self, target_ids: Iterable[int], enabled: bool) -> None:
        with self.connect(immediate=True) as connection:
            for target_id in dict.fromkeys(target_ids):
                result = connection.execute(
                    "UPDATE targets SET enabled = ? WHERE id = ?", (int(enabled), target_id)
                )
                if result.rowcount != 1:
                    raise KeyError(target_id)

    def delete_targets(self, target_ids: Iterable[int]) -> None:
        with self.connect(immediate=True) as connection:
            for target_id in dict.fromkeys(target_ids):
                row = connection.execute(
                    "SELECT id FROM targets WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(target_id)
                referenced = connection.execute(
                    "SELECT 1 FROM send_attempts WHERE target_id = ? LIMIT 1", (target_id,)
                ).fetchone()
                if referenced:
                    connection.execute("UPDATE targets SET enabled = 0 WHERE id = ?", (target_id,))
                else:
                    connection.execute("DELETE FROM targets WHERE id = ?", (target_id,))

    @staticmethod
    def _target_from_row(row: sqlite3.Row) -> Target:
        try:
            evidence = json.loads(str(row["evidence_json"]))
        except json.JSONDecodeError:
            evidence = {}
        return Target(
            id=int(row["id"]),
            stable_key=str(row["stable_key"]),
            display_name=str(row["display_name"]),
            douyin_id=str(row["douyin_id"]),
            profile_url=str(row["profile_url"]),
            avatar_url=str(row["avatar_url"]),
            search_query=str(row["search_query"]),
            evidence=evidence if isinstance(evidence, dict) else {},
            enabled=bool(row["enabled"]),
            confirmed_at=str(row["confirmed_at"]),
        )

    def get_plan(self) -> Plan:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM plan WHERE singleton = 1").fetchone()
        assert row is not None
        return Plan(
            enabled=bool(row["enabled"]),
            send_time=str(row["send_time"]),
            message_text=str(row["message_text"]),
            confirmed_at=str(row["confirmed_at"]) if row["confirmed_at"] else None,
            updated_at=str(row["updated_at"]),
            delay_min_seconds=int(row["delay_min_seconds"]),
            delay_max_seconds=int(row["delay_max_seconds"]),
            message_kind=MessageKind(row["message_kind"]),
        )

    def save_plan(
        self,
        *,
        enabled: bool,
        send_time: str,
        message_text: str,
        confirmed: bool,
        delay_min_seconds: int = 3,
        delay_max_seconds: int = 8,
        message_kind: MessageKind = MessageKind.TEXT,
    ) -> Plan:
        self._validate_send_time(send_time)
        self.validate_delay_range(delay_min_seconds, delay_max_seconds)
        message_kind = MessageKind(message_kind)
        if message_kind is MessageKind.SPARK_STICKER:
            message_text = ""
        if enabled and message_kind is MessageKind.TEXT and not message_text.strip():
            raise ValueError("启用计划前必须填写消息文本")
        timestamp = now_iso()
        confirmed_at = timestamp if confirmed else None
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                UPDATE plan SET enabled = ?, send_time = ?, message_text = ?,
                    confirmed_at = ?, updated_at = ?, delay_min_seconds = ?,
                    delay_max_seconds = ?, message_kind = ? WHERE singleton = 1
                """,
                (
                    int(enabled),
                    send_time,
                    message_text,
                    confirmed_at,
                    timestamp,
                    delay_min_seconds,
                    delay_max_seconds,
                    message_kind.value,
                ),
            )
        return self.get_plan()

    @staticmethod
    def _validate_send_time(value: str) -> None:
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("发送时间必须为 HH:MM") from exc
        if parsed.tzinfo is not None or parsed.isoformat(timespec="minutes") != value:
            raise ValueError("发送时间必须为 HH:MM")

    @staticmethod
    def validate_delay_range(minimum: int, maximum: int) -> None:
        if isinstance(minimum, bool) or isinstance(maximum, bool):
            raise TypeError("好友间等待时间必须为整数秒")
        if not 0 <= minimum <= maximum <= 120:
            raise ValueError("好友间等待时间必须满足 0 ≤ 最短 ≤ 最长 ≤ 120 秒")

    def start_batch(
        self,
        mode: BatchMode,
        *,
        target_count: int,
        scheduled_for: str | None = None,
    ) -> str:
        batch_id = str(uuid.uuid4())
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO batch_runs(
                    id, mode, scheduled_for, started_at, status, target_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    mode.value,
                    scheduled_for,
                    now_iso(),
                    BatchStatus.RUNNING.value,
                    target_count,
                ),
            )
        return batch_id

    def finish_batch(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        error_code: ErrorCode | None = None,
    ) -> None:
        with self.connect(immediate=True) as connection:
            counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM send_attempts WHERE batch_id = ? GROUP BY status",
                    (batch_id,),
                ).fetchall()
            }
            connection.execute(
                """
                UPDATE batch_runs SET finished_at = ?, status = ?, error_code = ?,
                    success_count = ?, failed_count = ?, unknown_count = ?, duplicate_count = ?
                WHERE id = ?
                """,
                (
                    now_iso(),
                    status.value,
                    error_code.value if error_code else None,
                    counts.get(AttemptStatus.SUCCESS.value, 0),
                    counts.get(AttemptStatus.FAILED.value, 0),
                    counts.get(AttemptStatus.UNKNOWN.value, 0),
                    counts.get(AttemptStatus.DUPLICATE.value, 0),
                    batch_id,
                ),
            )

    def record_terminal_attempt(
        self,
        *,
        batch_id: str,
        account_key: str,
        target_id: int,
        run_date: str,
        message_text: str,
        status: AttemptStatus,
        error_code: ErrorCode | None = None,
        message_kind: MessageKind = MessageKind.TEXT,
    ) -> str:
        message_kind = MessageKind(message_kind)
        if message_kind is MessageKind.SPARK_STICKER:
            message_text = ""
        if status not in {AttemptStatus.FAILED, AttemptStatus.CANCELLED}:
            raise ValueError("只能直接记录未触发的失败或取消")
        attempt_id = str(uuid.uuid4())
        timestamp = now_iso()
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO send_attempts(
                    id, batch_id, account_key, target_id, run_date, message_text,
                    status, manual_override, started_at, finished_at, error_code, message_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    batch_id,
                    account_key,
                    target_id,
                    run_date,
                    message_text,
                    status.value,
                    timestamp,
                    timestamp,
                    error_code.value if error_code else None,
                    message_kind.value,
                ),
            )
        return attempt_id

    def reserve_attempt(
        self,
        *,
        batch_id: str,
        account_key: str,
        target_id: int,
        run_date: str,
        message_text: str,
        manual_override: bool,
        message_kind: MessageKind = MessageKind.TEXT,
    ) -> Reservation:
        message_kind = MessageKind(message_kind)
        if message_kind is MessageKind.SPARK_STICKER:
            message_text = ""
        attempt_id = str(uuid.uuid4())
        timestamp = now_iso()
        with self.connect(immediate=True) as connection:
            guard = connection.execute(
                """
                SELECT source_attempt_id, status FROM daily_send_guards
                WHERE account_key = ? AND target_id = ? AND run_date = ?
                """,
                (account_key, target_id, run_date),
            ).fetchone()
            if guard and not manual_override:
                connection.execute(
                    """
                    INSERT INTO send_attempts(
                        id, batch_id, account_key, target_id, run_date, message_text,
                        status, manual_override, override_of, started_at, finished_at, error_code,
                        message_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        batch_id,
                        account_key,
                        target_id,
                        run_date,
                        message_text,
                        AttemptStatus.DUPLICATE.value,
                        str(guard["source_attempt_id"]),
                        timestamp,
                        timestamp,
                        ErrorCode.DUPLICATE_BLOCKED.value,
                        message_kind.value,
                    ),
                )
                return Reservation(attempt_id, False, str(guard["source_attempt_id"]))

            override_of = str(guard["source_attempt_id"]) if guard else None
            connection.execute(
                """
                INSERT INTO send_attempts(
                    id, batch_id, account_key, target_id, run_date, message_text,
                    status, manual_override, override_of, started_at, message_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    batch_id,
                    account_key,
                    target_id,
                    run_date,
                    message_text,
                    AttemptStatus.SENDING.value,
                    int(manual_override),
                    override_of,
                    timestamp,
                    message_kind.value,
                ),
            )
            if guard is None:
                connection.execute(
                    """
                    INSERT INTO daily_send_guards(
                        account_key, target_id, run_date, source_attempt_id, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_key,
                        target_id,
                        run_date,
                        attempt_id,
                        AttemptStatus.SENDING.value,
                        timestamp,
                        timestamp,
                    ),
                )
        return Reservation(attempt_id, True, override_of)

    def mark_attempt_triggered(self, attempt_id: str) -> bool:
        """Atomically claim the actual send day; False means rejected before sending."""
        with self.connect(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT account_key, target_id, run_date, status, triggered_at,
                       manual_override, override_of
                FROM send_attempts WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != AttemptStatus.SENDING.value
                or row["triggered_at"] is not None
            ):
                raise ValueError("发送尝试不在可触发状态")
            run_date = today_iso()
            timestamp = now_iso()
            guard = connection.execute(
                """
                SELECT source_attempt_id FROM daily_send_guards
                WHERE account_key = ? AND target_id = ? AND run_date = ?
                """,
                (row["account_key"], row["target_id"], run_date),
            ).fetchone()
            changed_day = run_date != row["run_date"]
            owns_guard = guard is not None and guard["source_attempt_id"] == attempt_id
            authorized_override = (
                not changed_day
                and row["manual_override"]
                and guard is not None
                and guard["source_attempt_id"] == row["override_of"]
            )
            rejected = (changed_day and row["manual_override"]) or (
                guard is not None and not owns_guard and not authorized_override
            )
            if rejected:
                # Preserve override_of and its authorization day for the audit trail.
                connection.execute(
                    """
                    UPDATE send_attempts SET status = ?, finished_at = ?, error_code = ?,
                        run_date = ?, override_of = ?
                    WHERE id = ?
                    """,
                    (
                        AttemptStatus.DUPLICATE.value, timestamp,
                        ErrorCode.DUPLICATE_BLOCKED.value,
                        row["run_date"] if row["manual_override"] else run_date,
                        row["override_of"] if row["manual_override"] else guard["source_attempt_id"],
                        attempt_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM daily_send_guards WHERE source_attempt_id = ? AND status = ?",
                    (attempt_id, AttemptStatus.SENDING.value),
                )
                return False
            if changed_day:
                connection.execute(
                    "DELETE FROM daily_send_guards WHERE source_attempt_id = ? AND status = ?",
                    (attempt_id, AttemptStatus.SENDING.value),
                )
            if guard is None:
                connection.execute(
                    """
                    INSERT INTO daily_send_guards(
                        account_key, target_id, run_date, source_attempt_id, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["account_key"], row["target_id"], run_date, attempt_id,
                        AttemptStatus.SENDING.value, timestamp, timestamp,
                    ),
                )
            connection.execute(
                "UPDATE send_attempts SET run_date = ?, triggered_at = ? WHERE id = ?",
                (run_date, timestamp, attempt_id),
            )
        return True

    def finish_attempt(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        error_code: ErrorCode | None = None,
    ) -> None:
        if status not in {
            AttemptStatus.SUCCESS,
            AttemptStatus.FAILED,
            AttemptStatus.UNKNOWN,
            AttemptStatus.CANCELLED,
        }:
            raise ValueError(f"不能以 {status.value} 结束发送尝试")
        timestamp = now_iso()
        with self.connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT account_key, target_id, run_date, status FROM send_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if not row:
                raise KeyError(attempt_id)
            if str(row["status"]) != AttemptStatus.SENDING.value:
                raise ValueError("发送尝试已经结束")
            connection.execute(
                """
                UPDATE send_attempts SET status = ?, finished_at = ?, error_code = ? WHERE id = ?
                """,
                (status.value, timestamp, error_code.value if error_code else None, attempt_id),
            )
            guard = connection.execute(
                """
                SELECT source_attempt_id FROM daily_send_guards
                WHERE account_key = ? AND target_id = ? AND run_date = ?
                """,
                (str(row["account_key"]), int(row["target_id"]), str(row["run_date"])),
            ).fetchone()
            owns_guard = bool(guard and str(guard["source_attempt_id"]) == attempt_id)
            if status in {AttemptStatus.SUCCESS, AttemptStatus.UNKNOWN}:
                if owns_guard:
                    connection.execute(
                        """
                        UPDATE daily_send_guards SET status = ?, updated_at = ?
                        WHERE source_attempt_id = ?
                        """,
                        (status.value, timestamp, attempt_id),
                    )
                elif guard is None:
                    connection.execute(
                        """
                        INSERT INTO daily_send_guards(
                            account_key, target_id, run_date, source_attempt_id, status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(row["account_key"]),
                            int(row["target_id"]),
                            str(row["run_date"]),
                            attempt_id,
                            status.value,
                            timestamp,
                            timestamp,
                        ),
                    )
            elif owns_guard:
                connection.execute(
                    "DELETE FROM daily_send_guards WHERE source_attempt_id = ?", (attempt_id,)
                )

    def recover_inflight_if_idle(self) -> int:
        """Recover interrupted sends only while no automation task can be active."""
        try:
            with WindowsTaskMutex():
                return self.recover_inflight_attempts()
        except AlreadyRunningError:
            return 0

    def recover_inflight_attempts(self) -> int:
        timestamp = now_iso()
        with self.connect(immediate=True) as connection:
            rows = connection.execute(
                "SELECT id FROM send_attempts WHERE status = ?", (AttemptStatus.SENDING.value,)
            ).fetchall()
            for row in rows:
                attempt_id = str(row["id"])
                connection.execute(
                    """
                    UPDATE send_attempts SET status = ?, finished_at = ?, error_code = ? WHERE id = ?
                    """,
                    (
                        AttemptStatus.UNKNOWN.value,
                        timestamp,
                        ErrorCode.SEND_UNKNOWN.value,
                        attempt_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE daily_send_guards SET status = ?, updated_at = ?
                    WHERE source_attempt_id = ?
                    """,
                    (AttemptStatus.UNKNOWN.value, timestamp, attempt_id),
                )
        return len(rows)

    def has_daily_guard(self, account_key: str, target_id: int, run_date: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM daily_send_guards
                WHERE account_key = ? AND target_id = ? AND run_date = ?
                """,
                (account_key, target_id, run_date),
            ).fetchone()
        return row is not None

    def record_event(
        self,
        level: str,
        category: str,
        message: str,
        *,
        batch_id: str | None = None,
        target_alias: str | None = None,
    ) -> None:
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO events(created_at, level, category, batch_id, target_alias, message)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso(), level.upper(), category, batch_id, target_alias, message),
            )

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 1000)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_history(self, limit: int = 200) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 1000)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.id, a.batch_id, a.run_date, a.message_text, a.message_kind, a.status,
                       a.manual_override, a.started_at, a.finished_at, a.error_code,
                       t.display_name AS target_name, b.mode
                FROM send_attempts a
                JOIN targets t ON t.id = a.target_id
                JOIN batch_runs b ON b.id = a.batch_id
                ORDER BY a.started_at DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_pending_action(
        self,
        kind: str,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> str:
        action_id = str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO pending_actions(id, kind, dedupe_key, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (action_id, kind, dedupe_key, payload_json, now_iso()),
            )
            row = connection.execute(
                "SELECT id FROM pending_actions WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
        assert row is not None
        return str(row["id"])

    def list_pending_actions(self, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM pending_actions WHERE status = 'pending'"
        params: tuple[Any, ...] = ()
        if kind:
            query += " AND kind = ?"
            params = (kind,)
        query += " ORDER BY created_at"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        actions: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(str(item.pop("payload_json")))
            except json.JSONDecodeError:
                item["payload"] = {}
            if item["kind"] == "missed_schedule" and isinstance(item["payload"], dict):
                item["payload"].setdefault("message_kind", MessageKind.TEXT.value)
            actions.append(item)
        return actions

    def resolve_pending_action(self, action_id: str, resolution: str) -> None:
        if resolution not in {"completed", "skipped", "expired"}:
            raise ValueError("无效的待处理事项结果")
        with self.connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE pending_actions SET status = ?, resolved_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (resolution, now_iso(), action_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(action_id)

    def scheduled_batch_exists(self, schedule_date: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM batch_runs
                WHERE mode = ? AND substr(scheduled_for, 1, 10) = ?
                LIMIT 1
                """,
                (BatchMode.SCHEDULED.value, schedule_date),
            ).fetchone()
        return row is not None

    def list_batches(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 1000)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_runs ORDER BY started_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_history_preserving_today(self, current_date: date | None = None) -> dict[str, int]:
        date_text = (current_date or datetime.now().astimezone().date()).isoformat()
        with self.connect(immediate=True) as connection:
            deleted_events = connection.execute(
                "DELETE FROM events WHERE substr(created_at, 1, 10) < ?", (date_text,)
            ).rowcount
            connection.execute("DELETE FROM daily_send_guards WHERE run_date < ?", (date_text,))
            deleted_attempts = connection.execute(
                "DELETE FROM send_attempts WHERE run_date < ?", (date_text,)
            ).rowcount
            deleted_batches = connection.execute(
                """
                DELETE FROM batch_runs
                WHERE substr(started_at, 1, 10) < ?
                  AND id NOT IN (SELECT DISTINCT batch_id FROM send_attempts)
                """,
                (date_text,),
            ).rowcount
            deleted_pending = connection.execute(
                "DELETE FROM pending_actions WHERE status != 'pending' AND substr(created_at, 1, 10) < ?",
                (date_text,),
            ).rowcount
        return {
            "events": deleted_events,
            "attempts": deleted_attempts,
            "batches": deleted_batches,
            "pending": deleted_pending,
        }

    def close_running_batch(self, batch_id: str, error_code: ErrorCode) -> None:
        self.finish_batch(batch_id, BatchStatus.FAILED, error_code=error_code)

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM send_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        return dict(row) if row else None

    def count_rows(self, table: str) -> int:
        allowed: Sequence[str] = (
            "account",
            "targets",
            "plan",
            "batch_runs",
            "send_attempts",
            "daily_send_guards",
            "pending_actions",
            "events",
        )
        if table not in allowed:
            raise ValueError("不允许的表名")
        with self.connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
