from __future__ import annotations

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from spark_keeper import __main__ as entry
from spark_keeper import maintenance
from spark_keeper.ui import app as app_module


def test_scheduled_entry_does_not_start_gui(monkeypatch):
    worker = AsyncMock(return_value=7)
    monkeypatch.setitem(entry.sys.modules, "spark_keeper.worker", SimpleNamespace(run_scheduled=worker))
    gui = Mock(side_effect=AssertionError("scheduled started GUI"))
    monkeypatch.setattr(app_module, "run", gui)
    assert entry.main(["--scheduled"]) == 7
    worker.assert_awaited_once_with()
    gui.assert_not_called()


def test_gui_initialization_error_returns_failure_and_shows_clear_message(monkeypatch):
    failure = RuntimeError("数据库结构版本较新，请使用新版程序")
    monkeypatch.setattr(app_module, "run", Mock(side_effect=failure))
    show_error = Mock()
    monkeypatch.setattr(entry, "_show_startup_error", show_error)
    assert entry.main([]) == 1
    show_error.assert_called_once_with(failure, smoke_mode=False)


def test_smoke_failure_is_noninteractive_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(app_module, "run", Mock(side_effect=RuntimeError("schema failed")))
    assert entry.main(["--smoke"]) == 1
    stderr = capsys.readouterr().err
    assert "未继续执行任何任务" in stderr
    assert "schema failed" in stderr
    assert "不要删除数据库" in stderr


@pytest.fixture
def isolated_gui_entry(tmp_path, monkeypatch):
    name = rf"Global\SparkKeeper.Maintenance.Test.{uuid.uuid4().hex}"
    monkeypatch.setattr(maintenance, "maintenance_mutex_name", lambda: name)
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    application = Mock()
    application.exec.return_value = 0
    monkeypatch.setattr(app_module, "QApplication", SimpleNamespace(instance=lambda: application))
    monkeypatch.setattr(app_module.theme, "apply_theme", Mock())
    guard = Mock()
    guard.start_or_activate.return_value = True
    monkeypatch.setattr(app_module, "GuiSingleInstance", Mock(return_value=guard))
    return application, guard


@pytest.mark.parametrize("blocked_by", ["busy", "pending"])
def test_gui_primary_refuses_before_window_or_database(
    isolated_gui_entry, monkeypatch, blocked_by
):
    _, guard = isolated_gui_entry
    window = Mock(side_effect=AssertionError("window reached"))
    database = Mock(side_effect=AssertionError("database reached"))
    monkeypatch.setattr(app_module, "SparkKeeperApp", window)
    monkeypatch.setattr(app_module, "Database", database)
    if blocked_by == "busy":
        with maintenance.MaintenanceLease(), pytest.raises(maintenance.MaintenanceBusy):
            app_module.run()
    else:
        path = maintenance.maintenance_journal_path()
        path.parent.mkdir(parents=True)
        path.write_text("corrupt", encoding="utf-8")
        with pytest.raises(maintenance.MaintenanceRequired, match="重新运行安装包"):
            app_module.run()
    window.assert_not_called()
    database.assert_not_called()
    guard.close.assert_called_once_with()


def test_gui_secondary_activates_without_maintenance_or_business_initialization(
    isolated_gui_entry, monkeypatch
):
    _, guard = isolated_gui_entry
    guard.start_or_activate.return_value = False
    for attribute in ("MaintenanceLease", "assert_maintenance_clear", "SparkKeeperApp", "Database"):
        monkeypatch.setattr(
            app_module, attribute, Mock(side_effect=AssertionError(f"secondary reached {attribute}"))
        )
    assert app_module.run() == 0
    guard.close.assert_called_once_with()


@pytest.mark.parametrize("initialization_fails", [False, True])
def test_gui_holds_gate_only_during_initialization(
    isolated_gui_entry, monkeypatch, initialization_fails
):
    application, guard = isolated_gui_entry
    window = Mock()

    def initialize(**_kwargs):
        guard.start_or_activate.assert_called_once_with()
        with pytest.raises(maintenance.MaintenanceBusy), maintenance.MaintenanceLease():
            pytest.fail("GUI initialized without maintenance gate")
        if initialization_fails:
            raise RuntimeError("database initialization failed")
        return window

    def event_loop():
        # A running GUI must not exclude an independent scheduled worker.
        with maintenance.MaintenanceLease():
            pass
        return 0

    monkeypatch.setattr(app_module, "SparkKeeperApp", initialize)
    application.exec.side_effect = event_loop
    if initialization_fails:
        with pytest.raises(RuntimeError, match="database initialization failed"):
            app_module.run()
    else:
        assert app_module.run() == 0
        window.show.assert_called_once_with()
        application.exec.assert_called_once_with()
    with maintenance.MaintenanceLease():
        pass
    guard.close.assert_called_once_with()


def test_gui_smoke_ignores_production_gate_and_journal(isolated_gui_entry, monkeypatch, tmp_path):
    _, guard = isolated_gui_entry
    path = maintenance.maintenance_journal_path()
    path.parent.mkdir(parents=True)
    path.write_text("corrupt production journal", encoding="utf-8")
    roots = []

    def initialize(*, smoke_mode):
        assert smoke_mode
        roots.append(os.environ["SPARK_KEEPER_ROOT"])
        assert roots[-1] != str(tmp_path)
        maintenance.assert_maintenance_clear()
        return Mock()

    monkeypatch.setattr(app_module, "SparkKeeperApp", initialize)
    monkeypatch.setattr(app_module, "QTimer", SimpleNamespace(singleShot=Mock()))
    with maintenance.MaintenanceLease():
        assert app_module.run(smoke_mode=True) == 0
    guard.start_or_activate.assert_not_called()
    assert os.environ["SPARK_KEEPER_ROOT"] == str(tmp_path)
    assert not os.path.exists(roots[0])
    assert path.read_text() == "corrupt production journal"
