#requires -version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Scenario)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding
$identity = 'SparkKeeper.Test.' + [Guid]::NewGuid().ToString('N')
$privateRoot = Join-Path ([IO.Path]::GetTempPath()) "SparkKeeper-maintenance\$identity"
$expectedData = Join-Path $privateRoot 'data'
$expectedJournal = Join-Path $expectedData 'maintenance\pending.json'
$target = Join-Path $env:PROBE_ROOT 'installed'
$incoming = Join-Path $env:PROBE_ROOT 'incoming\release-manifest.json'
$script:Task = $null
$script:OtherTasks = @()
$script:DeleteCalls = 0
$script:DeleteFails = $false
$script:DeleteIgnored = $false
$script:PurgeHandle = $null
$script:FailPurge = $false
$script:ConfirmPurge = $Scenario -match '^(uninstall|purge)-' -and $Scenario -notin @('uninstall-no-confirm', 'uninstall-no-confirm-finish')
$createdPrivateRoot = $false
$junction = $null

# Fail closed even if production accidentally bypasses the three stub boundaries.
function New-Object { throw 'Unexpected object/COM construction in isolated maintenance probe' }

Add-Type -TypeDefinition @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Security;
using System.Text;
namespace MaintenanceProbe {
    public sealed class Action {
        public int Type = 0;
        public string Path;
        public string Arguments = "--scheduled";
        public string WorkingDirectory;
    }
    public sealed class Actions : IEnumerable {
        public readonly List<Action> Values = new List<Action>();
        public int Count { get { return Values.Count; } }
        public Action Item(int index) { return Values[index - 1]; }
        public IEnumerator GetEnumerator() { return Values.GetEnumerator(); }
    }
    public sealed class Principal { public string UserId; }
    public sealed class Settings {
        public bool StartWhenAvailable = false;
        public bool Enabled = true;
    }
    public sealed class Definition {
        public readonly Principal Principal = new Principal();
        public readonly Actions Actions = new Actions();
        public readonly Settings Settings = new Settings();
    }
    public sealed class Instances { public int Count; }
    public sealed class Task {
        public readonly Definition Definition = new Definition();
        public string Path;
        public int State = 3;
        public int InstanceCount;
        public int EnabledWrites;
        public bool IgnoreDisable;
        public bool FailRestore;
        public string Description = "owned fixture";
        public string XmlPrincipalUserId;
        public bool ExtraPrincipal;
        public string GroupId;
        public bool Enabled {
            get { return Definition.Settings.Enabled; }
            set {
                EnabledWrites++;
                if (value && FailRestore) throw new COMException("Injected restore failure");
                if (!value && IgnoreDisable) return;
                Definition.Settings.Enabled = value;
            }
        }
        public Instances GetInstances(int flags) {
            if (flags != 0) throw new ArgumentException("Unexpected instance flags");
            return new Instances { Count = InstanceCount };
        }
        private static string Escape(string value) { return SecurityElement.Escape(value); }
        public string Xml {
            get {
                var xml = new StringBuilder("<Task xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\" version=\"1.2\">");
                xml.Append("<RegistrationInfo><Description>").Append(Escape(Description)).Append("</Description></RegistrationInfo>");
                string userId = XmlPrincipalUserId ?? Definition.Principal.UserId;
                xml.Append("<Principals><Principal id=\"Author\">");
                if (GroupId == null) xml.Append("<UserId>").Append(Escape(userId)).Append("</UserId>");
                else xml.Append("<GroupId>").Append(Escape(GroupId)).Append("</GroupId>");
                xml.Append("</Principal>");
                if (ExtraPrincipal) xml.Append("<Principal id=\"Other\"><UserId>").Append(Escape(userId)).Append("</UserId></Principal>");
                xml.Append("</Principals>");
                xml.Append("<Settings><Enabled>").Append(Enabled ? "true" : "false").Append("</Enabled><StartWhenAvailable>");
                xml.Append(Definition.Settings.StartWhenAvailable ? "true" : "false").Append("</StartWhenAvailable></Settings><Actions Context=\"Author\">");
                foreach (Action action in Definition.Actions.Values) {
                    xml.Append("<Exec><Command>").Append(Escape(action.Path)).Append("</Command><Arguments>");
                    xml.Append(Escape(action.Arguments)).Append("</Arguments><WorkingDirectory>");
                    xml.Append(Escape(action.WorkingDirectory)).Append("</WorkingDirectory></Exec>");
                }
                return xml.Append("</Actions></Task>").ToString();
            }
        }
    }
}
'@

# Dot-source this function to reload real helpers and rebind *all* scheduler access.
function Import-ProbeGuard {
    . $env:INSTALLER_GUARD -AppIdentity $identity -PurgeUserData:$script:ConfirmPurge
    function Get-MaintenanceTask { return $script:Task }
    function Get-MaintenanceScheduledTasks {
        if ($null -ne $script:Task) { $script:Task }
        foreach ($other in $script:OtherTasks) { $other }
    }
    function Get-MaintenanceTaskFolder {
        $folder = [pscustomobject]@{}
        $folder | Add-Member -MemberType ScriptMethod -Name DeleteTask -Value {
            param($Name, $Flags)
            if ($Name -cne "$identity.LocalDaily" -or $Flags -ne 0) { throw 'Unexpected task deletion target' }
            $script:DeleteCalls++
            if ($script:DeleteFails) { throw [Runtime.InteropServices.COMException]::new('Injected delete failure') }
            if (-not $script:DeleteIgnored) { $script:Task = $null }
        }
        return $folder
    }
    function Assert-SchedulerStopped { throw 'Legacy scheduler boundary must not be used' }
    function Assert-InstallerIdle([string]$Target) {}
    function Test-RegisteredTarget([string]$Target, [string]$IncomingVersion = '') { return $false }
}

function Assert-Probe([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed [$Scenario]: $Message" }
}

function Expect-Code([scriptblock]$Action, [int]$Code) {
    try { & $Action } catch {
        if ($_.Exception.Data['ExitCode'] -ne $Code) { throw }
        return
    }
    throw "Expected maintenance error $Code [$Scenario]"
}

function Expect-Failure([scriptblock]$Action) {
    try { & $Action } catch { return }
    throw "Expected failure [$Scenario]"
}

function New-ProbeTask([bool]$Enabled = $true) {
    $task = [MaintenanceProbe.Task]::new()
    $task.Path = '\' + (Get-MaintenanceTaskName)
    $task.Definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $task.Definition.Settings.Enabled = $Enabled
    $action = [MaintenanceProbe.Action]::new()
    $action.Path = Join-Path $target 'SparkKeeper.exe'
    $action.WorkingDirectory = $target
    $task.Definition.Actions.Values.Add($action)
    return $task
}

function Assert-Journal([string]$Phase, [string]$Operation = 'install') {
    Assert-Probe (Test-Path -LiteralPath $expectedJournal) 'pending journal missing'
    $record = Get-Content -LiteralPath $expectedJournal -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($name in @('format', 'product', 'target', 'appIdentity', 'operation', 'phase', 'task', 'oldManifest', 'newManifest', 'priorManifests', 'purgeAuthorized', 'purgeStarted')) {
        Assert-Probe ($null -ne $record.PSObject.Properties[$name]) "journal field missing: $name"
    }
    Assert-Probe ($record.format -eq 1 -and $record.product -ceq 'SparkKeeper') 'journal format/product'
    Assert-Probe ($record.target -ceq $target -and $record.appIdentity -ceq $identity) 'journal isolation binding'
    Assert-Probe ($record.phase -ceq $Phase -and $record.operation -ceq $Operation) 'journal operation/phase'
    Assert-Probe ($record.priorManifests -is [array]) 'prior manifests must be an array'
    Assert-Probe ($record.purgeAuthorized -is [bool] -and $record.purgeStarted -is [bool]) 'purge flags must be booleans'
    if ($null -ne $record.task) {
        Assert-Probe ($record.task.path -ceq '\' -and $record.task.name -ceq "$identity.LocalDaily") 'snapshot task binding'
        Assert-Probe ($record.task.sid -ceq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) 'snapshot SID'
        Assert-Probe ($record.task.originalEnabled -is [bool] -and $record.task.fingerprint -cmatch '^[A-F0-9]{64}$') 'snapshot state/fingerprint'
    }
    return $record
}

function Assert-NoPending {
    Assert-Probe (-not (Test-Path -LiteralPath $expectedJournal)) 'pending journal unexpectedly retained'
}

function Assert-Archived {
    Assert-NoPending
    $archives = @(Get-ChildItem -LiteralPath (Split-Path -Parent $expectedJournal) -Filter 'verified-no-restore-*.json')
    Assert-Probe ($archives.Count -eq 1) 'changed/missing task record must be archived'
}

function Copy-Incoming {
    [void][IO.Directory]::CreateDirectory($target)
    Copy-Item -Path (Join-Path $env:PROBE_ROOT 'incoming\*') -Destination $target -Recurse -Force
    # Match Inno [Files]: repaired installs retain the three real maintenance
    # helpers after the completed-uninstall receipt is removed.
    $metadata = Join-Path $target '.installer'
    [void][IO.Directory]::CreateDirectory($metadata)
    foreach ($name in @('installer_guard.ps1', 'update.ps1', 'maintenance_tasks.ps1')) {
        Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $env:INSTALLER_GUARD) $name) -Destination (Join-Path $metadata $name) -Force
    }
}

function Complete-Install {
    Invoke-MaintenanceMutating $target
    Copy-Incoming
    Invoke-InstallerFinish $target
}

function Remove-Payload {
    $record = Read-MaintenanceJournal $target
    foreach ($entry in $record.oldManifest.files.PSObject.Properties) {
        [IO.File]::Delete((Join-Path $target $entry.Name.Replace('/', '\')))
    }
    [IO.File]::Delete((Join-Path $target 'release-manifest.json'))
}

function Assert-DataIntact {
    Assert-Probe ([IO.File]::ReadAllText($sentinel) -ceq 'private user data') 'user data was changed'
    Assert-Probe ([IO.File]::ReadAllText($neighbor) -ceq 'neighbor data') 'neighbor data was changed'
}

function Assert-Purged {
    Assert-Probe (-not (Test-Path -LiteralPath $expectedData)) 'exact private data root must be removed completely'
    Assert-Probe ([IO.File]::ReadAllText($neighbor) -ceq 'neighbor data') 'purge escaped exact data root'
    Assert-Probe ($null -eq $script:Task) 'purge must not leave/recreate a task'
}

try {
    . Import-ProbeGuard
    Assert-Probe ((Get-MaintenanceTaskName) -ceq "$identity.LocalDaily") 'test task name'
    Assert-Probe ((Get-MaintenanceDataRoot) -ceq $expectedData) 'test data root'
    Assert-Probe ((Get-MaintenanceJournalPath) -ceq $expectedJournal) 'test journal root'
    Assert-Probe (-not (Test-Path -LiteralPath $privateRoot)) 'unique identity already exists'
    [void][IO.Directory]::CreateDirectory($expectedData)
    $createdPrivateRoot = $true
    $sentinel = Join-Path $expectedData 'user-state.db'
    $neighbor = Join-Path $privateRoot 'neighbor-sentinel.db'
    [IO.File]::WriteAllText($sentinel, 'private user data')
    [IO.File]::WriteAllText($neighbor, 'neighbor data')
    $script:Task = New-ProbeTask

    switch -Regex ($Scenario) {
        '^owned-(enabled|disabled|none|principal-account|principal-xml-sid)$' {
            $original = $Scenario -in @('owned-enabled', 'owned-principal-account', 'owned-principal-xml-sid')
            if ($Scenario -eq 'owned-none') { $script:Task = $null }
            else { $script:Task.Definition.Settings.Enabled = $original }
            if ($Scenario -eq 'owned-principal-account') {
                $script:Task.Definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            }
            if ($Scenario -eq 'owned-principal-xml-sid') {
                $script:Task.XmlPrincipalUserId = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
                $script:Task.Definition.Principal.UserId = $identity
                Assert-Probe (Test-MaintenanceTaskOwned $script:Task $target) 'registered XML SID must override an unresolvable bare COM principal'
            }
            Invoke-InstallerPrepare $target $incoming
            $record = Assert-Journal 'paused'
            Assert-Probe (-not $record.purgeAuthorized -and -not $record.purgeStarted) 'install is never purge authorization'
            if ($null -ne $script:Task) {
                Assert-Probe (-not $script:Task.Enabled -and $record.task.originalEnabled -eq $original) 'owned task pause/original state'
            } else { Assert-Probe ($null -eq $record.task) 'absent task must not receive snapshot' }
            Complete-Install
            Assert-NoPending
            if ($null -ne $script:Task) { Assert-Probe ($script:Task.Enabled -eq $original) 'original Enabled not restored' }
            else { Assert-Probe ($null -eq (Get-MaintenanceTask)) 'absent task was recreated' }
            Assert-Probe ($script:DeleteCalls -eq 0) 'install deleted a task'
            Assert-DataIntact
            break
        }
        '^foreign-(action|principal|principal-account|xml-principal|xml-multiple-principals|xml-group|xml-bare-principal|taskpath|multiple|arguments|working-directory|action-type)(-disabled)?$' {
            $action = $script:Task.Definition.Actions.Item(1)
            switch ($Matches[1]) {
                'action' { $action.Path = Join-Path $env:PROBE_ROOT 'other\SparkKeeper.exe' }
                'principal' { $script:Task.Definition.Principal.UserId = 'S-1-0-0' }
                'principal-account' { $script:Task.Definition.Principal.UserId = "$env:COMPUTERNAME\$identity" }
                'xml-principal' { $script:Task.XmlPrincipalUserId = 'S-1-0-0' }
                'xml-multiple-principals' { $script:Task.ExtraPrincipal = $true }
                'xml-group' { $script:Task.GroupId = 'S-1-5-32-545' }
                'xml-bare-principal' { $script:Task.XmlPrincipalUserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name.Split('\')[-1] }
                'taskpath' { $script:Task.Path = '\OtherFolder\' + (Get-MaintenanceTaskName) }
                'multiple' { $script:Task.Definition.Actions.Values.Add([MaintenanceProbe.Action]::new()) }
                'arguments' { $action.Arguments = '--scheduled --extra' }
                'working-directory' { $action.WorkingDirectory = $env:PROBE_ROOT }
                'action-type' { $action.Type = 5 }
            }
            Assert-Probe (-not (Test-MaintenanceTaskOwned $script:Task $target)) 'foreign or ambiguous registered principal was accepted as owned'
            if ($Scenario.EndsWith('-disabled')) {
                $script:Task.Definition.Settings.Enabled = $false
                Invoke-InstallerPrepare $target $incoming
                Assert-Probe ($null -eq (Assert-Journal 'paused').task) 'foreign task snapshotted as owned'
                Complete-Install
                Assert-Archived
            } else {
                Expect-Code { Invoke-InstallerPrepare $target $incoming } 30
                Assert-NoPending
            }
            Assert-Probe ($script:Task.EnabledWrites -eq 0 -and $script:DeleteCalls -eq 0) 'foreign task was modified'
            Assert-DataIntact
            break
        }
        '^running-(state|queued|instances|disabled-instances)$' {
            if ($Scenario -eq 'running-state') { $script:Task.State = 4 }
            elseif ($Scenario -eq 'running-queued') { $script:Task.State = 2 }
            else { $script:Task.InstanceCount = 1 }
            if ($Scenario -eq 'running-disabled-instances') { $script:Task.Definition.Settings.Enabled = $false }
            Expect-Code { Invoke-InstallerPrepare $target $incoming } 30
            Assert-Probe ($script:Task.EnabledWrites -eq 0) 'running task was modified'
            Assert-NoPending
            break
        }
        '^start-when-available$' {
            $script:Task.Definition.Settings.StartWhenAvailable = $true
            Expect-Code { Invoke-InstallerPrepare $target $incoming } 30
            Assert-Probe ($script:Task.EnabledWrites -eq 0) 'catch-up task was paused'
            Assert-NoPending
            break
        }
        '^fingerprint$' {
            $before = Get-MaintenanceFingerprint $script:Task
            $script:Task.Enabled = $false
            Assert-Probe ($before -ceq (Get-MaintenanceFingerprint $script:Task)) 'Enabled must be excluded from fingerprint'
            $script:Task.Definition.Settings.StartWhenAvailable = $true
            Assert-Probe ($before -cne (Get-MaintenanceFingerprint $script:Task)) 'other Settings must remain in fingerprint'
            $script:Task.Definition.Settings.StartWhenAvailable = $false
            $script:Task.Description = 'changed definition'
            Assert-Probe ($before -cne (Get-MaintenanceFingerprint $script:Task)) 'non-Enabled definition change must affect fingerprint'
            break
        }
        '^disable-readback$' {
            $script:Task.IgnoreDisable = $true
            Expect-Code { Invoke-InstallerPrepare $target $incoming } 30
            $record = Assert-Journal 'prepared'
            Assert-Probe ($record.task.originalEnabled -and $script:Task.Enabled) 'failed disable must preserve first snapshot'
            Assert-Probe ([IO.File]::ReadAllText((Join-Path $target 'SparkKeeper.exe')) -ceq 'fixture') 'prepare touched payload'
            break
        }
        '^restore-failure$' {
            Invoke-InstallerPrepare $target $incoming
            Invoke-MaintenanceMutating $target
            Copy-Incoming
            $script:Task.FailRestore = $true
            Expect-Code { Invoke-InstallerFinish $target } 41
            $record = Assert-Journal 'restoring'
            Assert-Probe (-not $script:Task.Enabled -and $record.task.originalEnabled) 'restore failure lost pause/snapshot'
            $script:Task.FailRestore = $false
            . Import-ProbeGuard
            Invoke-InstallerPrepare $target $incoming
            Assert-Probe ((Assert-Journal 'paused').task.originalEnabled) 'restart replaced original Enabled with paused state'
            Complete-Install
            Assert-Probe ($script:Task.Enabled) 'repair did not restore originally enabled task'
            Assert-NoPending
            break
        }
        '^cancel-(unchanged|mutating-old|partial)$' {
            Invoke-InstallerPrepare $target $incoming
            if ($Scenario -ne 'cancel-unchanged') { Invoke-MaintenanceMutating $target }
            if ($Scenario -eq 'cancel-partial') {
                [IO.File]::WriteAllText((Join-Path $target 'SparkKeeper.exe'), 'partial')
                Expect-Code { Invoke-MaintenanceAbort $target } 41
                [void](Assert-Journal 'mutating')
                Assert-Probe (-not $script:Task.Enabled) 'partial cancellation restored task'
            } else {
                Invoke-MaintenanceAbort $target
                Assert-NoPending
                Assert-Probe ($script:Task.Enabled) 'safe cancellation did not restore task'
                Assert-Probe ([IO.File]::ReadAllText((Join-Path $target 'SparkKeeper.exe')) -ceq 'fixture') 'cancel changed old payload'
            }
            Assert-DataIntact
            break
        }
        '^finish-(hash|manifest|unknown)-failure$' {
            Invoke-InstallerPrepare $target $incoming
            Invoke-MaintenanceMutating $target
            Copy-Incoming
            switch ($Matches[1]) {
                'hash' { [IO.File]::WriteAllText((Join-Path $target 'SparkKeeper.exe'), 'corrupt') }
                'manifest' {
                    $manifest = Get-Content -LiteralPath (Join-Path $target 'release-manifest.json') -Raw | ConvertFrom-Json
                    $manifest.version = '9.0.0'
                    $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $target 'release-manifest.json') -Encoding UTF8
                }
                'unknown' { [IO.File]::WriteAllText((Join-Path $target 'unknown.txt'), 'foreign') }
            }
            Expect-Code { Invoke-InstallerFinish $target } 20
            [void](Assert-Journal 'mutating')
            Assert-Probe (-not $script:Task.Enabled) 'unverified finish restored task'
            Assert-DataIntact
            break
        }
        '^restart-(partial|first-metadata|first-unknown)$' {
            if ($Scenario -ne 'restart-partial') { $target = Join-Path $env:PROBE_ROOT 'first-install' }
            $script:Task = New-ProbeTask
            Invoke-InstallerPrepare $target $incoming
            Invoke-MaintenanceMutating $target
            [void][IO.Directory]::CreateDirectory($target)
            [IO.File]::WriteAllText((Join-Path $target 'SparkKeeper.exe'), 'partial')
            if ($Scenario -eq 'restart-partial') {
                Copy-Item -LiteralPath (Join-Path $env:PROBE_ROOT 'incoming/_internal/new.dll') -Destination (Join-Path $target '_internal/new.dll')
            }
            if ($Scenario -ne 'restart-partial') {
                [void][IO.Directory]::CreateDirectory((Join-Path $target '.installer'))
                foreach ($name in @('.installer/installer_guard.ps1', '.installer/update.ps1', '.installer/maintenance_tasks.ps1', 'unins000.exe', 'unins000.dat', 'unins000.msg')) {
                    [IO.File]::WriteAllText((Join-Path $target $name), 'installer managed')
                }
            }
            if ($Scenario -eq 'restart-first-unknown') { [IO.File]::WriteAllText((Join-Path $target 'notes.txt'), 'unknown') }
            . Import-ProbeGuard
            if ($Scenario -eq 'restart-first-unknown') {
                Expect-Code { Invoke-InstallerPrepare $target $incoming } 20
                [void](Assert-Journal 'mutating')
                Assert-Probe (-not $script:Task.Enabled) 'unknown file repair restored task'
            } else {
                Invoke-InstallerPrepare $target $incoming
                Assert-Probe ((Assert-Journal 'paused').task.originalEnabled) 'retry overwrote initial Enabled'
                Complete-Install
                Assert-NoPending
                Assert-Probe ($script:Task.Enabled) 'reloaded transaction failed to restore original task'
            }
            Assert-DataIntact
            break
        }
        '^definition-(changed|deleted|created)$' {
            if ($Scenario -eq 'definition-created') { $script:Task = $null }
            Invoke-InstallerPrepare $target $incoming
            Invoke-MaintenanceMutating $target
            Copy-Incoming
            if ($Scenario -eq 'definition-deleted') { $script:Task = $null }
            elseif ($Scenario -eq 'definition-created') { $script:Task = New-ProbeTask $false }
            else { $script:Task.Description = 'changed while copying' }
            $writes = if ($null -ne $script:Task) { $script:Task.EnabledWrites } else { 0 }
            Invoke-InstallerFinish $target
            Assert-Archived
            Assert-Probe ($script:DeleteCalls -eq 0) 'finish deleted changed task'
            if ($null -ne $script:Task) { Assert-Probe ($script:Task.EnabledWrites -eq $writes) 'finish restored changed/new task' }
            else { Assert-Probe ($null -eq (Get-MaintenanceTask)) 'finish recreated deleted task' }
            break
        }
        '^uninstall-no-confirm$' {
            Expect-Code { Invoke-UninstallPreflight $target } 10
            Expect-Code { Invoke-UninstallPrepare $target } 10
            Assert-NoPending
            Assert-Probe ($script:Task.EnabledWrites -eq 0 -and $script:DeleteCalls -eq 0) 'unconfirmed uninstall modified task'
            Assert-DataIntact
            break
        }
        '^uninstall-no-confirm-finish$' {
            $PurgeUserData = $true
            Invoke-UninstallPrepare $target
            Remove-Payload
            $PurgeUserData = $false
            Expect-Code { Invoke-UninstallFinish $target } 41
            [void](Assert-Journal 'uninstall-mutating' 'uninstall')
            Assert-Probe ($script:DeleteCalls -eq 0) 'finish ignored missing authorization'
            Assert-DataIntact
            break
        }
        '^uninstall-(success|disabled|none|cancel|partial|payload-remains|delete-failure|delete-readback|changed|disappeared)$' {
            if ($Scenario -eq 'uninstall-disabled') { $script:Task.Definition.Settings.Enabled = $false }
            if ($Scenario -eq 'uninstall-none') { $script:Task = $null }
            Invoke-UninstallPreflight $target
            Assert-NoPending
            if ($null -ne $script:Task) { Assert-Probe ($script:Task.EnabledWrites -eq 0) 'preflight wrote Enabled' }
            Assert-DataIntact
            Invoke-UninstallPrepare $target
            $record = Assert-Journal 'uninstall-mutating' 'uninstall'
            Assert-Probe ($record.purgeAuthorized -and -not $record.purgeStarted) 'prepare authorization/irreversibility flags'
            Assert-DataIntact
            if ($Scenario -eq 'uninstall-cancel') {
                Invoke-MaintenanceAbort $target
                Assert-NoPending
                Assert-Probe ($script:Task.Enabled -and $script:DeleteCalls -eq 0) 'cancel failed to restore without deleting'
                Assert-DataIntact
                break
            }
            if ($Scenario -eq 'uninstall-partial') {
                [IO.File]::Delete((Join-Path $target 'SparkKeeper.exe'))
                Expect-Code { Invoke-MaintenanceAbort $target } 41
                [void](Assert-Journal 'uninstall-mutating' 'uninstall')
                Assert-Probe (-not $script:Task.Enabled -and $script:DeleteCalls -eq 0) 'partial uninstall restored/deleted task'
                Assert-DataIntact
                break
            }
            if ($Scenario -eq 'uninstall-payload-remains') {
                Expect-Code { Invoke-UninstallFinish $target } 41
                [void](Assert-Journal 'uninstall-mutating' 'uninstall')
                Assert-Probe (-not $script:Task.Enabled -and $script:DeleteCalls -eq 0) 'failed file deletion still deleted/restored task'
                Assert-DataIntact
                break
            }
            Remove-Payload
            if ($Scenario -eq 'uninstall-changed') {
                $script:Task.Description = 'changed during uninstall'
                Expect-Code { Invoke-UninstallFinish $target } 41
                Assert-Probe ($script:DeleteCalls -eq 0 -and -not $script:Task.Enabled) 'changed task was blindly deleted/restored'
                Assert-Probe (Test-Path -LiteralPath $expectedJournal) 'changed task lost journal'
                Assert-DataIntact
                break
            }
            if ($Scenario -eq 'uninstall-delete-failure' -or $Scenario -eq 'uninstall-delete-readback') {
                $script:DeleteFails = $Scenario -eq 'uninstall-delete-failure'
                $script:DeleteIgnored = $Scenario -eq 'uninstall-delete-readback'
                Expect-Failure { Invoke-UninstallFinish $target }
                Assert-Probe (Test-Path -LiteralPath $expectedJournal) 'delete failure lost journal'
                Assert-Probe ($script:DeleteCalls -eq 1 -and -not $script:Task.Enabled) 'delete failure restored task'
                Assert-DataIntact
                break
            }
            if ($Scenario -eq 'uninstall-disappeared') { $script:Task = $null }
            Invoke-UninstallFinish $target
            Assert-Purged
            $expectedDeletes = if ($Scenario -in @('uninstall-none', 'uninstall-disappeared')) { 0 } else { 1 }
            Assert-Probe ($script:DeleteCalls -eq $expectedDeletes) 'wrong task delete count'
            break
        }
        '^uninstall-remove-files-(success|locked-retry|unknown)$' {
            Invoke-UninstallPrepare $target
            $record = Assert-Journal 'uninstall-mutating' 'uninstall'
            [void][IO.Directory]::CreateDirectory((Join-Path $target '.installer'))
            $metadataNames = @('.installer/installer_guard.ps1', '.installer/update.ps1', '.installer/maintenance_tasks.ps1', 'unins000.exe', 'unins000.dat', 'unins000.msg')
            foreach ($name in $metadataNames) {
                [IO.File]::WriteAllText((Join-Path $target $name), 'installer metadata')
            }
            if ($Scenario -eq 'uninstall-remove-files-unknown') {
                $unknown = Join-Path $target 'personal.txt'
                [IO.File]::WriteAllText($unknown, 'unknown data')
                Expect-Code { Invoke-UninstallRemoveFiles $target } 20
                Assert-Probe ([IO.File]::ReadAllText($unknown) -ceq 'unknown data') 'removal touched an unknown file'
                Assert-Probe ([IO.File]::ReadAllText((Join-Path $target 'SparkKeeper.exe')) -ceq 'fixture') 'unknown-file rejection partially removed payload'
                [void](Assert-Journal 'uninstall-mutating' 'uninstall')
                Assert-Probe (-not $script:Task.Enabled -and $script:DeleteCalls -eq 0) 'unknown-file failure changed task'
                Assert-DataIntact
                break
            }
            if ($Scenario -eq 'uninstall-remove-files-locked-retry') {
                # Seed a completed deletion prefix, then fail on a genuinely
                # locked remaining file; recovery must accept this partial tree.
                [IO.File]::Delete((Join-Path $target 'SparkKeeper.exe'))
                $handle = [IO.File]::Open((Join-Path $target '_internal/runtime.dll'), [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
                try { Expect-Failure { Invoke-UninstallRemoveFiles $target } }
                finally { $handle.Dispose() }
                [void](Assert-Journal 'uninstall-mutating' 'uninstall')
                Assert-Probe (-not $script:Task.Enabled -and $script:DeleteCalls -eq 0) 'partial removal failure restored/deleted task'
                Assert-DataIntact
                . Import-ProbeGuard
                Invoke-UninstallPrepare $target
                $record = Assert-Journal 'uninstall-mutating' 'uninstall'
                Assert-Probe ($record.task.originalEnabled -and $record.purgeAuthorized -and -not $record.purgeStarted) 'retry lost initial snapshot/authorization'
            }
            Invoke-UninstallRemoveFiles $target
            foreach ($entry in $record.oldManifest.files.PSObject.Properties) {
                Assert-Probe (-not (Test-Path -LiteralPath (Join-Path $target $entry.Name))) 'journal payload member remains'
            }
            Assert-Probe (Test-Path -LiteralPath (Join-Path $target 'release-manifest.json')) 'helper must retain release manifest until native uninstall'
            foreach ($name in $metadataNames) {
                Assert-Probe ([IO.File]::ReadAllText((Join-Path $target $name)) -ceq 'installer metadata') 'helper deleted installer metadata prematurely'
            }
            Assert-DataIntact
            Invoke-UninstallFinish $target
            Assert-Purged
            Assert-Probe ($script:DeleteCalls -eq 1) 'successful removal must delete owned task exactly once'
            foreach ($name in $metadataNames) {
                Assert-Probe ([IO.File]::ReadAllText((Join-Path $target $name)) -ceq 'installer metadata') 'purge deleted installer metadata'
            }
            Expect-Code { Invoke-UninstallCleanup $target } 41
            Assert-Probe (Test-Path -LiteralPath (Join-Path $target '.installer/uninstall-completed.json')) 'premature native cleanup lost receipt'
            foreach ($name in @($metadataNames) + @('release-manifest.json')) {
                [IO.File]::Delete((Join-Path $target $name))
            }
            Invoke-UninstallCleanup $target
            Assert-Probe (-not (Test-Path -LiteralPath $target)) 'completed native cleanup left receipt or empty install directories'
            Assert-Purged
            break
        }
        '^purge-foreign-(named-enabled|named-disabled|shared-exe|shared-module|shared-data|shared-working-directory)$' {
            $foreign = New-ProbeTask $false
            $action = $foreign.Definition.Actions.Item(1)
            if ($Scenario -like 'purge-foreign-named-*') {
                $action.Path = Join-Path $env:PROBE_ROOT 'other\SparkKeeper.exe'
                $foreign.Definition.Settings.Enabled = $Scenario.EndsWith('-enabled')
                $script:Task = $foreign
            } else {
                $foreign.Path = '\UnrelatedFolder\OtherJob'
                $action.Path = Join-Path $env:PROBE_ROOT 'other\runner.exe'
                $action.Arguments = ''
                $action.WorkingDirectory = $env:PROBE_ROOT
                switch ($Scenario) {
                    'purge-foreign-shared-exe' { $action.Path = Join-Path $env:PROBE_ROOT 'other\SparkKeeper.exe' }
                    'purge-foreign-shared-module' { $action.Arguments = '-m spark_keeper.worker --scheduled' }
                    'purge-foreign-shared-data' { $action.Arguments = '--user-data-dir="' + $expectedData + '"' }
                    'purge-foreign-shared-working-directory' { $action.WorkingDirectory = $expectedData }
                }
                $script:OtherTasks = @($foreign)
            }
            Expect-Code { Invoke-UninstallPreflight $target } 30
            Expect-Code { Invoke-UninstallPrepare $target } 30
            Assert-NoPending
            Assert-Probe ($foreign.EnabledWrites -eq 0 -and $script:Task.EnabledWrites -eq 0 -and $script:DeleteCalls -eq 0) 'foreign reference modified a task'
            Assert-DataIntact
            break
        }
        '^purge-reference-appears$' {
            Invoke-UninstallPrepare $target
            Remove-Payload
            $foreign = New-ProbeTask $false
            $foreign.Path = '\OtherJob'
            $script:OtherTasks = @($foreign)
            Expect-Code { Invoke-UninstallFinish $target } 30
            [void](Assert-Journal 'uninstall-mutating' 'uninstall')
            Assert-Probe ($script:DeleteCalls -eq 0 -and $foreign.EnabledWrites -eq 0) 'late shared reference ignored'
            Assert-DataIntact
            break
        }
        '^purge-junction-(root|child)$' {
            $external = Join-Path $env:PROBE_ROOT 'junction-destination'
            [void][IO.Directory]::CreateDirectory($external)
            $externalSentinel = Join-Path $external 'outside.db'
            [IO.File]::WriteAllText($externalSentinel, 'outside data')
            if ($Scenario -eq 'purge-junction-root') {
                [IO.File]::Delete($sentinel)
                [IO.Directory]::Delete($expectedData)
                $junction = $expectedData
            } else { $junction = Join-Path $expectedData 'redirect' }
            try { [void](New-Item -ItemType Junction -Path $junction -Target $external) }
            catch { [Console]::WriteLine('SKIP: Cannot create isolated junction: ' + $_.Exception.Message); exit 77 }
            Expect-Code { Invoke-UninstallPrepare $target } 10
            Assert-Probe ([IO.File]::ReadAllText($externalSentinel) -ceq 'outside data') 'purge followed junction'
            Assert-Probe ($script:Task.EnabledWrites -eq 0 -and $script:DeleteCalls -eq 0) 'unsafe purge paused/deleted task'
            Assert-NoPending
            break
        }
        '^purge-receipt-(repair|foreign-identity|foreign-target)$' {
            Invoke-UninstallPrepare $target
            Invoke-UninstallRemoveFiles $target
            Invoke-UninstallFinish $target
            Assert-Purged
            $receiptPath = Join-Path $target '.installer/uninstall-completed.json'
            Assert-Probe (Test-Path -LiteralPath $receiptPath) 'completed purge lacks crash-tail receipt'
            $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
            Assert-Probe ($receipt.appIdentity -ceq $identity -and $receipt.target -ceq $target) 'receipt is not bound to exact identity/target'
            # Native Inno metadata deletion has not run. A later application may
            # already have created new data: the old authorization cannot erase it.
            [void][IO.Directory]::CreateDirectory($expectedData)
            [IO.File]::WriteAllText($sentinel, 'new data after completed purge')
            if ($Scenario -eq 'purge-receipt-foreign-identity') {
                $receipt.appIdentity = 'SparkKeeper.Test.' + [Guid]::NewGuid().ToString('N')
            } elseif ($Scenario -eq 'purge-receipt-foreign-target') {
                $receipt.target = Join-Path $env:PROBE_ROOT 'other-install'
            }
            if ($Scenario -ne 'purge-receipt-repair') {
                $receipt | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
            }
            $script:ConfirmPurge = $false
            . Import-ProbeGuard
            if ($Scenario -eq 'purge-receipt-repair') {
                Invoke-InstallerPrepare $target $incoming
                Assert-Probe ($null -eq (Assert-Journal 'paused').task) 'receipt resurrected old task snapshot'
                Complete-Install
                Assert-NoPending
                [void](Read-ReleaseManifest $target)
            } else {
                Expect-Code { Invoke-InstallerPrepare $target $incoming } 41
                Assert-NoPending
                Assert-Probe (-not (Test-Path -LiteralPath (Join-Path $target 'SparkKeeper.exe'))) 'foreign receipt changed payload'
            }
            Assert-Probe ($null -eq $script:Task -and $script:DeleteCalls -eq 1) 'receipt recovery recreated/deleted task'
            Assert-Probe ([IO.File]::ReadAllText($sentinel) -ceq 'new data after completed purge') 'completed purge authorization erased later data'
            Assert-Probe ([IO.File]::ReadAllText($neighbor) -ceq 'neighbor data') 'receipt recovery escaped data root'
            break
        }
        '^purge-failure-repair-(same|newer)$' {
            Invoke-UninstallPrepare $target
            Remove-Payload
            # Lock only after the real purge enumeration/open checks return. This
            # deterministically injects failure after purgeStarted is durable.
            $script:RealPurgeEntries = ${function:Get-MaintenancePurgeEntries}
            function Get-MaintenancePurgeEntries {
                $entries = & $script:RealPurgeEntries
                if ($script:FailPurge -and $null -eq $script:Task -and $null -eq $script:PurgeHandle) {
                    $script:PurgeHandle = [IO.File]::Open($sentinel, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
                }
                return ,$entries
            }
            $script:FailPurge = $true
            Expect-Code { Invoke-UninstallFinish $target } 41
            $record = Assert-Journal 'uninstall-purging' 'uninstall'
            Assert-Probe ($record.purgeAuthorized -and $record.purgeStarted) 'failed purge lost durable authorization/started flags'
            Assert-Probe ($null -eq $script:Task -and $script:DeleteCalls -eq 1) 'purge failure restored/recreated task'
            $script:PurgeHandle.Dispose()
            $script:PurgeHandle = $null
            $script:FailPurge = $false
            Expect-Code { Invoke-MaintenanceAbort $target } 41
            Assert-Probe (Test-Path -LiteralPath $expectedJournal) 'abort cleared failed-purge gate'
            # Put old payload back too: purgeStarted alone must prohibit rollback.
            Copy-Item -Path (Join-Path $env:PROBE_ROOT 'old-backup\*') -Destination $target -Recurse -Force
            Expect-Code { Invoke-MaintenanceAbort $target } 41
            [void](Assert-Journal 'uninstall-purging' 'uninstall')
            if ($Scenario.EndsWith('-newer')) {
                $manifest = Get-Content -LiteralPath $incoming -Raw | ConvertFrom-Json
                $manifest.version = '3.0.4'
                $manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $incoming -Encoding UTF8
            }
            $script:ConfirmPurge = $false
            . Import-ProbeGuard
            Invoke-InstallerPrepare $target $incoming
            $record = Assert-Journal 'paused'
            Assert-Probe ($record.purgeAuthorized -and $record.purgeStarted) 'Setup repair discarded unfinished purge'
            Invoke-MaintenanceMutating $target
            Copy-Incoming
            [IO.File]::WriteAllText((Join-Path $target 'SparkKeeper.exe'), 'partial repair')
            Expect-Code { Invoke-InstallerFinish $target } 20
            Assert-Probe (Test-Path -LiteralPath $expectedJournal) 'unverified repair cleared purge journal'
            Assert-Probe (Test-Path -LiteralPath $sentinel) 'purge ran before repaired payload verification'
            Copy-Incoming
            Invoke-InstallerFinish $target
            Assert-Purged
            Assert-Probe ($script:DeleteCalls -eq 1) 'repair deleted/recreated original task'
            [void](Read-ReleaseManifest $target)
            break
        }
        default { throw "Unknown maintenance scenario: $Scenario" }
    }
    [Console]::WriteLine("PASS: $Scenario")
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString() + [Environment]::NewLine + $_.ScriptStackTrace)
    exit 1
} finally {
    if ($null -ne $script:PurgeHandle) { $script:PurgeHandle.Dispose() }
    # Remove the link itself before recursive cleanup, never its destination.
    if ($null -ne $junction -and [IO.Directory]::Exists($junction)) { [IO.Directory]::Delete($junction) }
    if ($createdPrivateRoot -and (Test-Path -LiteralPath $privateRoot)) { Remove-Item -LiteralPath $privateRoot -Recurse -Force }
}
