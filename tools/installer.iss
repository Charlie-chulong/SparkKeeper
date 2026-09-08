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
WelcomeLabel2=安装前请在旧程序中停用每日计划，等待发送或浏览器任务自然结束，再正常退出所有版本。安装器不会强杀任务、修改计划或删除 AppData 用户数据。%n%n首次从便携版迁入将保留原便携目录；安装后请核对登录、好友及计划，并在新版人工重新保存/启用计划。不要再运行旧目录。
FinishedLabel=SparkKeeper 安装完成。请核对登录、好友及计划。首次从便携版迁入或安装路径改变后，请在新版人工重新保存/启用计划。用户数据保留在 AppData\Local\SparkKeeper。

[Tasks]
Name: "startmenu"; Description: "创建开始菜单快捷方式"; Flags: unchecked
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\release-manifest.json"; Flags: dontcopy
Source: "installer_guard.ps1"; Flags: dontcopy
Source: "update.ps1"; Flags: dontcopy
Source: "installer_guard.ps1"; DestDir: "{app}\.installer"; Flags: ignoreversion
Source: "update.ps1"; DestDir: "{app}\.installer"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\SparkKeeper"; Filename: "{app}\SparkKeeper.exe"; WorkingDir: "{app}"; Tasks: startmenu; Check: not IsTestBuild
Name: "{userdesktop}\SparkKeeper"; Filename: "{app}\SparkKeeper.exe"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not IsTestBuild

[Run]
Filename: "{app}\SparkKeeper.exe"; WorkingDir: "{app}"; Description: "启动 SparkKeeper 核对数据（不会自动启用计划）"; Flags: nowait postinstall skipifsilent unchecked; Check: CanLaunch

[Code]
var
  GuiMutex, AutomationMutex: LongWord;
  InstallVerified: Boolean;
  GuardDirectory: String;

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
  Result := InstallVerified and not IsTestBuild;
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
  Params := '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    GuardDirectory + '\installer_guard.ps1" -Mode ' + Mode +
    ' -TargetPath "' + ExpandConstant('{app}') + '" -ManifestPath "' +
    ExpandConstant('{tmp}\release-manifest.json') + '" -StatePath "' +
    ExpandConstant('{tmp}\previous-manifest.json') + '" -ResultPath "' + ResultFile +
    '" -ParentProcessId ' + IntToStr(GetCurrentProcessId) +
    ' -AppIdentity "' + ExpandConstant('{#SetupAppId}') + '"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params,
      '', SW_HIDE, ewWaitUntilTerminated, Code) then begin
    Result := '无法运行 Windows PowerShell 安全检查。未授权关闭任何程序。';
    exit;
  end;
  if Code <> 0 then begin
    if LoadStringFromFile(ResultFile, Detail) then Result := UTF8Decode(Detail)
    else Result := '安装安全检查失败，错误码 ' + IntToStr(Code) + '；请查看安装日志。';
    Log(Result);
  end;
end;

function AcquireLocks: String;
var
  Identity: AnsiString;
begin
  ReleaseLocks;
  Result := RunGuard('Identity');
  if Result <> '' then exit;
  if not LoadStringFromFile(ExpandConstant('{tmp}\guard-result.txt'), Identity) then begin
    Result := '无法读取当前用户/会话身份，安全中止。';
    exit;
  end;
  if not LockOne(UTF8Decode(Identity), GuiMutex) then begin
    Result := 'SparkKeeper 界面仍在运行或另一个安装器正在操作。请先停用计划，等待任务结束并正常退出所有版本。';
    exit;
  end;
  if not LockOne('Local\SparkKeeperLocalAutomation', AutomationMutex) then begin
    ReleaseLocks;
    Result := '自动化任务仍在运行，请等待其自然结束。安装器不会强杀发送或浏览器任务。';
  end;
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
  ExtractTemporaryFile('release-manifest.json');
  GuardDirectory := ExpandConstant('{tmp}');
  Result := AcquireLocks;
  if Result = '' then Result := RunGuard('Prepare');
  if Result <> '' then ReleaseLocks;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Failure: String;
begin
  if CurStep = ssPostInstall then begin
    Failure := RunGuard('Finish');
    if Failure <> '' then
      RaiseException(Failure + #13#10 + '程序文件可能已部分更新；请勿启动程序。保留安装日志后重新运行安装器或人工处理。用户数据未触碰，不承诺全事务回滚。');
    InstallVerified := True;
    ReleaseLocks;
  end;
end;

procedure DeinitializeSetup;
begin
  ReleaseLocks;
end;

function InitializeUninstall: Boolean;
var
  Failure: String;
begin
  GuardDirectory := ExpandConstant('{tmp}');
  Result := FileCopy(ExpandConstant('{app}\.installer\installer_guard.ps1'), GuardDirectory + '\installer_guard.ps1', False) and
    FileCopy(ExpandConstant('{app}\.installer\update.ps1'), GuardDirectory + '\update.ps1', False);
  if not Result then begin
    MsgBox('卸载安全检查文件缺失，未卸载。请重新安装同版本以修复。AppData 数据不会删除。', mbError, MB_OK);
    exit;
  end;
  Failure := AcquireLocks;
  if Failure = '' then Failure := RunGuard('Uninstall');
  Result := Failure = '';
  if not Result then begin
    ReleaseLocks;
    MsgBox(Failure, mbError, MB_OK);
  end;
end;

procedure DeinitializeUninstall;
begin
  ReleaseLocks;
end;
