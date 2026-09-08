from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spark_keeper import maintenance, worker
from spark_keeper.models import BatchStatus
from spark_keeper.mutex import WindowsTaskMutex
from spark_keeper.windows_identity import current_user_sid


@pytest.fixture
def isolated_gate(tmp_path, monkeypatch):
    name = rf"Global\SparkKeeper.Maintenance.Test.{uuid.uuid4().hex}"
    monkeypatch.setattr(maintenance, "maintenance_mutex_name", lambda: name)
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    monkeypatch.setattr(worker, "_MAINTENANCE_WAIT_SECONDS", 0.03)
    return name


def pending_record():
    return {
        "format": 1,
        "product": "SparkKeeper",
        "target": r"C:\isolated\SparkKeeper",
        "operation": "install",
        "phase": "mutating",
        "task": None,
    }


def put_pending(content):
    path = maintenance.maintenance_journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


CHILD_PROBE = r'''
import sys
from spark_keeper import maintenance
assert not any(name.startswith("PySide6") for name in sys.modules)
maintenance.maintenance_mutex_name = lambda: sys.argv[1]
try:
    with maintenance.MaintenanceLease():
        print("acquired", flush=True)
except maintenance.MaintenanceBusy:
    print("busy", flush=True)
'''


def probe_gate(name):
    result = subprocess.run(
        [sys.executable, "-c", CHILD_PROBE, name],
        capture_output=True, text=True, timeout=15, check=True,
    )
    return result.stdout.strip()


@pytest.mark.skipif(os.name != "nt", reason="Windows Global named mutex")
def test_global_gate_blocks_other_process_and_releases_on_exception(isolated_gate):
    with pytest.raises(ValueError, match="owner failed"), maintenance.MaintenanceLease():
        assert probe_gate(isolated_gate) == "busy"
        # The independent automation gate remains available, including on this thread.
        with WindowsTaskMutex(rf"Local\SparkKeeper.Automation.Test.{uuid.uuid4().hex}"):
            pass
        raise ValueError("owner failed")
    assert probe_gate(isolated_gate) == "acquired"


@pytest.mark.skipif(os.name != "nt", reason="Windows process-token SID")
def test_gate_name_is_user_global_not_session_path_or_version(monkeypatch, tmp_path):
    expected = rf"Global\SparkKeeper.Maintenance.{current_user_sid()}"
    assert maintenance.maintenance_mutex_name() == expected
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    monkeypatch.setenv("USERNAME", "not-the-current-user")
    monkeypatch.setenv("SESSIONNAME", "not-the-current-session")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "other-version.exe"))
    assert maintenance.maintenance_mutex_name() == expected


@pytest.mark.parametrize(
    "content",
    [
        "{broken", "[]", "null", "\ud800", "x" * 65537,
        json.dumps(pending_record()),
        json.dumps({**pending_record(), "phase": "<script>untrusted\nphase</script>"}),
        json.dumps({**pending_record(), "format": 999}),
    ],
    ids=[
        "malformed-json", "array", "null", "invalid-utf8", "oversized",
        "pending", "unknown-phase", "unknown-format",
    ],
)
def test_every_pending_record_refuses_without_removing_or_exposing_raw_phase(
    tmp_path, monkeypatch, content
):
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    path = maintenance.maintenance_journal_path()
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("utf-8", errors="surrogatepass"))
    before = path.read_bytes()
    with pytest.raises(maintenance.MaintenanceRequired) as error:
        maintenance.assert_maintenance_clear()
    assert "重新运行安装包" in str(error.value)
    assert "计划仍保持暂停" in str(error.value)
    assert "<script>" not in str(error.value)
    assert path.read_bytes() == before
    assert not (tmp_path / "work").exists()


def test_production_journal_and_override_are_isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARK_KEEPER_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    expected = tmp_path / "appdata" / "SparkKeeper" / "maintenance" / "pending.json"
    assert maintenance.maintenance_journal_path() == expected
    put_pending("broken")
    with pytest.raises(maintenance.MaintenanceRequired):
        maintenance.assert_maintenance_clear()
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path / "smoke"))
    maintenance.assert_maintenance_clear()
    assert expected.read_text() == "broken"
    assert not (tmp_path / "smoke").exists()


def test_unreadable_journal_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    maintenance.maintenance_journal_path().mkdir(parents=True)
    with pytest.raises(maintenance.MaintenanceRequired, match="不可读取"):
        maintenance.assert_maintenance_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["busy", "pending", "corrupt"])
async def test_worker_gate_rejects_before_paths_or_database(
    isolated_gate, monkeypatch, capsys, blocked_by
):
    database = Mock(side_effect=AssertionError("database reached"))
    discover = Mock(side_effect=AssertionError("paths reached"))
    monkeypatch.setattr(worker, "Database", database)
    monkeypatch.setattr(worker.AppPaths, "discover", discover)
    if blocked_by == "busy":
        with maintenance.MaintenanceLease():
            assert await worker.run_scheduled() == 2
    else:
        put_pending(json.dumps(pending_record()) if blocked_by == "pending" else "broken")
        assert await worker.run_scheduled() == 2
    database.assert_not_called()
    discover.assert_not_called()
    assert "未执行发送" in capsys.readouterr().err
    with maintenance.MaintenanceLease():
        pass


@pytest.mark.asyncio
async def test_worker_waits_for_installer_handoff_before_reading_journal_or_database(
    isolated_gate, tmp_path, monkeypatch
):
    monkeypatch.setattr(worker, "_MAINTENANCE_WAIT_SECONDS", 1.0)
    pending = put_pending(json.dumps({**pending_record(), "phase": "restoring"}))
    owner = maintenance.MaintenanceLease()
    owner.acquire()

    async def finish_install():
        await asyncio.sleep(0.02)
        assert not (tmp_path / "work").exists()
        pending.unlink()
        owner.release()

    finishing = asyncio.create_task(finish_install())
    try:
        # The real new database has a disabled plan; this never sends or notifies.
        assert await worker.run_scheduled() == 0
        await finishing
        assert (tmp_path / "work" / "local-data" / "spark-keeper.sqlite3").exists()
    finally:
        owner.release()
        await finishing


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "batch", "notification", "event"])
async def test_worker_holds_gate_through_notification_and_exception_cleanup(
    isolated_gate, monkeypatch, failure
):
    stages = []

    def assert_held(stage):
        stages.append(stage)
        with pytest.raises(maintenance.MaintenanceBusy), maintenance.MaintenanceLease():
            pytest.fail("maintenance gate released too early")

    paths = SimpleNamespace(
        database=Path("unused.sqlite3"), auth_state=Path("unused.bin"),
        ensure_runtime_dirs=lambda: assert_held("directories"),
    )
    plan = SimpleNamespace(enabled=True, confirmed_at="confirmed")
    database = Mock()
    database.recover_inflight_if_idle.side_effect = lambda: assert_held("recovery")
    database.get_plan.return_value = plan

    def make_database(_path):
        assert_held("database")
        return database

    def event(*_args, **_kwargs):
        assert_held("event")
        if failure == "event":
            raise RuntimeError("event failure")

    async def run_batch(*_args, **_kwargs):
        assert_held("batch")
        if failure == "batch":
            raise RuntimeError("batch failure")
        return SimpleNamespace(status=BatchStatus.SUCCESS, batch_id=1)

    def notify(_result):
        assert_held("notification")
        if failure == "notification":
            raise RuntimeError("notification failure")
        return SimpleNamespace(delivered=False, error="isolated notification failure")

    database.record_event.side_effect = event
    monkeypatch.setattr(worker.AppPaths, "discover", lambda: paths)
    monkeypatch.setattr(worker, "Database", make_database)
    monkeypatch.setattr(worker, "DpapiJsonStore", Mock())
    monkeypatch.setattr(worker, "BatchService", lambda *_args: SimpleNamespace(run_batch=run_batch))
    monkeypatch.setattr(worker, "scheduled_datetime", lambda *_args: SimpleNamespace(isoformat=lambda **_kw: "2026-09-08T09:00:00"))
    monkeypatch.setattr(worker, "notify_batch", notify)
    if failure in {"notification", "event"}:
        with pytest.raises(RuntimeError, match=f"{failure} failure"):
            await worker.run_scheduled()
    else:
        assert await worker.run_scheduled() == (2 if failure == "batch" else 0)
    assert stages[:3] == ["directories", "database", "recovery"]
    assert stages[-1] == ("notification" if failure == "notification" else "event")
    with maintenance.MaintenanceLease():
        pass
