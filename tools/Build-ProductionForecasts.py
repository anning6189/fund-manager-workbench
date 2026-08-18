# -*- coding: utf-8 -*-
"""生成美的/格力 2026E 正式财务预测包并运行阶段七引擎（production，非演示）。
情景假设锚定 2023A 实际比率与 2026Q1 实际增速，全部留痕。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\chi\Documents\ChatGPT\New project")
OUT = ROOT / "data" / "models" / "production"
OUT.mkdir(parents=True, exist_ok=True)
PY = sys.executable
ENGINE = ROOT / "tools" / "consumer_model_engine.py"
DB = ROOT / "data" / "curated" / "consumer-research.db"

CALCULATIONS = [
    {"output_id": "historical_gross_margin", "output_role": "historical_gross_margin", "formula": "1 - actual_cost / actual_revenue", "unit": "ratio", "content_label": "AGENT_CALCULATION"},
    {"output_id": "historical_selling_ratio", "output_role": "historical_selling_ratio", "formula": "actual_selling_expense / actual_revenue", "unit": "ratio", "content_label": "AGENT_CALCULATION"},
    {"output_id": "historical_cfo_conversion", "output_role": "historical_cfo_conversion", "formula": "actual_cfo / actual_net_profit", "unit": "multiple", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_revenue", "output_role": "forecast_revenue", "formula": "actual_revenue * (1 + revenue_growth)", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_gross_profit", "output_role": "forecast_gross_profit", "formula": "forecast_revenue * gross_margin", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_operating_expense", "output_role": "forecast_operating_expense", "formula": "forecast_revenue * operating_expense_ratio", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_operating_profit", "output_role": "forecast_operating_profit", "formula": "forecast_gross_profit - forecast_operating_expense", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_net_profit", "output_role": "forecast_net_profit", "formula": "forecast_operating_profit * (1 - effective_tax_rate)", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_cfo", "output_role": "forecast_cfo", "formula": "forecast_net_profit * cfo_conversion", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_capex", "output_role": "forecast_capex", "formula": "forecast_revenue * capex_ratio", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
    {"output_id": "forecast_fcf", "output_role": "forecast_fcf", "formula": "forecast_cfo - forecast_capex", "unit": "CNY", "content_label": "AGENT_CALCULATION"},
]

COMPANIES = [
    {
        "entity": "cr:legal_entity:midea", "name": "美的集团", "scope": "CN:MIDEA:CONSOLIDATED:2026E",
        "assumptions": {
            "bear": (0.010, 0.260, 0.140, 0.17, 1.50, 0.030),
            "base": (0.030, 0.268, 0.136, 0.17, 1.70, 0.030),
            "bull": (0.060, 0.275, 0.132, 0.17, 1.85, 0.030),
        },
        "rationale": "2023A毛利率26.5%、销售费用率9.4%、现金转换1.72x；2026Q1营收+2.55%。",
    },
    {
        "entity": "cr:legal_entity:gree", "name": "格力电器", "scope": "CN:GREE:CONSOLIDATED:2026E",
        "assumptions": {
            "bear": (0.015, 0.295, 0.135, 0.15, 1.70, 0.025),
            "base": (0.035, 0.304, 0.130, 0.15, 1.90, 0.025),
            "bull": (0.065, 0.310, 0.126, 0.15, 2.00, 0.025),
        },
        "rationale": "2023A毛利率30.6%、销售费用率8.4%、现金转换1.94x；2026Q1营收+3.52%。",
    },
]


def build_package(company: dict) -> dict:
    scope = company["scope"]
    facts = [
        ("actual_revenue", "CR.CO.REVENUE"), ("actual_cost", "CR.CO.OPERATING_COST"),
        ("actual_selling_expense", "CR.CO.SELLING_EXPENSE"), ("actual_net_profit", "CR.CO.PARENT_NET_PROFIT"),
        ("actual_cfo", "CR.CO.CFO_NET"),
    ]
    scenarios = []
    for sid, (g, gm, oe, tax, cfo, capex) in company["assumptions"].items():
        scenarios.append({
            "scenario_id": sid,
            "assumptions": [
                {"input_id": "revenue_growth", "value": g, "unit": "ratio", "scope_key": scope, "content_label": "SCENARIO_ASSUMPTION", "available_at": "2026-08-14T00:00:00+08:00", "rationale": company["rationale"], "confidence": 0.5},
                {"input_id": "gross_margin", "value": gm, "unit": "ratio", "scope_key": scope, "content_label": "SCENARIO_ASSUMPTION", "available_at": "2026-08-14T00:00:00+08:00", "rationale": company["rationale"], "confidence": 0.5},
                {"input_id": "operating_expense_ratio", "value": oe, "unit": "ratio", "scope_key": scope, "content_label": "SCENARIO_ASSUMPTION", "available_at": "2026-08-14T00:00:00+08:00", "rationale": company["rationale"], "confidence": 0.5},
                {"input_id": "effective_tax_rate", "value": tax, "unit": "ratio", "scope_key": scope, "content_label": "SCENARIO_ASSUMPTION", "available_at": "2026-08-14T00:00:00+08:00", "rationale": "按公司历史有效税率设定。", "confidence": 0.5},
                {"input_id": "cfo_conversion", "value": cfo, "unit": "multiple", "scope_key": scope, "content_label": "SCENARIO_ASSUMPTION", "available_at": "2026-08-14T00:00:00+08:00", "rationale": company["rationale"], "confidence": 0.5},
                {"input_id": "capex_ratio", "value": capex, "unit": "ratio", "scope_key": scope, "content_label": "SCENARIO_ASSUMPTION", "available_at": "2026-08-14T00:00:00+08:00", "rationale": "按近年资本开支强度设定。", "confidence": 0.5},
            ],
        })
    return {
        "package_id": f"CR.MODEL.PKG.PROD.FINANCIAL_FORECAST.{company['name']}.2026E.001",
        "model_id": "CR.MODEL.FINANCIAL_FORECAST",
        "model_type": "financial_forecast",
        "as_of_timestamp": "2026-08-14T00:00:00+08:00",
        "environment": "production",
        "scope": {
            "scope_key": scope, "entity_id": company["entity"], "forecast_period": "2026E",
            "currency": "CNY",
            "note": f"{company['name']}2026E正式预测；2023A来自阶段六事实仓，2026Q1实际值来自聚源授权数据。",
        },
        "fact_inputs": [
            {"input_id": fid, "entity_id": company["entity"], "metric_id": mid, "period_end": "2023-12-31",
             "unit": "CNY", "scope_key": scope, "content_label": "FACT_OBSERVATION"}
            for fid, mid in facts
        ],
        "scenarios": scenarios,
        "calculations": CALCULATIONS,
        "sensitivities": [
            {"sensitivity_id": f"{scope}:sens:growth:net_profit", "scenario_id": "base",
             "x_input_id": "revenue_growth",
             "x_values": [company["assumptions"]["base"][0] - 0.01, company["assumptions"]["base"][0], company["assumptions"]["base"][0] + 0.01],
             "output_id": "forecast_net_profit"},
            {"sensitivity_id": f"{scope}:sens:gm:growth:net_profit", "scenario_id": "base",
             "x_input_id": "gross_margin", "x_values": [company["assumptions"]["base"][1] - 0.01, company["assumptions"]["base"][1], company["assumptions"]["base"][1] + 0.01],
             "y_input_id": "revenue_growth", "y_values": [company["assumptions"]["base"][0] - 0.01, company["assumptions"]["base"][0], company["assumptions"]["base"][0] + 0.01],
             "output_id": "forecast_net_profit"},
        ],
    }


for company in COMPANIES:
    pkg = build_package(company)
    path = OUT / f"financial-forecast-{company['scope'].split(':')[1].lower()}-2026e.json"
    path.write_text(json.dumps(pkg, ensure_ascii=False, indent=2), encoding="utf-8")
    result = subprocess.run([PY, str(ENGINE), "--db", str(DB), "run", "--package", str(path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    line = (result.stdout or result.stderr).strip().replace("\n", " ")[:150]
    print(f"{company['name']}: rc={result.returncode} {line}")
