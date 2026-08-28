#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最终交付冒烟测试。

覆盖当前公开版核心能力：
- 首页/晨报/股票池/研报库/数据来源/规则审计；
- AI基金经理 30 只持仓、净值、三条公开指数基准；
- 用户模型配置接口与系统模型隔离状态；
- 数据库关键表与敏感信息边界。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
BASE = os.environ.get("CONSUMER_RESEARCH_TEST_BASE", "http://127.0.0.1:8765").rstrip("/")


checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    checks.append((name, ok, text))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" :: {text}" if text else ""))


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    health = get("/api/health")
    check("服务健康", health.get("status") == "ok", health)

    bootstrap = get("/api/bootstrap")
    counts = bootstrap.get("counts", {})
    check("基础数据规模", counts.get("sectors", 0) >= 10 and counts.get("products", 0) >= 100, counts)

    brief = get("/api/morning-brief")
    check("晨报可用", bool(brief.get("takeaway")) and bool(brief.get("risks")), {
        "takeaway": len(brief.get("takeaway", [])),
        "risks": len(brief.get("risks", [])),
        "date": brief.get("date") or brief.get("brief_source_date"),
    })

    focus = get("/api/stock-focus")
    board_counts = focus.get("board_counts") or {}
    main_push = focus.get("main_push") or []
    board = focus.get("board") or {}
    buy_candidate_count = board_counts.get("可以考虑买入", board_counts.get("核心候选", 0))
    check("股票推荐分层可用", len(main_push) <= 5 and buy_candidate_count >= 20, {
        "main_push": len(main_push),
        "board_counts": board_counts,
    })
    first_group = main_push or board.get("可以考虑买入") or []
    check("股票详情样本存在", bool(first_group and first_group[0].get("security_id")), first_group[0].get("security_id") if first_group else "无")

    heatmap = get("/api/sector-heatmap?period=day")
    check("热力图可用", len(heatmap.get("sectors", [])) >= 10, len(heatmap.get("sectors", [])))

    library = get("/api/research-library")
    check("研报库可用", bool(library.get("items") or library.get("sections") or library.get("reports")), list(library.keys()))

    sources = get("/api/data-sources")
    check("数据来源页可用", bool(sources.get("groups")), len(sources.get("groups", [])))

    audit = get("/api/self-calibration")
    check("规则审计可用", bool(audit), list(audit.keys()))

    llm_status = get("/api/llm/status")
    check("用户模型接口可用", "enabled" in llm_status, llm_status)

    system_llm = get("/api/system-llm/status")
    check("系统模型状态可读", "enabled" in system_llm and "note" in system_llm, system_llm)

    fund_overview = get("/api/ai-fund/overview")
    check("AI基金经理概览", fund_overview.get("position_count") == 30 and fund_overview.get("latest_nav", 0) > 0, {
        "date": fund_overview.get("date"),
        "positions": fund_overview.get("position_count"),
        "nav": fund_overview.get("latest_nav"),
    })

    fund_positions = get("/api/ai-fund/positions")
    positions = fund_positions.get("positions", [])
    total_weight = sum(float(p.get("weight") or 0) for p in positions)
    check("AI基金经理持仓权重", len(positions) == 30 and 99 <= total_weight <= 101, {
        "count": len(positions),
        "total_weight": round(total_weight, 2),
    })

    fund_nav = get("/api/ai-fund/nav")
    benchmarks = fund_nav.get("benchmarks", [])
    benchmark_names = {b.get("name") for b in benchmarks}
    benchmark_ok = benchmark_names >= {"沪深300", "中证消费指数", "800消费指数"} and all(b.get("points") for b in benchmarks)
    check("AI基金经理公开指数基准", benchmark_ok, [
        {"name": b.get("name"), "status": b.get("status"), "points": len(b.get("points") or [])}
        for b in benchmarks
    ])

    strategy = get("/api/ai-fund/strategy")
    check("AI基金经理策略说明", bool(strategy.get("summary") or strategy.get("ai_strategy")), {
        "ai_generated": strategy.get("ai_generated"),
        "mode": (strategy.get("ai_strategy") or {}).get("mode"),
    })

    history = get("/api/ai-fund/history")
    check("AI基金经理历史版本", bool(history.get("versions")), len(history.get("versions", [])))

    if DB_PATH.exists():
        connection = sqlite3.connect(DB_PATH)
        try:
            secret_hits = []
            for table in ("workbench_settings", "source_cursors"):
                try:
                    rows = connection.execute(f"SELECT * FROM {table} LIMIT 50").fetchall()
                    if any("sk-" in str(row) for row in rows):
                        secret_hits.append(table)
                except sqlite3.Error:
                    pass
            check("数据库未发现常见 OpenAI Key 字样", not secret_hits, secret_hits)
        finally:
            connection.close()

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\nFINAL_DELIVERY_SMOKE {passed}/{len(checks)} passed against {BASE}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
