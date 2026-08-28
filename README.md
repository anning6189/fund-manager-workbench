# 消费行研 Agent / Fund Manager Workbench

一个面向大消费行业研究的公开 Web Agent。项目把消费行业数据同步、每日晨报、股票推荐、研报库、单股走势、规则审计、自校准、AI基金经理模拟组合和公网部署整合到一个网页里。

公网示例：

- [http://47.95.254.215:8765](http://47.95.254.215:8765)

> 免责声明：本项目仅用于研究流程、数据工程和 Agent 产品原型展示，不构成投资建议、交易建议、真实基金持仓或自动交易指令。

## 项目目标

本项目的目标是做一个“任何人打开即用”的消费行业研究 Agent：

- 默认不依赖大模型，也能完成数据同步、股票评分、看板展示、走势查看和规则审计；
- 用户可自行填写 OpenAI-compatible 模型 Key，获得 AI研究员问答、个股解释、晨报增强和规则复盘增强；
- 站长可在服务器配置内部大模型 Key，驱动 AI基金经理的自动策略说明；
- 公网服务器独立自动运行，本地电脑不开机也不影响每日更新。

整体链路：

```text
公开/商业数据源
  ↓
每日自动同步任务
  ↓
SQLite 研究数据库
  ↓
规则评分 / 股票分层 / 事件归因 / 自检
  ↓
Web Agent 页面
  ↓
后验复盘 / 自校准 / AI基金经理模拟组合
```

## 已实现功能

### 1. 每日晨报

- 展示消费行业每日核心观点；
- 包含政策、研报、行业事件、风险提示；
- 支持历史日期回看；
- 显示研究截止日期、评级日期、行情日期；
- 对数据新鲜度、同步异常和日期错位做页面提示。

### 2. 今日主推与股票池看板

股票推荐围绕“中期 / 中长期持有价值”展开，不以一两日短线博弈为目标。

当前分层：

| 分层 | 数量口径 | 含义 |
|---|---:|---|
| 每日主推清单 | ≤ 5 只 | 当日最值得重点看的股票 |
| 可以考虑买入 | 25 只 | 达到推荐基础，但未进入主推 |
| 等待买点 | 动态 | 公司或逻辑不错，但买点不够好 |
| 长期观察 | 动态 | 长期好公司，当前不一定适合买 |
| 暂不推荐 / 行业扫描 | 动态 | 暂不推荐，仅保留行业覆盖和监控 |

每只股票展示：

- 投资分；
- 现价；
- 涨跌幅；
- 核心理由；
- 当前分层；
- 数据质量；
- 近一个月评级轨迹。

### 3. 单股详情

点击股票可以查看：

- 1周 / 1月 / 3月 / 半年 / 1年走势；
- 区间涨跌、区间高低点、样本交易日；
- 当前评级；
- 近一个月每天所处评价等级；
- 相关事件和数据口径提示。

### 4. 研报库

- 按日期归档消费相关研报、新闻、政策和行业数据；
- 支持按重要程度分类；
- 支持来源回链和数据新鲜度提示；
- 用于支撑每日晨报和股票推荐逻辑。

### 5. 数据来源

展示各数据流状态，包括：

- 消费新闻；
- 消费研报；
- 企业风险；
- 官方政策；
- 官方行业数据；
- 行情数据；
- 股票评级；
- 自动同步日志。

### 6. 规则与审计

用于解释 Agent 为什么这么推荐、推荐效果如何。

已包含：

- 推荐规则说明；
- 后验表现统计；
- T+1 / T+5 收益；
- 胜率；
- 推荐快照保存；
- 自检与自动修复；
- 规则自进化与后验表现。

后验统计中，“每日主推清单”和“可以考虑买入”视为推荐表现；“等待买点 / 长期观察 / 暂不推荐”用于校验分层是否合理。

### 7. AI研究员增强

用户可以在网页右上角设置里填写自己的 OpenAI-compatible API：

- API Base URL；
- API Key；
- 模型名称；
- 启用 / 关闭 AI 增强；
- 测试连接。

安全口径：

- 用户 Key 只保存在当前浏览器；
- 不写入项目数据库；
- 不提交 GitHub；
- 只在测试连接或问答时临时转发给后端。

### 8. AI基金经理

AI基金经理是一个独立页面，位于“首页”和“AI研究员”之间。

它是一个全自动模拟组合模块：

- 每周自动生成一版 30 只消费股模拟持仓；
- 根据投资分、分层、风险、行业分散度和换手约束自动分配权重；
- 计算扣费后净值；
- 计算成立以来收益、年化收益率、最大回撤、日胜率、模拟换手率；
- 模拟交易成本；
- 展示当前 30 只持仓；
- 支持点击持仓股票查看单股走势；
- 展示本周策略与相比上一版的优化；
- 支持历史组合复盘。

AI基金经理净值对比基准：

- 沪深300：`000300.SH`
- 中证消费指数：`000990.SH`
- 800消费指数：`000932.SH`

AI基金经理使用站长在服务器配置的内部模型 Key；AI研究员问答使用用户自己的 Key。两套模型通道隔离。

## 自动更新机制

正式口径：公网服务器是唯一自动同步源，本地只从公网同步数据库用于查看和调试。

公网 systemd 定时任务：

| 任务 | 时间 | 作用 |
|---|---:|---|
| `consumer-research-daily-sync.timer` | 交易日 08:40 | 同步晨报、研报、事件、股票评级等早盘数据 |
| `consumer-research-close-sync.timer` | 交易日 16:10 | 同步收盘行情并刷新评级 |
| 收盘重试任务 | 16:30、17:00、之后每半小时 | 如果行情覆盖不足，自动重试直到成功 |
| `consumer-research-ai-fund-weekly.timer` | 每周一 09:20 | 生成 AI基金经理周度持仓快照 |

本地同步公网数据库：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\Sync-PublicDbToLocal.ps1
```

## 技术栈

- Python 标准库 HTTP 服务；
- SQLite 本地研究数据库；
- 原生 HTML / CSS / JavaScript 前端；
- systemd 定时任务；
- 阿里云公网服务器；
- 聚源数据接口；
- 东方财富指数行情接口；
- OpenAI-compatible 大模型接口。

## 项目结构

```text
apps/fund-manager-workbench/
  server.py                 # Web Agent 后端服务
  public/
    index.html              # 单页应用入口
    app.js                  # 前端交互
    styles.css              # 页面样式

data/
  curated/consumer-research.db              # 研究数据库
  workbench/module5-fund-manager/           # Web 工作台数据

tools/
  Invoke-DailyMorningBriefSync.py           # 每日早盘同步
  Invoke-CloseSync.py                       # 收盘同步与重试
  Sync-IndexBenchmarks.py                   # 三个公开指数基准同步
  Generate-AiFundWeeklySnapshot.py          # AI基金经理周度快照
  Sync-PublicDbToLocal.ps1                  # 本地从公网同步数据库
  Start-FundManagerWorkbench.ps1            # 本地启动

deploy/
  consumer-research.service
  consumer-research-daily-sync.*
  consumer-research-close-sync.*
  consumer-research-ai-fund-weekly.*
  install-daily-sync.sh
  nginx-consumer-agent.conf

docs/
  design/                                   # 股票推荐与系统设计文档
  deploy/                                   # 部署文档
```

## 本地运行

进入项目目录：

```powershell
cd "C:\Users\chi\Documents\ChatGPT\New project"
```

启动本地服务：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\Start-FundManagerWorkbench.ps1
```

访问：

- [http://127.0.0.1:8765/#today](http://127.0.0.1:8765/#today)
- [http://127.0.0.1:8765/#fund](http://127.0.0.1:8765/#fund)

## 服务器部署要点

服务器目录：

```bash
/opt/fund-manager-workbench
```

环境变量放在服务器 `.env`，不要提交 GitHub：

```bash
GILDATA_MCP_TOKEN=你的聚源token
SYSTEM_LLM_PROVIDER=openai-compatible
SYSTEM_LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
SYSTEM_LLM_MODEL=glm-4-flash
SYSTEM_LLM_API_KEY=你的站长内部模型key
```

安装/更新自动任务：

```bash
cd /opt/fund-manager-workbench
bash deploy/install-daily-sync.sh
systemctl restart consumer-research.service
```

查看任务：

```bash
systemctl list-timers 'consumer-research*' --all --no-pager
systemctl status consumer-research.service --no-pager
```

## 数据与安全

- `.env` 不提交 GitHub；
- 不在日志中打印真实 token；
- 用户自己的模型 Key 不进入数据库；
- 公网服务默认允许游客查看；
- 写操作使用页面会话 token；
- 数据库可用于演示和复现，但应在公开前确认不包含敏感凭据。

## 交付状态

当前版本已经具备：

- 公网访问；
- 每日自动更新；
- 收盘自动补同步和重试；
- 股票推荐看板；
- 单股走势图和评级轨迹；
- 研报库；
- 数据来源监控；
- 规则审计；
- AI研究员用户 Key 增强；
- AI基金经理模拟组合；
- AI基金经理内部模型增强；
- GitHub 复现文档和部署脚本。

建议后续迭代方向：

- 接入更权威的 P0 财务/一致预期数据源；
- 增加更完整的行业指数和子行业基准；
- 扩展 AI基金经理的历史回测区间；
- 增加更细的规则版本对比；
- 增加 HTTPS、域名和访问限流。
