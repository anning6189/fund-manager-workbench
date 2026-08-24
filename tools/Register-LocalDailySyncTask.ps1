param(
    [string]$ServerHost = "47.95.254.215",
    [string]$ServerUser = "root",
    [string]$ServerAppDir = "/opt/fund-manager-workbench",
    [string]$RemoteServiceName = "consumer-research.service",
    [string]$DailyTime = "08:30"
)

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SyncScript = "$ProjectRoot\tools\Invoke-LocalSyncAndDeploy.ps1"
$TaskName = "FundWorkbench-DailySync"

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$SyncScript`" -ServerHost $ServerHost -ServerUser $ServerUser -ServerAppDir $ServerAppDir -RemoteServiceName $RemoteServiceName"

# PS 5.1: use -Weekly + -DaysOfWeek (NOT -Daily + -DaysOfWeek)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $DailyTime
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Trigger $trigger -Action $action -Principal $principal -Settings $settings -Description "Daily sync local DB and push to Aliyun server"

Write-Output "Task registered successfully."