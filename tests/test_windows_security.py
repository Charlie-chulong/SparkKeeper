from __future__ import annotations

import os

import pytest

from spark_keeper.dpapi import DpapiJsonStore, protect, unprotect
from spark_keeper.mutex import AlreadyRunningError, WindowsTaskMutex

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only contract")


def test_dpapi_round_trip_and_ciphertext_is_not_plaintext(tmp_path) -> None:
    plaintext = "敏感 cookie 内容".encode()
    encrypted = protect(plaintext)
    assert plaintext not in encrypted
    assert unprotect(encrypted) == plaintext


def test_dpapi_json_store_never_writes_cookie_plaintext(tmp_path) -> None:
    store = DpapiJsonStore(tmp_path / "auth-state.bin")
    state = {"cookies": [{"name": "sessionid", "value": "top-secret-value"}], "origins": []}
    store.save(state)
    raw = store.path.read_bytes()
    assert b"top-secret-value" not in raw
    assert store.load() == state


def test_named_mutex_rejects_overlapping_owner() -> None:
    name = rf"Local\SparkKeeperTest-{os.getpid()}"
    with WindowsTaskMutex(name), pytest.raises(AlreadyRunningError), WindowsTaskMutex(name):
        pass
    with WindowsTaskMutex(name):
        pass
