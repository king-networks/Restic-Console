<#
    Shows a Windows notification. Run by the per-user scheduled task \ResticConsole\Notify-<user>,
    which the service (SYSTEM) registers and starts, because SYSTEM cannot show toasts itself.
    Title and body arrive base64-encoded (UTF-8) so no quoting can break the command line.
#>
param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Body,
    [string]$Level = "test"
)
$ErrorActionPreference = "Stop"
$dec = { param($s) [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s)) }
$t = [Security.SecurityElement]::Escape((& $dec $Title))
$b = [Security.SecurityElement]::Escape((& $dec $Body))
$scenario = if ($Level -eq "failure") { ' scenario="reminder"' } else { "" }
$actions = if ($Level -eq "failure") { '<actions><action content="Dismiss" arguments="dismiss" activationType="system"/></actions>' } else { "" }

[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast$scenario><visual><binding template=`"ToastGeneric`"><text>$t</text><text>$b</text></binding></visual>$actions</toast>")
# Windows PowerShell's registered AppUserModelID, so the toast is allowed without installing an app identity.
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
    [Windows.UI.Notifications.ToastNotification]::new($xml))
