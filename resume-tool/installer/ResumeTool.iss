; Resume Tool 安装包脚本（Inno Setup 7）
; 编译：ISCC.exe installer\ResumeTool.iss
; 前置：先跑 PyInstaller 生成 ..\dist\ResumeTool\

#define AppName "Resume Tool"
#define AppVersion "1.0.0"
#define AppExe "ResumeTool.exe"
#define AppPublisher "Resume Tool"
#define AppRepo "https://github.com/Vanessa0712/vanessa-resume-tool"

[Setup]
; AppId 决定升级识别：改动后会被当成另一个程序，切勿修改
AppId={{B4C1F0A7-2E58-4F63-9B1D-7A6C5E2D8F41}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} Beta {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppRepo}
AppSupportURL={#AppRepo}
AppUpdatesURL={#AppRepo}/releases
DefaultDirName={autopf}\ResumeTool
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=no
; 默认以当前用户身份安装到 LocalAppData\Programs，无管理员权限也能装；
; 需要装到 Program Files 时安装界面会让用户选择「为所有用户安装」
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=ResumeTool-Setup-{#AppVersion}
SetupIconFile=..\assets\ResumeTool.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
WizardImageFile=..\assets\wizard-large.bmp
WizardSmallImageFile=..\assets\wizard-small.bmp
WizardImageStretch=no
InfoBeforeFile=install-notes.txt
; 安装程序自身也带版本资源，属性页可查
VersionInfoVersion=1.0.0.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 覆盖安装时尝试关闭正在运行的旧版本，避免文件被占用
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\ResumeTool\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  DataSubDir = 'ResumeTool';

// 安装前把数据目录与升级关系说清楚：数据不在安装目录，卸载也不会删
procedure InitializeWizard();
begin
  WizardForm.InfoBeforeMemo.Font.Name := 'Microsoft YaHei';
  WizardForm.InfoBeforeMemo.Font.Size := 10;
end;

// 卸载完成后提示数据仍在，避免用户以为简历被一起删掉
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\' + DataSubDir);
    if DirExists(DataDir) then
      MsgBox('程序已卸载。' + #13#10 + #13#10 +
             '你的简历数据仍保留在：' + #13#10 + DataDir + #13#10 + #13#10 +
             '如需彻底删除，请手动删除该文件夹。',
             mbInformation, MB_OK);
  end;
end;
