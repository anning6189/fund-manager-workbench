param(
    [string]$ServerHost = "47.95.254.215",
    [string]$ServerUser = "root",
    [string]$ServerAppDir = "/opt/fund-manager-workbench",
    [string]$RemoteServiceName = "consumer-research.service",
    [string]$DailyTime = "08:30"
)

Write-Error @"
Local daily sync task registration is disabled.

Current rule:
  Public Aliyun server is the only automatic data-sync source.
  Local workspace must only pull the public DB for viewing/debugging.

Use this instead when local data needs refreshing:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\Sync-PublicDbToLocal.ps1
"@
exit 1
