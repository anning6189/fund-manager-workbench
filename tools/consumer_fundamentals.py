# -*- coding: utf-8 -*-
"""全消费 A 股基本面与一致预期层：stock_fundamentals + stock_consensus。

一次 FinQuery 批量调用同时返回：财务报表（营收/净利/商誉）、财务分析（净利同比/股利支付率）、
一致预期（分年度净利/EPS/ROE/目标价）。按 15 只/批全池刷新。
基本面表按报告期幂等；一致预期按 估计日期+年度 幂等。"""
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
MCP_URL = "https://api.gildata.com/mcp-servers/aidata-assistant-srv-tool?token=ed82c6584c824d9ba18aeee99d852317"
BJ = timezone(timedelta(hours=8))

DDL = """
CREATE TABLE IF NOT EXISTS stock_fundamentals (
  security_id TEXT NOT NULL, security_name TEXT NOT NULL, report_period TEXT NOT NULL,
  revenue_yi REAL, revenue_yoy REAL, net_profit_yi REAL, profit_yoy REAL,
  goodwill_yi REAL, dividend_paid_ratio REAL, updated_at TEXT NOT NULL,
  PRIMARY KEY (security_id, report_period)
);
CREATE TABLE IF NOT EXISTS stock_consensus (
  security_id TEXT NOT NULL, security_name TEXT NOT NULL, estimate_date TEXT NOT NULL,
  forecast_year INTEGER NOT NULL, revenue_wy REAL, revenue_yoy REAL,
  net_profit_wy REAL, np_yoy REAL, eps REAL, roe REAL, pe REAL, pb REAL,
  target_price REAL, updated_at TEXT NOT NULL,
  PRIMARY KEY (security_id, forecast_year)
);
"""


def log(msg: str) -> None:
    print(f"[{datetime.now(BJ).isoformat(timespec='seconds')}] {msg}", flush=True)


def mcp_call(tool: str, query: str, timeout: int = 90) -> str:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": {"query": query}}}
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    parts = data.get("result", {}).get("content", [])
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def parse_results(text: str) -> dict[str, list[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    tables: dict[str, list[str]] = {}
    for r in data.get("results", []):
        if isinstance(r, dict):
            tables.setdefault(r.get("api_name", ""), []).append(r.get("table_markdown", ""))
    return tables


def rows_of(table: str) -> list[list[str]]:
    out = []
    for line in table.splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        out.append(cells)
    return out


def num(v: str):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def parse_batch(tables: dict[str, list[str]], now_utc: str) -> tuple[list, list]:
    """从三类表提取 基本面/一致预期 记录。"""
    fundamentals: dict[str, dict] = {}
    consensus: dict[str, list] = {}
    for table in tables.get("财务报表", []):
        for c in rows_of(table):
            if len(c) < 13 or c[0] in ("股票名称",):
                continue
            name, code, subject = c[0], c[1], c[2]
            period = c[7][:10]
            if not period or "年报" not in c[8]:
                continue
            key = (code, name, period)
            rec = fundamentals.setdefault(key, {
                "security_id": code, "security_name": name, "report_period": period,
                "revenue_yi": None, "revenue_yoy": None, "net_profit_yi": None,
                "profit_yoy": None, "goodwill_yi": None, "dividend_paid_ratio": None,
                "updated_at": now_utc,
            })
            if subject == "营业收入" and c[4] == "3级":
                rec["revenue_yi"], rec["revenue_yoy"] = num(c[10]), num(c[12])
            elif subject == "归属于母公司所有者的净利润":
                rec["net_profit_yi"], rec["profit_yoy"] = num(c[10]), num(c[12])
            elif subject == "商誉":
                rec["goodwill_yi"] = num(c[10])
    for table in tables.get("财务分析", []):
        for c in rows_of(table):
            if len(c) < 8 or c[0] in ("股票名称",):
                continue
            for key, rec in fundamentals.items():
                if key[1] == c[0] and key[0] == c[1] and key[2].startswith(c[5][:4]):
                    if "股利支付率" in c[2]:
                        rec["dividend_paid_ratio"] = num(c[7])
    for table in tables.get("一致预期", []):
        for c in rows_of(table):
            if len(c) < 20 or c[0] in ("股票名称",):
                continue
            year = int(c[3]) if c[3].isdigit() else None
            if not year:
                continue
            consensus.setdefault((c[1], c[0]), []).append({
                "security_id": c[1], "security_name": c[0], "estimate_date": c[2][:10],
                "forecast_year": year,
                "revenue_wy": num(c[4]), "revenue_yoy": num(c[5]),
                "net_profit_wy": num(c[14]), "np_yoy": num(c[15]),
                "eps": num(c[18]), "roe": num(c[19]), "pe": num(c[22]),
                "pb": num(c[23]), "target_price": num(c[26]) if len(c) > 26 else None,
                "updated_at": now_utc,
            })
    return list(fundamentals.values()), [r for rows in consensus.values() for r in rows]


def run(limit: int = 0, batch_size: int = 15) -> dict:
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)
    universe = db.execute(
        "SELECT security_id, security_name FROM research_universe_members WHERE trading_status='正常交易' ORDER BY security_id" + (f" LIMIT {limit}" if limit else "")
    ).fetchall()
    log(f"基本面/一致预期刷新: {len(universe)} 只")
    total_f, total_c = 0, 0
    for i in range(0, len(universe), batch_size):
        batch = universe[i:i + batch_size]
        names = "、".join(s["security_name"] for s in batch)
        query = f"查询{names}的最新年报营业收入及同比、归母净利润及同比、商誉、股利支付率，以及最新一致预期的净利润与目标价"
        try:
            tables = parse_results(mcp_call("FinQuery", query))
        except Exception as e:
            log(f"  批次 {i // batch_size + 1} 失败: {type(e).__name__} {str(e)[:60]}")
            continue
        fundamentals, consensus = parse_batch(tables, now_utc)
        for rec in fundamentals:
            db.execute(
                """INSERT OR REPLACE INTO stock_fundamentals(security_id,security_name,report_period,
                     revenue_yi,revenue_yoy,net_profit_yi,profit_yoy,goodwill_yi,dividend_paid_ratio,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (rec["security_id"], rec["security_name"], rec["report_period"], rec["revenue_yi"],
                 rec["revenue_yoy"], rec["net_profit_yi"], rec["profit_yoy"], rec["goodwill_yi"],
                 rec["dividend_paid_ratio"], rec["updated_at"]),
            )
        for rec in consensus:
            db.execute(
                """INSERT OR REPLACE INTO stock_consensus(security_id,security_name,estimate_date,forecast_year,
                     revenue_wy,revenue_yoy,net_profit_wy,np_yoy,eps,roe,pe,pb,target_price,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec["security_id"], rec["security_name"], rec["estimate_date"], rec["forecast_year"],
                 rec["revenue_wy"], rec["revenue_yoy"], rec["net_profit_wy"], rec["np_yoy"], rec["eps"],
                 rec["roe"], rec["pe"], rec["pb"], rec["target_price"], rec["updated_at"]),
            )
        db.commit()
        total_f += len(fundamentals)
        total_c += len(consensus)
        log(f"  批次 {i // batch_size + 1}/{(len(universe) + batch_size - 1) // batch_size}: 基本面 {len(fundamentals)} 一致预期 {len(consensus)}")
    log(f"完成: 基本面 {total_f} 条, 一致预期 {total_c} 条")
    db.close()
    return {"fundamentals": total_f, "consensus": total_c}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    run(limit=args.limit)
