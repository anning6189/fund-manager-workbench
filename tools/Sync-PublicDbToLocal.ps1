param(
    [string]$ServerHost = "47.95.254.215",
    [string]$ServerUser = "root",
    [string]$ServerAppDir = "/opt/fund-manager-workbench"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LocalDbPath = Join-Path $ProjectRoot "data\curated\consumer-research.db"
$BackupDir = Join-Path $ProjectRoot "data\curated\backups"
$TempDbPath = Join-Path $BackupDir "consumer-research.public-download.tmp.db"
$RemoteDbPath = "$ServerAppDir/data/curated/consumer-research.db"

if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
    Write-Error "scp not found. Please install/enable OpenSSH Client first."
    exit 1
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

if (Test-Path $LocalDbPath) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupPath = Join-Path $BackupDir "consumer-research.local-before-public-sync-$timestamp.db"
    Copy-Item -LiteralPath $LocalDbPath -Destination $backupPath -Force
    Write-Output "Backup created: $backupPath"
}

Write-Output "Pulling public DB from ${ServerUser}@${ServerHost}:${RemoteDbPath}"
Remove-Item -LiteralPath $TempDbPath -Force -ErrorAction SilentlyContinue
scp -C "${ServerUser}@${ServerHost}:${RemoteDbPath}" "$TempDbPath"
if ($LASTEXITCODE -ne 0) {
    Write-Error "SCP failed. Local DB backup is preserved."
    exit 1
}

Move-Item -LiteralPath $TempDbPath -Destination $LocalDbPath -Force
Write-Output "Local DB is now aligned with public server DB."
