; InkCast installer — Inno Setup 6
; Produces installer\InkCastSetup.exe.
;
; Per-user install into %LOCALAPPDATA%\Programs\InkCast:
;   - no admin / UAC prompt
;   - camera\ stays writable (it can download scrcpy and save its settings)
;
; Tasks offered during setup:
;   [x] Start Menu shortcut            (default on)
;   [ ] Start InkCast at Windows logon (default off)
;   [ ] Desktop shortcut               (default off)

#define AppName      "InkCast"
#define AppVersion   "2.2"
#define AppPublisher "InkCast"
#define AppExeName   "InkCast.exe"
#define SourceDir    "dist"

[Setup]
AppId={{8B9F5E21-3E2A-4B7C-9D6A-1F2E3C4A5B01}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
; %LOCALAPPDATA%\Programs\InkCast — per-user, writable, no admin
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
; Where the built setup.exe lands
OutputDir=dist
OutputBaseFilename=InkCastSetup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=inkcast.ico
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "Shortcuts:"
Name: "desktopicon";  Description: "Create a &desktop shortcut";    GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "startupicon";  Description: "Start {#AppName} at Windows &logon"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The single-file exe + the camera helper (for "Phone camera (USB)" in the tray menu)
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "camera\InkCast Camera.bat"; DestDir: "{app}\camera"; Flags: ignoreversion
Source: "camera\get-scrcpy.ps1"; DestDir: "{app}\camera"; Flags: ignoreversion

[Icons]
; Optional shortcuts
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExeName}"; Tasks: startmenuicon
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
; Optional extras
Name: "{userdesktop}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}";     Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up the settings file the camera helper writes next to the app
Type: files; Name: "{app}\camera\camera-settings.bat"
