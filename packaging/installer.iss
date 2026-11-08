; Compressor's Windows installer (Inno Setup 6). Built by CI from the PyInstaller folder:
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\dist\Compressor /DIconFile=..\dist\Compressor.ico packaging\installer.iss
; Installs for everyone (recommended: Program Files is protected from other programs), or only for the current
; user without administrator rights. Optional, all off: a Start menu shortcut, a desktop shortcut, the Explorer
; right-click menu, starting at sign-in, and (Windows 11 24H2+) Microsoft's JPEG XL Image Extension from the Store.
; How often to check for updates is a list on the same page (weekly by default; /UPDATES=launch|weekly|monthly|never
; for silent installs). Uninstalls cleanly: the right-click menu and start-at-sign-in are removed, and the user is
; asked whether to keep settings and History.

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
; What Settings → Apps, Control Panel and uninstallers (Revo and others) show for the installed app.
AppPublisherURL=https://github.com/Lanntern/Compressor
AppSupportURL=https://github.com/Lanntern/Compressor/issues
AppUpdatesURL=https://github.com/Lanntern/Compressor/releases/latest
AppComments=Shrink photos, videos and music safely: every result verified, every change undoable.
AppContact=https://github.com/Lanntern/Compressor/issues
; The setup .exe's own details (file Properties → Details).
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=Lanntern
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
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
; The licence, shown before installing. An information page rather than "I accept the agreement": the GPL asks
; nothing of people who just use the app (it covers sharing and changing it).
InfoBeforeFile=..\LICENSE
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
; The licence page: one line above the text instead of two "please read" lines.
WizardInfoBefore=License
InfoBeforeLabel=Compressor is free, open-source software.
InfoBeforeClickLabel=You don't need to accept anything to use it. The GNU General Public License below covers sharing and changing Compressor, and explains that it comes with no warranty.
; Shortcuts are optional (off by default), so don't send people looking for them.
FinishedLabel=Setup has finished installing [name] on your computer.
FinishedLabelNoIcons=Setup has finished installing [name] on your computer.

[Tasks]
Name: "startmenu"; Description: "Add a shortcut to the Start menu"; Flags: unchecked
Name: "desktopicon"; Description: "Add a shortcut to the desktop"; Flags: unchecked
Name: "contextmenu"; Description: "Add Compressor to the right-click menu in File Explorer"; Flags: unchecked
Name: "autostart"; Description: "Start Compressor when I sign in"; Flags: unchecked
Name: "jxlext"; Description: "Get the JPEG XL Image Extension from the Microsoft Store"; Flags: unchecked; Check: HasJxlExtensionSupport

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID: the same as the app's (compressor/gui/jumplist.py), so the shortcut, the running window and the
; jump list (taskbar right-click, Start, Windows Search actions) are one app.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Compressor.exe"; Comment: "Shrink photos, videos and music safely"; AppUserModelID: "Lanntern.Compressor"; Tasks: startmenu
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Compressor.exe"; Comment: "Shrink photos, videos and music safely"; AppUserModelID: "Lanntern.Compressor"; Tasks: desktopicon

[Run]
; The choices above that the app keeps itself (right-click menu, sign-in, update check).
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
  UpdatesLabel: TNewStaticText;
  UpdatesCombo: TNewComboBox;

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

// "Check for updates": a list below the tasks, the same choices as the app's Settings (compressor/updates.py).
function UpdateKey(Index: Integer): String;
begin
  case Index of
    0: Result := 'launch';
    2: Result := 'monthly';
    3: Result := 'never';
  else
    Result := 'weekly';
  end;
end;

procedure InitializeWizard();
var
  Gap: Integer;
begin
  UpdatesCombo := TNewComboBox.Create(WizardForm);
  UpdatesCombo.Parent := WizardForm.SelectTasksPage;
  UpdatesCombo.Style := csDropDownList;
  UpdatesCombo.Items.Add('Whenever I open Compressor');
  UpdatesCombo.Items.Add('Weekly (recommended)');
  UpdatesCombo.Items.Add('Monthly');
  UpdatesCombo.Items.Add('Never');
  UpdatesCombo.ItemIndex := 1;
  UpdatesCombo.Width := ScaleX(200);

  UpdatesLabel := TNewStaticText.Create(WizardForm);
  UpdatesLabel.Parent := WizardForm.SelectTasksPage;
  UpdatesLabel.AutoSize := True;
  UpdatesLabel.Caption := 'Check for &updates:';
  UpdatesLabel.FocusControl := UpdatesCombo;

  // On an upgrade the choice made in Settings stays, so there's nothing to ask.
  UpdatesLabel.Visible := not Upgrading;
  UpdatesCombo.Visible := not Upgrading;
  if not Upgrading then
  begin
    Gap := ScaleY(12);
    WizardForm.TasksList.Height := WizardForm.TasksList.Height - UpdatesCombo.Height - Gap;
    UpdatesCombo.Top := WizardForm.TasksList.Top + WizardForm.TasksList.Height + Gap;
    UpdatesLabel.Left := WizardForm.TasksList.Left;
    UpdatesLabel.Top := UpdatesCombo.Top + (UpdatesCombo.Height - UpdatesLabel.Height) div 2;
    UpdatesCombo.Left := UpdatesLabel.Left + UpdatesLabel.Width + ScaleX(8);
    UpdatesLabel.Anchors := [akLeft, akBottom];
    UpdatesCombo.Anchors := [akLeft, akBottom];
  end;
end;

function UpdatesChoice(): String;
begin
  // Silent installs: /UPDATES=launch|weekly|monthly|never.
  Result := Lowercase(ExpandConstant('{param:UPDATES|}'));
  if (Result <> 'launch') and (Result <> 'weekly') and (Result <> 'monthly') and (Result <> 'never') then
    Result := UpdateKey(UpdatesCombo.ItemIndex);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  // The last page has no Cancel: put Finish where the main button sits on every other page (bottom right),
  // not in the middle.
  if CurPageID = wpFinished then
    WizardForm.NextButton.Left := WizardForm.CancelButton.Left;
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
            ' updates=' + UpdatesChoice() + ' upgrade=' + Up;
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
