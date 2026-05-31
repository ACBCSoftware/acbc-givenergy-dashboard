; ============================================================
;  ACBC GivEnergy Dashboard — Inno Setup installer script
;  Requires: Inno Setup 6.x  (https://jrsoftware.org/isinfo.php)
;  To compile: open this file in Inno Setup IDE, press Ctrl+F9
; ============================================================

#define AppName    "ACBC GivEnergy Dashboard"
#define AppVersion "1.5"
#define AppPublisher "ACBC Software"
#define AppURL     "https://software.andrewcampbell.co.uk"
#define AppExeName "start_dashboard.bat"

[Setup]
AppId={{8F3A2C1D-4B5E-6F7A-8C9D-0E1F2A3B4C5D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\ACBC GivEnergy Dashboard
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
OutputBaseFilename=ACBC-GivEnergy-Dashboard-Setup-v{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
SetupLogging=yes
ChangesEnvironment=no
MinVersion=10.0

[Code]
// Resolved Python launcher. Either a full path to python.exe, or the
// reliable 'py' launcher (always on PATH, even when elevated), or 'python'.
var
  PyCmd: String;
  PyUsesLauncher: Boolean;

// Look in the registry (both hives) for any installed Python and return its exe.
function PythonFromRegistry(): String;
var
  RootKeys: array[0..1] of Integer;
  ri, i: Integer;
  Names: TArrayOfString;
  InstallPath, Cand: String;
begin
  Result := '';
  RootKeys[0] := HKEY_CURRENT_USER;
  RootKeys[1] := HKEY_LOCAL_MACHINE;
  for ri := 0 to 1 do begin
    if RegGetSubkeyNames(RootKeys[ri], 'Software\Python\PythonCore', Names) then begin
      for i := 0 to GetArrayLength(Names) - 1 do begin
        if RegQueryStringValue(RootKeys[ri],
             'Software\Python\PythonCore\' + Names[i] + '\InstallPath', '', InstallPath) then begin
          Cand := AddBackslash(InstallPath) + 'python.exe';
          if FileExists(Cand) then begin
            Result := Cand;
            exit;
          end;
        end;
      end;
    end;
  end;
end;

// Check the usual per-user install folders as a fallback.
function PythonFromCommonPaths(): String;
var
  Vers: array[0..7] of String;
  i: Integer;
  Cand: String;
begin
  Result := '';
  Vers[0]:='314'; Vers[1]:='313'; Vers[2]:='312'; Vers[3]:='311';
  Vers[4]:='310'; Vers[5]:='39';  Vers[6]:='3';   Vers[7]:='';
  for i := 0 to 7 do begin
    Cand := ExpandConstant('{localappdata}\Programs\Python\Python' + Vers[i] + '\python.exe');
    if FileExists(Cand) then begin Result := Cand; exit; end;
    Cand := ExpandConstant('{pf}\Python' + Vers[i] + '\python.exe');
    if FileExists(Cand) then begin Result := Cand; exit; end;
  end;
end;

// Find a usable Python. Sets PyCmd + PyUsesLauncher. Returns True if found.
function DetectPython(): Boolean;
var
  ResultCode: Integer;
  Found: String;
begin
  PyUsesLauncher := False;

  // 1. Registry (most reliable for per-user installs without PATH)
  Found := PythonFromRegistry();
  if Found = '' then Found := PythonFromCommonPaths();
  if Found <> '' then begin
    PyCmd := Found;
    Result := True;
    exit;
  end;

  // 2. The 'py' launcher — installed to the Windows dir, always on PATH
  if Exec('py', '-3 --version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then begin
    PyCmd := 'py';
    PyUsesLauncher := True;
    Result := True;
    exit;
  end;

  // 3. Bare 'python' on PATH (skips the Store stub which returns non-zero here)
  if Exec('python', '--version', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then begin
    PyCmd := 'python';
    Result := True;
    exit;
  end;

  Result := False;
end;

// Exposed to [Run] so the venv is created with the resolved interpreter,
// not a bare 'python' that may not be on PATH in this context.
function GetPyCmd(Param: String): String;
begin
  Result := PyCmd;
end;

function GetVenvParams(Param: String): String;
begin
  if PyUsesLauncher then
    Result := '-3 -m venv "' + ExpandConstant('{app}\venv') + '"'
  else
    Result := '-m venv "' + ExpandConstant('{app}\venv') + '"';
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not DetectPython() then begin
    MsgBox(
      'Python 3.9 or later is required but could not be found.' + #13#10 + #13#10 +
      'Please install Python from https://python.org' + #13#10 +
      '  • Tick "Add Python to PATH" on the first installer screen' + #13#10 +
      '  • Or simply use the default options (the "py" launcher is enough)' + #13#10 + #13#10 +
      'If you have just installed Python, close this window, then sign out' + #13#10 +
      'and back in (or restart) so Windows picks it up, and run this again.',
      mbError, MB_OK
    );
    Result := False;
  end;
end;

// On upgrade, stop any running dashboard first so its files aren't locked.
// Done INLINE (not via stop_dashboard.bat) so it works even when upgrading
// over an older install whose stop script may behave differently.
// config.ini and history.db are never in [Files], so they are always preserved.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  // Kill whatever is listening on the dashboard port (default 7890).
  Exec(ExpandConstant('{cmd}'),
       '/c for /f "tokens=5" %a in (''netstat -aon ^| findstr ":7890 " ^| findstr LISTENING'') do taskkill /PID %a /F',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// Detect an existing install so we can show the user a reassuring message.
procedure InitializeWizard();
begin
  if FileExists(ExpandConstant('{autopf}\ACBC GivEnergy Dashboard\dashboard_server.py')) then
    MsgBox(
      'An existing installation was detected.' + #13#10 + #13#10 +
      'This will upgrade it to the new version.' + #13#10 +
      'Your settings (config.ini) and history (history.db) will be kept.',
      mbInformation, MB_OK
    );
end;

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "autostart";   Description: "Start dashboard &automatically at Windows login"; GroupDescription: "Startup:"

[Files]
; Core application files
Source: "dashboard_server.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "dashboard.html";      DestDir: "{app}"; Flags: ignoreversion
Source: "manifest.json";       DestDir: "{app}"; Flags: ignoreversion
Source: "sw.js";               DestDir: "{app}"; Flags: ignoreversion
Source: "generate_icons.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "config.ini.example";  DestDir: "{app}"; Flags: ignoreversion
Source: "start_dashboard.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "stop_dashboard.bat";  DestDir: "{app}"; Flags: ignoreversion

[Dirs]
Name: "{app}\icons"

[Run]
; 1. Create Python virtual environment using the resolved interpreter
;    (full python.exe path, or the 'py' launcher — never a bare 'python'
;    that might not be on PATH in this install context).
Filename: "{code:GetPyCmd}"; Parameters: "{code:GetVenvParams}"; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; \
  StatusMsg: "Creating Python virtual environment...";

; 2. Upgrade pip silently
Filename: "{app}\venv\Scripts\pip.exe"; Parameters: "install --quiet --upgrade pip"; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; \
  StatusMsg: "Upgrading pip...";

; 3. Install required packages
Filename: "{app}\venv\Scripts\pip.exe"; \
  Parameters: "install --quiet ""flask>=3.1.3"" waitress givenergy-modbus Pillow pyopenssl"; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; \
  StatusMsg: "Installing packages (this takes a minute)...";

; 4. Generate PWA icons
Filename: "{app}\venv\Scripts\python.exe"; Parameters: "generate_icons.py"; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated; \
  StatusMsg: "Generating app icons...";

; 5. Copy example config if config.ini doesn't exist
Filename: "{cmd}"; \
  Parameters: "/c if not exist ""{app}\config.ini"" copy ""{app}\config.ini.example"" ""{app}\config.ini"""; \
  WorkingDir: "{app}"; Flags: runhidden waituntilterminated;

; 6. Offer to open config for editing
Filename: "notepad.exe"; Parameters: """{app}\config.ini"""; \
  Description: "Open configuration file (set your inverter IP)"; \
  Flags: postinstall nowait skipifsilent;

; 7. Offer to launch dashboard
Filename: "{app}\start_dashboard.bat"; \
  Description: "Launch the dashboard now"; \
  Flags: postinstall nowait skipifsilent unchecked;

[UninstallRun]
; Stop the dashboard before uninstalling, INLINE (kill the listener on port
; 7890) rather than calling stop_dashboard.bat — guarantees the uninstaller
; can never hang on a script that waits for input.
Filename: "{cmd}"; \
  Parameters: "/c for /f ""tokens=5"" %a in ('netstat -aon ^| findstr "":7890 "" ^| findstr LISTENING') do taskkill /PID %a /F"; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopDash"

[UninstallDelete]
; Remove runtime-generated folders that aren't tracked in [Files].
; config.ini, history.db and the backups folder are deliberately left in
; place so the user's settings and history survive an uninstall/reinstall.
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\icons"
Type: filesandordirs; Name: "{app}\__pycache__"

[Icons]
; Start Menu
Name: "{group}\Start Dashboard";      Filename: "{app}\start_dashboard.bat"; WorkingDir: "{app}"
Name: "{group}\Stop Dashboard";       Filename: "{app}\stop_dashboard.bat";  WorkingDir: "{app}"
Name: "{group}\Edit Configuration";   Filename: "notepad.exe"; Parameters: """{app}\config.ini"""; WorkingDir: "{app}"
Name: "{group}\Open Dashboard";       Filename: "http://localhost:7890"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"

; Desktop (optional)
Name: "{autodesktop}\GivEnergy Dashboard"; Filename: "{app}\start_dashboard.bat"; \
  WorkingDir: "{app}"; Tasks: desktopicon

; Startup (optional)
Name: "{userstartup}\GivEnergy Dashboard"; Filename: "{app}\start_dashboard.bat"; \
  WorkingDir: "{app}"; Tasks: autostart

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nYou will need your GivEnergy inverter's local IP address to hand — check your router's DHCP list.%n%nClick Next to continue.
