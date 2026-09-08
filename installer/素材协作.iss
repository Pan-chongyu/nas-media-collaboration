#define MyAppName "素材协作"
#ifndef MyAppVersion
  #define MyAppVersion "0.3.1"
#endif
#ifndef AppSourceDir
  #define AppSourceDir "..\build\0.3.1\dist\素材协作"
#endif
#define MyAppPublisher "素材协作"
#define MyAppExeName "素材协作.exe"

[Setup]
AppId={{7A8E0F4E-0A7D-4B4C-93F4-2C66DDE3B12A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\dist
OutputBaseFilename=素材协作-{#MyAppVersion}-setup
Compression=lzma
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
Uninstallable=not IsVerification
CreateUninstallRegKey=not IsVerification
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=局域网 NAS 素材协作工具

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Check: not IsVerification
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Check: not IsVerification

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent; Check: not IsVerification

[Code]
function IsVerification: Boolean;
begin
  Result := ExpandConstant('{param:VERIFYINSTALL|0}') = '1';
end;

function InitializeSetup: Boolean;
var
  Target: String;
begin
  Result := True;
  if IsVerification then begin
    Target := ExpandConstant('{param:DIR|}');
    { Verification must explicitly choose a fresh directory. }
    Result := (Target <> '') and not DirExists(Target);
    if not Result then Log('Verification requires a new /DIR target.');
  end;
end;
