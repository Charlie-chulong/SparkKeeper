from __future__ import annotations

import ctypes
import json
import os
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any

CRYPTPROTECT_UI_FORBIDDEN = 0x01
_ENTROPY = b"spark-keeper-local-auth-state-v1"


class DpapiUnavailableError(RuntimeError):
    pass


class SecretDataError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _require_windows() -> None:
    if os.name != "nt":
        raise DpapiUnavailableError("DPAPI 仅能在 Windows 上使用")


def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _copy_output(blob: _DataBlob) -> bytes:
    if not blob.pbData or not blob.cbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(data: bytes, *, description: str = "Spark Keeper local state") -> bytes:
    _require_windows()
    if not data:
        raise ValueError("不能加密空数据")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, _input_buffer = _input_blob(data)
    entropy_blob, _entropy_buffer = _input_blob(_ENTROPY)
    # 两个缓冲区必须在 CryptProtectData 返回前保持存活。
    output_blob = _DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        description,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _copy_output(output_blob)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def unprotect(data: bytes) -> bytes:
    _require_windows()
    if not data:
        raise SecretDataError("加密状态文件为空")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, _input_buffer = _input_blob(data)
    entropy_blob, _entropy_buffer = _input_blob(_ENTROPY)
    # 两个缓冲区必须在 CryptUnprotectData 返回前保持存活。
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise SecretDataError("当前 Windows 用户无法解密登录状态") from ctypes.WinError(
            ctypes.get_last_error()
        )
    try:
        return _copy_output(output_blob)
    finally:
        if description:
            kernel32.LocalFree(ctypes.cast(description, wintypes.HLOCAL))
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


class DpapiJsonStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, value: dict[str, Any]) -> None:
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted = protect(plaintext)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise SecretDataError("尚未保存登录状态")
        try:
            plaintext = unprotect(self.path.read_bytes())
            value = json.loads(plaintext.decode("utf-8"))
        except SecretDataError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretDataError("登录状态文件损坏或不可读取") from exc
        if not isinstance(value, dict):
            raise SecretDataError("登录状态格式无效")
        return value

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
