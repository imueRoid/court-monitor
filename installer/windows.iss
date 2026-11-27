#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "CourtMonitorPC.exe"

[Setup]
AppId={{D9DF187D-0894-4A47-B9AC-697D8058A601}
AppName=사건 일정 관리 PC
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}.0
AppPublisher=Court Monitor
AppPublisherURL=https://github.com/imueRoid/court-monitor
DefaultDirName={localappdata}\Programs\CourtMonitorPC
DefaultGroupName=사건 일정 관리 PC
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=CourtMonitorPC-Setup
SetupIconFile=..\build\desktop-assets\court-monitor.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
AppMutex=CourtMonitorDesktop
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; Flags: unchecked

[Files]
Source: "..\dist\CourtMonitorPC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\사건 일정 관리 PC"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\사건 일정 관리 PC"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "CourtMonitorDesktop"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "사건 일정 관리 PC 실행"; Flags: nowait postinstall skipifsilent
