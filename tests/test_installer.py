from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools" / "installer_guard.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(os.name != "nt" or not POWERSHELL, reason="Windows PowerShell required")


def manifest(directory: Path, version: str, files: dict[str, bytes]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        destination = directory / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    result = directory / "release-manifest.json"
    result.write_text(json.dumps({
        "format": 1, "product": "SparkKeeper", "version": version,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }), encoding="utf-8")
    return result


@pytest.fixture
def bundle(tmp_path):
    files = {"SparkKeeper.exe": b"fixture", "_internal/runtime.dll": b"runtime", "browsers/chrome.exe": b"browser"}
    old = tmp_path / "installed"
    new = tmp_path / "incoming"
    manifest(old, "3.0.1", {**files, "_internal/obsolete/old.dll": b"old"})
    incoming = manifest(new, "3.0.2", {**files, "_internal/new.dll": b"new"})
    return old, new, incoming


def probe(tmp_path, code, *, idle=True):
    script = tmp_path / "probe.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        ". $env:INSTALLER_GUARD -AppIdentity ('SparkKeeper.Test.' + [Guid]::NewGuid().ToString('N'))\n"
        + ("function Assert-InstallerIdle([string]$Target) {}\n" if idle else "")
        + "try {\n" + code + "\n} catch { [Console]::Error.WriteLine($_.Exception.ToString()); exit 1 }\n",
        encoding="utf-8-sig",
    )
    env = {**os.environ, "INSTALLER_GUARD": str(GUARD), "PROBE_ROOT": str(tmp_path)}
    env.pop("SPARK_KEEPER_ROOT", None)
    completed = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, timeout=60, env=env, check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")


def test_first_install_preflight_never_creates_target(tmp_path, bundle):
    probe(tmp_path, """
$target = Join-Path $env:PROBE_ROOT 'new-target'
Invoke-InstallerPrepare $target (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') (Join-Path $env:PROBE_ROOT 'state.json')
if (Test-Path -LiteralPath $target) { throw 'preflight created target' }
""")


def test_upgrade_retires_only_verified_obsolete_files(tmp_path, bundle):
    old, _new, _incoming = bundle
    probe(tmp_path, """
$target = Join-Path $env:PROBE_ROOT 'installed'
$state = Join-Path $env:PROBE_ROOT 'state.json'
Invoke-InstallerPrepare $target (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') $state
Copy-Item -Path (Join-Path $env:PROBE_ROOT 'incoming/*') -Destination $target -Recurse -Force
Invoke-InstallerFinish $target $state
""")
    assert not (old / "_internal/obsolete").exists()
    assert (old / "_internal/new.dll").read_bytes() == b"new"
    assert json.loads((old / "release-manifest.json").read_text())['version'] == "3.0.2"


@pytest.mark.parametrize("name", ["notes.txt", "unins000.dat", ".installer/update.ps1"])
def test_unknown_files_rejected_without_modification(tmp_path, bundle, name):
    old, _, _ = bundle
    unknown = old / name
    unknown.parent.mkdir(parents=True, exist_ok=True)
    unknown.write_bytes(b"user-owned")
    before = {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()}
    probe(tmp_path, """
try {
    Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'installed') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') (Join-Path $env:PROBE_ROOT 'state.json')
    throw 'expected manifest rejection'
} catch { if ($_.Exception.Data['ExitCode'] -ne 20) { throw } }
""")
    assert {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()} == before


def test_modified_payload_rejected(tmp_path, bundle):
    old, _, _ = bundle
    (old / "SparkKeeper.exe").write_bytes(b"modified")
    probe(tmp_path, """
try {
    Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'installed') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') (Join-Path $env:PROBE_ROOT 'state.json')
    throw 'expected hash rejection'
} catch { if ($_.Exception.Data['ExitCode'] -ne 20) { throw } }
""")
    assert (old / "SparkKeeper.exe").read_bytes() == b"modified"


def test_downgrade_is_rejected(tmp_path, bundle):
    old, _, incoming = bundle
    data = json.loads(incoming.read_text())
    data["version"] = "3.0.0"
    incoming.write_text(json.dumps(data))
    probe(tmp_path, """
try {
    Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'installed') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') (Join-Path $env:PROBE_ROOT 'state.json')
    throw 'expected downgrade rejection'
} catch { if ($_.Exception.Data['ExitCode'] -ne 21) { throw } }
""")
    assert (old / "_internal/obsolete/old.dll").read_bytes() == b"old"


def test_changed_obsolete_file_is_not_deleted(tmp_path, bundle):
    old, _, _ = bundle
    probe(tmp_path, """
$target = Join-Path $env:PROBE_ROOT 'installed'
$state = Join-Path $env:PROBE_ROOT 'state.json'
Invoke-InstallerPrepare $target (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') $state
Copy-Item -Path (Join-Path $env:PROBE_ROOT 'incoming/*') -Destination $target -Recurse -Force
[IO.File]::WriteAllText((Join-Path $target '_internal/obsolete/old.dll'), 'changed')
try { Invoke-InstallerFinish $target $state; throw 'expected changed obsolete rejection' }
catch { if ($_.Exception.Data['ExitCode'] -ne 20) { throw } }
""")
    assert (old / "_internal/obsolete/old.dll").read_bytes() == b"changed"


def test_data_root_and_manifest_traversal_rejected(tmp_path, bundle):
    _, _, incoming = bundle
    data = json.loads(incoming.read_text())
    data["files"]["../outside.txt"] = "a" * 64
    incoming.write_text(json.dumps(data))
    probe(tmp_path, """
try {
    Resolve-InstallTarget (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SparkKeeper')
    throw 'expected data directory rejection'
} catch { if ($_.Exception.Data['ExitCode'] -ne 10) { throw } }
try { Read-IncomingManifest (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json'); throw 'expected traversal rejection' }
catch { if ($_.Exception.Data['ExitCode'] -ne 20) { throw } }
""")


@pytest.mark.parametrize("image,command", [
    ("C:\\elsewhere\\SparkKeeper.exe", ""),
    ("C:\\Python\\pythonw.exe", "pythonw -m spark_keeper.worker --scheduled"),
    ("C:\\old\\SparkKeeper\\browsers\\chrome.exe", ""),
])
def test_other_installation_processes_block_operations(tmp_path, image, command):
    escaped_image = image.replace("'", "''")
    escaped_command = command.replace("'", "''")
    probe(tmp_path, f"""
function Assert-SchedulerStopped {{}}
function Get-CimInstance {{
    [pscustomobject]@{{ ProcessId=12345; Name=[IO.Path]::GetFileName('{escaped_image}'); ExecutablePath='{escaped_image}'; CommandLine='{escaped_command}' }}
}}
try {{ Assert-InstallerIdle (Join-Path $env:PROBE_ROOT 'target'); throw 'expected process rejection' }}
catch {{ if ($_.Exception.Data['ExitCode'] -ne 31) {{ throw }} }}
""", idle=False)


@pytest.mark.parametrize("version,expected", [("3.1.0", 21), ("3.0.1", 10)])
def test_registered_install_cannot_be_downgraded_or_redirected(tmp_path, bundle, version, expected):
    probe(tmp_path, f"""
$base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
    [Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
$path = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${{AppIdentity}}_is1"
try {{
    $key = $base.CreateSubKey($path)
    $key.SetValue('DisplayVersion', '{version}')
    $key.SetValue('InstallLocation', (Join-Path $env:PROBE_ROOT 'registered'))
    $key.Dispose()
    try {{
        Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'empty-target') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json') (Join-Path $env:PROBE_ROOT 'state.json')
        throw 'expected registered installation rejection'
    }} catch {{ if ($_.Exception.Data['ExitCode'] -ne {expected}) {{ throw }} }}
}} finally {{ $base.DeleteSubKeyTree($path, $false); $base.Dispose() }}
""")
    assert not (tmp_path / "empty-target").exists()


def test_uninstall_exempts_only_its_verified_bootstrap(tmp_path):
    probe(tmp_path, """
function Assert-SchedulerStopped {}
$Mode = 'Uninstall'
$ParentProcessId = 12345
$target = Join-Path $env:PROBE_ROOT 'target'
function Get-CimInstance {
    [pscustomobject]@{ProcessId=12345; ParentProcessId=12344; Name='unins000.tmp'; ExecutablePath=(Join-Path $env:PROBE_ROOT 'unins000.tmp'); CommandLine=''}
    [pscustomobject]@{ProcessId=12344; ParentProcessId=1; Name='unins000.exe'; ExecutablePath=(Join-Path $target 'unins000.exe'); CommandLine=''}
}
Assert-InstallerIdle $target
function Get-CimInstance {
    [pscustomobject]@{ProcessId=12345; ParentProcessId=1; Name='unins000.tmp'; ExecutablePath=(Join-Path $env:PROBE_ROOT 'unins000.tmp'); CommandLine=''}
    [pscustomobject]@{ProcessId=12344; ParentProcessId=1; Name='unins000.exe'; ExecutablePath=(Join-Path $target 'unins000.exe'); CommandLine=''}
}
try { Assert-InstallerIdle $target; throw 'unrelated uninstaller must block' }
catch { if ($_.Exception.Data['ExitCode'] -ne 31) { throw } }
""", idle=False)
