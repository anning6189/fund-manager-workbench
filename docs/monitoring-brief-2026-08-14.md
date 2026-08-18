# 消费行业每日研究监控简报

> 监控截止：2026-08-14 16:02:05 +08:00（Asia/Shanghai）
> 运行入口：tools/consumer_realtime_monitor.py
> 运行 ID：monitor:b1ffac25e0400a2baadd8415
> 用途：内部研究预警；不自动发布、不构成交易指令

## 与上日对比

| 指标 | 本次 | 上日（2026-08-13） | 变化 |
|---|---|---|---|
| 总警告数 | 41 | 54 | -13 |
| 严重 / 重要 / 关注 | 0 / 18 / 23 | 0 / 29 / 25 | -11 重要，-2 关注 |
| License gate 警告 | 0 | 7 | -7 |
| Stale 数据流 | 17 | 22 | -5 |
| 工作流积压 | 1 | 3 | -2 |
| 覆盖缺口 | 22 | 22 | 持平 |
| 重大事件 | 1 | 0 | +1 |

## 新增、升级或需人工处理的事项

### 1. 重大消费/宏观事件（important）
- **人民银行发布《2026年第二季度中国货币政策执行报告》**
  - 来源：CR.SRC.GILDATA.NEWS（聚源资讯舆情库）
  - 发布时间：2026-08-12 09:00 UTC
  - 入库时间：2026-08-14 01:15 UTC
  - 重要性：0.72 / important
  - 要点：6 月新发放贷款加权平均利率与个人住房贷款利率均降至 3.05%，创历史新低；报告新增强调关注人工智能对生产效率、成本与通胀的影响。
  - **建议任务**：触发 P0_06_EVENT_POLICY_IMPACT，评估低利率与 AI 主题对可选消费、耐用品、零售、服务各子行业的信贷环境与成本传导。

### 2. 许可闸门状态更新（已放行）
- 7 个 Gildata 商业数据源已于 2026-08-14 02:49 由项目负责人确认授权，license gate 警报全部清除：
  - CR.SRC.GILDATA.ANNOUNCEMENT
  - CR.SRC.GILDATA.ENTERPRISE
  - CR.SRC.GILDATA.FINQUERY
  - CR.SRC.GILDATA.MACRO_INDUSTRY
  - CR.SRC.GILDATA.NEWS
  - CR.SRC.GILDATA.RESEARCH
  - CR.SRC.GILDATA.STOCK_UNIVERSE
- 授权范围：内部研究使用（knowledge_base、research_fact_layer、internal_research_output），不授予再分发权；对外发布仍需另行授权。

### 3. 数据新鲜度改善
- 5 条数据流从 stale 转为 fresh：
  - CR.SRC.GILDATA.ENTERPRISE / enterprise_risk
  - CR.SRC.GILDATA.FINQUERY / financials
  - CR.SRC.GILDATA.MACRO_INDUSTRY / macro
  - CR.SRC.GILDATA.STOCK_UNIVERSE / master_data
  - CR.SRC.NBS / macro

### 4. 工作流积压清理
- 2 个因 `pending_human_review` 产生的 CR.MON.WORKFLOW.BACKLOG 警报已被监控引擎自动清理。

### 5. 仍需人工处理
- **覆盖刷新**：11 个消费子行业 A 股仍为 `definitions_only`，港股未 populated。虽然 Gildata 授权已确认，但 `research_coverage_status` 表自 2026-08-13 06:35 后未刷新，blockers_json 仍显示 `source_license_pending`。建议执行覆盖刷新以更新 blocker 状态。
- **阻塞工作流**：Stage8 比较工作流 `wr:682a85e1ac4156c4a2d133ba`（package CR.WORKFLOW.PKG.STAGE8.COMPARE.001）仍为 blocked，需人工诊断阻塞原因。
- **数据生产跟进**：6 条 stale 流、11 条未启动流需数据生产侧处理（详见附录）。
- **每日简报同步异常**：日志记录到一次 "IndexError no such group" 询价失败，需修复解析逻辑。

## 数据缺口与许可闸门如实披露

- **数据新鲜度**：22 条监控流中，5 条 fresh、6 条 stale、11 条 not_started。系统状态为 `partially_complete`。
- **研究覆盖**：11 个消费子行业（CR.D.AF/AP/AU/HL、CR.S.FB/PH/PT、CR.V.CE/FS/RT/TL）在 A 股仅完成指标定义，未 populated 实际数据；港股未建立 universe。
- **许可闸门**：Gildata 商业数据源已获内部研究授权；官方数据源为 `public_official`。当前无 pending license。
- **工作流积压**：1 个 blocked，0 个 pending_human_review。

## 边界声明

- 未使用或推断基金持仓、仓位。
- 未自动生成交易指令。
- 未自动发布正式研究报告。
- 所有产出为内部研究信号，数据缺口与许可闸门如实披露。

## 附录：当前 stale / not_started 数据流

### Stale（6 条）
- CR.SRC.CNINFO / financials（最新 2024-04-30，滞后约 20056 小时）
- CR.SRC.GILDATA.ANNOUNCEMENT / announcements（最新 2026-08-13，滞后 32 小时）
- CR.SRC.GILDATA.FINQUERY / market_daily（最新 2026-08-13，滞后 16 小时）
- CR.SRC.GILDATA.NEWS / news_leads（最新 2026-08-13，滞后 16 小时）
- CR.SRC.GILDATA.RESEARCH / research_metadata（最新 2026-08-10，滞后 88 小时）
- CR.SRC.PBOC / macro（最新 2026-08-12，滞后 40 小时）

### Not started（11 条）
- CR.SRC.BSE / announcements
- CR.SRC.CNINFO / announcements
- CR.SRC.CUSTOMS / official_industry_releases
- CR.SRC.GOVCN / official_policy_documents
- CR.SRC.MCT / official_industry_releases
- CR.SRC.MIIT / official_industry_releases
- CR.SRC.MOFCOM / official_industry_releases
- CR.SRC.NDRC / official_policy_documents
- CR.SRC.SAMR / official_policy_documents
- CR.SRC.SSE / announcements
- CR.SRC.SZSE / announcements

## 产物位置

- 机器可读简报：data/monitoring/module3-realtime-research/2026-08-14/monitor-b1ffac25e0400a2baadd8415/monitoring-brief.json
- Markdown 简报：data/monitoring/module3-realtime-research/2026-08-14/monitor-b1ffac25e0400a2baadd8415/monitoring-brief.md
