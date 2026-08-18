#define MyAppName "消费行业研究工作台"
#define MyAppVersion "1.3.0"
#define MyAppPublisher "基金公司内部投研系统"
#define MyAppExeName "consumer-research-workbench.exe"

[Setup]
AppId={{6D96D76B-4861-451B-B4CA-A7D72F4B08A1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\ConsumerResearchWorkbench
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\..\..\dist\installer
OutputBaseFilename=消费行业研究工作台-安装程序-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\data\curated\consumer-research.db"; DestDir: "{app}\data\curated"; Flags: onlyifdoesntexist
Source: "..\..\..\data\workflows\stage8\*"; DestDir: "{app}\data\workflows\stage8"; Flags: onlyifdoesntexist recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open --user-name {username} --role public_fund_manager"
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--open --user-name {username} --role public_fund_manager"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--open --user-name {username} --role public_fund_manager"; Description: "启动{#MyAppName}"; Flags: nowait postinstall skipifsilent
