#requires -version 5.1
[CmdletBinding()]
param(
    [ValidateSet('Identity', 'MaintenanceIdentity', 'Prepare', 'Mutating', 'Finish', 'Abort', 'Uninstall', 'UninstallPrepare', 'UninstallRemoveFiles', 'UninstallFinish', 'UninstallCleanup', 'UninstallAbort')][string]$Mode = 'Prepare',
    [string]$TargetPath,
    [string]$ManifestPath,
    [string]$ResultPath,
    [int]$ParentProcessId = 0,
    [string]$AppIdentity = '{B6A73841-9DB7-42EF-9835-A62D764D537B}',
    [switch]$PurgeUserData
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'update.ps1') -TargetPath $TargetPath
. (Join-Path $PSScriptRoot 'maintenance_tasks.ps1')

# Reuse the portable updater's path, hash and version contracts without changing it.
$script:ReleaseFileReader = ${function:Get-ReleaseFiles}
$script:InstallerMetadataAllowed = $false
function Get-ReleaseFiles([string]$Directory) {
    $files = & $script:ReleaseFileReader $Directory
    if ($script:InstallerMetadataAllowed) {
        foreach ($name in @('.installer/installer_guard.ps1', '.installer/update.ps1', '.installer/maintenance_tasks.ps1',
            '.installer/uninstall-completed.json', 'unins000.exe', 'unins000.dat', 'unins000.msg')) { [void]$files.Remove($name) }
    }
    return ,$files
}

function Test-RegisteredTarget([string]$Target, [string]$IncomingVersion = '') {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Registry64)
    try {
        $key = $base.OpenSubKey("Software\Microsoft\Windows\CurrentVersion\Uninstall\${AppIdentity}_is1")
        if ($null -eq $key) { return $false }
        try {
            $location = [string]$key.GetValue('InstallLocation', '')
            if ($IncomingVersion) {
                $registered = ConvertTo-ReleaseVersion ([string]$key.GetValue('DisplayVersion', ''))
                if ((Compare-ReleaseVersion (ConvertTo-ReleaseVersion $IncomingVersion) $registered) -lt 0) {
                    Stop-Update 21 "拒绝降级已注册版本：$($registered.Text) -> $IncomingVersion。"
                }
                if (-not $location.TrimEnd('\').Equals($Target, [StringComparison]::OrdinalIgnoreCase)) {
                    Stop-Update 10 "已有安装位于 $location，请选择原安装目录升级，不能用另一目录覆盖同一安装身份。"
                }
            }
            return $location.TrimEnd('\').Equals($Target, [StringComparison]::OrdinalIgnoreCase)
        } finally { $key.Dispose() }
    } finally { $base.Dispose() }
}

function Resolve-InstallTarget([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -notmatch '^[A-Za-z]:[\\/]' -or
        $Path.Substring(2) -match '[:*?"<>|]' -or $Path -match '[\x00-\x1f]') {
        Stop-Update 10 '安装目录必须是本地磁盘绝对路径。'
    }
    foreach ($part in ($Path.Substring(3) -split '[\\/]')) {
        if ($part -and ($part -in @('.', '..') -or $part -match '[. ]$' -or
            $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)')) {
            Stop-Update 10 "不安全的安装路径：$Path"
        }
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($full.Length -le 3) { Stop-Update 10 '不能安装到磁盘根目录。' }
    $ancestor = $full
    while (-not (Test-Path -LiteralPath $ancestor)) { $ancestor = [IO.Path]::GetDirectoryName($ancestor) }
    Assert-NoReparsePath $ancestor
    $canonical = Get-CanonicalDirectory $ancestor
    if ($canonical -ine $ancestor.TrimEnd('\')) { Stop-Update 10 '安装目录不能通过路径别名重定向。' }
    foreach ($special in @(
        (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SparkKeeper'),
        (Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'SparkKeeper'),
        (Get-MaintenanceDataRoot))) {
        if ((Test-PathWithin $full $special) -or (Test-PathWithin $special $full)) {
            Stop-Update 10 '安装目录不得包含或位于 AppData/SparkKeeper 用户数据目录。'
        }
    }
    foreach ($scope in @('Process', 'User', 'Machine')) {
        if ([Environment]::GetEnvironmentVariable('SPARK_KEEPER_ROOT', $scope)) {
            Stop-Update 10 '检测到 SPARK_KEEPER_ROOT 数据覆盖，请先移除覆盖并核对数据位置。'
        }
    }
    return $full
}

function Test-PackagedBrowser([string]$Image) {
    if (-not $Image) { return $false }
    $parent = [IO.Path]::GetDirectoryName($Image)
    while ($parent) {
        if ([IO.Path]::GetFileName($parent) -ieq 'browsers') {
            return Test-Path -LiteralPath (Join-Path ([IO.Path]::GetDirectoryName($parent)) 'SparkKeeper.exe') -PathType Leaf
        }
        $parent = [IO.Path]::GetDirectoryName($parent)
    }
    return $false
}

function Assert-InstallerIdle([string]$Target) {
    $data = Get-MaintenanceDataRoot
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $bootstrapId = 0
    if ($Mode.StartsWith('Uninstall') -and $ParentProcessId) {
        $uninstaller = @($processes | Where-Object { $_.ProcessId -eq $ParentProcessId })
        if ($uninstaller.Count -eq 1) {
            $bootstrap = @($processes | Where-Object {
                $_.ProcessId -eq $uninstaller[0].ParentProcessId -and
                [string]$_.ExecutablePath -ieq (Join-Path $Target 'unins000.exe')
            })
            if ($bootstrap.Count -eq 1) { $bootstrapId = $bootstrap[0].ProcessId }
        }
    }
    foreach ($process in $processes) {
        if ($process.ProcessId -eq $PID -or $process.ProcessId -eq $ParentProcessId -or
            $process.ProcessId -eq $bootstrapId) { continue }
        $image = [string]$process.ExecutablePath
        $command = [string]$process.CommandLine
        if ($process.Name -ieq 'SparkKeeper.exe' -or
            ($image -and (Test-PathWithin $image $Target)) -or
            ($process.Name -match '^(?i:pythonw?|python[0-9.]+)\.exe$' -and $command -match '(?i)spark_keeper|portable_entry\.py')) {
            Stop-Update 31 "程序或关联进程仍在运行（PID $($process.ProcessId)，$($process.Name)），请正常退出；不会强杀。"
        }
        if ($process.Name -match '^(?i:chrome|chrome-headless-shell|headless_shell|msedge|firefox)\.exe$' -and
            (([string]$process.CommandLine).IndexOf($data, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
             [string]$process.ExecutablePath -match '(?i)[\\/]SparkKeeper[\\/]browsers[\\/]' -or
             (Test-PackagedBrowser $image))) {
            Stop-Update 31 "关联浏览器仍在运行（PID $($process.ProcessId)），请在程序中正常结束任务并退出；不会强杀。"
        }
    }
}

function Read-IncomingManifest([string]$Path) {
    return Assert-IncomingManifest (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Assert-IncomingManifest($manifest) {
    if ($manifest.format -ne 1 -or $manifest.product -cne 'SparkKeeper' -or
        $manifest.files -isnot [Management.Automation.PSCustomObject]) { Stop-Update 20 '安装包清单无效。' }
    [void](ConvertTo-ReleaseVersion $manifest.version)
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $name = $entry.Name
        if ($name -match '(^/|\\|:|(^|/)\.{1,2}(/|$)|//|/$|[\x00-\x1f*?"<>|])' -or
            $name -eq 'release-manifest.json' -or $name -match '^(?i:\.installer/|unins[0-9]*\.(exe|dat|msg)$)' -or
            -not $names.Add($name) -or
            $entry.Value -isnot [string] -or $entry.Value -notmatch '^[A-Fa-f0-9]{64}$') {
            Stop-Update 20 "安装清单包含不安全路径或校验值：$name"
        }
        foreach ($segment in $name.Split('/')) {
            if ($segment -match '[. ]$' -or $segment -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') {
                Stop-Update 20 "安装清单包含不安全文件名：$name"
            }
        }
    }
    if (-not $names.Contains('SparkKeeper.exe') -or
        -not @($names | Where-Object { $_.StartsWith('_internal/') }).Count -or
        -not @($names | Where-Object { $_.StartsWith('browsers/') }).Count) {
        Stop-Update 20 '安装清单缺少完整程序文件。'
    }
    return $manifest
}

function Invoke-InstallerPrepare([string]$Target, [string]$IncomingPath) {
    $targetDir = Resolve-InstallTarget $Target
    $incoming = Read-IncomingManifest $IncomingPath
    $script:InstallerMetadataAllowed = Test-RegisteredTarget $targetDir $incoming.version
    $record = Read-MaintenanceJournal $targetDir
    $old = $null
    if ($null -ne $record) {
        $script:InstallerMetadataAllowed = $true
        foreach ($previous in @($record.oldManifest, $record.newManifest) + @($record.priorManifests)) {
            if ($null -ne $previous -and
                (Compare-ReleaseVersion (ConvertTo-ReleaseVersion $incoming.version) (ConvertTo-ReleaseVersion $previous.version)) -lt 0) {
                Stop-Update 21 '修复安装不得降级维护记录中的版本，请重跑同版或更新安装包。'
            }
        }
        Assert-MaintenanceFiles $record $incoming
    } elseif (Test-Path -LiteralPath $targetDir) {
        if (-not [IO.Directory]::Exists($targetDir)) { Stop-Update 10 '目标不是文件夹。' }
        if (@(Get-ChildItem -LiteralPath $targetDir -Force).Count) {
            $installed = Read-ReleaseManifest $targetDir
            if ((Compare-ReleaseVersion (ConvertTo-ReleaseVersion $incoming.version) $installed.Version) -lt 0) {
                Stop-Update 21 "拒绝降级：$($installed.Version.Text) -> $($incoming.version)，不会回滚数据库。"
            }
            Assert-FilesAvailable $installed.Files $true
            $old = Read-IncomingManifest (Join-Path $targetDir 'release-manifest.json')
        }
    }
    $task = Get-MaintenanceTask
    $owned = Test-MaintenanceTaskOwned $task $targetDir
    if ($null -ne $task -and -not $owned -and $task.Enabled) {
        Stop-Update 30 '同名已启用计划不属于本安装目录。首次迁入请先人工停用旧计划，安装后在新版重新保存绑定；安装器未修改该计划。'
    }
    if ($owned) { Assert-MaintenanceTaskIdle $task }
    Assert-InstallerIdle $targetDir
    if ($null -eq $record) {
        $record = [pscustomobject]@{
            format = 1; product = 'SparkKeeper'; appIdentity = $AppIdentity; target = $targetDir
            operation = 'install'; phase = 'prepared'; task = New-MaintenanceSnapshot $task $targetDir
            oldManifest = $old; newManifest = $incoming; priorManifests = @()
            purgeAuthorized = $false; purgeStarted = $false
        }
    } else {
        if ($null -ne $record.newManifest -and
            (Get-MaintenanceManifestDigest $record.newManifest) -cne (Get-MaintenanceManifestDigest $incoming)) {
            $record.priorManifests = @($record.priorManifests) + @($record.newManifest)
        }
        # Keep the first task/originalEnabled and oldManifest across retries.
        $record.newManifest = $incoming
        $record.operation = 'install'
        $record.phase = 'prepared'
    }
    Write-MaintenanceJournal $record
    Suspend-MaintenanceTask $record
    Assert-InstallerIdle $targetDir
    Assert-MaintenanceFiles $record $incoming
}

function Invoke-InstallerFinish([string]$Target) {
    $targetDir = Resolve-InstallTarget $Target
    $script:InstallerMetadataAllowed = $true
    $record = Read-MaintenanceJournal $targetDir
    if ($null -eq $record -or $record.operation -cne 'install' -or $record.phase -cne 'mutating') {
        Stop-Update 41 '缺少已开始复制的安装维护记录，拒绝恢复计划。'
    }
    Assert-InstallerIdle $targetDir
    $incoming = Read-IncomingManifest (Join-Path $targetDir 'release-manifest.json')
    if ((Get-MaintenanceManifestDigest $incoming) -cne (Get-MaintenanceManifestDigest $record.newManifest)) {
        Stop-Update 20 '安装后清单与安装包不匹配，计划保持暂停，请重跑安装包修复。'
    }
    $current = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $incoming.files.PSObject.Properties) {
        [void]$current.Add($entry.Name)
        $file = Join-Path $targetDir $entry.Name.Replace('/', '\')
        Assert-NoReparsePath $file
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value) {
            Stop-Update 20 "安装文件校验失败：$($entry.Name)；计划保持暂停，请重跑安装包修复。"
        }
    }
    $retired = @{}
    foreach ($manifest in @($record.oldManifest) + @($record.priorManifests)) {
        if ($null -eq $manifest) { continue }
        foreach ($entry in $manifest.files.PSObject.Properties) {
            if ($current.Contains($entry.Name)) { continue }
            if (-not $retired.ContainsKey($entry.Name)) { $retired[$entry.Name] = @() }
            $retired[$entry.Name] += $entry.Value
        }
    }
    $obsolete = [Collections.Generic.List[string]]::new()
    foreach ($name in $retired.Keys) {
        $file = Join-Path $targetDir $name.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $file)) { continue }
        Assert-NoReparsePath $file
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash
        if ($hash -inotin $retired[$name]) {
            Stop-Update 20 "过时文件在安装过程中变化，未删除：$file；计划保持暂停，请保留安装日志并人工处理。"
        }
        $obsolete.Add($file)
    }
    # Only unchanged manifest members can be retired. Never recursively delete.
    foreach ($file in $obsolete) {
        [IO.File]::Delete($file)
        $parent = [IO.Path]::GetDirectoryName($file)
        while ($parent -ine $targetDir -and @(Get-ChildItem -LiteralPath $parent -Force).Count -eq 0) {
            [IO.Directory]::Delete($parent)
            $parent = [IO.Path]::GetDirectoryName($parent)
        }
    }
    [void](Read-ReleaseManifest $targetDir)
    $record.phase = 'verified'
    Write-MaintenanceJournal $record
    $completion = Get-MaintenanceCompletionPath $targetDir
    if (Test-Path -LiteralPath $completion) { Assert-NoReparsePath $completion; [IO.File]::Delete($completion) }
    if ($record.purgeStarted) {
        Invoke-MaintenanceDataPurge $record
        $script:MaintenanceNotice = '程序文件已修复，并完成此前卸载已授权的全部用户数据清理。旧计划未重建；如需删除修复后的程序，请再次运行卸载。'
        if ($ResultPath) { [IO.File]::WriteAllText($ResultPath + '.no-launch', 'purge-repair', [Text.UTF8Encoding]::new($false)) }
    } else { Restore-MaintenanceTask $record }
    if (Test-Path -LiteralPath $completion) { Assert-NoReparsePath $completion; [IO.File]::Delete($completion) }
}

# Dot-source for isolated probes; no scheduler/process queries until explicitly invoked.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        switch ($Mode) {
            'Identity' {
                [void](Get-MaintenanceTaskName)
                $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
                $session = [Diagnostics.Process]::GetCurrentProcess().SessionId
                $name = "Local\SparkKeeper.Gui.$sid.$session"
                if ($AppIdentity.StartsWith('SparkKeeper.Test.', [StringComparison]::Ordinal)) { $name += ".$AppIdentity" }
                [IO.File]::WriteAllText($ResultPath, $name, [Text.UTF8Encoding]::new($false))
            }
            'MaintenanceIdentity' {
                [void](Get-MaintenanceTaskName)
                $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
                $name = "Global\SparkKeeper.Maintenance.$sid"
                if ($AppIdentity.StartsWith('SparkKeeper.Test.', [StringComparison]::Ordinal)) { $name += ".$AppIdentity" }
                [IO.File]::WriteAllText($ResultPath, $name, [Text.UTF8Encoding]::new($false))
            }
            'Prepare' { Invoke-InstallerPrepare $TargetPath $ManifestPath }
            'Mutating' { Invoke-MaintenanceMutating $TargetPath }
            'Finish' { Invoke-InstallerFinish $TargetPath }
            'Abort' { Invoke-MaintenanceAbort $TargetPath }
            'UninstallAbort' { Invoke-MaintenanceAbort $TargetPath }
            'Uninstall' { Invoke-UninstallPreflight $TargetPath }
            'UninstallPrepare' { Invoke-UninstallPrepare $TargetPath }
            'UninstallRemoveFiles' { Invoke-UninstallRemoveFiles $TargetPath }
            'UninstallFinish' { Invoke-UninstallFinish $TargetPath }
            'UninstallCleanup' { Invoke-UninstallCleanup $TargetPath }
        }
        if ($script:MaintenanceNotice -and $ResultPath) {
            [IO.File]::WriteAllText($ResultPath, $script:MaintenanceNotice, [Text.UTF8Encoding]::new($false))
        }
        exit 0
    } catch {
        $code = 99
        if ($_.Exception.Data.Contains('ExitCode')) { $code = [int]$_.Exception.Data['ExitCode'] }
        $message = "安装/卸载中止 [$code]：$($_.Exception.Message)"
        if ($ResultPath) { [IO.File]::WriteAllText($ResultPath, $message, [Text.UTF8Encoding]::new($false)) }
        [Console]::Error.WriteLine($message)
        exit $code
    }
}
