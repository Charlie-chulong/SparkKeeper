from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

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
        "$OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        "[Console]::OutputEncoding = $OutputEncoding\n"
        "$identity = 'SparkKeeper.Test.' + [Guid]::NewGuid().ToString('N')\n"
        "$privateRoot = Join-Path ([IO.Path]::GetTempPath()) ('SparkKeeper-maintenance/' + $identity)\n"
        "function New-Object { throw 'Unexpected object/COM construction in isolated probe' }\n"
        ". $env:INSTALLER_GUARD -AppIdentity $identity\n"
        "function Get-MaintenanceTask { return $null }\n"
        "function Get-MaintenanceTaskFolder { throw 'Unexpected scheduler folder access' }\n"
        "function Get-MaintenanceScheduledTasks { return @() }\n"
        + ("function Assert-InstallerIdle([string]$Target) {}\n" if idle else "")
        + "try {\n" + code + "\n} catch { [Console]::Error.WriteLine($_.Exception.ToString()); exit 1 }\n"
        + "finally { if (Test-Path -LiteralPath $privateRoot) { Remove-Item -LiteralPath $privateRoot -Recurse -Force } }\n",
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
Invoke-InstallerPrepare $target (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
if (Test-Path -LiteralPath $target) { throw 'preflight created target' }
""")


def test_upgrade_retires_only_verified_obsolete_files(tmp_path, bundle):
    old, _new, _incoming = bundle
    probe(tmp_path, """
$target = Join-Path $env:PROBE_ROOT 'installed'
Invoke-InstallerPrepare $target (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
Invoke-MaintenanceMutating $target
Copy-Item -Path (Join-Path $env:PROBE_ROOT 'incoming/*') -Destination $target -Recurse -Force
Invoke-InstallerFinish $target
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
    Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'installed') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
    throw 'expected manifest rejection'
} catch { if ($_.Exception.Data['ExitCode'] -ne 20) { throw } }
""")
    assert {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()} == before


def test_modified_payload_rejected(tmp_path, bundle):
    old, _, _ = bundle
    (old / "SparkKeeper.exe").write_bytes(b"modified")
    probe(tmp_path, """
try {
    Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'installed') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
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
    Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'installed') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
    throw 'expected downgrade rejection'
} catch { if ($_.Exception.Data['ExitCode'] -ne 21) { throw } }
""")
    assert (old / "_internal/obsolete/old.dll").read_bytes() == b"old"


def test_changed_obsolete_file_is_not_deleted(tmp_path, bundle):
    old, _, _ = bundle
    probe(tmp_path, """
$target = Join-Path $env:PROBE_ROOT 'installed'
Invoke-InstallerPrepare $target (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
Invoke-MaintenanceMutating $target
Copy-Item -Path (Join-Path $env:PROBE_ROOT 'incoming/*') -Destination $target -Recurse -Force
[IO.File]::WriteAllText((Join-Path $target '_internal/obsolete/old.dll'), 'changed')
try { Invoke-InstallerFinish $target; throw 'expected changed obsolete rejection' }
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
    Resolve-InstallTarget (Get-MaintenanceDataRoot)
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
function Assert-SchedulerStopped {{ throw 'Legacy real scheduler boundary must not be used' }}
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
        Invoke-InstallerPrepare (Join-Path $env:PROBE_ROOT 'empty-target') (Join-Path $env:PROBE_ROOT 'incoming/release-manifest.json')
        throw 'expected registered installation rejection'
    }} catch {{ if ($_.Exception.Data['ExitCode'] -ne {expected}) {{ throw }} }}
}} finally {{ $base.DeleteSubKeyTree($path, $false); $base.Dispose() }}
""")
    assert not (tmp_path / "empty-target").exists()


def test_uninstall_exempts_only_its_verified_bootstrap(tmp_path):
    probe(tmp_path, """
function Assert-SchedulerStopped { throw 'Legacy real scheduler boundary must not be used' }
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


MAINTENANCE_SCENARIOS = [
    "owned-enabled",
    "owned-disabled",
    "owned-none",
    "owned-principal-account",
    "owned-principal-xml-sid",
    *(
        f"foreign-{kind}{suffix}"
        for kind in (
            "action", "principal", "principal-account", "xml-principal", "xml-multiple-principals",
            "xml-group", "xml-bare-principal", "taskpath", "multiple", "arguments", "working-directory", "action-type",
        )
        for suffix in ("", "-disabled")
    ),
    "running-state",
    "running-instances",
    "running-disabled-instances",
    "running-queued",
    "start-when-available",
    "fingerprint",
    "disable-readback",
    "restore-failure",
    "cancel-unchanged",
    "cancel-mutating-old",
    "cancel-partial",
    "finish-hash-failure",
    "finish-manifest-failure",
    "finish-unknown-failure",
    "restart-partial",
    "restart-first-metadata",
    "restart-first-unknown",
    "definition-changed",
    "definition-deleted",
    "definition-created",
    "uninstall-no-confirm",
    "uninstall-no-confirm-finish",
    "uninstall-success",
    "uninstall-disabled",
    "uninstall-none",
    "uninstall-cancel",
    "uninstall-partial",
    "uninstall-payload-remains",
    "uninstall-delete-failure",
    "uninstall-delete-readback",
    "uninstall-changed",
    "uninstall-disappeared",
    "purge-foreign-named-enabled",
    "purge-foreign-named-disabled",
    "purge-foreign-shared-exe",
    "purge-foreign-shared-module",
    "purge-foreign-shared-data",
    "purge-foreign-shared-working-directory",
    "purge-reference-appears",
    "purge-junction-root",
    "purge-junction-child",
    "purge-failure-repair-same",
    "purge-failure-repair-newer",
    "uninstall-remove-files-success",
    "uninstall-remove-files-locked-retry",
    "uninstall-remove-files-unknown",
    "purge-receipt-repair",
    "purge-receipt-foreign-identity",
    "purge-receipt-foreign-target",
]


@pytest.mark.parametrize("scenario", MAINTENANCE_SCENARIOS)
def test_maintenance_transaction_contract(tmp_path, bundle, scenario):
    old, _, incoming = bundle
    if scenario.startswith("purge-failure-repair-"):
        shutil.copytree(old, tmp_path / "old-backup")
        if scenario.endswith("-same"):
            data = json.loads(incoming.read_text())
            data["version"] = "3.0.1"
            incoming.write_text(json.dumps(data), encoding="utf-8")
    env = {**os.environ, "INSTALLER_GUARD": str(GUARD), "PROBE_ROOT": str(tmp_path)}
    env.pop("SPARK_KEEPER_ROOT", None)
    completed = subprocess.run(
        [
            POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "tests" / "maintenance_tasks_probe.ps1"), "-Scenario", scenario,
        ],
        capture_output=True, timeout=90, env=env, check=False,
    )
    stdout = completed.stdout.decode(errors="replace")
    stderr = completed.stderr.decode(errors="replace")
    if completed.returncode == 77 and scenario.startswith("purge-junction-"):
        pytest.skip(stdout.strip())
    assert completed.returncode == 0, f"{scenario}\n{stdout}\n{stderr}"
    assert f"PASS: {scenario}" in stdout


def identity_probe(tmp_path, mode, app_identity):
    script = tmp_path / "identity-probe.ps1"
    result = tmp_path / f"identity-{uuid4().hex}.txt"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        "[Console]::OutputEncoding = $OutputEncoding\n"
        "function New-Object { throw 'Unexpected object/COM construction in identity probe' }\n"
        "& $env:INSTALLER_GUARD -Mode $env:PROBE_MODE -AppIdentity $env:PROBE_IDENTITY -ResultPath $env:PROBE_RESULT\n"
        "exit $LASTEXITCODE\n",
        encoding="utf-8-sig",
    )
    env = {
        **os.environ,
        "INSTALLER_GUARD": str(GUARD),
        "PROBE_MODE": mode,
        "PROBE_IDENTITY": app_identity,
        "PROBE_RESULT": str(result),
    }
    env.pop("SPARK_KEEPER_ROOT", None)
    completed = subprocess.run(
        [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, timeout=60, env=env, check=False,
    )
    return completed, result


@pytest.mark.parametrize("mode", ["Identity", "MaintenanceIdentity"])
def test_test_identity_has_isolated_mutex_name(tmp_path, mode):
    # These modes only return a name: no mutex is opened or acquired.
    production, production_result = identity_probe(tmp_path, mode, "{B6A73841-9DB7-42EF-9835-A62D764D537B}")
    assert production.returncode == 0, production.stderr.decode(errors="replace")
    production_name = production_result.read_text(encoding="utf-8")
    prefix = "Local\\SparkKeeper.Gui.S-" if mode == "Identity" else "Global\\SparkKeeper.Maintenance.S-"
    assert production_name.startswith(prefix)
    assert "SparkKeeper.Test." not in production_name

    identity = f"SparkKeeper.Test.{uuid4().hex}"
    isolated, isolated_result = identity_probe(tmp_path, mode, identity)
    assert isolated.returncode == 0, isolated.stderr.decode(errors="replace")
    assert isolated_result.read_text(encoding="utf-8") == production_name + "." + identity


@pytest.mark.parametrize("mode", ["Identity", "MaintenanceIdentity"])
@pytest.mark.parametrize("identity", ["SparkKeeper.Test.", "SparkKeeper.Test.invalid/path"])
def test_invalid_test_identity_never_emits_mutex_name(tmp_path, mode, identity):
    completed, result = identity_probe(tmp_path, mode, identity)
    assert completed.returncode != 0
    diagnostic = result.read_text(encoding="utf-8")
    assert "安装/卸载中止 [10]" in diagnostic
    assert "无效的测试安装身份" in diagnostic
    assert not diagnostic.startswith(("Local\\", "Global\\"))


def inno_setup_compiler() -> Path:
    candidates = [
        os.environ.get("INNO_SETUP_COMPILER"),
        os.environ.get("ISCC_PATH"),
        ROOT / "outputs" / "toolchain" / "InnoSetup6" / "ISCC.exe",
        shutil.which("ISCC.exe"),
    ]
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        if directory := os.environ.get(variable):
            base = Path(directory) / "Programs" if variable == "LOCALAPPDATA" else Path(directory)
            candidates.append(base / "Inno Setup 6" / "ISCC.exe")
    checked = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(os.path.expandvars(str(candidate).strip('"'))).expanduser()
        if path.is_dir():
            path /= "ISCC.exe"
        checked.append(str(path))
        if path.is_file():
            return path.resolve()
    pytest.skip("Optional native installer regression requires ISCC; searched: " + ", ".join(checked))


NATIVE_GUARD_STUB = r"""
param(
    [Parameter(Mandatory=$true)][string]$Mode,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][string]$ManifestPath,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [Parameter(Mandatory=$true)][int]$ParentProcessId,
    [Parameter(Mandatory=$true)][string]$AppIdentity,
    [switch]$PurgeUserData
)
$ErrorActionPreference = 'Stop'
function New-Object { throw 'Object/COM construction forbidden in native installer fixture' }
$utf8 = [Text.UTF8Encoding]::new($false)
try {
    if ($AppIdentity -cne $env:SPARK_NATIVE_EXPECTED_IDENTITY -or
        $AppIdentity -cnotmatch '^SparkKeeper\.Test\.[a-f0-9]{32}$') {
        throw 'Unexpected installer identity'
    }
    if ($TargetPath -ine $env:SPARK_NATIVE_EXPECTED_TARGET -or $PurgeUserData) {
        throw 'Unexpected target or user-data purge request'
    }
    if ($ParentProcessId -le 0 -or -not [IO.File]::Exists($ManifestPath)) {
        throw 'Missing native guard parameters'
    }
    $tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    foreach ($path in @($ResultPath, $env:SPARK_NATIVE_EVENT_LOG)) {
        if (-not [IO.Path]::GetFullPath($path).StartsWith(
                $tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Guard output escaped private temporary directory'
        }
    }
    [IO.File]::AppendAllText($env:SPARK_NATIVE_EVENT_LOG, $Mode + "`n", $utf8)
    switch ($Mode) {
        'Identity' {
            [IO.File]::WriteAllText($ResultPath, 'Local\' + $AppIdentity + '.NativeGui', $utf8)
            exit 0
        }
        'MaintenanceIdentity' {
            [IO.File]::WriteAllText($ResultPath, 'Global\' + $AppIdentity + '.NativeMaintenance', $utf8)
            exit 0
        }
        { $_ -in @('Mutating', 'Finish') } {
            if ($Mode -ceq $env:SPARK_NATIVE_FAIL_MODE) {
                [IO.File]::WriteAllText($ResultPath, 'NATIVE_TEST_REJECT_' + $Mode, $utf8)
                exit 73
            }
            exit 0
        }
        { $_ -in @('Prepare', 'Abort', 'Uninstall') } { exit 0 }
        default { throw ('Unexpected guard mode: ' + $Mode) }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 74
}
"""


def native_process(command, *, env, cwd, log_path=None):
    import ctypes
    import sys
    import time
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def checked(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def diagnostics(stdout, stderr):
        log = ""
        if log_path is not None and log_path.is_file():
            raw = log_path.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            log = raw.decode(encoding, errors="replace")
        return (
            f"Command: {command!r}\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}\nSetup log:\n{log}"
        )

    job = checked(kernel.CreateJobObjectW(None, None))
    process = None
    assigned = False
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        checked(kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        # Gate the launcher on stdin: the real compiler/Setup cannot start until
        # it belongs to our job. Descendants inherit membership; no PID lookup or
        # PID-based tree killing can target an unrelated, reused process ID.
        launcher = (
            "import subprocess,sys; "
            "token=sys.stdin.buffer.read(1); "
            "sys.exit(subprocess.call(sys.argv[1:]) if token == b'\\n' else 125)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", launcher, *command], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Popen retains this live Windows process HANDLE even if the PID exits.
        checked(kernel.AssignProcessToJobObject(job, int(process._handle)))
        assigned = True
        try:
            stdout, stderr = process.communicate(input=b"\n", timeout=120)
        except subprocess.TimeoutExpired:
            checked(kernel.TerminateJobObject(job, 124))
            stdout, stderr = process.communicate(timeout=30)
            pytest.fail("Native installer process timed out\n" + diagnostics(stdout, stderr))
    finally:
        try:
            if assigned:
                checked(kernel.TerminateJobObject(job, 124))
                deadline = time.monotonic() + 30
                while True:
                    accounting = Accounting()
                    checked(kernel.QueryInformationJobObject(
                        job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None,
                    ))
                    if accounting.ActiveProcesses == 0:
                        break
                    if time.monotonic() >= deadline:
                        pytest.fail("Private native installer job did not stop before cleanup")
                    time.sleep(0.05)
            elif process is not None and process.poll() is None:
                # Assignment failed: only the gated launcher exists; kill by
                # its retained handle, never by a potentially reused PID.
                process.kill()
            if process is not None:
                process.wait(timeout=30)
        finally:
            kernel.CloseHandle(job)
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
    return process.returncode, diagnostics(stdout, stderr)


@pytest.fixture
def native_setup():
    compiler = inno_setup_compiler()
    token = uuid4().hex
    identity = "SparkKeeper.Test." + token
    # Both Setup's extraction directory and /DIR live under this owned TEMP.
    # No real user profile, scheduler, installed guard, or uninstaller is used.
    with tempfile.TemporaryDirectory(
        prefix=f"SparkKeeper-InstallerTest-{token}-", dir=os.environ["TEMP"],
    ) as directory:
        private = Path(directory).resolve()
        tools = private / "tools"
        (tools / "installer").mkdir(parents=True)
        shutil.copyfile(ROOT / "tools" / "installer.iss", tools / "installer.iss")
        shutil.copyfile(
            ROOT / "tools" / "installer" / "ChineseSimplified.isl",
            tools / "installer" / "ChineseSimplified.isl",
        )
        (tools / "installer_guard.ps1").write_text(NATIVE_GUARD_STUB, encoding="utf-8-sig")
        for name in ("update.ps1", "maintenance_tasks.ps1"):
            (tools / name).write_text(
                "throw 'Native fixture must never execute business scripts'\n", encoding="utf-8-sig",
            )
        files = {"SparkKeeper.exe": b"fixture", "_internal/runtime.dll": b"runtime", "browsers/chrome.exe": b"browser"}
        payload = private / "payload"
        manifest(payload, "3.0.2", files)
        output = private / "output"
        output.mkdir()
        env = {
            **os.environ, "TEMP": str(private), "TMP": str(private),
            "SPARK_NATIVE_EXPECTED_IDENTITY": identity,
        }
        env.pop("SPARK_KEEPER_ROOT", None)
        code, diagnostic = native_process(
            [
                str(compiler), "/DAppVersion=3.0.2", f"/DTestAppId={token}",
                f"/DPayloadDir={payload}", f"/DOutputDir={output}",
                "/DOutputBaseFilename=NativeEventRegression", str(tools / "installer.iss"),
            ],
            env=env, cwd=tools,
        )
        setup = output / "NativeEventRegression.exe"
        assert code == 0, diagnostic
        assert setup.is_file(), diagnostic
        yield setup, private, env, identity, files


def test_native_setup_event_failures_are_not_swallowed(native_setup):
    import winreg

    setup, private, base_env, identity, files = native_setup
    uninstall_key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{identity}_is1"

    def registered_location():
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, uninstall_key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
        except FileNotFoundError:
            return None
        with key:
            return winreg.QueryValueEx(key, "InstallLocation")[0]

    def remove_registration():
        try:
            # Delete only this UUID's value-only key; never recurse the registry.
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, uninstall_key, winreg.KEY_WOW64_64KEY, 0)
        except FileNotFoundError:
            pass

    # One test owns one compiled AppId: cases cannot race under pytest-xdist.
    for failure in ("", "Mutating", "Finish"):
        scenario = failure or "success"
        target = private / f"SparkKeeper-InstallerTest-{uuid4().hex}-{scenario}"
        event_log = private / f"{scenario}-events.log"
        setup_log = private / f"{scenario}-setup.log"
        env = {
            **base_env, "SPARK_NATIVE_FAIL_MODE": failure,
            "SPARK_NATIVE_EXPECTED_TARGET": str(target), "SPARK_NATIVE_EVENT_LOG": str(event_log),
        }
        assert not target.exists()
        assert registered_location() is None
        try:
            code, diagnostic = native_process(
                [
                    str(setup), "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                    "/TASKS=", f"/DIR={target}", f"/LOG={setup_log}",
                ],
                env=env, cwd=private, log_path=setup_log,
            )
            events = event_log.read_text(encoding="utf-8").splitlines() if event_log.exists() else []
            diagnostic = f"Scenario: {scenario}; exit: {code}; events: {events!r}\n{diagnostic}"
            assert events[:4] == ["MaintenanceIdentity", "Identity", "Prepare", "Mutating"], diagnostic
            if failure == "Mutating":
                assert code == 7, diagnostic  # Native PrepareToInstall veto.
                assert events == ["MaintenanceIdentity", "Identity", "Prepare", "Mutating", "Abort"], diagnostic
                assert all(not (target / name).exists() for name in (*files, "release-manifest.json")), diagnostic
                assert registered_location() is None, diagnostic
            else:
                assert code == (41 if failure == "Finish" else 0), diagnostic
                assert events == [
                    "MaintenanceIdentity", "Identity", "Prepare", "Mutating", "Finish",
                ] + (["Abort"] if failure else []), diagnostic
                for name, content in files.items():
                    assert (target / name).is_file(), diagnostic
                    assert (target / name).read_bytes() == content, diagnostic
                assert (target / "release-manifest.json").read_bytes() == (
                    private / "payload" / "release-manifest.json"
                ).read_bytes(), diagnostic
                if not failure:
                    location = registered_location()
                    assert location is not None and Path(location).resolve() == target.resolve(), diagnostic
            if failure:
                assert f"NATIVE_TEST_REJECT_{failure}" in diagnostic, diagnostic
        finally:
            remove_registration()
            if target.exists():
                shutil.rmtree(target)
