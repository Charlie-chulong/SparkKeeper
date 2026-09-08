#requires -version 5.1
# Dot-sourced only by installer_guard.ps1. The Inno parent owns the same-SID
# maintenance gate throughout these calls; this helper never starts/stops a task.
$script:MaintenanceNotice = ''

function Get-MaintenanceTaskName {
    if ($AppIdentity.StartsWith('SparkKeeper.Test.', [StringComparison]::Ordinal)) {
        if ($AppIdentity -cnotmatch '^SparkKeeper\.Test\.[A-Za-z0-9_-]+$') { Stop-Update 10 '无效的测试安装身份。' }
        return "$AppIdentity.LocalDaily"
    }
    if ($AppIdentity -cne '{B6A73841-9DB7-42EF-9835-A62D764D537B}') { Stop-Update 10 '未知安装身份。' }
    return 'SparkKeeperLocalDaily'
}

function Get-MaintenanceDataRoot {
    [void](Get-MaintenanceTaskName)
    if ($AppIdentity.StartsWith('SparkKeeper.Test.', [StringComparison]::Ordinal)) {
        return Join-Path ([IO.Path]::GetTempPath()) "SparkKeeper-maintenance\$AppIdentity\data"
    }
    return Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'SparkKeeper'
}

function Get-MaintenanceJournalPath {
    $base = Join-Path (Get-MaintenanceDataRoot) 'maintenance'
    $ancestor = $base
    while (-not (Test-Path -LiteralPath $ancestor)) { $ancestor = [IO.Path]::GetDirectoryName($ancestor) }
    Assert-NoReparsePath $ancestor
    $path = Join-Path $base 'pending.json'
    if (Test-Path -LiteralPath $path) { Assert-NoReparsePath $path }
    return $path
}

function Get-MaintenanceTaskFolder {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    return $service.GetFolder('\')
}

function Get-MaintenanceTask {
    $folder = Get-MaintenanceTaskFolder
    try { return $folder.GetTask((Get-MaintenanceTaskName)) } catch {
        $cause = $_.Exception
        while ($null -ne $cause) {
            if ($cause.HResult -eq -2147024894) { return $null }
            $cause = $cause.InnerException
        }
        Stop-Update 30 "无法读取系统计划，未更改计划：$($_.Exception.Message)"
    }
}

function Read-MaintenanceTaskXml($Task) {
    $settings = [Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $reader = [Xml.XmlReader]::Create([IO.StringReader]::new([string]$Task.Xml), $settings)
    try {
        $xml = [Xml.XmlDocument]::new()
        $xml.XmlResolver = $null
        $xml.Load($reader)
        return ,$xml
    } finally { $reader.Dispose() }
}

function Get-MaintenanceFingerprint($Task) {
    $xml = Read-MaintenanceTaskXml $Task
    $ns = [Xml.XmlNamespaceManager]::new($xml.NameTable)
    $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
    $enabled = $xml.SelectSingleNode('/t:Task/t:Settings/t:Enabled', $ns)
    if ($null -ne $enabled) { [void]$enabled.ParentNode.RemoveChild($enabled) }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($xml.OuterXml))).Replace('-', '') }
    finally { $sha.Dispose() }
}

function Test-MaintenanceTaskOwned($Task, [string]$Target) {
    if ($null -eq $Task) { return $false }
    $definition = $Task.Definition
    if ([string]$Task.Path -cne ('\' + (Get-MaintenanceTaskName)) -or
        $definition.Actions.Count -ne 1) { return $false }
    try {
        # COM's Principal.UserId getter can normalize a registered SID into an
        # ambiguous bare account name. The registered XML retains its authority.
        $xml = Read-MaintenanceTaskXml $Task
        $ns = [Xml.XmlNamespaceManager]::new($xml.NameTable)
        $ns.AddNamespace('t', 'http://schemas.microsoft.com/windows/2004/02/mit/task')
        $principals = $xml.SelectNodes('/t:Task/t:Principals/t:Principal', $ns)
        if ($principals.Count -ne 1) { return $false }
        $users = $principals[0].SelectNodes('t:UserId', $ns)
        if ($users.Count -ne 1 -or $principals[0].SelectNodes('t:GroupId', $ns).Count -ne 0) { return $false }
        $actions = $xml.SelectNodes('/t:Task/t:Actions', $ns)
        if ($actions.Count -ne 1) { return $false }
        $context = $actions[0].GetAttribute('Context')
        if ($context -and $context -cne $principals[0].GetAttribute('id')) { return $false }
        $principal = $users[0].InnerText
        if ($principal -match '^S-[0-9]-') {
            $sid = [Security.Principal.SecurityIdentifier]::new($principal).Value
        } elseif ($principal -match '^[^\\]+\\[^\\]+$|^[^@\\]+@[^@\\]+$') {
            $sid = [Security.Principal.NTAccount]::new($principal).Translate([Security.Principal.SecurityIdentifier]).Value
        } else { return $false }
    } catch { return $false }
    if ($sid -cne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { return $false }
    $action = $definition.Actions.Item(1)
    return $action.Type -eq 0 -and [string]$action.Path -ieq (Join-Path $Target 'SparkKeeper.exe') -and
        [string]$action.Arguments -ceq '--scheduled' -and [string]$action.WorkingDirectory -ieq $Target
}

function Assert-MaintenanceTaskIdle($Task) {
    if ($null -ne $Task -and ($Task.State -in @(2, 4) -or $Task.GetInstances(0).Count -gt 0)) {
        Stop-Update 30 '计划任务仍在运行或排队，请等待自然结束后重试；安装器不会终止任务或丢弃队列。'
    }
}

function New-MaintenanceSnapshot($Task, [string]$Target) {
    if (-not (Test-MaintenanceTaskOwned $Task $Target)) { return $null }
    if ($Task.Definition.Settings.StartWhenAvailable) {
        Stop-Update 30 '本计划设置了错过后补跑，无法安全暂停；请先在程序中重新保存计划以恢复不补跑设置。'
    }
    return [pscustomobject]@{
        path = '\'; name = Get-MaintenanceTaskName
        sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        fingerprint = Get-MaintenanceFingerprint $Task
        originalEnabled = [bool]$Task.Enabled
    }
}

function Test-MaintenanceSnapshot($Task, $Snapshot, [string]$Target) {
    return $null -ne $Snapshot -and (Test-MaintenanceTaskOwned $Task $Target) -and
        (Get-MaintenanceFingerprint $Task) -ceq $Snapshot.fingerprint
}

function Get-MaintenanceCompletionPath([string]$Target) {
    return Join-Path (Resolve-InstallTarget $Target) '.installer\uninstall-completed.json'
}

function Read-MaintenanceJournal([string]$Target) {
    $path = Get-MaintenanceJournalPath
    $completion = $false
    if (-not (Test-Path -LiteralPath $path)) {
        $path = Get-MaintenanceCompletionPath $Target
        if (-not (Test-Path -LiteralPath $path)) { return $null }
        Assert-NoReparsePath $path
        $completion = $true
    }
    try {
        $record = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($record.format -isnot [int] -or $record.format -ne 1 -or $record.product -cne 'SparkKeeper' -or
            $record.appIdentity -cne $AppIdentity -or $record.target -isnot [string] -or
            $record.target -cne $Target -or (Resolve-InstallTarget $record.target) -cne $Target -or
            $record.operation -cnotin @('install', 'uninstall') -or
            $record.purgeAuthorized -isnot [bool] -or $record.purgeStarted -isnot [bool] -or
            ($record.purgeStarted -and -not $record.purgeAuthorized) -or
            $record.phase -cnotin @('prepared', 'paused', 'mutating', 'verified', 'restoring', 'uninstall-mutating', 'uninstall-verified', 'uninstall-purging')) {
            throw '产品、安装身份、目标目录或阶段不匹配'
        }
        if ($null -ne $record.oldManifest) { [void](Assert-IncomingManifest $record.oldManifest) }
        if ($record.operation -eq 'install' -and $null -eq $record.newManifest) { throw '缺少新清单' }
        if ($null -ne $record.newManifest) { [void](Assert-IncomingManifest $record.newManifest) }
        if ($null -eq $record.priorManifests -or $record.priorManifests -isnot [array]) { throw '旧清单列表无效' }
        foreach ($manifest in $record.priorManifests) { [void](Assert-IncomingManifest $manifest) }
        if ($null -ne $record.task) {
            if ($record.task.path -cne '\' -or $record.task.name -cne (Get-MaintenanceTaskName) -or
                $record.task.sid -cne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -or
                $record.task.originalEnabled -isnot [bool] -or $record.task.fingerprint -isnot [string] -or
                $record.task.fingerprint -cnotmatch '^[A-F0-9]{64}$') { throw '计划快照无效' }
        }
        if ($completion) {
            if ($record.phase -cne 'uninstall-purging' -or -not $record.purgeStarted) { throw '卸载完成凭证阶段无效' }
            # A later Setup repairs program metadata, not already-purged data.
            $record.task = $null
            $record.purgeStarted = $false
            $record.purgeAuthorized = $false
            $record.phase = 'uninstall-verified'
        }
        return $record
    } catch { Stop-Update 41 "维护记录损坏或属于另一安装目标；未更改任何计划或文件。请保留 $path 并用原目标安装包修复：$($_.Exception.Message)" }
}

function Write-MaintenanceJournal($Record, [bool]$Completion = $false) {
    $path = if ($Completion) { Get-MaintenanceCompletionPath $Record.target } else { Get-MaintenanceJournalPath }
    $directory = [IO.Path]::GetDirectoryName($path)
    $ancestor = $directory
    while (-not (Test-Path -LiteralPath $ancestor)) { $ancestor = [IO.Path]::GetDirectoryName($ancestor) }
    Assert-NoReparsePath $ancestor
    [void][IO.Directory]::CreateDirectory($directory)
    Assert-NoReparsePath $directory
    if (Test-Path -LiteralPath $path) { Assert-NoReparsePath $path }
    if (-not ('SparkKeeper.MaintenanceFile' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace SparkKeeper {
    public static class MaintenanceFile {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool MoveFileEx(string source, string target, uint flags);
    }
}
'@
    }
    $temporary = Join-Path $directory ([Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Record | ConvertTo-Json -Depth 30))
        $stream = [IO.FileStream]::new($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
            [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        if (-not [SparkKeeper.MaintenanceFile]::MoveFileEx($temporary, $path, 9)) {
            throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Complete-MaintenanceJournal([bool]$Archive = $false) {
    $path = Get-MaintenanceJournalPath
    if ($Archive) {
        [IO.File]::Move($path, (Join-Path ([IO.Path]::GetDirectoryName($path)) ('verified-no-restore-' + [Guid]::NewGuid().ToString('N') + '.json')))
    } else { [IO.File]::Delete($path) }
}

function Suspend-MaintenanceTask($Record) {
    $task = Get-MaintenanceTask
    if (Test-MaintenanceSnapshot $task $Record.task $Record.target) {
        Assert-MaintenanceTaskIdle $task
        # The durable original Enabled snapshot always precedes this first write.
        $task.Enabled = $false
        $task = Get-MaintenanceTask
        if (-not (Test-MaintenanceSnapshot $task $Record.task $Record.target) -or $task.Enabled) {
            Stop-Update 30 '计划暂停后读回校验失败，已保留维护记录；不会复制程序文件，请重跑安装包修复。'
        }
        Assert-MaintenanceTaskIdle $task
    } elseif ($null -ne $task -and $task.Enabled -and $Record.operation -eq 'install') {
        Stop-Update 30 '同名计划不属于本目录或定义已变化。请人工停用并核对首次迁入路径，安装后在新版重新保存绑定；安装器不会修改该计划。'
    }
    $Record.phase = 'paused'
    Write-MaintenanceJournal $Record
}

function Restore-MaintenanceTask($Record) {
    $task = Get-MaintenanceTask
    if ($null -eq $Record.task -and $null -eq $task) { Complete-MaintenanceJournal; return }
    if (-not (Test-MaintenanceSnapshot $task $Record.task $Record.target)) {
        $script:MaintenanceNotice = '程序文件已验证，但计划已被删除、创建或修改；安装器未覆盖、重建或恢复该计划。请人工核对并在新版重新保存计划。维护记录已归档，不再阻止程序启动。'
        Complete-MaintenanceJournal $true
        return
    }
    Assert-MaintenanceTaskIdle $task
    $Record.phase = 'restoring'
    Write-MaintenanceJournal $Record
    try {
        $task.Enabled = $Record.task.originalEnabled
        $task = Get-MaintenanceTask
        if (-not (Test-MaintenanceSnapshot $task $Record.task $Record.target) -or
            [bool]$task.Enabled -ne $Record.task.originalEnabled) { throw '恢复后读回不一致' }
    } catch { Stop-Update 41 "文件已验证，但计划恢复失败；维护记录保留，计划可能仍暂停。请重新运行安装包修复：$($_.Exception.Message)" }
    Complete-MaintenanceJournal
}

function Assert-MaintenanceFiles($Record, $Incoming) {
    # Partial copies may contain empty, manifest-known directories. The portable
    # updater intentionally rejects them, so keep recovery enumeration local.
    if (-not (Test-Path -LiteralPath $Record.target)) { return }
    Assert-NoReparsePath $Record.target
    $known = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $directories = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($manifest in @($Record.oldManifest, $Record.newManifest, $Incoming) + @($Record.priorManifests)) {
        if ($null -eq $manifest) { continue }
        foreach ($entry in $manifest.files.PSObject.Properties) {
            [void]$known.Add($entry.Name)
            $parent = [IO.Path]::GetDirectoryName($entry.Name.Replace('/', '\'))
            while ($parent) {
                [void]$directories.Add($parent.Replace('\', '/'))
                $parent = [IO.Path]::GetDirectoryName($parent)
            }
        }
    }
    $metadata = @('.installer/installer_guard.ps1', '.installer/update.ps1', '.installer/maintenance_tasks.ps1',
        '.installer/uninstall-completed.json', 'unins000.exe', 'unins000.dat', 'unins000.msg')
    [void]$directories.Add('.installer')
    [void]$known.Add('release-manifest.json')
    $files = [Collections.Generic.Dictionary[string,IO.FileInfo]]::new([StringComparer]::OrdinalIgnoreCase)
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Record.target)
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            $name = $item.FullName.Substring($Record.target.Length + 1).Replace('\', '/')
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Update 20 "修复目录包含链接：$name"
            }
            if ($item.PSIsContainer) {
                if (-not $directories.Contains($name)) { Stop-Update 20 "修复目录包含未知目录：$name" }
                $pending.Push($item.FullName)
            } elseif ($name -notin $metadata) {
                if (-not $known.Contains($name)) { Stop-Update 20 "修复目录包含维护记录之外的文件，未更改：$name" }
                $files.Add($name, $item)
            }
        }
    }
    Assert-FilesAvailable $files $true
}

function Test-MaintenanceOriginalFiles($Record) {
    try {
        if ($null -eq $Record.oldManifest) {
            return -not (Test-Path -LiteralPath $Record.target) -or @(Get-ChildItem -LiteralPath $Record.target -Force).Count -eq 0
        }
        [void](Read-ReleaseManifest $Record.target)
        $current = Read-IncomingManifest (Join-Path $Record.target 'release-manifest.json')
        return (Get-MaintenanceManifestDigest $current) -ceq (Get-MaintenanceManifestDigest $Record.oldManifest)
    } catch { return $false }
}

function Get-MaintenanceManifestDigest($Manifest) {
    # Validation is structural; comparisons are independent of JSON whitespace/order.
    $parts = [Collections.Generic.List[string]]::new()
    $parts.Add([string]$Manifest.version)
    foreach ($entry in ($Manifest.files.PSObject.Properties | Sort-Object Name)) {
        $parts.Add($entry.Name + ':' + $entry.Value.ToUpperInvariant())
    }
    return [string]::Join("`n", $parts)
}

function Invoke-MaintenanceAbort([string]$Target) {
    $targetDir = Resolve-InstallTarget $Target
    $record = Read-MaintenanceJournal $targetDir
    if ($null -eq $record) { return }
    if ($record.purgeStarted) {
        Stop-Update 41 '用户数据清理已开始，不能恢复计划。请重跑同目录安装包完成已授权清理并修复程序，再卸载。'
    }
    $script:InstallerMetadataAllowed = $true
    if (-not (Test-MaintenanceOriginalFiles $record)) {
        Stop-Update 41 '安装/卸载未完成，旧文件无法完整验证。计划保持暂停，维护记录已保留；请重新运行安装包修复，勿启动旧程序。'
    }
    Assert-InstallerIdle $targetDir
    Restore-MaintenanceTask $record
}

function Invoke-MaintenanceMutating([string]$Target, [string]$Operation = 'install') {
    $targetDir = Resolve-InstallTarget $Target
    $record = Read-MaintenanceJournal $targetDir
    if ($null -eq $record -or $record.operation -cne $Operation -or $record.phase -cne 'paused') {
        Stop-Update 41 '缺少已暂停的维护事务，拒绝修改文件。'
    }
    Suspend-MaintenanceTask $record
    Assert-InstallerIdle $targetDir
    $script:InstallerMetadataAllowed = $true
    Assert-MaintenanceFiles $record $record.newManifest
    $record.phase = if ($Operation -eq 'install') { 'mutating' } else { 'uninstall-mutating' }
    Write-MaintenanceJournal $record
}

function Invoke-UninstallPreflight([string]$Target) {
    if (-not $PurgeUserData) { Stop-Update 10 '卸载会永久删除此用户的全部 SparkKeeper 共享数据，必须明确确认；静默卸载需 /PURGEUSERDATA。' }
    $targetDir = Resolve-InstallTarget $Target
    $script:InstallerMetadataAllowed = Test-RegisteredTarget $targetDir
    $record = Read-MaintenanceJournal $targetDir
    if ($null -eq $record) { [void](Read-ReleaseManifest $targetDir) }
    else {
        $script:InstallerMetadataAllowed = $true
        Assert-MaintenanceFiles $record $record.newManifest
    }
    Assert-MaintenanceSharedReferences $targetDir
    $task = Get-MaintenanceTask
    if (Test-MaintenanceTaskOwned $task $targetDir) { Assert-MaintenanceTaskIdle $task; [void](New-MaintenanceSnapshot $task $targetDir) }
    Assert-InstallerIdle $targetDir
    [void](Get-MaintenancePurgeEntries)
}

function Invoke-UninstallPrepare([string]$Target) {
    Invoke-UninstallPreflight $Target
    $targetDir = Resolve-InstallTarget $Target
    $record = Read-MaintenanceJournal $targetDir
    if ($null -eq $record) {
        $record = [pscustomobject]@{
            format = 1; product = 'SparkKeeper'; appIdentity = $AppIdentity; target = $targetDir
            operation = 'uninstall'; phase = 'prepared'; task = New-MaintenanceSnapshot (Get-MaintenanceTask) $targetDir
            oldManifest = Read-IncomingManifest (Join-Path $targetDir 'release-manifest.json')
            newManifest = $null; priorManifests = @(); purgeAuthorized = $true; purgeStarted = $false
        }
    } else { $record.operation = 'uninstall'; $record.phase = 'prepared'; $record.purgeAuthorized = $true }
    Write-MaintenanceJournal $record
    Suspend-MaintenanceTask $record
    Invoke-MaintenanceMutating $targetDir 'uninstall'
}

function Invoke-UninstallRemoveFiles([string]$Target) {
    $targetDir = Resolve-InstallTarget $Target
    $record = Read-MaintenanceJournal $targetDir
    if (-not $PurgeUserData -or $null -eq $record -or -not $record.purgeAuthorized -or
        $record.operation -cne 'uninstall' -or $record.phase -cne 'uninstall-mutating') {
        Stop-Update 41 '缺少已确认的卸载维护事务，拒绝删除程序文件。'
    }
    Assert-InstallerIdle $targetDir
    Assert-MaintenanceSharedReferences $targetDir
    Assert-MaintenanceFiles $record $record.newManifest
    $known = @{}
    foreach ($manifest in @($record.oldManifest, $record.newManifest) + @($record.priorManifests)) {
        if ($null -eq $manifest) { continue }
        foreach ($entry in $manifest.files.PSObject.Properties) {
            if (-not $known.ContainsKey($entry.Name)) { $known[$entry.Name] = @() }
            $known[$entry.Name] += $entry.Value
        }
    }
    $files = [Collections.Generic.List[string]]::new()
    foreach ($name in $known.Keys) {
        $file = Join-Path $targetDir $name.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $file)) { continue }
        Assert-NoReparsePath $file
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -notin $known[$name]) {
            Stop-Update 20 "待卸载程序文件已变化，未开始删除：$name；请人工核对或用安装包修复。"
        }
        $files.Add($file)
    }
    # Validate every existing payload before deletion. Keep release-manifest and
    # .installer/unins until Inno's native phase so fatal failures remain retryable.
    foreach ($file in $files) {
        Assert-NoReparsePath $file
        [IO.File]::Delete($file)
        $parent = [IO.Path]::GetDirectoryName($file)
        while ($parent -ine $targetDir -and @(Get-ChildItem -LiteralPath $parent -Force).Count -eq 0) {
            [IO.Directory]::Delete($parent)
            $parent = [IO.Path]::GetDirectoryName($parent)
        }
    }
}

function Invoke-UninstallFinish([string]$Target) {
    $targetDir = Resolve-InstallTarget $Target
    $record = Read-MaintenanceJournal $targetDir
    if (-not $PurgeUserData -or $null -eq $record -or -not $record.purgeAuthorized -or
        $record.operation -cne 'uninstall' -or $record.phase -cne 'uninstall-mutating') {
        Stop-Update 41 '卸载维护记录缺失或阶段错误，未删除系统计划或数据。'
    }
    foreach ($manifest in @($record.oldManifest, $record.newManifest) + @($record.priorManifests)) {
        if ($null -eq $manifest) { continue }
        foreach ($entry in $manifest.files.PSObject.Properties) {
            if (Test-Path -LiteralPath (Join-Path $targetDir $entry.Name.Replace('/', '\'))) {
                Stop-Update 41 '卸载后仍有程序文件，可能被占用；计划保持暂停，请重新安装修复后再卸载。'
            }
        }
    }
    Assert-InstallerIdle $targetDir
    Assert-MaintenanceSharedReferences $targetDir
    [void](Get-MaintenancePurgeEntries)
    $record.phase = 'uninstall-verified'
    Write-MaintenanceJournal $record
    $task = Get-MaintenanceTask
    if (Test-MaintenanceSnapshot $task $record.task $targetDir) {
        Assert-MaintenanceTaskIdle $task
        $folder = Get-MaintenanceTaskFolder
        $folder.DeleteTask((Get-MaintenanceTaskName), 0)
        if ($null -ne (Get-MaintenanceTask)) { Stop-Update 41 '卸载文件完成但计划删除读回失败，维护记录已保留，请重新安装修复。' }
    } elseif ($null -ne $task) {
        Stop-Update 41 '卸载期间计划定义已变化，未删除任务或用户数据。请人工核对计划后重新安装修复。'
    }
    Invoke-MaintenanceDataPurge $record
}

function Get-MaintenanceScheduledTasks {
    if ($AppIdentity.StartsWith('SparkKeeper.Test.', [StringComparison]::Ordinal)) {
        # The isolated test data root cannot be referenced by production tasks.
        # Never enumerate the user's production task tree during test uninstalls.
        $task = Get-MaintenanceTask
        if ($null -ne $task) { $task }
        return
    }
    $pending = [Collections.Generic.Stack[object]]::new()
    $pending.Push((Get-MaintenanceTaskFolder))
    while ($pending.Count) {
        $folder = $pending.Pop()
        foreach ($task in $folder.GetTasks(1)) { $task }
        foreach ($child in $folder.GetFolders(0)) { $pending.Push($child) }
    }
}

function Assert-MaintenanceSharedReferences([string]$Target) {
    $named = Get-MaintenanceTask
    if ($null -ne $named -and -not (Test-MaintenanceTaskOwned $named $Target)) {
        Stop-Update 30 '同名计划属于其他目录或身份，可能仍引用共享数据；请先人工处理该计划，再卸载。未更改该计划或数据。'
    }
    $data = Get-MaintenanceDataRoot
    foreach ($task in Get-MaintenanceScheduledTasks) {
        if (Test-MaintenanceTaskOwned $task $Target) { continue }
        foreach ($action in $task.Definition.Actions) {
            if ($action.Type -ne 0) { continue }
            $image = [string]$action.Path
            $arguments = [string]$action.Arguments
            if ([IO.Path]::GetFileName($image) -ieq 'SparkKeeper.exe' -or
                $arguments -match '(?i)spark_keeper|portable_entry\.py' -or
                $arguments.IndexOf($data, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                ([string]$action.WorkingDirectory).Equals($data, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Update 30 "其他系统计划 $($task.Path) 可能引用共享数据，请先人工处理再卸载；未修改它。"
            }
        }
    }
}

function Get-MaintenancePurgeEntries {
    # No caller-supplied path. Do not use recursive Remove-Item: enumerate and
    # validate the entire fixed root before the first irreversible deletion.
    $root = Get-MaintenanceDataRoot
    $parent = [IO.Path]::GetDirectoryName($root)
    $ancestor = $parent
    while (-not (Test-Path -LiteralPath $ancestor)) { $ancestor = [IO.Path]::GetDirectoryName($ancestor) }
    Assert-NoReparsePath $ancestor
    if ((Get-CanonicalDirectory $ancestor) -ine $ancestor -or $root.Length -le 3 -or
        [IO.Path]::GetFullPath($root).TrimEnd('\') -cne $root) { Stop-Update 10 '用户数据根路径不安全，拒绝删除。' }
    $entries = [Collections.Generic.List[IO.FileSystemInfo]]::new()
    if (-not (Test-Path -LiteralPath $root)) { return ,$entries }
    Assert-NoReparsePath $root
    if ((Get-CanonicalDirectory $root) -ine $root) { Stop-Update 10 '用户数据目录被重定向，拒绝删除。' }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($root)
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                -not (Test-PathWithin $item.FullName $root)) { Stop-Update 10 "用户数据含链接或外部路径，拒绝删除：$($item.FullName)" }
            $entries.Add($item)
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
            else {
                try {
                    $handle = [IO.File]::Open($item.FullName, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                    $handle.Dispose()
                } catch { Stop-Update 32 "用户数据文件被占用或不可写，未开始清理：$($item.FullName)" }
            }
        }
    }
    return ,$entries
}

function Invoke-MaintenanceDataPurge($Record) {
    if (-not $Record.purgeAuthorized) { Stop-Update 41 '维护记录没有用户数据删除授权。' }
    Assert-InstallerIdle $Record.target
    Assert-MaintenanceSharedReferences $Record.target
    if ($null -ne (Get-MaintenanceTask)) { Stop-Update 41 '系统计划仍存在，拒绝清空共享数据。' }
    $entries = Get-MaintenancePurgeEntries
    $root = Get-MaintenanceDataRoot
    $journal = Get-MaintenanceJournalPath
    $maintenance = [IO.Path]::GetDirectoryName($journal)
    $Record.purgeStarted = $true
    $Record.phase = 'uninstall-purging'
    Write-MaintenanceJournal $Record
    try {
        foreach ($item in ($entries | Where-Object { -not $_.PSIsContainer })) {
            if ($item.FullName -ieq $journal) { continue }
            Assert-NoReparsePath $item.FullName
            [IO.File]::Delete($item.FullName)
        }
        foreach ($item in ($entries | Where-Object { $_.PSIsContainer } | Sort-Object { $_.FullName.Length } -Descending)) {
            if ($item.FullName -ieq $maintenance) { continue }
            Assert-NoReparsePath $item.FullName
            [IO.Directory]::Delete($item.FullName)
        }
        # Journal is the final file removed. If final directory removal fails,
        # recreate it before returning failure so no worker can create an empty DB.
        # Keep a target-bound receipt across pending removal and native cleanup.
        Write-MaintenanceJournal $Record $true
        Assert-NoReparsePath $journal
        [IO.File]::Delete($journal)
        [IO.Directory]::Delete($maintenance)
        [IO.Directory]::Delete($root)
    } catch {
        $failure = $_.Exception.Message
        Write-MaintenanceJournal $Record
        Stop-Update 41 "用户数据清理未完成，维护记录与启动门禁保留，计划不会恢复。请重跑原卸载器；若卸载器损坏，重跑同目录安装包完成清理并修复程序，再卸载：$failure"
    }
}

function Invoke-UninstallCleanup([string]$Target) {
    # Only native Inno metadata cleanup remains at this point. Keep the receipt
    # if it was interrupted; no second purge and no task recreation is allowed.
    $targetDir = Resolve-InstallTarget $Target
    $receipt = Get-MaintenanceCompletionPath $targetDir
    if (-not (Test-Path -LiteralPath $receipt)) { return }
    [void](Read-MaintenanceJournal $targetDir)
    foreach ($name in @('release-manifest.json', '.installer/installer_guard.ps1', '.installer/update.ps1',
        '.installer/maintenance_tasks.ps1', 'unins000.exe', 'unins000.dat', 'unins000.msg')) {
        if (Test-Path -LiteralPath (Join-Path $targetDir $name.Replace('/', '\'))) {
            Stop-Update 41 '用户数据已清空，但 Inno 卸载元数据仍有残留；完成凭证已保留，请重跑卸载器或用安装包修复后再卸载。'
        }
    }
    if ((Test-RegisteredTarget $targetDir) -or (Test-Path -LiteralPath (Get-MaintenanceJournalPath))) {
        Stop-Update 41 '卸载注册或维护状态尚未清理，完成凭证保留；请用安装包修复后再次卸载。'
    }
    Assert-NoReparsePath $receipt
    [IO.File]::Delete($receipt)
    $directory = [IO.Path]::GetDirectoryName($receipt)
    if (@(Get-ChildItem -LiteralPath $directory -Force).Count -eq 0) { [IO.Directory]::Delete($directory) }
    if (@(Get-ChildItem -LiteralPath $targetDir -Force).Count -eq 0) { [IO.Directory]::Delete($targetDir) }
}
