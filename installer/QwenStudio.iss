; Qwen Studio installer - per-user, no admin rights, no Python needed.
; Build:  ISCC.exe installer\QwenStudio.iss   (after building desktop\dist-installer\Qwen Studio)
#define AppName "Qwen Studio"
#define AppVersion "1.0.0"
#define Src ".."

[Setup]
AppId={{6F2C8A51-3B7E-4D7A-9C1E-QWENSTUDIO01}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Qwen Studio
DefaultDirName={localappdata}\Programs\Qwen Studio
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=Qwen-Studio-Setup
SetupIconFile={#Src}\desktop\qwen.ico
UninstallDisplayIcon={app}\desktop\qwen.ico
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=yes

[Messages]
WelcomeLabel2=This installs [name] — your own private Qwen AI, running free on Kaggle's GPUs.%n%nBEFORE YOU START:%n• You need a free Kaggle account: kaggle.com%n• Your Kaggle account must have a verified phone number (Kaggle Settings → Phone verification). Without it Kaggle gives no GPUs.%n%nAfter installing, the app asks you to sign in to Kaggle once in your browser. That's all the setup.

[Files]
Source: "{#Src}\desktop\dist-installer\Qwen Studio\*"; DestDir: "{app}\desktop\app\Qwen Studio"; Flags: recursesubdirs ignoreversion
Source: "{#Src}\desktop\shell.html"; DestDir: "{app}\desktop"; Flags: ignoreversion
Source: "{#Src}\desktop\qwen.ico"; DestDir: "{app}\desktop"; Flags: ignoreversion
Source: "{#Src}\desktop\qwen_256.png"; DestDir: "{app}\desktop"; Flags: ignoreversion
Source: "{#Src}\app\chat.html"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#Src}\gateway\gateway.py"; DestDir: "{app}\gateway"; Flags: ignoreversion
Source: "{#Src}\gateway\kernel_template.py"; DestDir: "{app}\gateway"; Flags: ignoreversion

[Dirs]
Name: "{app}\kernel"

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\desktop\app\Qwen Studio\Qwen Studio.exe"; IconFilename: "{app}\desktop\qwen.ico"
Name: "{userprograms}\{#AppName}"; Filename: "{app}\desktop\app\Qwen Studio\Qwen Studio.exe"; IconFilename: "{app}\desktop\qwen.ico"

[Run]
Filename: "{app}\desktop\app\Qwen Studio\Qwen Studio.exe"; Description: "Open Qwen Studio now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; generated at runtime: this install's private settings, chat cache, logs
Type: filesandordirs; Name: "{app}\gateway\auth.json"
Type: filesandordirs; Name: "{app}\kernel"
Type: filesandordirs; Name: "{app}\desktop\webview_data"
Type: files; Name: "{app}\desktop\*.json"
Type: files; Name: "{app}\desktop\*.log"
Type: files; Name: "{app}\desktop\chat_local.html"
Type: files; Name: "{app}\app\endpoint.txt"
Type: files; Name: "{app}\CREDENTIALS.txt"
Type: filesandordirs; Name: "{app}\desktop\.kaggle_cfg"
