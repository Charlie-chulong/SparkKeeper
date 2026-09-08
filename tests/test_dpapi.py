from __future__ import annotations

from pathlib import Path

import pytest

from spark_keeper.dpapi import DpapiJsonStore, SecretDataError


@pytest.mark.parametrize("phase", ["encrypt", "flush", "replace"])
def test_failed_new_credentials_leave_no_usable_state_or_temporary_files(tmp_path, monkeypatch, phase):
    store = DpapiJsonStore(tmp_path / "auth-state.bin")
    store.path.write_bytes(b"old-encrypted-state")
    store.delete()

    def fail(*_args, **_kwargs):
        raise OSError(f"{phase} failed")

    monkeypatch.setattr("spark_keeper.dpapi.protect", lambda _data: b"encrypted-new-state")
    if phase == "encrypt":
        monkeypatch.setattr("spark_keeper.dpapi.protect", fail)
    elif phase == "flush":
        monkeypatch.setattr("spark_keeper.dpapi.os.fsync", fail)
    else:
        monkeypatch.setattr("spark_keeper.dpapi.os.replace", fail)
    with pytest.raises(OSError, match=f"{phase} failed"):
        store.save({"cookies": [{"name": "sessionid", "value": "secret-cookie"}]})
    assert not store.exists()
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(SecretDataError, match="尚未保存登录状态"):
        store.load()


def test_delete_permission_failure_is_not_suppressed(tmp_path, monkeypatch):
    store = DpapiJsonStore(tmp_path / "auth-state.bin")
    store.path.write_bytes(b"old-encrypted-state")
    unlink = Path.unlink

    def denied(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("credential deletion denied")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(PermissionError, match="credential deletion denied"):
        store.delete()
    assert store.path.read_bytes() == b"old-encrypted-state"


def test_delete_missing_credentials_is_idempotent(tmp_path):
    store = DpapiJsonStore(tmp_path / "missing-state.bin")
    store.delete()
    store.delete()
    assert not store.exists()
