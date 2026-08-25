# 消费行业全自动荐股 Agent 系统（AutoInvest Agent）

> 适用对象：消费行业量化/AI 研究团队
> 核心目标：每日定时全自动运行，从数据拉取到晨报推送，零人工介入
> 架构版本：V2.0（完全重写，自动化优先）
> 更新日期：2026-08-21
> V2.1 对齐口径：承接《股票推荐思路.md》V1.5；当前优先重做现有消费行研 agent 的“今日股票关注”模块，不另起系统；主推清单全自动生成，但必须保留数据质量、规则依据和审计日志（2026-08-21）

---

## 一、设计哲学

### 1.1 什么是"全自动"

**本系统的"全自动"定义为**：

- 每日定时触发（北京时间 07:30），Agent 自动完成：数据获取 → 指标计算 → 筛选排序 → 晨报生成 → 输出推送
- 全链路无人工参与日常决策，阈值由季度 review 更新，参数在配置文件中管理
- 遇到数据缺失或异常时，Agent 自主决定降级处理（用 P2 数据或跳过该指标）并记录日志，不卡死流程
- Agent 对输出结果负责，有自我复核能力（输出后做一次逻辑 cross-check）

### 1.2 什么不是"全自动"

- **季度阈值 review**：由人在每季度初调整 `config/thresholds.json`，Agent 自动读取新参数
- **极端市场熔断**：大盘连续下跌超 15% 时，Agent 自动降低建仓权重上限，人工确认后恢复
- **系统故障恢复**：数据源宕机时 Agent 自动切换备选数据源并报警，人工确认后继续
- 以上"人工介入"场景均为**非日常触发**，不影响"全自动日常运行"的定义

### 1.3 与 V1.x 的根本区别

| 维度 | V1.x（人工操作型） | V2.0（全自动 Agent 型） |
|------|------------------|----------------------|
| 日常运行 | 研究员每日手动操作或确认 | 定时触发，Agent 全自动 |
| 标签判定 | 研究员审核后赋予 | Agent 自动判定并赋予 |
| 晨报生成 | 研究员撰写 | Agent 自动生成 |
| 阈值调整 | 人工修改文档 | 人工修改配置文件，Agent 自动读取 |
| 卖出监控 | 研究员每日盯市场 | Agent 实时监控，触发后自动降级 |
| 异常处理 | 流程卡住等人介入 | Agent 自主降级并报警 |

---

## 二、系统架构

### 2.1 全局架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    AutoInvest Agent                          │
│                                                             │
│  [定时触发器]  每日 07:30 北京时间                          │
│       │                                                     │
│       ▼                                                     │
│  [数据获取层]                                                │
│  ├─ 行情数据（AKShare / Wind API）                          │
│  ├─ 财务数据（季报 TTM ROE、OCF、负债率）                    │
│  ├─ 一致预期（卖方盈利预测快照）                             │
│  ├─ 治理数据（公告关键词过滤）                               │
│  ├─ 催化事件（季报日历 + 实时公告流）                        │
│  └─ 市场状态（消费指数、基金仓位、社零/CPI）                  │
│       │                                                     │
│       ▼                                                     │
│  [指标计算层]                                                │
│  ├─ 估值分位数（PE/PB 历史分位，按子行业分组）               │
│  ├─ PEG（未来 2 年盈利复合增速）                             │
│  ├─ 股价位置（相对子行业指数超额收益分位）                    │
│  ├─ 市场一致预期偏离度（当年/2 年盈利增速 vs 历史均值）        │
│  ├─ 消费市况合成得分（0-100 分）                             │
│  └─ 格局指标 CR3（当前为观察项，不卡流程）                    │
│       │                                                     │
│       ▼                                                     │
│  [筛选决策层]                                                │
│  ├─ 硬 Gate 过滤（G1-G5）                                    │
│  ├─ 子行业分组识别（成熟/可选/成长）                         │
│  ├─ 中期阈值判定（T1-M ~ T5-M）                              │
│  ├─ 中长期阈值判定（T1-L ~ T5-L，不含 T3-L）                  │
│  ├─ 组合层 Gate 校验                                         │
│  └─ 标签自动赋予                                             │
│       │                                                     │
│       ▼                                                     │
│  [输出生成层]                                                │
│  ├─ 每日主推清单（≤5 只，含标签/仓位/逻辑/降级条件）          │
│  ├─ 消费股票池看板（四层状态自动更新）                       │
│  ├─ 晨报文本（自动撰写，含市况判断和配仓建议）                │
│  └─ 降级/卖出预警（当日触发项自动写入日志）                   │
│       │                                                     │
│       ▼                                                     │
│  [输出推送层]                                                │
│  ├─ 写入数据库（每日结果存档）                               │
│  ├─ 推送晨报（Webhook / 邮件 / 企业微信）                    │
│  └─ 异常报警（数据缺失 / 逻辑异常 / 产出为空）                │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| Agent 核心 | 现有消费行研 agent 后端 + 规则引擎；Codex SDK 仅用于复杂文本/组合解释增强 | 日常筛选必须可复现，不能依赖不可审计的自由生成 |
| 数据获取 | P0/P1/P2 分层；AKShare 为 P2 开发期/兜底源，Wind/Choice/券商 PCB 等为 P0 生产源 | 自动切换，输出必须标注来源等级 |
| 数据库 | 复用现有 `consumer-research.db`；生产期可迁移 PostgreSQL | 优先扩展现有表，不推倒重建 |
| 配置管理 | `config/thresholds.json` | 阈值参数与业务参数分离 |
| 调度器 | `schedule` 或系统 cron | 每日 07:30 触发 |
| 输出推送 | Webhook（飞书/企业微信）+ 文件落盘 | 推送渠道可配置 |
| 日志 | Python `logging` 模块 | 完整执行日志，可回溯 |

### 2.3 与现有 agent 的集成边界（V2.1）

本系统不从零新建一个孤立项目，而是作为现有“消费行研 agent”的自动荐股子系统落地：

| 现有模块/表 | 用途 | V2.1 处理方式 |
|-------------|------|---------------|
| `daily_stock_ratings` | 现有“今日股票关注”评分结果 | 保留为候选特征输入，不再直接等同推荐结论 |
| `stock_daily_quotes` | 行情与走势 | 继续作为行情、流动性、股价位置计算基础 |
| `research_universe_members` | 股票池与子行业映射 | 扩展/复核子行业组：成熟、服务、成长 |
| `monitor_events` | 事件、公告、风险流 | 扩展为正向催化与负向降级触发源 |
| `stock_consensus` | 一致预期 | 增加 T、T-7、T-30 快照口径 |
| `stock_fundamentals` | 财务指标 | 补季报 TTM 和单季字段 |
| 前端 `#today` | 当前“今日股票关注”入口 | 重做为“每日主推清单 + 消费股票池看板 + 自动审计日志” |

---

## 三、数据获取规格（自动化接口）

### 3.1 每日必取数据

| 数据项 | 来源 | 字段 | 更新频率 | 质量要求 |
|--------|------|------|---------|---------|
| 日线行情 | Wind/Choice/交易所行情为 P0；AKShare 为 P2 兜底 | close, volume, amount（20 日均成交额） | 每日 | 主推 P0，看板可 P2 |
| PE/PB 分位 | P0 估值序列自算；AKShare/东方财富为 P2 校验 | pe_percentile_3y, pb_percentile_3y | 每日 | 主推 P0，看板可 P2 |
| ROE（TTM） | Wind/Choice/券商 PCB 为 P0；AKShare 为 P2 | roe_ttm | 季报后次日 | 主推 P0 |
| OCF/净利 | Wind/Choice/财报原文为 P0；AKShare 为 P2 | 经营现金流净额 / 净利润 | 季报后次日 | 主推 P0 |
| 资产负债率 | Wind/Choice/财报原文为 P0；AKShare 为 P2 | debt_to_assets | 季报后次日 | 主推 P0 |
| 一致预期盈利增速 | 研报平台/券商一致预期快照为 P0/P1；AKShare 为 P2 | 当年/明年/后年净利润一致预期 | 每日 | 主推 P0/P1 |
| 公告治理数据 | 交易所/证监会/公司公告原文为 P0/P1；AKShare 标题为 P2 | 是否含"监管函/减持/调查" | 每日 | 主推 P0/P1 |
| 消费指数 | Wind/Choice/中证指数为 P0；AKShare 为 P2 | 中证消费 20 日均线方向 | 每日 | 主推 P0 |
| 公募仓位分位 | 基金季报/券商持仓统计为 P1；AKShare 聚合为 P2 | 消费主题基金仓位分位（近 1 年） | 每周/每季 | P1 |
| 社零/CPI | 国家统计局为 P1；AKShare 宏观为 P2 | 社零同比，CPI 当月同比 | 每月 | P1 |
| 利率 | 中债/交易所/央行数据为 P0/P1；AKShare 为 P2 | 10 年国债收益率方向 | 每日 | P0/P1 |
| 季报日历 | 内置日历逻辑 | Q1: 4-30, Q2: 8-31, Q3: 10-31, Q4: 次年 4-30 | 自动 | P0 |

> 关键规则：Agent 可以用 P2 数据全自动运行和展示看板，但“每日主推清单”若含 P2 关键字段，必须在输出中标注 `数据源降级`，并降低建议动作强度，例如从“建仓”降为“小仓位观察/等待核验”。

### 3.2 数据质量容错规则

Agent 在数据缺失时的处理策略（**不卡死流程，自主降级**）：

| 场景 | Agent 行为 | 日志记录 |
|------|-----------|---------|
| 单只股票某日无行情数据 | 跳过该指标，用最近可用数据代替，标注 `data_gap=1d` | `WARN: 600519 行情数据缺失 1 天，使用前一日` |
| 单只股票 ROE 数据陈旧（超过 2 个季度） | 降级为 P2，标记 `roe_stale=True`，不进入主推 | `INFO: 600519 ROE 数据超过 2 个季度，降为 P2` |
| 一致预期数据缺失 | 不进入主推清单；看板可用估算值并标注 `consensus_imputed=True` | `INFO: 600519 一致预期缺失，仅进入看板` |
| 某日全市场数据获取失败（数据源宕机） | 切换到备用数据源（AKShare → Tushare），报警 | `ERROR: AKShare 行情接口失败，切换 Tushare` |
| 消费指数数据缺失 | 用沪深 300 替代，标注 `market_state_approx=True` | `WARN: 消费指数数据缺失，用沪深 300 替代` |

---

## 四、指标计算规格（自动化）

### 4.1 估值分位数计算

```python
def calc_pe_percentile(stock_code: str, current_pe: float, lookback: int = 3) -> float:
    """
    计算当前 PE 在历史 3 年分位数。
    - 使用前复权日线数据计算每日 PE（若当日无 PE 用前值填充）
    - 分位数 = (当前 PE 在历史序列中的排名) / 总数据点数
    - 返回 0.0~1.0，0.3 表示处于历史 30% 分位（便宜）
    """
    hist_pe = fetch_historical_pe(stock_code, days=lookback * 365)
    percentile = stats.percentileofscore(hist_pe, current_pe) / 100.0
    return percentile
```

### 4.2 PEG 计算

```python
def calc_peg(stock_code: str) -> float:
    """
    PEG = 当前 PE / 未来 2 年盈利复合增速（CAGR）
    - 当前 PE：TTM PE
    - 未来 2 年盈利 CAGR：取一致预期（当年 + 明年 + 后年三年净利润复合增速）
    - 若无后年数据，用当年 + 明年两年复合增速
    - 返回 float，None 表示数据不足跳过该指标
    """
    pe_ttm = fetch_pe_ttm(stock_code)
    consensus = fetch_consensus_forecast(stock_code)  # 当年/明年/后年
    if len(consensus) >= 3:
        cagr = (consensus['year3'] / consensus['year0']) ** (1/2) - 1
    elif len(consensus) >= 2:
        cagr = (consensus['year1'] / consensus['year0']) ** (1/1) - 1
    else:
        return None
    return pe_ttm / (cagr * 100)  # cagr 为小数，转为百分比
```

### 4.3 股价位置（相对子行业超额收益分位）

```python
def calc_price_position(stock_code: str, sub_industry_code: str, days: int = 60) -> float:
    """
    计算股价在过去 N 个交易日相对子行业指数的超额收益分位。
    - 基准：对应子行业指数（中证消费二级行业指数或用市值加权组合模拟）
    - 超额收益 = 个股涨幅 - 行业指数涨幅
    - 在过去 N 个交易日的超额收益序列中计算当前超额收益的分位数
    - 分位 > 0.8（处于前 20%）= 不满足 T5-M/T5-L（追高警告）
    """
    stock_returns = get_cumulative_returns(stock_code, days=days)
    index_returns = get_sub_industry_returns(sub_industry_code, days=days)
    excess_returns = stock_returns - index_returns
    current_excess_rank = stats.percentileofscore(excess_returns, excess_returns[-1]) / 100.0
    return current_excess_rank
```

### 4.4 消费市况合成得分（0-100 分）

```python
def calc_market_score() -> tuple[int, str]:
    """
    消费市况量化合成，每周一更新（交易日）。
    返回：(总分 0-100，市况标签)
    """
    score = 0

    # 因子 1：消费指数趋势（30 分）
    consumer_ret, hs300_ret = get_consumer_index_return(days=60)
    excess = consumer_ret - hs300_ret
    if consumer_ret > 0.10 and excess > 0.05:
        score += 30
    elif consumer_ret >= 0:
        score += 15

    # 因子 2：公募消费仓位分位（25 分）
    fund_position_pct = get_fund_position_percentile()  # 0-100
    if fund_position_pct < 50:
        score += 25
    elif fund_position_pct < 75:
        score += 15
    else:
        score += 5

    # 因子 3：宏观消费环境（25 分）
    retail_yoy, cpi = get_macro_indicators()  # 社零同比，CPI 同比
    if retail_yoy > 5 and 0 <= cpi <= 3:
        score += 25
    elif retail_yoy >= 3:
        score += 15
    else:
        score += 5

    # 因子 4：利率环境（20 分）
    rate_direction = get_10y_treasury_direction()  # 'down'/'stable'/'up'
    if rate_direction == 'down':
        score += 20
    elif rate_direction == 'stable':
        score += 10

    # 映射
    if score >= 70:
        label = '消费牛市'
    elif score >= 40:
        label = '消费震荡'
    elif score >= 20:
        label = '消费防御'
    else:
        label = '消费熊市'

    return score, label
```

### 4.5 消费市况权重映射

| 市况得分 | 市况标签 | 估值容忍度 | 成长权重 | 操作建议 |
|---------|---------|-----------|---------|---------|
| ≥ 70 | 消费牛市 | 放宽（PE 分位可到 40%） | 提高 | 允许追强势股，仓位可到上限 |
| 40-69 | 消费震荡 | 中性（PE 分位 30-35%） | 中性 | 估值与成长并重 |
| 20-39 | 消费防御 | 收紧（PE 分位 20-25%） | 偏低 | 必选消费、现金流、股息为主 |
| < 20 | 消费熊市 | 最严（PE 分位 ≤ 20%） | 低 | 严格控制仓位，只保留最确定中期票 |

---

## 五、决策树实现（自动化）

### 5.1 完整决策树代码框架

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional

class SubIndustryGroup(Enum):
    MATURE = "成熟必需消费"      # T1-M ~ T5-M, PE/PB 分位
    OPTIONAL = "可选/服务消费"   # T1-M ~ T5-M, EV/EBITDA 替代 PE
    GROWTH = "成长消费"          # T2-M 替换为 PS/PEG

class HoldingPeriod(Enum):
    MEDIUM = "中期"
    LONG_TERM = "中长期"
    LONG_TERM_MEDIUM_BUILD = "中长期·中期可建仓"
    LONG_TERM_WAIT = "中长期·暂不建仓"

@dataclass
class StockResult:
    code: str
    name: str
    sub_industry: str
    sub_group: SubIndustryGroup
    gates_passed: bool
    holding_period: Optional[HoldingPeriod]
    board_status: str  # 看板状态：核心候选/等信号/好公司/全覆盖
    score_details: dict  # 每项阈值通过情况
    recommendation_action: Optional[str]  # 建仓/加仓/持有
    recommended_weight: Optional[float]   # 仓位建议
    core_logic: Optional[str]             # 核心逻辑一句话
    catalyst_date: Optional[str]          # 催化/复核日期
    downgrade_condition: Optional[str]    # 降级条件
    data_quality_flags: list[str]         # 数据质量标注


def run_decision_tree(stocks: list[str], config: dict) -> list[StockResult]:
    """
    全自动决策树，每日定时运行。
    输入：全体消费股票代码列表（1275 只）
    输出：StockResult 列表，含标签和输出状态
    """
    results = []
    market_score, market_label = calc_market_score()
    config['market_label'] = market_label
    config['market_score'] = market_score

    for code in stocks:
        try:
            result = evaluate_single_stock(code, config)
            results.append(result)
        except Exception as e:
            # 单只股票出错不影响全局，记录日志继续下一只
            logger.error(f"股票 {code} 评估异常: {e}")
            continue

    # 全局组合层 Gate 校验（需要全局视野）
    results = apply_portfolio_gate(results, config)

    return results


def evaluate_single_stock(code: str, config: dict) -> StockResult:
    """对单只股票执行完整决策树"""
    r = StockResult(
        code=code,
        name=get_stock_name(code),
        sub_industry=get_sub_industry(code),
        sub_group=identify_sub_group(code),
        gates_passed=False,
        holding_period=None,
        board_status="",
        score_details={},
        recommendation_action=None,
        recommended_weight=None,
        core_logic=None,
        catalyst_date=None,
        downgrade_condition=None,
        data_quality_flags=[]
    )

    # ===== 第一步：硬 Gate 筛选 =====
    gates = check_hard_gates(code, config)
    r.score_details['gates'] = gates
    if not all(gates.values()):
        r.board_status = "剔除"
        return r
    r.gates_passed = True

    # ===== 第二步：识别子行业组 =====
    thresholds = get_threshold_set(r.sub_group, horizon="medium")  # 中期阈值组
    thresholds_l = get_threshold_set(r.sub_group, horizon="long")  # 中长期阈值组

    # ===== 第三步：中期阈值判定 =====
    medium_pass, medium_details = check_threshold_set(code, thresholds)
    r.score_details['medium'] = medium_details

    # ===== 第三步：中长期阈值判定（不含 T3-L）=====
    long_pass, long_details = check_threshold_set(code, thresholds_l, exclude=["T3-L"])
    r.score_details['long'] = long_details

    # ===== 判定持有期资格 =====
    if medium_pass and long_pass:
        r.holding_period = HoldingPeriod.LONG_TERM_MEDIUM_BUILD
    elif long_pass:
        r.holding_period = HoldingPeriod.LONG_TERM
    elif medium_pass:
        r.holding_period = HoldingPeriod.MEDIUM
    else:
        # 中长期阈值不满足，检查是否接近（缺 1 项）→ 暂不建仓
        if is_close(long_details, config):
            r.holding_period = HoldingPeriod.LONG_TERM_WAIT
        else:
            r.holding_period = None

    # ===== 第四步：组合层 Gate 校验（在全局批量评估中做）=====
    # 此处先记录资格，最终组合 Gate 在 apply_portfolio_gate 中统一处理

    # ===== 生成看板状态和推荐内容 =====
    if r.holding_period in [HoldingPeriod.MEDIUM, HoldingPeriod.LONG_TERM,
                             HoldingPeriod.LONG_TERM_MEDIUM_BUILD]:
        r.board_status = "核心候选"
        r.recommendation_action, r.recommended_weight = \
            generate_recommendation(r, config)
        r.core_logic = generate_logic_statement(r, config)
        r.catalyst_date = identify_next_catalyst(code)
        r.downgrade_condition = generate_downgrade_condition(r)
    elif r.holding_period == HoldingPeriod.LONG_TERM_WAIT:
        r.board_status = "长期好公司"
        # 不进主推，记录等什么信号
    else:
        # 长期逻辑通过 + 时机不满足 → 第三层
        if check_long_fundamentals(code):
            r.board_status = "长期好公司"
        else:
            r.board_status = "行业扫描"

    return r
```

### 5.2 硬 Gate 自动校验

```python
def check_hard_gates(code: str, config: dict) -> dict[str, bool]:
    """
    自动校验 G1-G5，返回每项 Gate 的通过状态。
    """
    gates = {}

    # G1: 治理（公告关键词扫描）
    governance_ok, gov_details = check_governance(code)
    gates['G1'] = governance_ok

    # G2: ROE 底线（子行业差异化）
    roe_ok = check_roe_gate(code, config)
    gates['G2'] = roe_ok

    # G3: 经营现金流
    ocf_ratio_ok = check_ocf_gate(code, config)
    gates['G3'] = ocf_ratio_ok

    # G4: 负债率
    debt_ok = check_debt_gate(code, config)
    gates['G4'] = debt_ok

    # G5: 流动性（20 日均成交额 ≥ 5000 万，看板；≥ 1 亿，主推）
    liquidity_ok = check_liquidity_gate(code, threshold=50_000_000)
    gates['G5'] = liquidity_ok

    return gates


def check_governance(code: str) -> tuple[bool, str]:
    """
    治理 Gate：扫描近 3 年公告关键词。
    返回：(是否通过, 触发关键词列表)
    """
    ban_words = [
        "行政处罚", "监管函", "立案", "调查", "问询",
        "非标审计", "资金占用", "违规担保", "实控人变更",
        "股份冻结", "质押平仓", "减持预披露"
    ]
    notices = fetch_recent_notices(code, days=3*365)
    triggered = [w for w in ban_words if any(w in n['title'] for n in notices)]
    return (len(triggered) == 0, triggered)
```

### 5.3 阈值判定（含子行业差异化）

```python
def check_threshold_set(
    code: str,
    thresholds: list[dict],
    exclude: list[str] = None
) -> tuple[bool, dict[str, dict]]:
    """
    判定一组阈值是否全部通过。
    返回：(是否全部通过, 每项阈值详情)
    exclude: 要跳过的阈值编号列表（如 ["T3-L"]）
    """
    exclude = exclude or []
    results = {}
    passed_count = 0
    total_count = len([t for t in thresholds if t['id'] not in exclude])

    for t in thresholds:
        tid = t['id']
        if tid in exclude:
            results[tid] = {'passed': True, 'skipped': True, 'note': '排除项'}
            continue

        value = fetch_indicator(code, t['indicator'])
        passed, detail = evaluate_threshold(value, t, code)
        results[tid] = {'passed': passed, 'value': value, **detail}

        if passed:
            passed_count += 1

    all_passed = (passed_count == total_count)
    return all_passed, results


def evaluate_threshold(value: float, threshold: dict, code: str) -> tuple[bool, dict]:
    """
    对单个阈值做判定。
    阈值格式：{'id': 'T1-M', 'rule': 'lte', 'value': 0.30, 'market_adjust': {...}}
    """
    rule = threshold['rule']
    threshold_value = threshold['value']

    # 动态调整（根据市场状态）
    market_label = config['market_label']
    if 'market_adjust' in threshold and market_label in threshold['market_adjust']:
        threshold_value = threshold['market_adjust'][market_label]

    if rule == 'lte':  # less than or equal
        passed = (value is not None) and (value <= threshold_value)
    elif rule == 'gt':  # greater than
        passed = (value is not None) and (value >= threshold_value)
    elif rule == 'rank_le':  # 分位排名（前 % 分位以内）
        passed = (value is not None) and (value <= threshold_value)
    elif rule == 'rank_ge':  # 分位排名（后 % 分位以内，即在底部）
        passed = (value is not None) and (value >= threshold_value)

    return passed, {'threshold_applied': threshold_value, 'actual': value}


def identify_sub_group(code: str) -> SubIndustryGroup:
    """根据子行业代码识别所属组"""
    sub = get_sub_industry(code)
    # 成熟必需：CR.S.FB, CR.S.PH, CR.S.PT, CR.D.AP（核心龙头）
    # 可选/服务：CR.V.*, CR.D.AU, CR.D.HL, CR.D.AF
    # 成长消费：CR.S.PT（宠物）, CR.D.AP（出海消费电子）
    if sub in ["CR.S.FB", "CR.S.PH"]:
        return SubIndustryGroup.MATURE
    elif sub in ["CR.V.FS", "CR.V.TL", "CR.V.RT", "CR.V.CE", "CR.D.AU", "CR.D.HL", "CR.D.AF"]:
        return SubIndustryGroup.OPTIONAL
    else:
        return SubIndustryGroup.GROWTH


def get_threshold_set(group: SubIndustryGroup, horizon: str) -> list[dict]:
    """
    获取对应子行业组和持有期的阈值集合。
    horizon: 'medium' 或 'long'
    """
    if horizon == 'medium':
        base = MEDIUM_THRESHOLDS.copy()
        if group == SubIndustryGroup.OPTIONAL:
            base = replace_with_ev_ebitda(base)
        elif group == SubIndustryGroup.GROWTH:
            base = replace_peg_with_ps_peg(base)
        return base
    else:  # long
        base = LONG_THRESHOLDS.copy()
        if group == SubIndustryGroup.OPTIONAL:
            base = replace_with_ev_ebitda(base)
        elif group == SubIndustryGroup.GROWTH:
            base = replace_peg_with_ps_peg(base)
        # 排除 T3-L（观察项，不卡流程）
        return base
```

### 5.4 组合层 Gate（全局批量校验）

```python
def apply_portfolio_gate(candidates: list[StockResult], config: dict) -> list[StockResult]:
    """
    全局组合层 Gate，对全部核心候选做子行业分散度和仓位校验。
    组合 Gate 通过的 → 进入主推清单
    组合 Gate 未通过的 → 留在看板核心候选（board_status = 核心候选）
    """
    # 只对有持有期资格的候选执行组合 Gate
    eligible = [r for r in candidates if r.holding_period is not None]

    # 子行业分散度
    sub_industry_counts = count_by_sub_industry([r for r in eligible if r.board_status == "核心候选"])

    # 必选/可选比例
    required_ratio = calculate_required_optional_ratio(eligible)

    # 流动性支持
    liquid_candidates = [r for r in eligible if check_liquidity_gate(r.code, threshold=100_000_000)]

    # 尝试放入组合，取子行业分散度最高的组合
    # 贪心算法：优先选相关性低、逻辑强的标的
    selected = greedy_portfolio_select(
        candidates=liquid_candidates,
        max_stocks=5,
        min_sub_industries=3,
        required_optional_range=(0.4, 0.6),
        max_single_sub_exposure=0.25
    )

    selected_codes = {s.code for s in selected}

    # 赋予主推标签
    for r in candidates:
        if r.code in selected_codes and r.holding_period in [
            HoldingPeriod.MEDIUM, HoldingPeriod.LONG_TERM,
            HoldingPeriod.LONG_TERM_MEDIUM_BUILD
        ]:
            r.board_status = "主推"
            r.data_quality_flags.append("p0_verified")
        elif r.board_status == "核心候选":
            pass  # 留在看板，不进主推

    return candidates
```

---

## 六、自动化输出生成

### 6.1 每日主推清单生成

```python
def generate_daily_report(results: list[StockResult], market_score: int, market_label: str) -> str:
    """
    自动生成晨报文本。
    输出格式：Markdown，含主推清单 + 看板摘要 + 市况判断 + 风险提示
    """
    report_lines = [
        f"# 消费行业每日荐股晨报 [{get_today_str()}]",
        f"> 市况：{market_label}（得分 {market_score}/100）",
        "",
    ]

    # 主推清单
    main_push = [r for r in results if r.board_status == "主推"]
    report_lines.append("## 今日主推清单（≤5 只）")
    report_lines.append("")
    report_lines.append("| 代码 | 名称 | 子行业 | 标签 | 建议动作 | 仓位 | 核心逻辑 | 催化日 | 降级条件 |")
    report_lines.append("|------|------|--------|------|----------|------|----------|--------|----------|")

    for r in main_push[:5]:
        report_lines.append(
            f"| {r.code} | {r.name} | {r.sub_industry} | 【{r.holding_period.value}】 | "
            f"{r.recommendation_action} | {r.recommended_weight:.1%} | "
            f"{r.core_logic} | {r.catalyst_date} | {r.downgrade_condition} |"
        )

    report_lines.append("")
    report_lines.append(f"数据源：P0（Wind/Choice），已核验。生成时间：{get_timestamp()}。")

    # 看板核心候选（头屏第二屏）
    core_board = [r for r in results if r.board_status == "核心候选"]
    report_lines.append("")
    report_lines.append("## 消费股票池看板（核心候选，非主推）")
    report_lines.append("")
    report_lines.append("| 代码 | 名称 | 子行业 | 看板状态 | 时机满足度 | 未进主推原因 |")
    report_lines.append("|------|------|--------|----------|------------|--------------|")

    for r in core_board[:15]:
        timing = calc_timing_score(r.score_details)
        reason = get_block_reason(r)
        report_lines.append(
            f"| {r.code} | {r.name} | {r.sub_industry} | 【核心·时机满足】 | "
            f"{timing:.0%} | {reason} |"
        )

    # 降级预警
    downgrade_alerts = [r for r in results if check_downgrade_trigger(r)]
    if downgrade_alerts:
        report_lines.append("")
        report_lines.append("## 今日降级预警")
        for r in downgrade_alerts:
            trigger = identify_downgrade_trigger(r)
            report_lines.append(f"- **{r.code} {r.name}**：{trigger}，建议降层处理")

    return "\n".join(report_lines)
```

### 6.2 实时卖出/降级监控（当日运行中）

```python
def monitor_and_update(results: list[StockResult]):
    """
    在日间（09:30 - 15:00）持续监控持仓标的状态。
    触发降级条件时自动更新看板状态，不人工介入。
    """
    held = [r for r in results if r.board_status == "主推"]

    for r in held:
        # 触发项 1：同店/批价/订单连续转弱（高频数据监控）
        if detect_sequential_weakness(r.code):
            logger.warning(f"{r.code} 同店/批价连续转弱，触发降级")
            r.board_status = "长期好公司"
            r.downgrade_condition = "已触发：同店/批价连续转弱"
            append_audit_log(r.code, "降级", "同店/批价连续转弱")

        # 触发项 2：季报低于关键假设
        if detect_earning_miss(r.code):
            logger.warning(f"{r.code} 季报低于关键假设，暂停建仓")
            r.recommendation_action = "暂停建仓"
            append_audit_log(r.code, "暂停建仓", "季报低于关键假设")

        # 触发项 3：估值分位突破上限
        pe_pct = calc_pe_percentile(r.code, get_current_pe(r.code))
        if pe_pct > 0.80 and not is_earning_growing(r.code):
            logger.warning(f"{r.code} PE 分位突破 80% 且业绩未同步上修，降为暂不建仓")
            r.board_status = "长期好公司"
            r.holding_period = HoldingPeriod.LONG_TERM_WAIT
            append_audit_log(r.code, "降级", "估值分位突破 80%")

        # 触发项 4：治理事件
        gov_triggered, keywords = check_governance(r.code)
        if not gov_triggered:
            logger.error(f"{r.code} 触发治理 Gate：{keywords}，建议剔除")
            r.board_status = "剔除"
            append_audit_log(r.code, "剔除", f"治理事件: {keywords}")
```

---

## 七、配置管理（人机边界）

### 7.1 配置文件结构

所有阈值参数、业务规则均通过配置文件管理，人工修改后 Agent 自动读取，**无需改代码**。

**`config/thresholds.json`**（季度 review 更新）：

```json
{
  "version": "2.0.0",
  "last_review": "2026-08-21",
  "next_review": "2026-11-21",
  "market_thresholds": {
    "bull_score": 70,
    "bear_score": 20
  },
  "gate_thresholds": {
    "G2_roe_mature": 0.12,
    "G2_roe_optional": 0.08,
    "G2_roe_growth": 0.05,
    "G3_ocf_mature": 0.60,
    "G3_ocf_optional": 0.40,
    "G4_debt_mature": 0.70,
    "G4_debt_optional": 0.75,
    "G5_liquidity_board": 50000000,
    "G5_liquidity_main_push": 100000000
  },
  "medium_thresholds": {
    "T1_M_pe_percentile": {
      "mature": {"value": 0.30, "market_adjust": {"熊市": 0.20, "反弹市": 0.40}},
      "optional": "EV/EBITDA 替代",
      "growth": "PS 分位替代"
    },
    "T2_M_peg": {
      "mature": {"value": 1.0, "market_adjust": {"牛市": 1.5}},
      "growth": "PS/PEG + 收入增速，不要求 PEG ≤ 1"
    },
    "T3_M_catalyst_days": {"value": 90},
    "T4_M_consensus_delta": {"value": 0.05, "market_adjust": {"牛市": 0.10}},
    "T5_M_price_rank": {"value": 0.20}
  },
  "long_thresholds": {
    "T1_L_implied_return": {
      "value": 0.15,
      "market_adjust": {"熊市": 0.18, "牛市": 0.12}
    },
    "T2_L_pe_percentile": {"value": 0.40},
    "T3_L_cr3": {"value": 0.30, "status": "观察项，不卡流程"},
    "T4_L_consensus_delta_2y": {"value": 0.10},
    "T5_L_price_rank": {"value": 0.15}
  },
  "market_scoring": {
    "consumer_index_weight": 30,
    "fund_position_weight": 25,
    "macro_retail_weight": 25,
    "rate_weight": 20
  },
  "portfolio_gates": {
    "min_sub_industries": 3,
    "required_optional_min": 0.40,
    "required_optional_max": 0.60,
    "max_single_sub_exposure": 0.25
  },
  "data_sources": {
    "primary": "wind",
    "fallback": "akshare",
    "quality_requirement": "P0 for main push, P1 for board"
  }
}
```

### 7.2 人的工作：季度 Review 流程

| 时间 | 动作 | 产出 |
|------|------|------|
| 每季度初 | 研究员 review 上季度阈值有效性 | 更新 `config/thresholds.json` |
| 每季度初 | 检查子行业分类是否有边界变化 | 更新 `config/sub_industry_mapping.json` |
| 每季度初 | 确认 CR3 数据是否成熟到可以升级为阈值 | 通知工程团队修改 `status: observe → threshold` |
| 任意时间 | 大盘连续下跌超 15% 时手动熔断 | 更新 `config/circuit_breaker.json`（`active: true`） |

---

## 八、Agent 日志与审计

### 8.1 必需日志字段

每条执行记录必须包含：

```python
@dataclass
class DailyRunLog:
    run_id: str              # UUID，每日唯一
    run_date: str            # 日期（YYYY-MM-DD）
    started_at: str          # 北京时间启动时间
    finished_at: str         # 北京时间结束时间
    total_stocks: int        # 当日评估股票总数
    passed_gates: int        # 通过 Gate 数量
    main_push_count: int     # 主推清单数量
    core_board_count: int    # 看板核心候选数量
    market_score: int        # 当日市况得分
    market_label: str        # 当日市况标签
    data_quality_flags: list # 当日数据异常标注
    errors: list[str]        # 当日出错的股票和原因
    output_path: str         # 晨报文件路径
```

### 8.2 审计日志（Audit Log）

所有标签变更、降级、剔除必须写入 audit log：

```python
def append_audit_log(
    code: str,
    action: str,        # 降级 / 剔除 / 升级 / 新增主推
    reason: str,        # 具体原因
    triggered_by: str = "agent",  # agent / human
    reviewed_by: str = None       # 人工介入时记录
):
    log_entry = {
        "timestamp": get_timestamp(),
        "code": code,
        "action": action,
        "reason": reason,
        "triggered_by": triggered_by,
        "reviewed_by": reviewed_by,
        "run_id": current_run_id
    }
    db.insert("audit_log", log_entry)
```

### 8.3 异常报警规则

| 报警级别 | 触发条件 | 处理方式 |
|---------|---------|---------|
| P0 故障 | 主数据源（Wind/Choice）全量宕机 | 切换 AKShare，发送报警，晨报标注"数据源降级" |
| P1 异常 | 主推清单为空（无任何合格标的） | 发送报警，人工确认是否市场极端情况 |
| P2 异常 | 超过 10% 股票数据不完整 | 记录日志，生成时标注数据质量，晨报说明 |
| P3 注意 | 单只股票评估失败 | 记录日志，不影响全局，继续运行 |

### 8.4 Agent 对输出负责的落地定义（V2.1）

“Agent 对输出负责”不是隐藏不确定性，而是每个输出都必须带上可审计依据：

| 输出字段 | 必填要求 |
|----------|----------|
| `decision_basis` | 说明通过了哪些 Gate、哪些阈值、组合层 Gate 是否通过 |
| `data_quality_flags` | 标注 P0/P1/P2、缺失、陈旧、估算、降级 |
| `not_main_reason` | 看板核心候选未进入主推清单的原因 |
| `downgrade_condition` | 主推标的的自动降级/卖出条件 |
| `audit_log_id` | 每次新增、升级、降级、剔除的审计记录 |
| `generated_by` | 默认 `agent`，季度参数调整或人工熔断时记录 `human_config` |

Agent 可以在零人工介入下生成每日主推，但不得生成“无依据结论”。若关键依据缺失，Agent 仍自动输出，但必须降级为看板或降低建议动作强度。

---

## 九、部署架构

### 9.1 部署拓扑

```
[服务器 / 云主机]
│
├── auto_invest_agent/
│   ├── agent.py              # 主 Agent 入口（规则引擎 + 可选 Codex SDK 增强）
│   ├── decision_tree.py      # 决策树逻辑
│   ├── data_fetcher.py       # 数据获取层（AKShare / Wind / Tushare）
│   ├── metrics.py            # 指标计算
│   ├── output_generator.py   # 输出生成
│   ├── config/
│   │   ├── thresholds.json   # 阈值配置（人维护）
│   │   ├── sub_industry_mapping.json
│   │   └── circuit_breaker.json
│   ├── logs/
│   │   └── daily_runs/       # 每日运行日志
│   └── data/
│       ├── consumer-research.db  # 复用现有 SQLite 数据库
│       └── reports/              # 生成的晨报存档
│
├── [每日定时触发]
│   └── cron: 07:30 北京时间  →  python agent.py
│
└── [输出推送]
    ├── Webhook → 飞书群 / 企业微信
    ├── 晨报 PDF/HTML → 文件服务器
    └── 数据库存档 → 可查询历史
```

### 9.2 定时任务配置

```bash
# crontab -e
30 7 * * 1-5 cd /path/to/auto_invest_agent && /usr/bin/python3 agent.py >> logs/cron.log 2>&1

# 工作日（周一至周五）07:30 运行
# 周末不运行（无交易日）
```

### 9.3 Agent 调用方式

日常主流程优先使用可复现的规则引擎；Codex SDK 作为“解释、摘要、复杂组合比较”的增强能力，不作为唯一决策源：

```python
# agent.py（主入口）
import os
from datetime import datetime

def main():
    run_date = datetime.now().strftime("%Y-%m-%d")
    print(f"[{run_date}] AutoInvest Agent 启动")

    # ===== Step 1: 数据获取 =====
    print("Step 1: 获取今日行情数据...")
    fetch_all_market_data(run_date)

    # ===== Step 2: 指标计算 =====
    print("Step 2: 计算所有指标...")
    calculate_all_metrics(run_date)

    # ===== Step 3: 决策树执行 =====
    print("Step 3: 执行决策树...")
    stocks = load_stock_universe()
    results = run_decision_tree(stocks, config)

    # ===== Step 4: 监控降级 =====
    print("Step 4: 监控降级触发...")
    monitor_and_update(results)

    # ===== Step 5: 生成输出 =====
    print("Step 5: 生成晨报...")
    report = generate_daily_report(results, market_score, market_label)
    save_report(report, run_date)

    # ===== Step 6: 推送 =====
    print("Step 6: 推送晨报...")
    push_to_webhook(report)
    push_to_file(report, run_date)

    print(f"[{run_date}] Agent 执行完成")


if __name__ == "__main__":
    main()
```

若需要 Agent 在执行过程中做复杂判断（如跨标的比较、子行业优先级排序），可在 `run_decision_tree` 内部调用 Codex SDK，但输出必须回写结构化字段，并保留规则依据：

```python
def run_decision_tree_with_agent(stocks: list[str], config: dict) -> list[StockResult]:
    """
    当决策树遇到需要复杂推理的场景（如组合最优解）时，
    唤醒 Codex Agent 做子问题推理。
    """
    prompt = f"""
    从以下候选池中选出最优的 5 只股票组成主推清单，要求：
    1. 子行业分散度 ≥ 3
    2. 必选/可选比例在 4:6 ~ 6:4
    3. 单只建议仓位考虑流动性和上行空间
    候选池：
    {format_candidates_for_agent(stocks)}
    市况：{config['market_label']}（得分 {config['market_score']}/100）
    """
    result = Agent.prompt(
        prompt,
        AgentOptions(
            api_key=os.environ["CURSOR_API_KEY"],
            model="composer-2.5",
            local=LocalAgentOptions(cwd=os.getcwd()),
        ),
    )
    selected = parse_agent_selection(result)
    return enforce_structured_audit(selected, required_fields=[
        "decision_basis",
        "data_quality_flags",
        "not_main_reason",
        "downgrade_condition"
    ])
```

---

## 十、与 V1.x 的完整对照

### 10.1 架构升级对照

| V1.x 章节 | V2.0 对应 | 核心变化 |
|----------|----------|---------|
| §1 框架设计理念 | §1 设计哲学 | 从"研究员手册"改为"系统设计文档" |
| §2 七维筛选体系 | §4 指标计算规格 | 全部量化为 Python 函数 |
| §3 硬 Gate 规则 | §5.2 硬 Gate 自动校验 | G1-G5 全部代码化，无人工判读 |
| §4 买入时机阈值表 | §5.3 阈值判定 | 子行业差异化代码化，动态调整自动化 |
| §5 市场状态判定 | §4.4 消费市况得分 | 量化合成 0-100 分，自动计算 |
| §6 标签判定决策树 | §5.1 决策树代码框架 | 完整 Python 实现，全自动判定 |
| §7 每日推荐呈现 | §6 自动化输出生成 | 晨报自动生成，Webhook 推送 |
| §8 研究员与 AI 分工 | §7 配置管理 | 人只管配置文件，Agent 自动读取 |
| §9 数据源与接入 | §3 数据获取规格 | 数据获取全部代码化，容错机制明确 |
| §10 实施路线图 | §9 部署架构 | 部署脚本化，cron 定时触发 |

### 10.2 每日流程对照

**V1.x（人工型）**：
> 研究员每日打开系统 → 查看数据 → 手动筛选 → 判断标签 → 撰写晨报 → 发送

**V2.0（全自动）**：
> 07:30 cron 触发 → Agent 自动获取数据 → 计算指标 → 执行决策树 → 生成推送 → 存档

---

## 附录 A：完整配置文件

### A.1 `config/thresholds.json`（完整模板）

见 §7.1。

### A.2 `config/sub_industry_mapping.json`

```json
{
  "CR.S.FB": {
    "name": "食品饮料",
    "group": "mature",
    "index_code": "000932.SH",
    "pe_benchmark": "PE/PB 分位"
  },
  "CR.S.PH": {
    "name": "美容个护",
    "group": "mature",
    "index_code": "000980.SH",
    "pe_benchmark": "PE/PB 分位"
  },
  "CR.D.AP": {
    "name": "家电与消费电子",
    "group": "optional",
    "index_code": "930721.CSI",
    "pe_benchmark": "EV/EBITDA（出海业务为主）"
  },
  "CR.V.FS": {
    "name": "餐饮与本地生活",
    "group": "optional",
    "index_code": "935925.CSI",
    "pe_benchmark": "EV/EBITDA + 同店修复"
  },
  "CR.V.TL": {
    "name": "酒店旅游",
    "group": "optional",
    "index_code": "935697.CSI",
    "pe_benchmark": "EV/EBITDA + RevPAR 修复"
  },
  "CR.D.AF": {
    "name": "纺织服饰与运动户外",
    "group": "optional",
    "index_code": "935938.CSI",
    "pe_benchmark": "EV/EBITDA"
  },
  "CR.D.AU": {
    "name": "汽车与出行",
    "group": "optional",
    "index_code": "931008.CSI",
    "pe_benchmark": "EV/EBITDA + 周期位置"
  },
  "CR.D.HL": {
    "name": "家居与生活空间",
    "group": "optional",
    "index_code": "935793.CSI",
    "pe_benchmark": "EV/EBITDA"
  },
  "CR.S.PT": {
    "name": "宠物与家庭新消费",
    "group": "growth",
    "index_code": "931488.CSI",
    "pe_benchmark": "PS/PEG + 收入增速"
  },
  "CR.V.CE": {
    "name": "文化娱乐与教育消费",
    "group": "growth",
    "index_code": "931793.CSI",
    "pe_benchmark": "PS/PEG"
  },
  "CR.V.RT": {
    "name": "商贸零售与电商",
    "group": "optional",
    "index_code": "935791.CSI",
    "pe_benchmark": "EV/EBITDA"
  }
}
```

---

## 附录 B：数据库表结构

V2.1 优先扩展现有 `consumer-research.db`，不要一上来替换成孤立的新库。下面表结构可作为新增表/视图；已有表继续复用。

```sql
CREATE TABLE daily_run_logs (
    run_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total_stocks INTEGER,
    passed_gates INTEGER,
    main_push_count INTEGER,
    core_board_count INTEGER,
    market_score INTEGER,
    market_label TEXT,
    output_path TEXT,
    status TEXT DEFAULT 'running'
);

CREATE TABLE stock_evaluation_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    sub_industry TEXT,
    sub_group TEXT,
    holding_period TEXT,
    board_status TEXT,
    gates_passed INTEGER,
    medium_passed INTEGER,
    long_passed INTEGER,
    recommended_action TEXT,
    recommended_weight REAL,
    core_logic TEXT,
    catalyst_date TEXT,
    downgrade_condition TEXT,
    data_quality_flags TEXT,
    FOREIGN KEY (run_id) REFERENCES daily_run_logs(run_id)
);

-- 建议新增：每日主推清单，承接“今日股票关注”重做后的第一屏
CREATE TABLE daily_main_push (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    rating_date TEXT NOT NULL,
    security_id TEXT NOT NULL,
    name TEXT NOT NULL,
    sector_code TEXT,
    holding_label TEXT,
    recommendation_action TEXT,
    recommended_weight REAL,
    core_logic TEXT,
    catalyst_date TEXT,
    downgrade_condition TEXT,
    decision_basis TEXT,
    data_quality_flags TEXT,
    audit_log_id INTEGER,
    created_at TEXT NOT NULL
);

-- 建议新增：消费股票池看板，承接“今日股票关注”重做后的第二屏以后
CREATE TABLE daily_watchlist_board (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    rating_date TEXT NOT NULL,
    security_id TEXT NOT NULL,
    name TEXT NOT NULL,
    sector_code TEXT,
    board_status TEXT,
    timing_score REAL,
    model_score REAL,
    not_main_reason TEXT,
    decision_basis TEXT,
    data_quality_flags TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    triggered_by TEXT DEFAULT 'agent',
    reviewed_by TEXT,
    FOREIGN KEY (run_id) REFERENCES daily_run_logs(run_id)
);

CREATE TABLE stock_valuation_history (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe REAL,
    pb REAL,
    pe_percentile_3y REAL,
    pb_percentile_3y REAL,
    price_position_60d REAL,
    PRIMARY KEY (code, date)
);
```

---

*本框架为 V2.0 全自动版本，核心设计目标：每日定时全自动运行，从数据到输出零人工介入。阈值参数通过配置文件管理，季度 review 时由人更新，Agent 自动读取并应用。*
