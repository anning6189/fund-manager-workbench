# 阶段七：研究模型与计算引擎

## 1. 阶段结论

阶段七已经完成。阶段六的消费行业事实仓现已具备一层可复算、可审计、支持历史时点、三情景和敏感性分析的研究模型引擎。

本阶段实现的是研究计算能力，不是自动投资决策。系统不读取基金持仓、不推断基金仓位，也不生成自动买卖指令。财务预测和估值结果通过必需系统质量门后直接供内部研究使用，不设置人工或外部审核。

## 2. 已实现的五类模型

### 2.1 市场空间双路径模型

- Top-down：目标人群 × 渗透率 × 购买频次 × 客单价。
- Bottom-up：可覆盖网点 × 单网点销量 × 平均售价。
- 自动计算两种路径的中点和差异率。
- 强制市场口径使用同一个 `scope_key`。
- 支持 Bear、Base、Bull 三情景和二维敏感性矩阵。

### 2.2 竞争格局与集中度模型

- 计算头部公司市场份额。
- 计算 CR3、CR5 和长尾份额。
- 自动检查份额范围、`CR3 <= CR5` 以及 `CR5 + 长尾份额 = 100%`。
- 强制公司销售额和市场分母采用相同市场口径。

### 2.3 公司经营量价桥

- 从阶段六事实仓按研究截止时点读取公司历史收入。
- 分解销量贡献、价格与结构贡献、交互项。
- 自动计算并检查桥接残差。
- 演示包使用美的 2022A、2023A 收入事实，量增参数明确标为假设。

### 2.4 财务预测模型

- 从阶段六读取收入、营业成本、销售费用、归母净利润和经营现金流事实。
- 计算历史毛利率、销售费用率和现金转换率。
- 在 Bear、Base、Bull 情景下计算收入、毛利、经营利润、净利润、经营现金流、资本开支和自由现金流。
- 自动检查利润表和现金流关系。
- 支持收入增速 × 毛利率二维敏感性分析。
- 正式运行结果为 `internal_research_ready`；演示包仍为 `demonstration_only`。

### 2.5 估值与预期差模型

- 计算 Forward PE、目标 PE 隐含市值。
- 反向估值当前市值隐含的盈利要求。
- 计算模型预测与一致预期的差异，以及模型盈利与市场隐含盈利的预期差。
- 强制估值输入时点和预测快照时点一致。
- 支持盈利增速 × 目标 PE 二维敏感性分析。
- 通过估值时点、反向估值、三情景和敏感性等系统质量门后直接交付。

## 3. 统一数据与审计规则

所有输入和输出只允许三类标签：

- `FACT_OBSERVATION`：只能来自阶段六事实仓，必须保留 `observation_id`、`evidence_id` 和 `available_at`。
- `SCENARIO_ASSUMPTION`：必须有口径、形成时间、依据说明和置信度，不能伪装成事实。
- `AGENT_CALCULATION`：必须保存公式、直接依赖和单位。

研究截止时点统一使用 UTC 规范化。事实和假设的可用时间一旦晚于研究截止时点，模型即阻断运行。因此模型可以按历史时点重放，不会把后来披露的信息写回过去。

公式引擎不是 Python `eval`。它只允许数字、变量、加减乘除、幂运算，以及 `abs`、`min`、`max`、`round` 四个白名单函数；属性访问、导入、文件操作和网络操作均被拒绝。

## 4. 数据库存储

迁移脚本 `sql/002_consumer_research_model_engine.sql` 在阶段六数据库上新增：

- `model_definitions`：五类模型定义和必需输出角色。
- `model_packages`：模型输入包、口径、截止时点和内容哈希。
- `model_runs`：每个情景的运行状态、质量门交付要求和内部展示状态。
- `model_inputs`：事实与假设输入；事实保持到阶段六证据的外键。
- `model_outputs`：数值、公式、角色、单位和依赖链。
- `model_sensitivity_results`：一维或二维敏感性格点。
- `model_validation_events`：质量异常审计接口。
- `v_model_reproducibility_trace`：从输入、证据到公式输出的复算视图。

同一个 `package_id` 如果内容哈希不同会被阻断；相同内容重跑为幂等操作，不重复写入结果。

## 5. 如何运行

设置 Python 路径后，可一次运行整个演示模型套件：

```powershell
$env:CODEX_PYTHON = "<python.exe 的完整路径>"
.\tools\Invoke-Stage7ModelSuite.ps1
```

也可以单独运行：

```powershell
python .\tools\consumer_model_engine.py init
python .\tools\consumer_model_engine.py validate --package .\data\models\stage7\financial-forecast.v1.json
python .\tools\consumer_model_engine.py run --package .\data\models\stage7\financial-forecast.v1.json
python .\tools\consumer_model_engine.py status
```

运行专项验收：

```powershell
python .\tools\Test-Stage7ModelEngine.py
```

## 6. 验收结果

阶段七专项验收为 40/40 通过，覆盖：

- 五类模型静态合同与实际运行。
- 双路径市场空间、CR3/CR5、量价桥、财务预测和反向估值公式核对。
- Bear、Base、Bull 结果顺序。
- 三组 3×3 敏感性矩阵。
- 所有输出从数据库存储输入重新计算一致。
- 历史时点未来信息阻断。
- 假设伪装事实、口径不一致、估值时点不一致和恶意公式阻断。
- 基金持仓边界、直接内部交付、内容哈希冲突和运行幂等性。
- SQLite 完整性和外键检查。

验收证据见 `tests/stage-7-acceptance-report.v1.json`。

正式数据库当前保存 5 个模型定义、5 个演示包、11 个情景运行、78 条输入、76 条输出和 27 个敏感性格点。

## 7. 演示数据边界

模型算法已经正式接通，但演示结果不是当前投资观点：

- 公司历史财务输入来自阶段六事实仓。
- 市场空间、竞争销售额、市值、一致预期、目标估值和未来预测参数仍是演示假设。
- 这些值均标记为 `SCENARIO_ASSUMPTION`，模型运行标记为 `demonstration_only`。
- 正式使用时，应由可合法使用的数据源持续写入阶段六事实仓；假设须有依据并通过系统质量门，再运行同一模型合同。

这一区分保证系统不会把“算法已经可用”误写成“实时数据已经齐备”。
