# 本地 Windows 自动同步到阿里云

当前阿里云服务器不能稳定访问聚源 MCP，所以每日同步必须在本地 Windows 执行，再把更新后的数据库推送到服务器。

## 流程

1. 本地 Windows 执行 `tools/Invoke-DailyMorningBriefSync.py`
2. 本地 Windows 用 `scp` 推送 `data/curated/consumer-research.db`
3. 本地 Windows 用 `ssh` 重启服务器上的 `consumer-research.service`

服务器路径按当前部署：

```text
/opt/fund-manager-workbench
```

## 前置条件

本地 Windows 需要能执行：

```powershell
ssh root@47.95.254.215 "echo ok"
scp "data\curated\consumer-research.db" root@47.95.254.215:/tmp/consumer-research.db
```

如果每次都要求输入密码，先配置 SSH 免密。

## 手工跑一次

在本地项目根目录执行：

```powershell
.\tools\Invoke-LocalSyncAndDeploy.ps1 `
  -ServerHost "47.95.254.215" `
  -ServerUser "root" `
  -ServerAppDir "/opt/fund-manager-workbench" `
  -RemoteServiceName "consumer-research.service"
```

只推送当前数据库，不重新跑聚源同步：

```powershell
.\tools\Invoke-LocalSyncAndDeploy.ps1 -SkipSync
```

## 注册 Windows 每日任务

```powershell
.\tools\Register-LocalDailySyncTask.ps1 `
  -ServerHost "47.95.254.215" `
  -ServerUser "root" `
  -ServerAppDir "/opt/fund-manager-workbench" `
  -RemoteServiceName "consumer-research.service" `
  -DailyTime "08:30"
```

立即测试任务：

```powershell
Start-ScheduledTask -TaskName "ConsumerResearchLocalSyncDeploy"
```

查看任务：

```powershell
Get-ScheduledTask -TaskName "ConsumerResearchLocalSyncDeploy"
```

查看本地同步部署日志：

```powershell
Get-Content "data\monitoring\module3-realtime-research\local-sync-deploy.log" -Tail 80
```

## 验证公网已更新

```powershell
Invoke-RestMethod "http://47.95.254.215:8765/api/bootstrap"
Invoke-RestMethod "http://47.95.254.215:8765/api/research-library"
```

如果 `monitor.completed_at` 或研报库日期仍停在旧日期，优先查本地日志和服务器服务状态：

```powershell
ssh root@47.95.254.215 "systemctl status consumer-research.service --no-pager -l"
```
