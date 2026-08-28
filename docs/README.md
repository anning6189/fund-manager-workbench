# 文档目录说明

本目录保留项目交付与后续维护需要的文档，已移除早期临时报告、调试脚本、运行日志和测试数据库。

## 核心设计

- `design/股票推荐思路.md`：消费股票中期/中长期推荐框架、分层逻辑、评分与组合约束。
- `design/AutoInvest_Agent_系统设计文档.md`：全自动运行、数据同步、评级、复盘、自进化与模型增强的系统设计。

## 部署与运行

- `deploy/public-agent-deploy-guide.md`：阿里云公网部署说明。
- `deploy/local-windows-sync-to-aliyun.md`：历史本地同步方案说明。当前正式口径为“公网服务器自动同步，本地只镜像公网数据库”。
- `fund-manager-workbench-quick-start.md`：本地启动和快速体验说明。

## 模块说明

- `module-1-data-production-and-full-backfill.md`：数据生产与回填。
- `module-2-full-consumer-research-coverage.md`：消费股票池覆盖。
- `module-3-realtime-research-and-continuous-monitoring.md`：实时研究与持续监控。
- `module-4-product-grade-research-task-library.md`：研报库与任务库。
- `module-5-fund-manager-user-interface.md`：基金经理工作台界面。

## 阶段规格

- `stage-3-consumer-domain-model.md`
- `stage-4-research-and-evidence-standard.md`
- `stage-5-data-source-integration.md`
- `stage-6-knowledge-base-and-warehouse.md`
- `stage-7-research-model-engine.md`
- `stage-8-research-workflow-and-agent-orchestration.md`

这些文档记录从数据、证据、知识库、模型引擎到工作流的底层设计，作为后续扩展依据。

## 演示素材

- `assets/demo-screenshots/`：历史界面截图，仅用于 README、归档展示和交付说明。

