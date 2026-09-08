from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATER = Path(__file__).resolve().parents[1] / "tools" / "update.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")


def make_release(directory: Path, version: str = "3.0.0", marker: str = "new") -> None:
    directory.mkdir(parents=True)
    files = {}
    for name in ("SparkKeeper.exe", "_internal/runtime.bin", "browsers/chrome.bin"):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"{marker}:{name}".encode())
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (directory / "release-manifest.json").write_text(
        json.dumps({"format": 1, "product": "SparkKeeper", "version": version, "files": files}),
        encoding="utf-8",
    )


def snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def release_pair(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SPARK_KEEPER_ROOT", raising=False)
    source = tmp_path / "unpacked" / "SparkKeeper"
    target = tmp_path / "installed" / "SparkKeeper"
    make_release(source)
    make_release(target, "2.9.0", "old")
    return source, target


def run_update(tmp_path: Path, source: Path, target: Path, *, setup: str = "", accepted: bool = False):
    # Filesystem cases isolate only the external, read-only scheduler/process gate.
    # Production has no skip-check switch and executes both gates twice.
    script = tmp_path / "exercise-updater.ps1"
    script.write_text(
        ". $env:UPDATER_SCRIPT\n"
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        "function Assert-UpdateIdle([string]$Source, [string]$Target) {}\n"
        + setup
        + "\ntry {\n"
        + f"$result = Invoke-SparkKeeperUpdate $env:UPDATE_SOURCE $env:UPDATE_TARGET ${str(accepted).lower()}\n"
        + "$result | ConvertTo-Json -Compress\nexit 0\n"
        + "} catch {\n"
        + "[Console]::Error.WriteLine($_.Exception.Message)\n"
        + "if ($_.Exception.Data.Contains('ExitCode')) { exit ([int]$_.Exception.Data['ExitCode']) }\n"
        + "exit 99\n}\n",
        encoding="utf-8-sig",
    )
    env = os.environ.copy()
    env.update(UPDATER_SCRIPT=str(UPDATER), UPDATE_SOURCE=str(source), UPDATE_TARGET=str(target))
    return subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=45,
        check=False,
    )


def test_update_replaces_whole_directory_and_keeps_old_files_and_external_data(tmp_path, release_pair):
    source, target = release_pair
    old = snapshot(target)
    external_data = tmp_path / "isolated-appdata" / "SparkKeeper" / "data"
    external_data.mkdir(parents=True)
    for name in ("spark-keeper.sqlite3", "spark-keeper.sqlite3-wal", "auth-state.bin"):
        (external_data / name).write_bytes(b"private-state-do-not-touch")
    before = snapshot(external_data)
    result = run_update(tmp_path, source, target)
    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout.splitlines()[-1])
    assert snapshot(target) == snapshot(source)
    assert snapshot(Path(details["Backup"])) == old
    assert snapshot(external_data) == before
    assert not list(target.parent.glob(".SparkKeeper-stage-*"))


@pytest.mark.parametrize("damage", ["hash", "missing", "extra", "product", "version", "traversal"])
def test_invalid_source_preserves_target(tmp_path, release_pair, damage):
    source, target = release_pair
    old = snapshot(target)
    manifest_path = source / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "hash":
        (source / "_internal/runtime.bin").write_bytes(b"corrupted")
    elif damage == "missing":
        (source / "browsers/chrome.bin").unlink()
    elif damage == "extra":
        (source / "unexpected.txt").write_text("not in manifest")
    else:
        if damage == "product":
            manifest["product"] = "UnrelatedProduct"
        elif damage == "version":
            manifest["version"] = "3.0.0.dev1"
        else:
            manifest["files"]["../outside.exe"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_update(tmp_path, source, target)
    assert result.returncode == (21 if damage == "version" else 20), result.stderr
    assert snapshot(target) == old
    assert not list(target.parent.glob("SparkKeeper.backup-*"))


@pytest.mark.parametrize("name", ["personal-notes.txt", "_internal/private.db", "browsers/auth-state.bin"])
def test_target_unknown_files_are_never_discarded(tmp_path, release_pair, name):
    source, target = release_pair
    (target / name).write_bytes(b"user content")
    old = snapshot(target)
    result = run_update(tmp_path, source, target)
    assert result.returncode == 20
    assert snapshot(target) == old


def test_unknown_empty_directory_is_not_silently_discarded(tmp_path, release_pair):
    source, target = release_pair
    personal = target / "personal-folder"
    personal.mkdir()
    result = run_update(tmp_path, source, target)
    assert result.returncode == 20, result.stderr
    assert personal.is_dir()


def test_downgrade_preserves_target(tmp_path, release_pair):
    source, target = release_pair
    manifest_path = target / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "4.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    old = snapshot(target)
    result = run_update(tmp_path, source, target)
    assert result.returncode == 21
    assert snapshot(target) == old


def test_shared_read_file_lock_refuses_update(tmp_path, release_pair):
    source, target = release_pair
    old = snapshot(target)
    # Hash checks can read this file, but exclusive preflight must reject it.
    result = run_update(
        tmp_path, source, target,
        setup="$lock = [IO.File]::Open((Join-Path $env:UPDATE_TARGET '_internal/runtime.bin'), 'Open', 'Read', 'Read')",
    )
    assert result.returncode == 32, result.stderr
    assert snapshot(target) == old


def test_second_rename_failure_restores_real_old_directory(tmp_path, release_pair):
    source, target = release_pair
    old = snapshot(target)
    result = run_update(
        tmp_path, source, target,
        setup="""
function Move-UpdateDirectory([string]$From, [string]$To) {
    if ([IO.Path]::GetFileName($From).StartsWith('.SparkKeeper-stage-')) {
        # An actual Directory.Move failure, not a pretend successful mutation.
        [IO.Directory]::Move($From, $env:UPDATE_SOURCE)
    } else { [IO.Directory]::Move($From, $To) }
}
""",
    )
    assert result.returncode == 40, result.stderr
    assert snapshot(target) == old
    assert not list(target.parent.glob("SparkKeeper.backup-*"))
    assert not list(target.parent.glob(".SparkKeeper-stage-*"))


def test_rollback_failure_retains_complete_backup_without_deleting_unknown_target(tmp_path, release_pair):
    source, target = release_pair
    old = snapshot(target)
    result = run_update(
        tmp_path, source, target,
        setup="""
function Move-UpdateDirectory([string]$From, [string]$To) {
    if ([IO.Path]::GetFileName($From).StartsWith('.SparkKeeper-stage-')) {
        [void][IO.Directory]::CreateDirectory($To)
        [IO.File]::WriteAllText((Join-Path $To 'concurrent-user-file.txt'), 'keep me')
    }
    [IO.Directory]::Move($From, $To)
}
""",
    )
    assert result.returncode == 41, result.stderr
    backups = list(target.parent.glob("SparkKeeper.backup-*"))
    assert len(backups) == 1
    assert snapshot(backups[0]) == old
    assert (target / "concurrent-user-file.txt").read_text() == "keep me"


def test_legacy_requires_explicit_acceptance_and_preserves_complete_backup(tmp_path, release_pair):
    source, target = release_pair
    (target / "release-manifest.json").unlink()
    (target / "使用说明.txt").write_text("续火花助手 Windows 本地自用版 0.2.0.dev18\n", encoding="utf-8-sig")
    old = snapshot(target)
    result = run_update(tmp_path, source, target)
    assert result.returncode == 22, result.stderr
    assert snapshot(target) == old
    result = run_update(tmp_path, source, target, accepted=True)
    assert result.returncode == 0, result.stderr
    details = json.loads(result.stdout.splitlines()[-1])
    assert snapshot(Path(details["Backup"])) == old
    assert (target / "release-manifest.json").is_file()


@pytest.mark.parametrize("kind", ["same", "nested", "root", "override"])
def test_unsafe_paths_fail_before_mutation(tmp_path, release_pair, monkeypatch, kind):
    source, target = release_pair
    old = snapshot(target)
    if kind == "same":
        source = target
    elif kind == "nested":
        source = target / "_internal"
    elif kind == "root":
        source = Path(source.anchor)
    else:
        monkeypatch.setenv("SPARK_KEEPER_ROOT", str(tmp_path / "developer-data"))
    result = run_update(tmp_path, source, target)
    assert result.returncode == 10, result.stderr
    assert snapshot(target) == old


@pytest.mark.parametrize("gate", ["enabled", "running", "scheduler-error", "process", "source-process"])
def test_safety_gates_abort_before_staging(tmp_path, release_pair, gate):
    source, target = release_pair
    old = snapshot(target)
    if gate in {"process", "source-process"}:
        image = "$env:UPDATE_SOURCE + '\\browsers\\chrome.exe'" if gate == "source-process" else "'C:\\elsewhere\\SparkKeeper.exe'"
        name = "chrome.exe" if gate == "source-process" else "SparkKeeper.exe"
        setup = f"""
function Get-CimInstance {{
    [pscustomobject]@{{ ProcessId = 999999; Name = '{name}'; ExecutablePath = ({image}); CommandLine = '' }}
}}
function Assert-UpdateIdle([string]$Source, [string]$Target) {{ Assert-ProcessesStopped $Source $Target }}
"""
    elif gate == "scheduler-error":
        setup = """
function New-Object { throw 'Scheduler unavailable' }
function Assert-UpdateIdle([string]$Source, [string]$Target) { Assert-SchedulerStopped }
"""
    else:
        enabled = "$true" if gate == "enabled" else "$false"
        state = 4 if gate == "running" else 3
        setup = f"""
function New-Object {{
    $service = [pscustomobject]@{{}}
    $service | Add-Member ScriptMethod Connect {{}}
    $service | Add-Member ScriptMethod GetFolder {{
        $folder = [pscustomobject]@{{}}
        $folder | Add-Member ScriptMethod GetTask {{ [pscustomobject]@{{Enabled={enabled}; State={state}}} }}
        return $folder
    }}
    return $service
}}
function Assert-UpdateIdle([string]$Source, [string]$Target) {{ Assert-SchedulerStopped }}
"""
    result = run_update(tmp_path, source, target, setup=setup)
    assert result.returncode == (31 if "process" in gate else 30), result.stderr
    assert snapshot(target) == old
    assert not list(target.parent.glob(".SparkKeeper-stage-*"))


@pytest.mark.parametrize(
    ("installed", "incoming", "code"),
    [
        ("3.0.0rc2", "3.0.0rc10", 0),
        ("3.0.0rc10", "3.0.0rc2", 21),
        ("3.0.0rc10", "3.0.0", 0),
        ("3.0.0", "3.0.0rc10", 21),
        ("3.0.0", "3.1.0rc1", 0),
        ("4.0.0rc1", "3.9.9", 21),
        ("3.0.0rc1", "3.0.0rc1", 0),
        ("2.9.0", "3.0.0rc0", 21),
        ("2.9.0", "3.0.0rc01", 21),
        ("2.9.0", "3.0.0-rc.1", 21),
        ("2.9.0", "3.0.0.dev18", 21),
    ],
)
def test_release_candidate_version_ordering(tmp_path, installed, incoming, code):
    source, target = tmp_path / "source", tmp_path / "target"
    make_release(source, incoming)
    make_release(target, installed, "old")
    old = snapshot(target)
    result = run_update(tmp_path, source, target)
    assert result.returncode == code, result.stderr
    if code:
        assert snapshot(target) == old
    else:
        assert snapshot(target) == snapshot(source)


def test_target_mutex_refuses_concurrent_updater_then_releases(tmp_path, release_pair):
    source, target = release_pair
    old = snapshot(target)
    holder_script = tmp_path / "hold-update-lock.ps1"
    holder_script.write_text(
        ". $env:UPDATER_SCRIPT\n"
        "$mutex = Enter-UpdateMutex (Resolve-UpdateDirectory $env:UPDATE_TARGET)\n"
        "try {\n"
        "[Console]::WriteLine('LOCK_HELD'); [Console]::Out.Flush()\n"
        "[void][Console]::ReadLine()\n"
        "} finally { $mutex.ReleaseMutex(); $mutex.Dispose() }\n",
        encoding="utf-8-sig",
    )
    env = os.environ.copy()
    env.update(UPDATER_SCRIPT=str(UPDATER), UPDATE_TARGET=str(target))
    holder = subprocess.Popen(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(holder_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert holder.stdout.readline().strip() == b"LOCK_HELD"
        result = run_update(tmp_path, source, target)
        assert result.returncode == 60, result.stderr
        assert snapshot(target) == old
        assert not list(target.parent.glob(".SparkKeeper-stage-*"))
    finally:
        holder.communicate(b"\n", timeout=15)
    assert holder.returncode == 0
    result = run_update(tmp_path, source, target)
    assert result.returncode == 0, result.stderr
    assert snapshot(target) == snapshot(source)


def test_target_mutex_releases_after_failed_update(tmp_path, release_pair):
    source, target = release_pair
    runtime = source / "_internal/runtime.bin"
    valid = runtime.read_bytes()
    runtime.write_bytes(b"corrupt")
    result = run_update(tmp_path, source, target)
    assert result.returncode == 20
    runtime.write_bytes(valid)
    result = run_update(tmp_path, source, target)
    assert result.returncode == 0, result.stderr
