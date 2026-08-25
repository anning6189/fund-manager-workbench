param(
    [string]$ServerHost = "47.95.254.215",
    [string]$ServerUser = "root",
    [string]$ServerAppDir = "/opt/fund-manager-workbench",
    [string]$RemoteServiceName = "consumer-research.service"
)

$ErrorActionPreference = "Stop"
Write-Error @"
This workflow is disabled.

Current rule:
  Public Aliyun server is the only automatic data-sync source.
  Local workspace must not run daily Gildata sync and push DB to public server.

Use this instead:
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\Sync-PublicDbToLocal.ps1
"@
exit 1
