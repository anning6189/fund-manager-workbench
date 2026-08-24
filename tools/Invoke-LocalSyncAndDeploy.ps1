param(
    [string]$ServerHost = "47.95.254.215",
    [string]$ServerUser = "root",
    [string]$ServerAppDir = "/opt/fund-manager-workbench",
    [string]$RemoteServiceName = "consumer-research.service"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LocalDbPath = "$ProjectRoot\data\curated\consumer-research.db"
$RemoteDbPath = "$ServerAppDir/data/curated/consumer-research.db"

# Find python
$PythonExe = ""
foreach ($candidate in @("python", "python3", "py")) {
    try { $cmd = Get-Command $candidate -ErrorAction SilentlyContinue; if ($cmd) { $PythonExe = $candidate; break } } catch {}
}
if (-not $PythonExe) { Write-Error "Python not found"; exit 1 }

Write-Output "=== Step 1: Running local sync script ==="
& $PythonExe "$ProjectRoot\tools\Invoke-DailyMorningBriefSync.py"
if ($LASTEXITCODE -ne 0) { Write-Error "Sync FAILED"; exit 1 }

Write-Output "=== Step 2: Stopping remote service ==="
ssh "$ServerUser@$ServerHost" "systemctl stop $RemoteServiceName"

Write-Output "=== Step 3: Pushing DB to server ==="
scp "$LocalDbPath" "${ServerUser}@${ServerHost}:${RemoteDbPath}"
if ($LASTEXITCODE -ne 0) { Write-Error "SCP FAILED"; exit 1 }

Write-Output "=== Step 4: Starting remote service ==="
ssh "$ServerUser@$ServerHost" "systemctl start $RemoteServiceName"

Write-Output "=== ALL DONE ==="