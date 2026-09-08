from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from spark_keeper import __main__ as entry
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
