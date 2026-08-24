# Fund Manager Workbench / 消费行研 Agent

一个面向大消费行业研究的公开 Web Agent 原型。项目目标是把研究能力做成“任何人打开即用”的应用：从数据采集、每日晨报、研报库、股票池看板、单股走势、规则审计，到公网部署、每日自动同步与 Agent 自校准，均在一个网页界面中完成闭环。

> 免责声明：本项目仅用于研究流程、数据工程和 Agent 产品原型展示，不构成任何投资建议或交易指令。

## 项目定位

- 作用对象：研究员、投研运营、基金经理。
- 核心价值：
  - 每日自动生成消费行业晨报与重点研究提示；
  - 消费股票池按中期 / 中长期持有价值自动分层；
  - 单股走势、评价等级轨迹和证据链可追溯；
  - 数据源新鲜度、质量状态和同步状态可视化；
  - Agent 自动自检、自动修复、自动保存推荐快照与后验结果；
  - 支持本地运行，也支持阿里云公网部署。

## 当前能力

- 每日消费行研晨报：宏观政策、重点研报、行业事件、风险提示。
- 今日主推与股票池看板：按“可考虑买入 / 等待买点 / 长期观察 / 暂不推荐”分层展示。
- 单股走势：支持 1周、1月、3月、半年、1年切换，并自动过滤 0 价和孤立异常行情点。
- 研报库：按日期、板块、重要程度组织消费相关研报/新闻/政策。
- 数据来源页：展示来源目录、同步状态和数据口径。
- 规则与审计：展示荐股规则、数据质量、Agent 自校准状态。
- Agent 自校准：自动自检、自动修复、推荐快照、后验结果、影子规则和规则事件。
- 公网部署：支持阿里云服务器 + systemd 定时同步。

## 技术栈

- Python 后端：原生 `http.server`
- 数据库：SQLite
- 前端：原生 HTML / CSS / JavaScript
- 部署：Linux systemd / Nginx 可选
- 数据源：聚源 MCP / 本地研究数据库 / 官方来源采集脚本

## 项目结构

```text
apps/fund-manager-workbench/      # Web 工作台后端与静态前端
tools/                            # 数据同步、荐股评分、自校准、回填脚本
deploy/                           # Linux/systemd 部署脚本
docs/                             # 部署与运行说明
data/curated/consumer-research.db # 当前研究数据库（本仓库随附）
specs/                            # 数据/连接器/规则规格
```

## 本地运行

需要 Python 3.11+。

```bash
python apps/fund-manager-workbench/server.py --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765/#today
```

## 数据源配置

仓库不包含真实数据源密钥。复制 `.env.example` 后填入自己的配置：

```bash
cp .env.example .env
```

核心变量：

```text
GILDATA_MCP_URL=...
# 或
GILDATA_MCP_TOKEN=...
LOCAL_LLM_URL=http://127.0.0.1:15721/v1/chat/completions
```

Windows PowerShell 示例：

```powershell
$env:GILDATA_MCP_TOKEN="你的聚源 token"
python tools/Invoke-DailyMorningBriefSync.py
```

Linux 示例：

```bash
export GILDATA_MCP_TOKEN="你的聚源 token"
python3 tools/Invoke-DailyMorningBriefSync.py
```

## 每日同步

手动同步：

```bash
python tools/Invoke-DailyMorningBriefSync.py
```

单独运行股票评分：

```bash
python tools/consumer_stock_focus.py
```

单独运行 Agent 自校准：

```bash
python tools/agent_self_calibration.py
```

回填最近 30 个评级日的自校准快照与后验：

```bash
python tools/agent_self_calibration.py --backfill-days 30
```

## 阿里云部署

部署脚本在 `deploy/` 目录。典型流程：

```bash
cd /opt/fund-manager-workbench
python3 apps/fund-manager-workbench/server.py --host 0.0.0.0 --port 8765
```

systemd 服务与每日定时器：

```text
deploy/consumer-research.service
deploy/consumer-research-daily-sync.service
deploy/consumer-research-daily-sync.timer
deploy/install-daily-sync.sh
deploy/update-server.sh
```

详细说明见：

```text
docs/deploy/public-agent-deploy-guide.md
docs/deploy/local-windows-sync-to-aliyun.md
```

## Agent 自校准设计

自校准系统不是人工审批，而是全自动闭环：

1. 每日同步后自动自检；
2. 自动清理低风险数据问题；
3. 保存每日推荐快照；
4. 计算推荐后验表现；
5. 维护正式规则与影子规则；
6. 记录规则事件；
7. 后续可扩展为自动灰度、自动晋级、自动回滚。

相关表：

```text
agent_self_audit_runs
agent_recommendation_snapshots
agent_recommendation_outcomes
agent_rule_versions
agent_rule_events
```

## 公开仓库注意事项

- `.env`、日志、备份库不会上传。
- `data/curated/consumer-research.db` 是当前演示数据库，已随仓库保留。
- 若你使用自己的数据源，请自行确认数据授权与再分发边界。
- 公开部署前建议自行配置 HTTPS、访问控制和限流。
