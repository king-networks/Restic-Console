#Requires -Version 5.1
<#
.SYNOPSIS
    Remove Restic Console from Windows. Backup repositories are never touched.

.PARAMETER Purge
    Also delete C:\ProgramData\ResticConsole (config, repository passwords, run history).
    Save your repository passwords first!

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\windows\uninstall.ps1
#>
param([switch]$Purge)
$ErrorActionPreference = "Stop"
$Prefix = Join-Path $env:ProgramFiles "ResticConsole"
$Data = Join-Path $env:ProgramData "ResticConsole"

$me = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($me)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "error: run this from an elevated PowerShell" -ForegroundColor Red; exit 1
}

$tasks = @(Get-ScheduledTask -TaskPath "\ResticConsole\" -ErrorAction SilentlyContinue)
foreach ($t in $tasks) {
    Stop-ScheduledTask -InputObject $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -InputObject $t -Confirm:$false
}
try {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $svc.GetFolder("\").DeleteFolder("ResticConsole", 0)
} catch { }
Write-Host "Removed $($tasks.Count) scheduled task(s)."

Start-Sleep -Seconds 1
Remove-Item -Recurse -Force $Prefix -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Restic Console.lnk") -ErrorAction SilentlyContinue

if ($Purge) {
    Remove-Item -Recurse -Force $Data -ErrorAction SilentlyContinue
    Write-Host "Removed, including $Data."
} else {
    Write-Host "Removed. Config, repository passwords and run history are still in $Data."
}
