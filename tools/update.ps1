#requires -version 5.1
[CmdletBinding()]
param(
    [string]$TargetPath,
    [string]$SourcePath = (Join-Path $PSScriptRoot 'SparkKeeper'),
    [switch]$NoLaunch,
    [switch]$AcceptLegacy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes: 10 paths; 20 manifest; 21 version; 22 legacy confirmation;
# 30 scheduler; 31 processes; 32 files/permissions/space; 40 switch restored;
# 41 rollback needs manual recovery; 50 installed but launch failed; 60 update busy; 99 unexpected.
function Stop-Update([int]$Code, [string]$Message) {
    $failure = [InvalidOperationException]::new($Message)
    $failure.Data['ExitCode'] = $Code
    throw $failure
}

function Test-PathWithin([string]$Path, [string]$Root) {
    return $Path.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or
        $Path.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePath([string]$Path) {
    $cursor = $Path
    while ($cursor) {
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Update 10 "拒绝符号链接、目录联接或挂载点：$cursor"
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function Get-CanonicalDirectory([string]$Path) {
    if (-not ('SparkKeeper.UpdatePaths' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace SparkKeeper {
    public static class UpdatePaths {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
            IntPtr security, uint disposition, uint flags, IntPtr template);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle,
            StringBuilder path, uint length, uint flags);
        public static string Resolve(string path) {
            using (var handle = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
                if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
                var buffer = new StringBuilder(32768);
                uint size = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
                if (size == 0 || size >= buffer.Capacity)
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                return buffer.ToString();
            }
        }
    }
}
'@
    }
    try { $resolved = [SparkKeeper.UpdatePaths]::Resolve($Path) } catch {
        Stop-Update 10 "无法解析真实目录路径：$Path；$($_.Exception.Message)"
    }
    if ($resolved -notmatch '^\\\\\?\\[A-Za-z]:\\') {
        Stop-Update 10 "仅支持本地盘符目录，不支持网络重定向：$Path"
    }
    return $resolved.Substring(4).TrimEnd('\')
}

function Resolve-UpdateDirectory([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -notmatch '^[A-Za-z]:[\\/]' -or
        $Path.Substring(2) -match '[:*?"<>|]' -or $Path -match '[\x00-\x1f]') {
        Stop-Update 10 '请选择本地磁盘上的绝对目录路径；不支持网络、设备路径或通配符。'
    }
    $parts = $Path.Substring(3) -split '[\\/]'
    foreach ($part in $parts) {
        if ($part -and ($part -in @('.', '..') -or $part -match '[. ]$' -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)')) {
            Stop-Update 10 "路径包含不安全的目录名称：$part"
        }
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full.Length -le 3 -or -not [IO.Directory]::Exists($full)) {
        Stop-Update 10 "必须选择已经存在的程序目录，不能选择磁盘根目录：$Path"
    }
    Assert-NoReparsePath $full
    $canonical = Get-CanonicalDirectory $full
    if ($canonical.Length -le 3) { Stop-Update 10 '目标或源路径实际指向磁盘根目录。' }
    Assert-NoReparsePath $canonical
    return $canonical
}

function Get-ReleaseFiles([string]$Directory) {
    $files = [Collections.Generic.Dictionary[string,IO.FileInfo]]::new([StringComparer]::OrdinalIgnoreCase)
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Directory)
    while ($pending.Count) {
        $currentDirectory = $pending.Pop()
        $children = @(Get-ChildItem -LiteralPath $currentDirectory -Force)
        if ($children.Count -eq 0) {
            Stop-Update 20 "目录包含清单无法验证的空目录，拒绝丢弃：$currentDirectory"
        }
        foreach ($item in $children) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Update 20 "程序目录不得包含链接：$($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Push($item.FullName); continue }
            $relative = $item.FullName.Substring($Directory.Length + 1).Replace('\', '/')
            if ($item.Name -match '(?i)\.(sqlite3?|db)(?:-wal|-shm)?$' -or
                $item.Name -in @('auth-state.bin', 'spark-keeper-task.xml') -or
                $relative -match '(?i)(^|/)(local-data|work|outputs|backups)(/|$)') {
                Stop-Update 20 "程序目录发现用户数据，拒绝更新；请先人工妥善保管：$relative"
            }
            $files.Add($relative, $item)
        }
    }
    return ,$files
}

function ConvertTo-ReleaseVersion([object]$Value) {
    if ($Value -isnot [string] -or $Value -cnotmatch '^(?<core>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))(?:rc(?<rc>[1-9][0-9]*))?$') {
        Stop-Update 21 "无效的发行版本号（仅支持 X.Y.Z 或 X.Y.ZrcN）：$Value"
    }
    try {
        return [pscustomobject]@{
            Core = [version]$Matches['core']
            Candidate = if ($Matches.ContainsKey('rc')) { [Numerics.BigInteger]::Parse($Matches['rc']) } else { $null }
            Text = $Value
        }
    } catch { Stop-Update 21 "版本号超出支持范围：$Value" }
}

function Compare-ReleaseVersion($Left, $Right) {
    $core = $Left.Core.CompareTo($Right.Core)
    if ($core -ne 0) { return $core }
    if ($null -eq $Left.Candidate -and $null -eq $Right.Candidate) { return 0 }
    if ($null -eq $Left.Candidate) { return 1 }
    if ($null -eq $Right.Candidate) { return -1 }
    return $Left.Candidate.CompareTo($Right.Candidate)
}

function Read-ReleaseManifest([string]$Directory) {
    $files = Get-ReleaseFiles $Directory
    if (-not $files.ContainsKey('release-manifest.json')) {
        Stop-Update 20 "缺少 release-manifest.json：$Directory"
    }
    try {
        $manifest = Get-Content -LiteralPath $files['release-manifest.json'].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($manifest.format -ne 1 -or $manifest.product -cne 'SparkKeeper' -or
            $manifest.files -isnot [Management.Automation.PSCustomObject]) {
            Stop-Update 20 '发行清单格式或产品标识不匹配。'
        }
        $version = ConvertTo-ReleaseVersion $manifest.version
        $expected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $manifest.files.PSObject.Properties) {
            $name = $entry.Name
            if ($name -match '(^/|\\|:|(^|/)\.{1,2}(/|$)|//|/$|[\x00-\x1f*?"<>|])' -or
                $name -eq 'release-manifest.json' -or -not $expected.Add($name)) {
                Stop-Update 20 "清单文件路径不合法或重复：$name"
            }
            foreach ($segment in $name.Split('/')) {
                if ($segment -match '[. ]$' -or $segment -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                    Stop-Update 20 "清单含不安全文件名：$name"
                }
            }
            if ($entry.Value -isnot [string] -or $entry.Value -notmatch '^[A-Fa-f0-9]{64}$' -or
                -not $files.ContainsKey($name)) {
                Stop-Update 20 "清单文件缺失或 SHA256 格式错误：$name"
            }
            $actual = (Get-FileHash -LiteralPath $files[$name].FullName -Algorithm SHA256).Hash
            if ($actual -ine $entry.Value) { Stop-Update 20 "文件校验失败：$name" }
        }
        foreach ($name in $files.Keys) {
            if ($name -ne 'release-manifest.json' -and -not $expected.Contains($name)) {
                Stop-Update 20 "目录存在清单之外的文件，拒绝丢弃：$name"
            }
        }
        if (-not $expected.Contains('SparkKeeper.exe') -or
            -not @($expected | Where-Object { $_.StartsWith('_internal/') }).Count -or
            -not @($expected | Where-Object { $_.StartsWith('browsers/') }).Count) {
            Stop-Update 20 '发行包必须包含 SparkKeeper.exe、_internal 和 browsers 整套程序。'
        }
        return [pscustomobject]@{ Version = $version; Files = $files }
    } catch {
        if ($_.Exception.Data.Contains('ExitCode')) { throw }
        Stop-Update 20 "无法解析或读取发行清单：$($_.Exception.Message)"
    }
}

function Read-InstalledRelease([string]$Directory, [bool]$LegacyAccepted) {
    if (Test-Path -LiteralPath (Join-Path $Directory 'release-manifest.json')) {
        return Read-ReleaseManifest $Directory
    }
    $files = Get-ReleaseFiles $Directory
    foreach ($item in Get-ChildItem -LiteralPath $Directory -Force) {
        if (($item.PSIsContainer -and $item.Name -notin @('_internal', 'browsers', 'licenses')) -or
            (-not $item.PSIsContainer -and $item.Name -notin @('SparkKeeper.exe', '使用说明.txt', 'THIRD_PARTY_NOTICES.txt'))) {
            Stop-Update 20 "旧目录包含无法识别的额外内容，拒绝更新：$($item.Name)"
        }
    }
    if (-not $files.ContainsKey('SparkKeeper.exe') -or -not $files.ContainsKey('使用说明.txt') -or
        -not @($files.Keys | Where-Object { $_.StartsWith('_internal/') }).Count -or
        -not @($files.Keys | Where-Object { $_.StartsWith('browsers/') }).Count -or
        (Get-Content -LiteralPath $files['使用说明.txt'].FullName -Encoding UTF8 -TotalCount 1) -notmatch '续火花助手 Windows 本地自用版 0\.2\.0\.dev18\s*$') {
        Stop-Update 20 '目标不是带清单的 SparkKeeper，也不是可识别的 0.2.0.dev18 完整发行目录。'
    }
    Write-Host '首次迁入 dev18：旧版没有逐文件清单，无法证明依赖目录内每个文件的来源；整目录将完整保存在旁路备份。以后只接受同产品正式清单。'
    Write-Host '请先确认旧目录内没有自行存入的文件；更新后核对数据，并在新版人工重新保存计划。不会自动回滚数据库。'
    if (-not $LegacyAccepted) { Stop-Update 22 '迁入旧 dev18 需要明确确认；交互输入确认，自动化使用 -AcceptLegacy。' }
    return [pscustomobject]@{ Version = (ConvertTo-ReleaseVersion '0.2.0'); Files = $files }
}

function Assert-SchedulerStopped {
    try {
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        $folder = $service.GetFolder('\')
        try { $task = $folder.GetTask('SparkKeeperLocalDaily') } catch {
            $cause = $_.Exception
            while ($null -ne $cause) {
                if ($cause.HResult -eq -2147024894) { return }
                $cause = $cause.InnerException
            }
            throw
        }
        if ($task.Enabled -or $task.State -eq 4) {
            Stop-Update 30 '请先在旧程序中停用每日计划，等待已运行任务结束，再正常退出全部程序。更新器不会修改计划。'
        }
    } catch {
        if ($_.Exception.Data.Contains('ExitCode')) { throw }
        Stop-Update 30 "无法确认计划任务已停用，安全中止：$($_.Exception.Message)"
    }
}

function Assert-ProcessesStopped([string]$Source, [string]$Target) {
    try { $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop) } catch {
        Stop-Update 31 "无法检查运行进程：$($_.Exception.Message)"
    }
    foreach ($process in $processes) {
        if ($process.ProcessId -eq $PID) { continue }
        $image = [string]$process.ExecutablePath
        $command = [string]$process.CommandLine
        if ($process.Name -ieq 'SparkKeeper.exe' -or
            ($image -and ((Test-PathWithin $image $Source) -or (Test-PathWithin $image $Target))) -or
            ($process.Name -match '^(?i:pythonw?|python[0-9.]+)\.exe$' -and $command -match '(?i)spark_keeper|portable_entry\.py')) {
            Stop-Update 31 "程序或关联浏览器仍在运行（PID $($process.ProcessId)，$($process.Name)），请正常退出；不会强杀进程。"
        }
    }
}

function Assert-UpdateIdle([string]$Source, [string]$Target) {
    Assert-SchedulerStopped
    Assert-ProcessesStopped $Source $Target
}

function Assert-FilesAvailable($Files, [bool]$Writable) {
    foreach ($file in $Files.Values) {
        try {
            $access = [IO.FileAccess]::Read
            if ($Writable) { $access = [IO.FileAccess]::ReadWrite }
            $handle = [IO.File]::Open($file.FullName, [IO.FileMode]::Open, $access, [IO.FileShare]::None)
            $handle.Dispose()
        } catch { Stop-Update 32 "文件被占用或无权限：$($file.FullName)；$($_.Exception.Message)" }
    }
}

function Move-UpdateDirectory([string]$From, [string]$To) {
    [IO.Directory]::Move($From, $To)
}

function Get-UpdateMutexName([string]$CanonicalTarget) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($CanonicalTarget.ToUpperInvariant())
        $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')
        return 'Global\SparkKeeperUpdate-' + $digest
    } finally { $sha.Dispose() }
}

function Enter-UpdateMutex([string]$CanonicalTarget) {
    $mutex = $null
    try {
        $mutex = [Threading.Mutex]::new($false, (Get-UpdateMutexName $CanonicalTarget))
        try { $acquired = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { Stop-Update 60 '此程序目录正在被另一个更新器处理，请等待它完成后再试。' }
        return $mutex
    } catch {
        if ($null -ne $mutex) { $mutex.Dispose() }
        if ($_.Exception.Data.Contains('ExitCode')) { throw }
        Stop-Update 60 "无法取得目标目录更新锁，安全中止：$($_.Exception.Message)"
    }
}

function Invoke-SparkKeeperUpdate([string]$Source, [string]$Target, [bool]$LegacyAccepted = $false) {
    $canonicalTarget = Resolve-UpdateDirectory $Target
    $mutex = Enter-UpdateMutex $canonicalTarget
    try {
        return Invoke-SparkKeeperUpdateLocked $Source $canonicalTarget $LegacyAccepted
    } finally {
        try { $mutex.ReleaseMutex() } finally { $mutex.Dispose() }
    }
}

function Invoke-SparkKeeperUpdateLocked([string]$Source, [string]$Target, [bool]$LegacyAccepted) {
    foreach ($scope in @('Process', 'User', 'Machine')) {
        if ([Environment]::GetEnvironmentVariable('SPARK_KEEPER_ROOT', $scope)) {
            Stop-Update 10 '检测到 SPARK_KEEPER_ROOT 开发数据覆盖；更新器只支持默认 AppData 数据布局，请先移除覆盖并核对数据位置。'
        }
    }
    $sourceDir = Resolve-UpdateDirectory $Source
    $targetDir = Resolve-UpdateDirectory $Target
    if ((Test-PathWithin $sourceDir $targetDir) -or (Test-PathWithin $targetDir $sourceDir) -or
        (Test-PathWithin $PSScriptRoot $targetDir)) {
        Stop-Update 10 '源目录、目标目录不能相同或互相包含，更新脚本必须位于目标目录之外。'
    }
    $dataRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SparkKeeper'
    foreach ($directory in @($sourceDir, $targetDir)) {
        if ((Test-PathWithin $directory $dataRoot) -or (Test-PathWithin $dataRoot $directory)) {
            Stop-Update 10 '更新目录不得包含或位于用户 AppData 数据目录。'
        }
    }
    Assert-UpdateIdle $sourceDir $targetDir
    $release = Read-ReleaseManifest $sourceDir
    $installed = Read-InstalledRelease $targetDir $LegacyAccepted
    if ((Compare-ReleaseVersion $release.Version $installed.Version) -lt 0) { Stop-Update 21 "拒绝降级：$($installed.Version.Text) -> $($release.Version.Text)；不会回滚数据库。" }
    Assert-FilesAvailable $release.Files $false
    Assert-FilesAvailable $installed.Files $true
    $parent = [IO.Directory]::GetParent($targetDir).FullName
    $required = [long]0
    foreach ($file in $release.Files.Values) { $required += $file.Length }
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($parent))
    if ($drive.AvailableFreeSpace -lt ($required + 64MB)) { Stop-Update 32 '目标磁盘空间不足，必须能容纳完整新版及至少 64 MB 余量；不会删除旧版腾空间。' }
    $suffix = [Guid]::NewGuid().ToString('N')
    $stage = Join-Path $parent ('.SparkKeeper-stage-' + $suffix)
    $backup = $targetDir + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $suffix.Substring(0, 8)
    $movedOld = $false
    $installedNew = $false
    try {
        try {
            [void][IO.Directory]::CreateDirectory($stage)
            foreach ($entry in $release.Files.GetEnumerator()) {
                $destination = Join-Path $stage $entry.Key.Replace('/', '\')
                [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
                [IO.File]::Copy($entry.Value.FullName, $destination, $false)
            }
            [void](Read-ReleaseManifest $stage)
        } catch {
            if ($_.Exception.Data.Contains('ExitCode')) { throw }
            Stop-Update 32 "完整新版准备失败，旧版未变：$($_.Exception.Message)"
        }
        # Recheck immediately before the only mutation of the installed directory.
        Assert-NoReparsePath $targetDir
        Assert-NoReparsePath $parent
        Assert-UpdateIdle $sourceDir $targetDir
        $current = Read-InstalledRelease $targetDir $LegacyAccepted
        if ((Compare-ReleaseVersion $current.Version $installed.Version) -ne 0) { Stop-Update 21 '校验期间目标版本变化，请重新更新。' }
        Assert-FilesAvailable $current.Files $true
        try {
            Move-UpdateDirectory $targetDir $backup
            $movedOld = $true
            Move-UpdateDirectory $stage $targetDir
            $installedNew = $true
        } catch {
            $switchError = $_.Exception.Message
            if ($movedOld) {
                try { Move-UpdateDirectory $backup $targetDir; $movedOld = $false } catch {
                    Stop-Update 41 "目录切换及还原失败。旧版完整保留在 $backup；请勿启动任何版本，人工恢复目录。切换：$switchError；还原：$($_.Exception.Message)"
                }
            }
            Stop-Update 40 "目录切换失败，旧版已保留/还原，未启动新版：$switchError"
        }
        Write-Host "更新完成：$($release.Version.Text) -> $targetDir"
        Write-Host "旧版完整备份：$backup"
        Write-Host '用户数据未触碰。请在新版核对好友、登录状态和计划后再人工启用；不要直接运行旧备份或用旧数据库覆盖当前数据。'
        return [pscustomobject]@{ Target = $targetDir; Backup = $backup; Version = $release.Version.Text }
    } finally {
        if (-not $installedNew -and [IO.Directory]::Exists($stage)) {
            try { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction Stop } catch {
                Write-Warning "临时目录未能清理，请人工处理：$stage"
            }
        }
    }
}

# Dot-sourcing exposes the same operations for isolated filesystem verification.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        $interactive = [string]::IsNullOrWhiteSpace($TargetPath)
        if ($interactive) {
            Write-Host '离线更新：先在旧程序停用每日计划，等待任务结束并正常退出全部实例。'
            Write-Host '请先将完整更新包解压到旧目录之外。此操作不联网、不提权、不改计划、不碰用户数据。'
            Add-Type -AssemblyName System.Windows.Forms
            $dialog = New-Object Windows.Forms.FolderBrowserDialog
            try {
                $dialog.Description = '选择现有 SparkKeeper.exe 所在的固定程序目录（不是新解压的目录）'
                $dialog.ShowNewFolderButton = $false
                if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) { Stop-Update 10 '已取消更新。' }
                $TargetPath = $dialog.SelectedPath
            } finally { $dialog.Dispose() }
            if (-not (Test-Path -LiteralPath (Join-Path $TargetPath 'release-manifest.json'))) {
                Write-Host '旧 dev18 没有文件清单。只允许确认来源的完整旧发行目录；额外数据请先人工保管。整个旧目录会保留备份。'
                $AcceptLegacy = (Read-Host '确认首次从 0.2.0.dev18 迁入请输入 dev18；其他输入取消') -ceq 'dev18'
            }
        }
        $result = Invoke-SparkKeeperUpdate $SourcePath $TargetPath ([bool]$AcceptLegacy)
        if (-not $NoLaunch) {
            $launch = -not $interactive -or (Read-Host '输入 Y 启动新版进行核对（不会自动启用计划）') -ieq 'Y'
            if ($launch) {
                try { Start-Process -FilePath (Join-Path $result.Target 'SparkKeeper.exe') -WorkingDirectory $result.Target } catch {
                    Stop-Update 50 "更新已经完成，但启动失败，请手动启动新版：$($_.Exception.Message)"
                }
            }
        }
        exit 0
    } catch {
        $code = 99
        if ($_.Exception.Data.Contains('ExitCode')) { $code = [int]$_.Exception.Data['ExitCode'] }
        [Console]::Error.WriteLine("更新中止 [$code]：$($_.Exception.Message)")
        exit $code
    }
}
