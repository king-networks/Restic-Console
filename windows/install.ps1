#Requires -Version 5.1
<#
.SYNOPSIS
    Install or upgrade Restic Console on Windows.

.DESCRIPTION
    Copies the app to "C:\Program Files\ResticConsole", keeps settings and passwords in
    "C:\ProgramData\ResticConsole" (locked to SYSTEM, Administrators and the desktop user's
    read access to the sign-in token), registers the web server as a SYSTEM scheduled task
    that starts at boot, and adds "Restic Console" to the Start menu.

    Safe to re-run for upgrades: config, jobs, keys, run history and the token are kept.

.PARAMETER User
    Desktop user who gets alerts and opens the app (default: you). Example: DESKTOP-1\alice

.PARAMETER Restic
    Full path of restic.exe (default: found on PATH).

.PARAMETER Python
    Full path of python.exe, 3.9 or newer (default: found with the py launcher or on PATH).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\windows\install.ps1
#>
param(
    [string]$User = "",
    [string]$Restic = "",
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Prefix = Join-Path $env:ProgramFiles "ResticConsole"
$Data = Join-Path $env:ProgramData "ResticConsole"
$Src = Split-Path -Parent $PSScriptRoot

function Die($msg) { Write-Host "error: $msg" -ForegroundColor Red; exit 1 }
function Quiet {
    # Run a program ignoring its output. Windows PowerShell 5.1 turns redirected stderr into
    # terminating errors when $ErrorActionPreference is Stop, so relax it for this call only.
    $ErrorActionPreference = "Continue"
    $out = & $args[0] @($args | Select-Object -Skip 1) 2>&1
    return $out
}
function Step($msg) { Write-Host "== $msg" -ForegroundColor Cyan }

$me = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($me)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die "run this from an elevated PowerShell (right-click PowerShell > Run as administrator)"
}
if (-not $User) { $User = $me.Name }
try {
    $sid = (New-Object Security.Principal.NTAccount($User)).Translate([Security.Principal.SecurityIdentifier]).Value
} catch { Die "no such user: $User" }
$userProfile = (Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" -ErrorAction SilentlyContinue).LocalPath
if (-not $userProfile) { $userProfile = Join-Path (Split-Path $env:USERPROFILE) ($User -replace '^.*\\', '') }

# ---------------------------------------------------------------- prerequisites
function Find-Python {
    if ($Python) { return $Python }
    $cands = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $cands += @(Quiet $py.Source -3 -c "import sys; print(sys.executable)") | Where-Object { $_ -is [string] } }
    foreach ($c in (Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -notlike "*\WindowsApps\*") { $cands += $c.Source }   # skip the Microsoft Store stub
    }
    foreach ($c in $cands) { if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path } }
    Die "Python 3.9+ was not found. Install it with:  winget install Python.Python.3.12 --scope machine"
}
$Python = Find-Python
& $Python -c "import sys; sys.exit(sys.version_info < (3, 9))"
if ($LASTEXITCODE) { Die "$Python is older than 3.9" }

if (-not $Restic) {
    $c = Get-Command restic.exe -ErrorAction SilentlyContinue
    if (-not $c) { Die "restic was not found. Install it with:  winget install restic.restic   (then open a new PowerShell)" }
    $Restic = $c.Source
    $item = Get-Item $Restic
    if ($item.LinkType -and $item.Target) { $Restic = @($item.Target)[0] }    # winget puts a symlink in WinGet\Links
}
if (-not (Test-Path $Restic)) { Die "restic not found at $Restic" }
$rv = ((& $Restic version) -split ' ')[1]
$v = [version](($rv -split '\.')[0..1] -join '.')
if ($v -lt [version]"0.16") { Die "restic $rv is too old; 0.16 or newer is needed" }
foreach ($p in @($Python, $Restic)) {
    if ($p -like "$env:SystemDrive\Users\*") {
        Write-Host "   note: $p is installed for one user only. The service (SYSTEM) will run it from there;" -ForegroundColor Yellow
        Write-Host "         a machine-wide install (winget ... --scope machine) is sturdier." -ForegroundColor Yellow
    }
}
Write-Host "   python $Python"
Write-Host "   restic $rv at $Restic"
Write-Host "   desktop user $User ($userProfile)"

# ---------------------------------------------------------------- program files
Step "Program files -> $Prefix"
Quiet schtasks.exe /end /tn "\ResticConsole\Server" | Out-Null
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force -Path $Prefix, "$Prefix\static", "$Prefix\windows" | Out-Null
Remove-Item "$Prefix\*.py" -Force -ErrorAction SilentlyContinue
Copy-Item "$Src\*.py" $Prefix -Force
Copy-Item "$Src\static\*" "$Prefix\static" -Force
Copy-Item "$Src\windows\Restic-Console.ps1", "$Src\windows\notify-toast.ps1", "$Src\windows\icon.ico" "$Prefix\windows" -Force

# ---------------------------------------------------------------- data + permissions
Step "Settings in $Data"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
# Only SYSTEM and Administrators; nothing inherited from ProgramData (which lets Users read).
& icacls.exe $Data /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE) { Die "could not set permissions on $Data" }
$env:RC_DEV = ""
& $Python -I "$Prefix\server.py" init --user $User --restore-root $userProfile --restic $Restic
if ($LASTEXITCODE) { Die "server.py init failed" }
# The launcher runs as the desktop user without elevation, so that user needs to read the token.
& icacls.exe "$Data\token" /grant "*${sid}:(R)" | Out-Null

# ---------------------------------------------------------------- tasks
Step "Scheduled tasks (\ResticConsole\)"
& $Python -I "$Prefix\server.py" install-tasks
if ($LASTEXITCODE) { Die "registering the scheduled tasks failed" }

# ---------------------------------------------------------------- Start menu
Step "Start menu shortcut"
$lnk = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Restic Console.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Prefix\windows\Restic-Console.ps1`""
$s.IconLocation = "$Prefix\windows\icon.ico"
$s.Description = "Restic Console: scheduled restic backups"
$s.WindowStyle = 7
$s.Save()

# ---------------------------------------------------------------- check
$code = 0
foreach ($i in 1..15) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:8790/api/status" | Out-Null; $code = 200 }
    catch { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
    if ($code -eq 401) { break }
    Start-Sleep -Seconds 1
}
Write-Host ""
if ($code -eq 401) {
    $ver = & $Python -I "$Prefix\server.py" --version
    Write-Host "Restic Console $ver is running on http://127.0.0.1:8790" -ForegroundColor Green
    Write-Host "Open it from the Start menu: Restic Console"
} else {
    Write-Host "WARNING: the service did not answer (got '$code')." -ForegroundColor Yellow
    Write-Host "Check Task Scheduler > Task Scheduler Library > ResticConsole > Server, or run:"
    Write-Host "  & '$Python' -I '$Prefix\server.py'"
    exit 1
}
