# 消费行研 Agent / Fund Manager Workbench

一个面向大消费行业研究的公开 Web Agent 项目。它把消费行业的数据同步、每日晨报、股票推荐、研报库、单股走势、规则审计、自校准和公网部署整合到一个网页里，目标是做到：

> **默认打开即用；不接大模型也能完整运行；用户接入自己的模型 Key 后获得 AI 增强解释、问答、复盘和自校对能力。**

项目公网示例：

- [http://47.95.254.215:8765](http://47.95.254.215:8765)

> 免责声明：本项目仅用于研究流程、数据工程和 Agent 产品原型展示，不构成任何投资建议、交易建议或自动交易指令。

## 1. 项目做成了什么

这个项目最终产出是一个“消费行业研究 Web Agent”：

- 给基金经理/研究员每天看消费行业晨报；
- 自动维护消费股票池；
- 自动生成“每日主推清单”和“股票池看板”；
- 每只股票可点击查看走势、评级轨迹和证据链；
- 每日自动同步数据、自动评分、自动自检；
- 公网可访问，普通用户打开即可使用基础版；
- 用户可自行填写大模型 API Key，启用 AI 增强能力。

目前项目已形成完整闭环：

```text
数据源同步
  ↓
研究数据库 SQLite
  ↓
每日晨报 / 研报库 / 股票评分 / 热力图
  ↓
网页 Agent 展示
  ↓
规则审计 / 推荐快照 / 后验复盘
  ↓
自校准与规则自进化
```

## 2. 核心功能

### 2.1 每日晨报

- 展示消费行业每日核心观点；
- 包含宏观政策、重点研报、行业事件、风险提示；
- 支持历史日期回看；
- 显示数据截止日期、行情日期、评分日期，避免日期错位；
- 支持“AI晨报增强解读”：接入用户自己的模型 Key 后，可生成更像晨会口径的总结。

### 2.2 今日主推与股票池看板

股票推荐围绕“中期 / 中长期持有价值”展开，不再使用旧的“当日动量40% / 估值30% / 事件30%”短线打分。

当前看板分为：

| 分层 | 含义 | 当前定位 |
|---|---|---|
| 每日主推清单 | 当日最值得重点看的股票 | 最多 5 只 |
| 可以考虑买入 | 达到推荐基础，但未进入主推 | 25 只 |
| 等待买点 | 公司或逻辑不错，但买点还不够好 | 观察池 |
| 长期观察 | 长期好公司，但当前不一定适合买 | 长期池 |
| 暂不推荐 / 行业扫描 | 当前不推荐，仅保留行业覆盖 | 扫描池 |

每只股票展示：

- 投资分；
- 现价；
- 涨跌幅；
- 核心逻辑；
- 降级条件；
- 数据质量。

### 2.3 单股详情页

点击股票后可以查看：

- 1周 / 1月 / 3月 / 半年 / 1年走势；
- 区间高点、低点、样本交易日；
- 当前评级；
- 近一个月评价等级轨迹；
- 推荐证据链与风险复核；
- AI 个股解释。

走势图已做异常价格处理：过滤 0 价和孤立异常点，避免图形被错误数据拉坏。

### 2.4 研报库

- 按日期组织研报、新闻、政策和行业事件；
- 支持板块筛选；
- 支持历史回看；
- 每条内容可查看分点摘要和来源。

### 2.5 数据来源与同步状态

- 展示数据源目录；
- 展示同步状态、新鲜度、异常信息；
- 支持公网服务器每日自动同步；
- 当前规则是：**公网服务器是唯一自动同步源，本地只拉取公网数据库做调试查看**。

### 2.6 规则与审计

规则与审计模块展示：

- 当前荐股规则；
- 推荐后验表现；
- 样本数量；
- T+1 / T+5 平均收益；
- 胜率；
- 自动规则事件；
- 自检状态；
- 正式规则与影子规则；
- AI 复盘解释。

后验统计中，“每日主推清单”和“可以考虑买入”作为推荐表现；“等待买点 / 长期观察 / 暂不推荐”用于校验分层是否合理。

### 2.7 Agent 自校准与自进化

系统已经具备自动闭环：

1. 每日同步后自动自检；
2. 自动修复低风险数据问题；
3. 保存每日推荐快照；
4. 计算推荐后验表现；
5. 维护正式规则与影子规则；
6. 记录规则事件；
7. 接入大模型后可生成复盘解释和规则优化建议。

原则：

- 不需要人工批准；
- 但大模型不直接替代规则模型；
- 规则模型负责计算；
- 大模型负责解释、总结、复盘和质检。

## 3. 大模型增强设计

本项目采用“双层 Agent”结构：

```text
基础规则层
  ├─ 数据同步
  ├─ 股票评分
  ├─ 晨报
  ├─ 研报库
  ├─ 走势图
  ├─ 热力图
  └─ 规则审计

大模型增强层
  ├─ AI研究员问答
  ├─ AI晨报增强
  ├─ AI个股解释
  ├─ AI规则复盘
  └─ 后续规则自进化解释
```

默认情况下，不接入大模型也能完整使用。

用户可以在网页右上角“模型增强设置”中填写自己的模型配置：

- API Base URL；
- 模型名称；
- API Key；
- 服务商标识。

支持 OpenAI-compatible 接口，例如：

```text
https://api.openai.com/v1
https://api.deepseek.com/v1
https://dashscope.aliyuncs.com/compatible-mode/v1
```

安全设计：

- 用户 Key 保存在当前浏览器；
- 不写入项目数据库；
- 不提交 GitHub；
- 不覆盖服务器 `.env`；
- 页面只做脱敏展示；
- 点击 AI 增强按钮时才调用模型，避免自动消耗 token。

## 4. 技术栈

| 层级 | 技术 |
|---|---|
| 后端 | Python 原生 `http.server` |
| 前端 | 原生 HTML / CSS / JavaScript |
| 数据库 | SQLite |
| 数据同步 | Python 脚本 + 聚源 MCP + 官方来源采集 |
| 部署 | 阿里云 ECS + systemd |
| 定时任务 | systemd timer |
| 大模型 | 用户自带 OpenAI-compatible API |

项目刻意没有引入复杂框架，目的是方便部署、复现和二次开发。

## 5. 项目结构

```text
.
├─ apps/fund-manager-workbench/       # Web Agent 后端和前端
│  ├─ server.py                       # Python 服务端
│  └─ public/                         # 前端页面
├─ data/                              # 示例/运行研究数据
│  └─ curated/consumer-research.db    # 当前演示 SQLite 数据库
├─ deploy/                            # 公网部署与 systemd 服务
├─ docs/                              # 项目文档
│  ├─ design/                         # 股票推荐框架与 Agent 系统设计
│  ├─ deploy/                         # 本地/公网部署说明
│  └─ assets/demo-screenshots/        # 页面截图
├─ specs/                             # 数据源、模型、规则、产品规格
├─ sql/                               # 数据库建表与迁移 SQL
├─ tools/                             # 同步、评分、回填、自校准脚本
├─ tests/                             # 测试用例与验收报告
├─ .env.example                       # 环境变量模板，不含真实密钥
└─ README.md
```

## 6. 快速开始

### 6.1 本地运行

需要 Python 3.11+。

```bash
python apps/fund-manager-workbench/server.py --host 127.0.0.1 --port 8765
```

打开：

```text
http://127.0.0.1:8765/#today
```

### 6.2 配置数据源

复制环境变量模板：

```bash
cp .env.example .env
```

填写自己的聚源配置：

```text
GILDATA_MCP_URL=...
GILDATA_MCP_TOKEN=...
```

注意：

- `.env` 已被 `.gitignore` 忽略；
- 不要把真实 token 提交到 GitHub；
- 公开仓库只保留 `.env.example`。

### 6.3 手动同步数据

```bash
python tools/Invoke-DailyMorningBriefSync.py
```

单独运行股票评分：

```bash
python tools/consumer_stock_focus.py --historical-local
```

运行 Agent 自校准：

```bash
python tools/agent_self_calibration.py --backfill-days 30
```

### 6.4 从公网同步数据库到本地

当前推荐架构是公网服务器每日自动同步，本地只用于查看和调试：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/Sync-PublicDbToLocal.ps1
```

## 7. 公网部署

公网部署目录：

```text
/opt/fund-manager-workbench
```

核心服务：

```text
consumer-research.service
consumer-research-daily-sync.service
consumer-research-daily-sync.timer
```

部署脚本：

```text
deploy/consumer-research.service
deploy/consumer-research-daily-sync.service
deploy/consumer-research-daily-sync.timer
deploy/install-daily-sync.sh
deploy/update-server.sh
deploy/nginx-consumer-agent.conf
```

常用命令：

```bash
systemctl status consumer-research.service
systemctl status consumer-research-daily-sync.timer
systemctl restart consumer-research.service
```

详细说明见：

- [公网部署指南](docs/deploy/public-agent-deploy-guide.md)
- [本地同步到阿里云方案](docs/deploy/local-windows-sync-to-aliyun.md)

## 8. 重要文档

- [股票推荐思路](docs/design/股票推荐思路.md)
- [AutoInvest Agent 系统设计文档](docs/design/AutoInvest_Agent_系统设计文档.md)
- [基金经理工作台快速开始](docs/fund-manager-workbench-quick-start.md)
- [模块 5：基金经理用户界面](docs/module-5-fund-manager-user-interface.md)
- [Stage 8：研究工作流与 Agent 编排](docs/stage-8-research-workflow-and-agent-orchestration.md)

## 9. 数据与安全说明

本仓库包含演示数据库：

```text
data/curated/consumer-research.db
```

但不包含真实密钥：

```text
.env
*.log
data/curated/backups/
```

公开部署前建议自行补充：

- HTTPS；
- 访问限流；
- 日志脱敏；
- 数据授权边界说明；
- 服务器防火墙规则。

## 10. 当前版本亮点

- “规则 Agent + 大模型增强”的双层架构；
- 不依赖大模型也能完整运行；
- 用户自带 Key，避免站长承担 token 成本；
- 每日主推清单与股票池看板解耦；
- 更重视中期 / 中长期持有价值，而非短线动量；
- 单股支持走势、评级轨迹、证据链、AI解释；
- 规则审计支持推荐后验、胜率、平均收益和 AI复盘；
- 公网服务器自动同步，本地只做调试镜像；
- Agent 自校准具备自动自检、自动修复、快照、后验和规则事件。

## 11. GitHub 复现建议

克隆后：

```bash
git clone https://github.com/anning6189/fund-manager-workbench.git
cd fund-manager-workbench
python apps/fund-manager-workbench/server.py --host 127.0.0.1 --port 8765
```

然后打开：

```text
http://127.0.0.1:8765/#today
```

如果要接入自己的数据源，请配置 `.env`；如果只想看演示效果，可以直接使用仓库内随附的 SQLite 数据库。

