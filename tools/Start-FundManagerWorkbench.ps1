[CmdletBinding()]
param(
    [string]$UserName = $env:CONSUMER_RESEARCH_USER,
    [ValidateSet('public_fund_manager','research_analyst','research_operator')]
    [string]$Role = 'public_fund_manager',
    [ValidateRange(1024,65535)]
    [int]$Port = 8765,
    [ValidateSet('127.0.0.1','localhost','0.0.0.0')]
    [string]$BindHost = '127.0.0.1',
    [ValidateSet('local_single_user','internal_network')]
    [string]$DeploymentMode = 'local_single_user',
    [string[]]$AllowHosts = @(),
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ServerPath = Join-Path $ProjectRoot 'apps\fund-manager-workbench\server.py'
$DbPath = Join-Path $ProjectRoot 'data\curated\consumer-research.db'
$DataRoot = Join-Path $ProjectRoot 'data\workbench\module5-fund-manager'

if ([string]::IsNullOrWhiteSpace($UserName)) {
    $UserName = [Environment]::UserName
}
if ($UserName.ToLowerInvariant() -in @('anonymous','agent','ai','unassigned')) {
    throw '必须使用具名内部用户启动研究工作台。'
}

$RuntimeCandidates = @(
    (Join-Path $ProjectRoot 'runtime\python\python.exe'),
    'C:\Users\chi\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe',
    (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
    (Get-Command py -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $RuntimeCandidates) {
    throw '未找到本机运行组件。请使用正式交付包，或联系系统管理员完成安装。'
}

$Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
$DisplayHost = if ($BindHost -eq '0.0.0.0') { '127.0.0.1' } else { $BindHost }
$CurrentUrl = "http://${DisplayHost}:${Port}/"
if ($Listener) {
    if (-not $NoBrowser) { Start-Process $CurrentUrl }
    Write-Output "研究工作台已在运行：$CurrentUrl"
    exit 0
}

$Arguments = @(
    "`"$ServerPath`"",
    '--host',$BindHost,
    '--port',"$Port",
    '--db',"`"$DbPath`"",
    '--data-root',"`"$DataRoot`"",
    '--user-name',"`"$UserName`"",
    '--role',$Role,
    '--deployment-mode',$DeploymentMode
)
if ($AllowHosts.Count -gt 0) {
    $Arguments += '--allow-hosts'
    $Arguments += (($AllowHosts | ForEach-Object { $_.Trim() }) -join ',')
}
if (-not $NoBrowser) { $Arguments += '--open' }

$Process = Start-Process -FilePath $RuntimeCandidates[0] -ArgumentList $Arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
$Deadline = (Get-Date).AddSeconds(20)
$Healthy = $false
while ((Get-Date) -lt $Deadline) {
    if ($Process.HasExited) { throw "研究工作台启动失败，退出码：$($Process.ExitCode)" }
    try {
        $Health = Invoke-RestMethod -Uri "http://${DisplayHost}:${Port}/api/health" -TimeoutSec 1
        if ($Health.status -eq 'ok') { $Healthy = $true; break }
    } catch {
        Start-Sleep -Milliseconds 300
    }
}
if (-not $Healthy) { throw '研究工作台未在20秒内完成启动。' }
Write-Output "研究工作台已启动：$CurrentUrl"
