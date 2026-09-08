from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from ..mutex import AlreadyRunningError, WindowsTaskMutex
from ..windows_identity import current_user_sid


@dataclass(frozen=True, slots=True)
class InstanceNames:
    mutex: str
    server: str


def current_instance_names() -> InstanceNames:
    """The identity is stable across versions, executable paths and data overrides."""
    if os.name != "nt":
        raise RuntimeError("界面单实例仅支持 Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user_sid = current_user_sid()
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    identity = f"SparkKeeper.Gui.{user_sid}.{session.value}"
    return InstanceNames(mutex=f"Local\\{identity}", server=identity)


def allow_foreground(process_id: int) -> None:
    if os.name == "nt":
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user32.AllowSetForegroundWindow.restype = wintypes.BOOL
        user32.AllowSetForegroundWindow(process_id)


def activate_window(window: QWidget) -> None:
    """Restore without discarding maximization; never hide a modal behind its parent."""
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    window.show()
    modal = QApplication.activeModalWidget()
    target = modal if modal is not None else window
    target.show()
    target.raise_()
    target.activateWindow()
    if os.name == "nt":
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow(int(target.winId()))
    QApplication.alert(target, 3000)


class GuiSingleInstance(QObject):
    """Mutex elects the owner; a user-only local pipe carries only activate requests."""

    def __init__(self, names: InstanceNames | None = None) -> None:
        super().__init__()
        self.names = names or current_instance_names()
        self._mutex = WindowsTaskMutex(self.names.mutex)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept_connections)
        self._clients: set[QLocalSocket] = set()
        self._activate: Callable[[], None] | None = None
        self._pending = False
        self._owner = False

    def start_or_activate(self, timeout_ms: int = 8000) -> bool:
        """True: own GUI lease. False: existing GUI acknowledged. Never fail open."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                self._mutex.acquire()
            except AlreadyRunningError:
                pass
            else:
                self._owner = True
                if not self._server.listen(self.names.server):
                    error = self._server.errorString()
                    self.close()
                    raise RuntimeError(f"无法建立界面激活通道：{error}")
                return True
            if time.monotonic() >= deadline:
                raise RuntimeError("续火花助手已在运行，但暂未响应。请等待原窗口完成启动或当前操作；不会另开窗口。")
            if self._notify_owner(deadline):
                return False
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def _notify_owner(self, deadline: float) -> bool:
        socket = QLocalSocket()

        def remaining() -> int:
            return max(1, min(200, int((deadline - time.monotonic()) * 1000)))

        def read_line() -> bytes:
            while time.monotonic() < deadline:
                if socket.canReadLine():
                    return bytes(socket.readLine(64))
                if socket.bytesAvailable() > 63 or not socket.waitForReadyRead(remaining()):
                    return b""
            return b""

        try:
            socket.connectToServer(self.names.server)
            if not socket.waitForConnected(remaining()):
                return False
            greeting = read_line()
            prefix = b"SparkKeeper-activate/1 "
            if not greeting.startswith(prefix) or not greeting.endswith(b"\n"):
                return False
            raw_pid = greeting[len(prefix):-1]
            if not raw_pid.isdigit() or not 0 < int(raw_pid) < 0xFFFFFFFF:
                return False
            allow_foreground(int(raw_pid))
            socket.write(b"activate\n")
            socket.flush()
            return read_line() == b"ok\n"
        finally:
            socket.abort()

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            if len(self._clients) >= 16:
                socket.abort()
                socket.deleteLater()
                continue
            self._clients.add(socket)
            socket.setReadBufferSize(64)
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(socket.abort)
            timer.start(2000)
            socket.disconnected.connect(lambda client=socket: self._drop_client(client))
            socket.readyRead.connect(lambda client=socket: self._receive_activation(client))
            socket.write(f"SparkKeeper-activate/1 {os.getpid()}\n".encode("ascii"))
            socket.flush()
            if socket.bytesAvailable():
                self._receive_activation(socket)

    def _drop_client(self, socket: QLocalSocket) -> None:
        self._clients.discard(socket)
        socket.deleteLater()

    def _receive_activation(self, socket: QLocalSocket) -> None:
        if socket.bytesAvailable() > len(b"activate\n"):
            socket.abort()
            return
        if not socket.canReadLine():
            return
        if bytes(socket.readAll()) != b"activate\n":
            socket.abort()
            return
        if self._activate is None:
            self._pending = True
        else:
            self._activate()
        socket.write(b"ok\n")
        socket.flush()
        socket.disconnectFromServer()

    def set_activate_callback(self, callback: Callable[[], None]) -> None:
        self._activate = callback
        if self._pending:
            self._pending = False
            callback()

    def close(self) -> None:
        self._activate = None
        self._pending = False
        self._server.close()
        for socket in tuple(self._clients):
            socket.abort()
        self._clients.clear()
        if self._owner:
            self._owner = False
            self._mutex.release()
