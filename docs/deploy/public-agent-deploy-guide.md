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
