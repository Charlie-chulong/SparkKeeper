from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from types import TracebackType
from typing import Self

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102

_registry_lock = threading.Lock()
_owned_names: set[str] = set()


class AlreadyRunningError(RuntimeError):
    pass


class WindowsTaskMutex:
    def __init__(self, name: str = r"Local\SparkKeeperLocalAutomation") -> None:
        self.name = name
        self._handle: int | None = None
        self._owned = False

    def acquire(self) -> None:
        if os.name != "nt":
            raise RuntimeError("跨进程任务互斥仅支持 Windows")
        if self._handle is not None:
            raise RuntimeError("互斥量实例不能重复获取")
        with _registry_lock:
            if self.name in _owned_names:
                raise AlreadyRunningError("已有网页自动化任务正在运行")
            _owned_names.add(self.name)

        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = int(handle)
            outcome = kernel32.WaitForSingleObject(handle, 0)
            if outcome in (WAIT_OBJECT_0, WAIT_ABANDONED):
                self._owned = True
                return
            kernel32.CloseHandle(handle)
            self._handle = None
            if outcome == WAIT_TIMEOUT:
                raise AlreadyRunningError("已有网页自动化任务正在运行")
            raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            with _registry_lock:
                _owned_names.discard(self.name)
            raise

    def release(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = wintypes.HANDLE(self._handle)
        try:
            if self._owned and not kernel32.ReleaseMutex(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
            self._handle = None
            self._owned = False
            with _registry_lock:
                _owned_names.discard(self.name)

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
