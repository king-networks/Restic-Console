<#
    Opens Restic Console in its own Edge (or Chrome) app window and signs it in.
    The token never goes on a command line: it is sent to the local service in a header
    in exchange for a one-time code valid for 60 seconds, and only the code goes in the URL.
#>
$ErrorActionPreference = "Stop"
$Url = "http://127.0.0.1:8790"
$TokenFile = Join-Path $env:ProgramData "ResticConsole\token"
$BrowserProfile = Join-Path $env:LOCALAPPDATA "ResticConsole\browser"

function Fail($msg) {
    Add-Type -AssemblyName PresentationFramework
    [void][System.Windows.MessageBox]::Show($msg, "Restic Console", "OK", "Error")
    exit 1
}

try { $token = (Get-Content -Raw -LiteralPath $TokenFile).Trim() }
catch { Fail "Cannot read $TokenFile.`n`nRe-run windows\install.ps1 (as administrator) with -User set to your Windows account." }

try {
    $r = Invoke-RestMethod -UseBasicParsing -Method Post -Uri "$Url/api/handoff" -Headers @{ "X-Token" = $token } `
        -ContentType "application/json" -Body "{}" -TimeoutSec 5
} catch {
    Fail "The Restic Console service is not answering on $Url.`n`nStart it in Task Scheduler (ResticConsole > Server) or restart the PC."
}

$target = "$Url/#code=$($r.code)"
$browsers = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$exe = $browsers | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($exe) {
    New-Item -ItemType Directory -Force -Path $BrowserProfile | Out-Null
    Start-Process -FilePath $exe -ArgumentList @("--user-data-dir=`"$BrowserProfile`"", "--no-first-run",
        "--no-default-browser-check", "--app=$target", "--window-size=1280,860")
} else {
    Start-Process $target
}
