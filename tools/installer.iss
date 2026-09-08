; Requires Inno Setup 6.5 or newer (Unicode), Windows PowerShell 5.1.
#ifndef AppVersion
  #error AppVersion must come from tools/build_installer.py
#endif
#ifndef PayloadDir
  #error PayloadDir must point to the validated SparkKeeper payload
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "SparkKeeper-" + AppVersion + "-Setup"
#endif
#if Pos("rc", AppVersion) > 0
  #define AppVersionCore Copy(AppVersion, 1, Pos("rc", AppVersion) - 1)
#else
  #define AppVersionCore AppVersion
#endif
#ifdef TestAppId
  #define SetupAppId "SparkKeeper.Test." + TestAppId
#else
  #define SetupAppId "{{B6A73841-9DB7-42EF-9835-A62D764D537B}"
#endif

[Setup]
AppId={#SetupAppId}
AppName=SparkKeeper
AppVersion={#AppVersion}
AppPublisher=Charlie-chulong
AppPublisherURL=https://github.com/Charlie-chulong/SparkKeeper
AppSupportURL=https://github.com/Charlie-chulong/SparkKeeper/issues
DefaultDirName={localappdata}\Programs\SparkKeeper
DefaultGroupName=SparkKeeper
DisableWelcomePage=no
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SparkKeeper.exe
UninstallDisplayName=SparkKeeper
CloseApplications=no
RestartApplications=no
AllowCancelDuringInstall=no
SetupLogging=yes
VersionInfoProductName=SparkKeeper
VersionInfoVersion={#AppVersionCore}
VersionInfoProductVersion={#AppVersionCore}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription=SparkKeeper Setup
#ifdef TestAppId
UsePreviousAppDir=no
UsePreviousTasks=no
#else
UsePreviousAppDir=yes
#endif

[Languages]
Name: "chinesesimplified"; MessagesFile: "installer\ChineseSimplified.isl"

[Messages]
WelcomeLabel1=欢迎使用 SparkKeeper 安装向导
WelcomeLabel2=安装器会暂时暂停属于本目录的 Windows 每日计划，完整校验成功后恢复原启用状态；原停用或无任务保持不变。请等待已有任务自然结束并退出所有版本，安装器不会强杀。中断或部分更新会保持暂停，请重跑同目录安装包修复。%n%n首次便携迁入如计划仍指向旧目录，请先人工停用，安装后在新版重新保存绑定。升级保留用户数据；卸载将经确认永久删除本用户共享的全部 SparkKeeper 数据。
FinishedLabel=SparkKeeper 安装完成。属于本目录且定义未变化的每日计划已恢复原状态。首次便携迁入请在新版核对登录、好友及计划并重新保存绑定。升级保留 AppData\Local\SparkKeeper 数据；卸载将经确认永久清空全部共享数据。

[Tasks]
Name: "startmenu"; Description: "创建开始菜单快捷方式"; Flags: unchecked
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\release-manifest.json"; Flags: dontcopy
Source: "installer_guard.ps1"; Flags: dontcopy
Source: "update.ps1"; Flags: dontcopy
Source: "maintenance_tasks.ps1"; Flags: dontcopy
Source: "installer_guard.ps1"; DestDir: "{app}\.installer"; Flags: ignoreversion
Source: "update.ps1"; DestDir: "{app}\.installer"; Flags: ignoreversion
Source: "maintenance_tasks.ps1"; DestDir: "{app}\.installer"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\SparkKeeper"; Filename: "{app}\SparkKeeper.exe"; WorkingDir: "{app}"; Tasks: startmenu; Check: not IsTestBuild
Name: "{userprograms}\卸载 SparkKeeper"; Filename: "{uninstallexe}"; Tasks: startmenu; Check: not IsTestBuild
Name: "{userdesktop}\SparkKeeper"; Filename: "{app}\SparkKeeper.exe"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not IsTestBuild

[Run]
Filename: "{app}\SparkKeeper.exe"; WorkingDir: "{app}"; Description: "启动 SparkKeeper 核对数据（不会自动启用计划）"; Flags: nowait postinstall skipifsilent unchecked; Check: CanLaunch

[Code]
var
  GuiMutex, AutomationMutex, MaintenanceMutex: LongWord;
  InstallVerified, InstallTransaction, UninstallVerified, UninstallTransaction: Boolean;
  PurgeConfirmed, SuppressLaunch, InstallFailed: Boolean;
  GuardDirectory, GuardNotice, InstallFailure: String;

function CreateMutex(Security: Integer; InitialOwner: Boolean; Name: String): LongWord;
  external 'CreateMutexW@kernel32.dll stdcall';
function WaitForSingleObject(Handle, Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function ReleaseMutex(Handle: LongWord): Boolean;
  external 'ReleaseMutex@kernel32.dll stdcall';
function CloseHandle(Handle: LongWord): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function GetCurrentProcessId: LongWord;
  external 'GetCurrentProcessId@kernel32.dll stdcall';

function IsTestBuild: Boolean;
begin
#ifdef TestAppId
  Result := True;
#else
  Result := False;
#endif
end;

function CanLaunch: Boolean;
begin
  Result := InstallVerified and not InstallFailed and not SuppressLaunch and not IsTestBuild;
end;

procedure ReleaseLocks;
begin
  if GuiMutex <> 0 then begin
    ReleaseMutex(GuiMutex);
    CloseHandle(GuiMutex);
    GuiMutex := 0;
  end;
  if AutomationMutex <> 0 then begin
    ReleaseMutex(AutomationMutex);
    CloseHandle(AutomationMutex);
    AutomationMutex := 0;
  end;
  if MaintenanceMutex <> 0 then begin
    ReleaseMutex(MaintenanceMutex);
    CloseHandle(MaintenanceMutex);
    MaintenanceMutex := 0;
  end;
  if GuardNotice <> '' then begin
    SuppressibleMsgBox(GuardNotice, mbInformation, MB_OK, IDOK);
    GuardNotice := '';
  end;
end;

function LockOne(Name: String; var Handle: LongWord): Boolean;
var
  Outcome: LongWord;
begin
  Handle := CreateMutex(0, False, Name);
  Result := False;
  if Handle = 0 then exit;
  Outcome := WaitForSingleObject(Handle, 0);
  Result := (Outcome = 0) or (Outcome = $80);
  if not Result then begin
    CloseHandle(Handle);
    Handle := 0;
  end;
end;

function RunGuard(Mode: String): String;
var
  Code: Integer;
  Params, ResultFile: String;
  Detail: AnsiString;
begin
  Result := '';
  ResultFile := ExpandConstant('{tmp}\guard-result.txt');
  DeleteFile(ResultFile);
  DeleteFile(ResultFile + '.no-launch');
  Params := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    GuardDirectory + '\installer_guard.ps1" -Mode ' + Mode +
    ' -TargetPath "' + ExpandConstant('{app}') + '" -ManifestPath "' +
    ExpandConstant('{tmp}\release-manifest.json') + '" -ResultPath "' + ResultFile +
    '" -ParentProcessId ' + IntToStr(GetCurrentProcessId) +
    ' -AppIdentity "' + ExpandConstant('{#SetupAppId}') + '"';
  if PurgeConfirmed then Params := Params + ' -PurgeUserData';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
      '', SW_HIDE, ewWaitUntilTerminated, Code) then begin
    Result := '无法运行 Windows PowerShell 安全检查。未授权关闭任何程序。';
    exit;
  end;
  if Code <> 0 then begin
    if LoadStringFromFile(ResultFile, Detail) then Result := UTF8Decode(Detail)
    else Result := '安装安全检查失败，错误码 ' + IntToStr(Code) + '；请查看安装日志。';
    Log(Result);
  end else begin
    if FileExists(ResultFile + '.no-launch') then SuppressLaunch := True;
    if (Mode <> 'Identity') and (Mode <> 'MaintenanceIdentity') and
        LoadStringFromFile(ResultFile, Detail) then begin
      Log(UTF8Decode(Detail));
      GuardNotice := UTF8Decode(Detail);
    end;
  end;
end;

function AcquireLocks: String;
var
  Identity: AnsiString;
begin
  ReleaseLocks;
  { Only read-only identity discovery runs before the same-SID global gate. }
  Result := RunGuard('MaintenanceIdentity');
  if Result <> '' then exit;
  if not LoadStringFromFile(ExpandConstant('{tmp}\guard-result.txt'), Identity) then begin
    Result := '无法读取当前用户维护锁身份，安全中止。';
    exit;
  end;
  if not LockOne(UTF8Decode(Identity), MaintenanceMutex) then begin
    Result := 'SparkKeeper 任务或另一个安装/卸载正在维护，请等待自然结束后重试；不会强杀。';
    exit;
  end;
  Result := RunGuard('Identity');
  if Result = '' then begin
    if not LoadStringFromFile(ExpandConstant('{tmp}\guard-result.txt'), Identity) then
      Result := '无法读取当前用户/会话身份，安全中止。'
    else if not LockOne(UTF8Decode(Identity), GuiMutex) then
      Result := 'SparkKeeper 界面仍在运行，请等待任务结束并正常退出所有版本。';
  end;
#ifdef TestAppId
  if (Result = '') and not LockOne('Local\SparkKeeperLocalAutomation.{#SetupAppId}', AutomationMutex) then
#else
  if (Result = '') and not LockOne('Local\SparkKeeperLocalAutomation', AutomationMutex) then
#endif
    Result := '自动化任务仍在运行，请等待其自然结束。安装器不会强杀发送或浏览器任务。';
  if Result <> '' then ReleaseLocks;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  TestTarget, TestPrefix: String;
begin
  Result := '';
  if IsTestBuild then begin
    TestTarget := ExpandConstant('{param:DIR|}');
    TestPrefix := AddBackslash(GetEnv('TEMP')) + 'SparkKeeper-InstallerTest-';
    if (TestTarget = '') or (CompareText(Copy(TestTarget, 1, Length(TestPrefix)), TestPrefix) <> 0) then begin
      Result := '测试安装器必须显式 /DIR="%TEMP%\SparkKeeper-InstallerTest-唯一标识"，不会使用真实安装目录。';
      exit;
    end;
    if CompareText(RemoveBackslashUnlessRoot(ExpandConstant('{app}')), RemoveBackslashUnlessRoot(TestTarget)) <> 0 then begin
      Result := '测试目标与 /DIR 不一致。';
      exit;
    end;
  end;
  ExtractTemporaryFile('installer_guard.ps1');
  ExtractTemporaryFile('update.ps1');
  ExtractTemporaryFile('maintenance_tasks.ps1');
  ExtractTemporaryFile('release-manifest.json');
  GuardDirectory := ExpandConstant('{tmp}');
  { A repeated preparing callback reuses this thread's existing ownership. }
  if not InstallTransaction then begin
    Result := AcquireLocks;
    if Result = '' then InstallTransaction := True;
  end;
  if Result = '' then Result := RunGuard('Prepare');
  { CurStepChanged exceptions are not a reliable veto. Persist mutation intent
    here: a nonempty PrepareToInstall result really prevents file copying. }
  if Result = '' then Result := RunGuard('Mutating');
  if Result <> '' then begin
    if InstallTransaction then begin
      TestTarget := RunGuard('Abort');
      if TestTarget <> '' then Result := Result + #13#10 + TestTarget;
      InstallTransaction := False;
    end;
    ReleaseLocks;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Failure: String;
begin
  if CurStep = ssPostInstall then begin
    { Fail closed even if an unexpected script exception interrupts this call.
      Native Inno may swallow a post-install exception and otherwise exit zero. }
    InstallFailed := True;
    InstallFailure := '安装后校验未完成；请勿启动程序。请重跑同目录安装包修复。';
    Failure := RunGuard('Finish');
    if Failure <> '' then begin
      InstallFailure := Failure;
      Log('安装未完成，返回自定义失败码 41：' + InstallFailure);
      exit;
    end;
    InstallVerified := True;
    InstallFailed := False;
    InstallFailure := '';
    InstallTransaction := False;
    ReleaseLocks;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then exit;
  if InstallFailed then begin
    WizardForm.FinishedHeadingLabel.Caption := 'SparkKeeper 安装未完成';
    WizardForm.FinishedLabel.Caption := '安装后完整校验或维护恢复失败，程序文件可能已部分更新。请勿启动程序；维护记录已保留，计划不会被无条件恢复。' + #13#10 +
      '请保留安装日志并重新运行同目录安装包修复。不承诺全事务回滚。安装器将返回非零失败码 41。';
    WizardForm.RunList.Visible := False;
  end else if SuppressLaunch then begin
    WizardForm.FinishedHeadingLabel.Caption := 'SparkKeeper 程序文件已修复';
    WizardForm.FinishedLabel.Caption := '程序文件已修复，并完成此前卸载已授权的全部用户数据清理。旧计划未重建，不会启动应用。如需删除修复后的程序，请再次运行卸载。';
    WizardForm.RunList.Visible := False;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  if InstallFailed then Result := 41
  else Result := 0;
end;

procedure DeinitializeSetup;
var
  Failure: String;
begin
  if InstallTransaction and not InstallVerified then begin
    Failure := RunGuard('Abort');
    if Failure <> '' then SuppressibleMsgBox(Failure, mbError, MB_OK, IDOK);
  end;
  ReleaseLocks;
end;

function InitializeUninstall: Boolean;
var
  Failure: String;
  Index: Integer;
begin
  PurgeConfirmed := False;
  for Index := 1 to ParamCount do
    if CompareText(ParamStr(Index), '/PURGEUSERDATA') = 0 then PurgeConfirmed := True;
  if not PurgeConfirmed then begin
    if UninstallSilent then begin
      Log('静默卸载必须显式传入 /PURGEUSERDATA，同意永久删除全部共享用户数据。');
      Result := False;
      exit;
    end;
    PurgeConfirmed := MsgBox('卸载将永久删除当前 Windows 用户 AppData\Local\SparkKeeper 中全部数据，包括登录态、数据库、好友、计划、发送历史、防重记录、日志、输出及备份。' + #13#10 +
      '同一用户的其他便携副本也共享这些数据，删除后无法恢复。升级不会删除数据。' + #13#10 +
      '是否确认卸载并清空全部共享数据？', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  end;
  Result := PurgeConfirmed;
  if not Result then exit;
  GuardDirectory := ExpandConstant('{tmp}');
  Result := FileCopy(ExpandConstant('{app}\.installer\installer_guard.ps1'), GuardDirectory + '\installer_guard.ps1', False) and
    FileCopy(ExpandConstant('{app}\.installer\update.ps1'), GuardDirectory + '\update.ps1', False) and
    FileCopy(ExpandConstant('{app}\.installer\maintenance_tasks.ps1'), GuardDirectory + '\maintenance_tasks.ps1', False);
  if not Result then begin
    Failure := '卸载安全检查文件缺失，未卸载或清理数据。请重跑同目录安装包修复后再次卸载。';
    Log(Failure);
    SuppressibleMsgBox(Failure, mbError, MB_OK, IDOK);
    exit;
  end;
  Failure := AcquireLocks;
  if Failure = '' then Failure := RunGuard('Uninstall');
  Result := Failure = '';
  if not Result then begin
    ReleaseLocks;
    SuppressibleMsgBox(Failure, mbError, MB_OK, IDOK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Failure: String;
begin
  if CurUninstallStep = usUninstall then begin
    { usPostUninstall exceptions are non-fatal in Inno. Complete every fallible
      payload/task/data operation here, while the original uninstaller remains. }
    UninstallTransaction := True;
    Failure := RunGuard('UninstallPrepare');
    if Failure = '' then Failure := RunGuard('UninstallRemoveFiles');
    if Failure = '' then Failure := RunGuard('UninstallFinish');
    if Failure <> '' then RaiseException(Failure);
    UninstallVerified := True;
    UninstallTransaction := False;
  end;
  if CurUninstallStep = usPostUninstall then begin
    Failure := RunGuard('UninstallCleanup');
    ReleaseLocks;
    if Failure <> '' then SuppressibleMsgBox(Failure, mbError, MB_OK, IDOK);
  end;
end;

procedure DeinitializeUninstall;
var
  Failure: String;
begin
  if UninstallTransaction and not UninstallVerified then begin
    Failure := RunGuard('UninstallAbort');
    if Failure <> '' then SuppressibleMsgBox(Failure, mbError, MB_OK, IDOK);
  end;
  ReleaseLocks;
end;
