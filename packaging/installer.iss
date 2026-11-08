; Compressor's Windows installer (Inno Setup 6). Built by CI from the PyInstaller folder:
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\dist\Compressor /DIconFile=..\dist\Compressor.ico packaging\installer.iss
; Installs for everyone (recommended: Program Files is protected from other programs), or only for the current
; user without administrator rights. Optional, all off except the update check: a Start menu entry, a desktop shortcut, the Explorer context menu, starting at
; sign-in, and (Windows 11 24H2+) Microsoft's JPEG XL Image Extension from the Store. Uninstalls cleanly: the
; context menu and start-at-sign-in are removed, and the user is asked whether to keep settings and History.

#define AppName "Compressor"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Compressor"
#endif

[Setup]
AppId={{8A6F2C1E-5B7D-4C2A-9E3F-6D1B2C3A4F50}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Lanntern
AppCopyright=Copyright (C) 2026 Lanntern. GNU GPL version 3.
AppPublisherURL=https://github.com/Lanntern/Compressor
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
; For everyone (recommended): Program Files can only be changed by an administrator, so other programs can't
; replace Compressor or the encoders it runs. "Only for me" stays available on the same page (no admin needed).
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Compressor-setup-x64
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The licence page: GPL-3.0 (no warranty), shown before installing.
LicenseFile=..\LICENSE
UninstallDisplayIcon={app}\Compressor.exe
UninstallDisplayName={#AppName}
CloseApplications=yes
RestartApplications=no
#ifdef IconFile
SetupIconFile={#IconFile}
#endif

[Messages]
; The page's title says it all: no subtitle, no instructions.
SelectTasksDesc=
SelectTasksLabel2=

[Tasks]
Name: "startmenu"; Description: "Add to Start menu"; Flags: unchecked
Name: "desktopicon"; Description: "Add to desktop"; Flags: unchecked
Name: "contextmenu"; Description: "Add Compressor to the File Explorer context menu"; Flags: unchecked
Name: "autostart"; Description: "Start when I sign in"; Flags: unchecked
Name: "updatecheck"; Description: "Check for updates weekly"
Name: "jxlext"; Description: "Open the Microsoft Store to get the JPEG XL Image Extension"; Flags: unchecked; Check: HasJxlExtensionSupport

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Compressor.exe"; Tasks: startmenu
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Compressor.exe"; Tasks: desktopicon

[Run]
; The choices above that the app keeps itself (context menu, sign-in, update check).
; (As the signed-in user, not the administrator account that approved the install: these are their settings.)
Filename: "{app}\Compressor.exe"; Parameters: "--installer-setup {code:SetupArgs}"; Flags: runhidden waituntilterminated runasoriginaluser; StatusMsg: "Applying your choices…"
Filename: "ms-windows-store://pdp/?ProductId=9MZPRTH5C0TB"; Flags: shellexec nowait skipifsilent runasoriginaluser; Tasks: jxlext
Filename: "{app}\Compressor.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; The downloaded installer isn't needed any more (removed a few seconds after Setup closes).
Filename: "{cmd}"; Parameters: "/c ping -n 4 127.0.0.1 >nul & del /f /q ""{srcexe}"""; Description: "Delete the installer"; Flags: postinstall runhidden nowait skipifsilent runasoriginaluser

[UninstallRun]
; Remove the Explorer context menu and start-at-sign-in (both point at this copy of the app).
Filename: "{app}\Compressor.exe"; Parameters: "--uninstall-cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "Cleanup"

[Code]
var
  Upgrading: Boolean;

function InitializeSetup(): Boolean;
begin
  // Installed before? Then the update-check choice made in Settings since is kept.
  Upgrading := RegKeyExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8A6F2C1E-5B7D-4C2A-9E3F-6D1B2C3A4F50}_is1') or
               RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8A6F2C1E-5B7D-4C2A-9E3F-6D1B2C3A4F50}_is1');
  Result := True;
end;

function HasJxlExtensionSupport(): Boolean;
var
  Version: TWindowsVersion;
begin
  // Microsoft's JPEG XL Image Extension needs Windows 11 24H2 (build 26100) or later.
  GetWindowsVersionEx(Version);
  Result := Version.Build >= 26100;
end;

function Flag(const Task: String): String;
begin
  if WizardIsTaskSelected(Task) then Result := '1' else Result := '0';
end;

function SetupArgs(Param: String): String;
var
  Up: String;
begin
  if Upgrading then Up := '1' else Up := '0';
  Result := 'context=' + Flag('contextmenu') + ' autostart=' + Flag('autostart') +
            ' updates=' + Flag('updatecheck') + ' upgrade=' + Up;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('Also remove your Compressor settings and History?' + #13#10#13#10 +
              'History is how compressed files can be undone. Keep it if you might reinstall.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\Compressor'), True, True, True);
      RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Compressor');
    end;
end;
