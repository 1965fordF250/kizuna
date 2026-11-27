; Inno Setup script for the Kizuna Windows installer.
; Build (from the repository root, after PyInstaller has produced dist\Kizuna):
;   iscc /DAppVersion=1.0.0 /DRepoUrl=https://github.com/OWNER/kizuna packaging\windows\installer.iss
; Output: dist\Kizuna-<version>-Windows-x64-Setup.exe

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef RepoUrl
  #define RepoUrl "https://github.com"
#endif
#define AppName "Kizuna"
#define AppExe "Kizuna.exe"

[Setup]
; Same AppId as when the app was called Track Merger, so installing Kizuna replaces it.
AppId={{1ABEE7C1-A35F-418F-9D74-A40E6E7A3409}
; Install into "Kizuna" even when upgrading an install in the old "Track Merger" folder.
UsePreviousAppDir=no
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Kizuna contributors
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Installs for the current user without admin rights; the user can choose "all users".
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\..\LICENSE
SetupIconFile=..\..\build\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\..\dist
OutputBaseFilename=Kizuna-{#AppVersion}-Windows-x64-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; temporary work files only; logs are kept in case they are needed for a bug report
Type: filesandordirs; Name: "{localappdata}\{#AppName}\work"
Type: files; Name: "{localappdata}\{#AppName}\running.url"

[InstallDelete]
; Upgrading from Track Merger (this app's name before 2.1.0): remove the old program
; folder and shortcuts. Settings are carried over by the app itself on first launch.
Type: filesandordirs; Name: "{autopf}\Track Merger"; Check: OldTrackMergerInstalled
Type: files; Name: "{autoprograms}\Track Merger.lnk"
Type: files; Name: "{autodesktop}\Track Merger.lnk"

[Code]
function OldTrackMergerInstalled: Boolean;
begin
  { only a folder that really is the old app }
  Result := FileExists(ExpandConstant('{autopf}\Track Merger\Track Merger.exe'));
end;
