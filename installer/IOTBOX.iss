#define AppName "IOTBOX"
#define AppVersion "2026.08.11"
#define AppPublisher "IOTBOX"
#define AppExeName "gui_app.exe"

[Setup]
AppId={{B7C9C4E7-7E1D-4F47-9A0F-IOTBOX2026}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\IOTBOX
DefaultGroupName=IOTBOX
OutputDir=..\release
OutputBaseFilename=IOTBOX-SETUP
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\gui_app.exe
SetupIconFile=..\assets\iotbox-icon.ico

[Files]
Source: "..\dist\gui_app\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\dist\run_http\*"; DestDir: "{app}\runtime"; Flags: recursesubdirs ignoreversion
Source: "..\assets\iotbox-icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\runtime_config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist ignoreversion

[Icons]
Name: "{group}\IOTBOX"; Filename: "{app}\gui_app.exe"; IconFilename: "{app}\iotbox-icon.ico"
Name: "{userdesktop}\IOTBOX"; Filename: "{app}\gui_app.exe"; IconFilename: "{app}\iotbox-icon.ico"

[Run]
Filename: "{app}\gui_app.exe"; Description: "启动 IOTBOX"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM gui_app.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM run_http.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(800);
  Result := '';
end;
