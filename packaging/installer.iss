; Compressor's Windows installer (Inno Setup 6). Built by CI from the PyInstaller folder:
;   iscc /DAppVersion=0.1.0 /DSourceDir=..\dist\Compressor /DIconFile=..\dist\Compressor.ico packaging\installer.iss
; Installs for everyone (recommended: Program Files is protected from other programs), or only for the current
; user without administrator rights. Optional: a Start menu shortcut (on by default: without one, Windows Search
; can't find the app or show its actions), and, off by default, a desktop shortcut, the Explorer right-click menu,
; starting at sign-in, and (Windows 11 24H2+) Microsoft's JPEG XL Image Extension from the Store.
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
; Windows 10 version 1809 or later (Qt 6's minimum): older versions get a clear message instead of a broken app.
MinVersion=10.0.17763
OutputDir=..\dist
OutputBaseFilename=Compressor-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The licence, accepted before installing.
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
; The licence page: a subtitle and one instruction, instead of two "please read" lines saying the same thing.
LicenseLabel=Compressor is free, open-source software.
LicenseLabel3=Please read the license below. You must accept its terms to continue with the installation.
; Shortcuts are optional (off by default), so don't send people looking for them.
FinishedLabel=Setup has finished installing [name] on your computer.
FinishedLabelNoIcons=Setup has finished installing [name] on your computer.

[Tasks]
Name: "startmenu"; Description: "Add a shortcut to the Start menu"
Name: "desktopicon"; Description: "Add a shortcut to the desktop"; Flags: unchecked
Name: "contextmenu"; Description: "Add Compressor to the right-click menu in File Explorer"; Flags: unchecked
Name: "autostart"; Description: "Start Compressor when I sign in"; Flags: unchecked
Name: "jxlext"; Description: "Get the JPEG XL Image Extension from the Microsoft Store"; Flags: unchecked; Check: HasJxlExtensionSupport

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; (An earlier uninstall's clean-up for other users must not run on this new install: see RegisterCleanupForOtherUsers.)
Root: HKLM; Subkey: "Software\Microsoft\Active Setup\Installed Components\{{6E3B5C1A-2F44-4C1E-9B8A-0C6D2E7F4A31}"; ValueType: none; Flags: deletekey; Check: IsAdminInstallMode
; App Paths: typing "compressor" in Win+R (and launchers that use it) starts the app.
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\Compressor.exe"; ValueType: string; ValueName: ""; ValueData: "{app}\Compressor.exe"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\Compressor.exe"; ValueType: string; ValueName: "Path"; ValueData: "{app}"

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
; (Also the folder it was unzipped into, and that .zip, when both are named after the installer: see CleanupCommand.)
Filename: "{cmd}"; Parameters: "{code:CleanupCommand}"; WorkingDir: "{sys}"; Description: "Delete the installer"; Flags: postinstall runhidden nowait skipifsilent runasoriginaluser

[UninstallRun]
; Remove the Explorer context menu and start-at-sign-in (both point at this copy of the app).
Filename: "{app}\Compressor.exe"; Parameters: "--uninstall-cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "Cleanup"

[Code]
var
  Upgrading: Boolean;
  UpdatesLabel: TNewStaticText;
  UpdatesCombo: TNewComboBox;
  JxlChecked, JxlInstalled: Boolean;

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

// Is Microsoft's JPEG XL Image Extension already installed (for this user)? Asked once, the first time the
// tasks page needs it.
function JxlExtensionInstalled(): Boolean;
var
  Code: Integer;
begin
  if not JxlChecked then
  begin
    JxlChecked := True;
    JxlInstalled := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -NonInteractive -Command "if (Get-AppxPackage -Name ''*JpegXL*'') { exit 0 } else { exit 1 }"',
      '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  end;
  Result := JxlInstalled;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
begin
  // Already have the JPEG XL extension: show that option greyed out and unticked.
  if CurPageID = wpSelectTasks then
    for I := 0 to WizardForm.TasksList.Items.Count - 1 do
      if Pos('JPEG XL Image Extension', WizardForm.TasksList.ItemCaption[I]) > 0 then
        if JxlExtensionInstalled() then
        begin
          WizardForm.TasksList.Checked[I] := False;
          WizardForm.TasksList.ItemEnabled[I] := False;
          if Pos('(Already installed)', WizardForm.TasksList.ItemCaption[I]) = 0 then
            WizardForm.TasksList.ItemCaption[I] := WizardForm.TasksList.ItemCaption[I] + ' (Already installed)';
        end;
  // The last page has no Cancel: put Finish where the main button sits on every other page (bottom right),
  // not in the middle.
  if CurPageID = wpFinished then
    WizardForm.NextButton.Left := WizardForm.CancelButton.Left;
end;

// "Delete the installer": the setup .exe, a few seconds after Setup closes. When it was unzipped from a download
// (e.g. Compressor-setup.zip extracted to a Compressor-setup folder), that folder goes too if nothing else is in
// it (rmdir refuses otherwise), and so does the .zip beside it. Only names starting with "Compressor-setup", so a
// Downloads or Desktop folder is never touched.
function CleanupCommand(Param: String): String;
var
  Exe, Dir, Name, Zip: String;
begin
  Exe := ExpandConstant('{srcexe}');
  Dir := ExtractFileDir(Exe);
  Name := ExtractFileName(Dir);
  Result := '/c ping -n 4 127.0.0.1 >nul & del /f /q "' + Exe + '"';
  if Pos('compressor-setup', Lowercase(Name)) = 1 then
  begin
    Result := Result + ' & rmdir "' + Dir + '"';
    Zip := ExtractFileDir(Dir) + '\' + Name + '.zip';
    if FileExists(Zip) then
      Result := Result + ' & del /f /q "' + Zip + '"';
  end;
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

// Settings › Apps › Installed apps › … › Modify: opens Compressor's Settings (menus, start at sign-in,
// notifications), the closest thing a classic app has to "Advanced options". (Inno marks it NoModify otherwise.)
procedure CurStepChanged(CurStep: TSetupStep);
var
  Root: Integer;
  Key: String;
begin
  if CurStep = ssPostInstall then
  begin
    if IsAdminInstallMode then Root := HKLM else Root := HKCU;
    Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8A6F2C1E-5B7D-4C2A-9E3F-6D1B2C3A4F50}_is1';
    RegWriteDWordValue(Root, Key, 'NoModify', 0);
    RegWriteStringValue(Root, Key, 'ModifyPath', '"' + ExpandConstant('{app}\Compressor.exe') + '" --open=settings');
  end;
end;

// Other people's leftovers after an install for everyone: start at sign-in, menus, Open with, notifications
// are written per user (in each one's own registry), and the uninstaller can only reach the person running it.
// Windows' Active Setup runs a command once for each user at their next sign-in: this one removes those entries
// (never anyone's files, settings or History). A later install removes the Active Setup entry again ([Registry]).
const
  CleanupKey = 'Software\Microsoft\Active Setup\Installed Components\{6E3B5C1A-2F44-4C1E-9B8A-0C6D2E7F4A31}';

function CleanupScript(): String;
begin
  Result :=
    '$c=''HKCU:\Software\Classes''; ' +
    'foreach($k in ''Compressor.Picture'',''Compressor.Video'',''Compressor.Audio'',''compressor'',' +
    '''*\shell\Compressor'',''Directory\shell\Compressor'',' +
    '''SystemFileAssociations\image\shell\CompressorConvert'',''SystemFileAssociations\video\shell\CompressorConvert'',' +
    '''SystemFileAssociations\audio\shell\CompressorConvert'',''Applications\Compressor.exe'',' +
    '''AppUserModelId\Lanntern.Compressor''){Remove-Item -LiteralPath ($c+''\''+$k) -Recurse -Force -EA 0}; ' +
    'Get-ChildItem $c -EA 0 | Where-Object {$_.PSChildName -like ''.*''} | ForEach-Object {' +
    'Remove-ItemProperty -LiteralPath ($_.PSPath+''\OpenWithProgids'') ' +
    '-Name ''Compressor.Picture'',''Compressor.Video'',''Compressor.Audio'' -EA 0}; ' +
    'Remove-ItemProperty -LiteralPath ''HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'' -Name ''Compressor'' -EA 0; ' +
    'Remove-ItemProperty -LiteralPath ''HKCU:\Software\RegisteredApplications'' -Name ''Compressor'' -EA 0; ' +
    'Remove-Item -LiteralPath ''HKCU:\Software\Lanntern\Compressor'',' +
    '''HKCU:\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\Lanntern.Compressor'',' +
    '''HKCU:\Software\Compressor\Shell'' -Recurse -Force -EA 0; ' +
    'Get-AppxPackage -Name ''Lanntern.Compressor.ContextMenu'' | Remove-AppxPackage -EA 0';
end;

procedure RegisterCleanupForOtherUsers();
begin
  RegWriteStringValue(HKLM, CleanupKey, '', 'Compressor clean-up after uninstalling');
  RegWriteStringValue(HKLM, CleanupKey, 'StubPath',
    ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe') +
    ' -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "' + CleanupScript() + '"');
  // A new version number each time, so a user who ran an earlier clean-up gets this one too.
  RegWriteStringValue(HKLM, CleanupKey, 'Version', '1,' + GetDateTimeString('yyyy,mmdd,hhnn', #0, #0));
  RegWriteDWordValue(HKLM, CleanupKey, 'IsInstalled', 1);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Choices: TArrayOfString;
begin
  if (CurUninstallStep = usPostUninstall) and IsAdminInstallMode then
    RegisterCleanupForOtherUsers();
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
  begin
    // Command links: each choice says what it does. Keeping is the default, and the safe answer.
    // (Built here, not inline: Inno Setup reads any line starting with a bracket as a new section.)
    SetArrayLength(Choices, 2);
    Choices[0] := 'Keep them' + #13#10 + 'If you install Compressor again, it carries on where you left off, ' +
      'and you can still undo past compressions from History.';
    Choices[1] := 'Remove them' + #13#10 + 'Deletes Compressor''s settings, History and log. Your files ' +
      'aren''t touched, but compressions can no longer be undone from History.';
    if TaskDialogMsgBox('Keep your settings and History?',
         'Compressor has been removed. Your settings and History are still on this PC.',
         mbConfirmation, MB_YESNO, Choices, 0) = IDNO then
    begin
      DelTree(ExpandConstant('{localappdata}\Compressor'), True, True, True);
      RegDeleteKeyIncludingSubkeys(HKCU, 'Software\Compressor');
    end;
  end;
end;
