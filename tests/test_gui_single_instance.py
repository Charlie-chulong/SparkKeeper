from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from unittest.mock import Mock

import pytest
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QDialog, QWidget

from spark_keeper import maintenance
from spark_keeper.ui import app as app_module
from spark_keeper.ui.single_instance import (
    GuiSingleInstance,
    InstanceNames,
    activate_window,
    current_instance_names,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named mutex and local pipe")


@pytest.fixture(autouse=True)
def isolated_maintenance_gate(tmp_path, monkeypatch):
    name = rf"Global\SparkKeeper.Maintenance.Test.{uuid.uuid4().hex}"
    monkeypatch.setattr(maintenance, "maintenance_mutex_name", lambda: name)
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))


def unique_names():
    name = f"SparkKeeper.Gui.Test.{uuid.uuid4().hex}"
    return InstanceNames(mutex=f"Local\\{name}", server=name)


CHILD = r'''
import sys, time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from spark_keeper.mutex import WindowsTaskMutex
from spark_keeper.ui.single_instance import GuiSingleInstance, InstanceNames
app = QApplication([])
names = InstanceNames(sys.argv[1], sys.argv[2])
mode = sys.argv[3]
if mode == "lease":
    lease = WindowsTaskMutex(names.mutex)
    lease.acquire()
    print("locked", flush=True)
    time.sleep(float(sys.argv[4]))
    lease.release()
else:
    guard = GuiSingleInstance(names)
    try:
        owner = guard.start_or_activate(timeout_ms=3000)
        print("owner" if owner else "secondary", flush=True)
        if owner:
            guard.set_activate_callback(lambda: print("activated", flush=True))
            time.sleep(float(sys.argv[4]))
            QTimer.singleShot(2500, app.quit)
            app.exec()
    finally:
        guard.close()
'''


def spawn(names, mode="gui", delay="0"):
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, names.mutex, names.server, mode, delay],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


def finish(process):
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stderr
    return stdout


def test_identity_is_stable_across_versions_paths_and_environment(monkeypatch, tmp_path):
    expected = current_instance_names()
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    monkeypatch.setenv("USERNAME", "not-the-token-user")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "new-version.exe"))
    assert current_instance_names() == expected
    assert "S-1-" in expected.server
    assert expected.server.rsplit(".", 1)[1].isdigit()


@pytest.mark.parametrize("delay", ["0", "0.6"])
def test_second_process_only_activates_even_during_startup(delay):
    names = unique_names()
    primary = spawn(names, delay=delay)
    assert primary.stdout.readline().strip() == "owner"
    secondary = spawn(names)
    assert finish(secondary).strip() == "secondary"
    assert "activated" in finish(primary)
    # Normal exit relinquishes the lease and pipe.
    replacement = spawn(names)
    assert finish(replacement).splitlines()[0] == "owner"


def test_simultaneous_processes_elect_exactly_one_owner():
    names = unique_names()
    children = [spawn(names) for _ in range(3)]
    results = [finish(child).splitlines() for child in children]
    assert sum(lines[0] == "owner" for lines in results) == 1
    assert sum(lines[0] == "secondary" for lines in results) == 2


def test_owner_exits_before_pipe_ready_allows_safe_takeover(qapp):
    names = unique_names()
    owner = spawn(names, "lease", "0.5")
    assert owner.stdout.readline().strip() == "locked"
    guard = GuiSingleInstance(names)
    try:
        assert guard.start_or_activate(timeout_ms=2000)
    finally:
        guard.close()
    finish(owner)


def test_unresponsive_owner_times_out_without_new_owner(qapp):
    names = unique_names()
    owner = spawn(names, "lease", "1.0")
    assert owner.stdout.readline().strip() == "locked"
    guard = GuiSingleInstance(names)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="不会另开窗口"):
            guard.start_or_activate(timeout_ms=100)
        assert not guard._server.isListening()
        assert time.monotonic() - started < 1
    finally:
        guard.close()
    finish(owner)


def test_pending_activation_is_delivered_once_and_unknown_commands_rejected(qapp):
    guard = GuiSingleInstance(unique_names())
    assert guard.start_or_activate()
    callback = Mock()
    try:
        for command in (b"send\n", b"activate\n", b"activate\n"):
            socket = QLocalSocket()
            socket.connectToServer(guard.names.server)
            assert socket.waitForConnected(1000)
            socket.write(command)
            socket.flush()
            deadline = time.monotonic() + 2
            while socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                qapp.processEvents()
                assert time.monotonic() < deadline
            response = bytes(socket.readAll())
            assert (b"ok\n" in response) == (command == b"activate\n")
        guard.set_activate_callback(callback)
        callback.assert_called_once_with()
    finally:
        guard.close()


def test_modal_receives_activation_and_minimized_maximized_state_is_preserved(qapp, monkeypatch):
    window = QWidget()
    dialog = QDialog(window)
    dialog.setModal(True)
    try:
        window.showMaximized()
        dialog.show()
        qapp.processEvents()
        window.showMinimized()
        qapp.processEvents()
        raise_modal = Mock()
        monkeypatch.setattr(dialog, "raise_", raise_modal)
        activate_window(window)
        assert not window.isMinimized()
        assert window.isMaximized()
        raise_modal.assert_called_once_with()
        assert dialog.isVisible()
    finally:
        dialog.close()
        window.close()


def test_secondary_run_never_constructs_business_window(qapp, monkeypatch):
    guard = Mock()
    guard.start_or_activate.return_value = False
    window = Mock(side_effect=AssertionError("secondary initialized database"))
    monkeypatch.setattr(app_module, "GuiSingleInstance", Mock(return_value=guard))
    monkeypatch.setattr(app_module, "SparkKeeperApp", window)
    assert app_module.run() == 0
    window.assert_not_called()
    guard.close.assert_called_once_with()


def test_initialization_failure_releases_lease(qapp, monkeypatch):
    names = unique_names()
    guard = GuiSingleInstance(names)
    monkeypatch.setattr(app_module, "GuiSingleInstance", lambda: guard)
    monkeypatch.setattr(app_module, "SparkKeeperApp", Mock(side_effect=RuntimeError("newer schema")))
    with pytest.raises(RuntimeError, match="newer schema"):
        app_module.run()
    replacement = GuiSingleInstance(names)
    try:
        assert replacement.start_or_activate(timeout_ms=100)
    finally:
        replacement.close()


def test_smoke_bypasses_production_lease_and_uses_temporary_data(qapp, monkeypatch, tmp_path):
    roots = []
    monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path))
    monkeypatch.setattr(app_module, "GuiSingleInstance", Mock(side_effect=AssertionError("smoke acquired production lease")))

    def build_window(*, smoke_mode):
        assert smoke_mode
        roots.append(os.environ["SPARK_KEEPER_ROOT"])
        window = QWidget()
        return window

    monkeypatch.setattr(app_module, "SparkKeeperApp", build_window)
    assert app_module.run(smoke_mode=True) == 0
    assert roots[0] != str(tmp_path)
    assert not os.path.exists(roots[0])
    assert os.environ["SPARK_KEEPER_ROOT"] == str(tmp_path)
