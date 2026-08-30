# 消费行研 Agent 部署边界说明

本文档用于固定当前项目的两条部署线，避免后续误同步、误覆盖。

## 1. 开发 / 迭代线

这一条线继续用于项目开发、功能优化、GitHub 归档和个人公网测试。

- 本地项目：`C:\Users\chi\Documents\ChatGPT\New project`
- GitHub 仓库：`https://github.com/anning6189/fund-manager-workbench`
- 老公网：`http://47.95.254.215:8765`

约定：

- 本地修改可以提交到 GitHub。
- 本地修改可以部署到老公网。
- 本地数据库同步脚本默认只对接老公网。
- 老公网可以作为个人测试版、开发演示版或备用公网版本。

## 2. 独立交付线

这一条线是已经交付出去的独立公网版本。

- 新公网域名：`https://xiaofei.becoming.fund`
- 新公网服务器 IP：`47.94.233.5`
- 服务器目录：`/opt/fund-manager-workbench`

约定：

- 新公网不再从本地项目自动同步。
- 新公网不再从 GitHub 自动拉取更新。
- 新公网不再跟随老公网变化。
- 本地后续继续优化项目，不应自动影响新公网。
- 新公网只保留自己的 `.env`、数据库、定时任务、HTTPS、Nginx 和 systemd 服务配置。
- 如未来确实要更新新公网，必须明确说明“更新 xiaofei 独立交付线”，不要默认同步。

## 3. 当前同步关系

```text
开发/迭代线：
本地项目 ⇄ GitHub ⇄ 老公网 47.95.254.215:8765

独立交付线：
新公网 xiaofei.becoming.fund
独立运行，不跟随开发/迭代线自动变化
```

## 4. 操作提醒

- 不要把本地 `Sync-PublicDbToLocal.ps1` 的默认服务器改成 `47.94.233.5`。
- 不要把 `deploy/update-server.sh` 默认用于新公网。
- 不要在新公网服务器上执行来自 GitHub 的自动拉取更新流程。
- 不要把 `.env`、API Key、聚源 token 或系统大模型 Key 提交到 GitHub。
- 新公网需要继续保留自己的每日同步 timer，保证它独立更新数据。
