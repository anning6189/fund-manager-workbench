[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8765)

$Listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $Listeners) {
    Write-Output "端口 $Port 没有正在运行的研究工作台。"
    exit 0
}
foreach ($ProcessId in ($Listeners.OwningProcess | Select-Object -Unique)) {
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($Process -and $Process.ProcessName -match 'python|consumer-research-workbench') {
        Stop-Process -Id $ProcessId
        Write-Output "研究工作台已停止。"
    } else {
        throw "端口 $Port 被其他程序占用，未执行停止操作。"
    }
}
