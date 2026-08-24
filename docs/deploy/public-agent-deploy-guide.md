# 公开发布部署指南（消费行研 Agent）

目标：让任何人打开网页即可体验，先做演示可用、后续可控放量。

## 一、服务启动参数（已对齐本项目）

在生产主机上启动（Windows/Linux 均可，示例为 Windows PowerShell）：

```powershell
cd "C:\Users\chi\Documents\ChatGPT\New project"
.\tools\Start-FundManagerWorkbench.ps1 `
  -Host 0.0.0.0 `
  -Port 8765 `
  -DeploymentMode internal_network `
  -AllowHosts "example.com,api.example.com"
```

说明：
- `0.0.0.0`：监听公网网卡（反向代理再限制来源）
- `internal_network`：服务端写入的部署口径
- `-AllowHosts`：`X-Forwarded` 下仍要匹配的 Host 白名单；`*` 表示全放行（不推荐生产）
- 前端静态页依旧走同一个服务，不需要额外配置 API 根路径

## 二、Linux（Ubuntu）Nginx 反向代理 + HTTPS

1. 下载安装 Nginx 与 Certbot

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
```

2. 部署配置文件（替换 `example.com`）

```bash
sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
sudo cp deploy/nginx-consumer-agent.conf /etc/nginx/sites-available/consumer-agent.conf
sudo sed -i "s/__DOMAIN__/example.com/g" /etc/nginx/sites-available/consumer-agent.conf
sudo ln -sfn /etc/nginx/sites-available/consumer-agent.conf /etc/nginx/sites-enabled/consumer-agent.conf
sudo nginx -t && sudo systemctl reload nginx
```

3. 证书签发与自动续期（Let's Encrypt）

```bash
sudo certbot --nginx -d example.com -d www.example.com
sudo systemctl status certbot.timer
```

如果你的证书路径不同，按实际路径更新 Nginx 配置中的证书文件位置。

4. 可选：启用 API Basic Auth（演示阶段可先上，生产可接登录系统）

```bash
sudo apt install -y apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd agentuser
```

取消注释 `consumer-agent.conf` 中：
- `include /etc/nginx/snippets/agent-basic-auth.conf;`

并在该 snippet 增加：

```nginx
auth_basic "Restricted";
auth_basic_user_file /etc/nginx/.htpasswd;
```

5. 重载 Nginx

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 三、防火墙与口令/端口策略

建议只开放：
- `80`/`443` 给外网
- `8765` 只允许本机或反向代理访问（尽量不要公网暴露）

```bash
# Ubuntu(UFW)
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny 8765/tcp
sudo ufw reload
```

## 四、应用层“限流 + 反垃圾”建议（先上即可）

- 已内置：Nginx 的 `consumer_per_ip`（每 IP 每分钟 20 次）和 `consumer_burst`（高峰 burst）策略
- 对 AI 问答接口已单独提高超时并设置更严格并发限制
- 保留页面 CSRF token 机制（写操作仍需 `X-Workbench-Token`）
- 首发建议：
  - 加上 IP 白名单（如你公司办公网段）
  - 打开访问日志并按分钟采样告警（超过请求失败率/请求耗时自动降级）

## 五、Windows 机本机内网演示（可选）

如果临时只做内网演示，不需要 Nginx，直接启动：

```powershell
.\tools\Start-FundManagerWorkbench.ps1 -Host 127.0.0.1
```

或绑定内网地址：

```powershell
.\tools\Start-FundManagerWorkbench.ps1 -Host 0.0.0.0 -AllowHosts "10.1.10.20,10.1.10.21"
```

## 六、上线验证清单（1 分钟）

1. `https://example.com/` 页面可打开  
2. `curl -I https://example.com/api/health` 返回 `200`  
3. 在控制台能看到 `today/#ask` 正常交互  
4. 80 端口自动跳转到 443  
5. 尝试高并发 30 次 `/api/ask`，超过限制时返回 429（速率限制生效）

## 七、上线后更新代码与每日数据同步

公网页面看不到本地新功能，通常不是浏览器问题，而是服务器没有拉取最新代码，或拉完后没有重启服务。公网页面看不到每日数据，通常是公网服务器没有拿到最新 `consumer-research.db`。

当前服务器实际目录：

```text
/opt/fund-manager-workbench
```

当前阿里云服务器已验证可以访问聚源 MCP 与官方公开源，因此正式方案采用“服务器自同步”：服务器每天定时运行 `tools/Invoke-DailyMorningBriefSync.py`，同步完成后自动重启网页服务；不依赖本地 Windows 开机。

### 7.1 安装 systemd 服务

假设项目部署在 `/opt/fund-manager-workbench`：

```bash
cd /opt/fund-manager-workbench
sudo cp deploy/consumer-research.service /etc/systemd/system/consumer-research.service
sudo systemctl daemon-reload
sudo systemctl enable --now consumer-research
sudo systemctl status consumer-research --no-pager -l
```

### 7.2 安装每日同步任务

推荐用一键脚本安装：

```bash
cd /opt/fund-manager-workbench
chmod +x deploy/install-daily-sync.sh
./deploy/install-daily-sync.sh
```

这个脚本会：
- 设置服务器时区为 `Asia/Shanghai`（失败不阻断）
- 安装 `consumer-research-daily-sync.service`
- 安装 `consumer-research-daily-sync.timer`
- 启用每周一至周五 `08:40`（北京时间）自动同步
- 显示下一次执行时间

如果要手工安装，也可以执行：

```bash
cd /opt/fund-manager-workbench
sudo cp deploy/consumer-research-daily-sync.service /etc/systemd/system/consumer-research-daily-sync.service
sudo cp deploy/consumer-research-daily-sync.timer /etc/systemd/system/consumer-research-daily-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now consumer-research-daily-sync.timer
systemctl list-timers consumer-research-daily-sync.timer
```

手工立即同步一次：

```bash
sudo systemctl start consumer-research-daily-sync.service
sudo journalctl -u consumer-research-daily-sync.service -n 120 --no-pager
tail -n 120 /opt/fund-manager-workbench/data/monitoring/module3-realtime-research/server-daily-sync.log
```

同步服务里已经配置 `ExecStartPost=/bin/systemctl try-restart consumer-research.service`，同步完成后会自动尝试重启网页服务；如果网页服务名称不是 `consumer-research.service`，需要改 `deploy/consumer-research-daily-sync.service` 里的这一行。

### 7.3 更新服务器代码并重启

每次本地功能改完并推送到 GitHub 后，在服务器执行：

```bash
cd /opt/fund-manager-workbench
chmod +x deploy/update-server.sh
BRANCH=main ./deploy/update-server.sh
```

如果你的默认分支不是 `main`，改成实际分支名。

### 7.4 排查公网与本地不一致

```bash
curl -s http://127.0.0.1:8765/api/health
curl -s http://127.0.0.1:8765/api/ops-status | head -c 1200
curl -s http://127.0.0.1:8765/api/bootstrap | head -c 800
curl -s http://127.0.0.1:8765/api/data-sources | head -c 1200
curl -s http://127.0.0.1:8765/app.js | sha256sum
git rev-parse --short HEAD
systemctl status consumer-research --no-pager -l
systemctl list-timers consumer-research-daily-sync.timer
```

判断标准：
- `/api/ops-status.status` 如果不是 `ok`，先看 `checks` 和 `last_failure`。
- `rating_date`、`market_date`、`brief_date` 不一致时，页面首页会显示黄色状态卡；说明不能把旧行情当成当日结论。
- `/app.js` 的 hash 如果和本地不同，说明服务器代码没更新或服务没重启。
- `consumer-research.service` 如果不是从 `/opt/fund-manager-workbench/apps/fund-manager-workbench/server.py` 启动，说明访问的是旧部署目录。

### 7.5 正式交付检查

交付前必须逐项通过：

- `systemctl is-active consumer-research.service` 返回 `active`
- `systemctl list-timers consumer-research-daily-sync.timer` 能看到下一次 08:40 执行时间
- `/api/ops-status` 显示自动同步、日期一致性、股票看板检查
- `/api/stock-focus` 的 `date` 与 `market_date` 是最近交易日
- 首页状态卡无红色异常；若黄色，需要能解释具体原因
- 单只股票详情能展示走势、近一个月评价等级轨迹、推荐证据链与风险复核
- 敏感页面和接口（例如 Token 用量）不在公网暴露
- 每次上线前创建 `/opt/fund-manager-workbench/backups/<timestamp>` 回滚备份
