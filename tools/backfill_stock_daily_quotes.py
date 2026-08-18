# -*- coding: utf-8 -*-
"""一次性回填：全消费 A 股研究池近 1 个月每日收盘价/涨跌幅到 stock_daily_quotes。
供板块热力图（日/周/月）使用；15 只一批查询聚源，幂等，中断重跑自动续传。
此后每个交易日由 consumer_stock_focus 追加当日快照。
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from consumer_stock_focus import BJ, DB, load_universe, log, mcp_call  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS stock_daily_quotes (
  security_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  close_price REAL,
  change_pct REAL,
  PRIMARY KEY (security_id, trade_date)
);
"""

ROW_RE = re.compile(r"^\|(?P<name>[^|]+)\|(?P<code>\d{6})\|(?P<date>\d{4}-\d{2}-\d{2})\|")


def parse_hist(text: str) -> list[tuple[str, str, float, float]]:
    """解析历史日行情 markdown → [(6位代码, 交易日, 收盘价, 涨跌幅)]。"""
    out: list[tuple[str, str, float, float]] = []
    try:
        data = json.loads(text)
        tables = [r.get("table_markdown", "") for r in data.get("results", []) if isinstance(r, dict)]
    except json.JSONDecodeError:
        tables = [text]
    for table in tables:
        for line in table.splitlines():
            m = ROW_RE.match(line.strip())
            if not m:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 12:
                continue
            try:
                out.append((m.group("code"), m.group("date"), float(cells[8]), float(cells[11])))
            except (ValueError, IndexError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-07-15")
    ap.add_argument("--end", default=datetime.now(BJ).strftime("%Y-%m-%d"))
    ap.add_argument("--batch-size", type=int, default=15)
    args = ap.parse_args()

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute(DDL)
    universe = load_universe(db)
    code_to_id = {s["security_code"]: s["security_id"] for s in universe}
    done = {r[0] for r in db.execute(
        "SELECT DISTINCT security_id FROM stock_daily_quotes WHERE trade_date >= ?", (args.start,))}
    todo = [s for s in universe if s["security_id"] not in done]
    log(f"研究池 {len(universe)} 只，已覆盖 {len(done)} 只，待回填 {len(todo)} 只（{args.start}~{args.end}）")

    total_rows = 0
    batches = (len(todo) + args.batch_size - 1) // args.batch_size
    for i in range(0, len(todo), args.batch_size):
        batch = todo[i:i + args.batch_size]
        names = "、".join(s["security_name"] for s in batch)
        query = f"查询{names}在{args.start}至{args.end}每个交易日的收盘价、涨跌幅"
        rows: list[tuple[str, str, float, float]] = []
        for attempt in range(3):
            try:
                rows = parse_hist(mcp_call("FinQuery", query, timeout=120))
                break
            except Exception as e:
                log(f"  批次 {i // args.batch_size + 1} 第 {attempt + 1} 次失败: {type(e).__name__} {str(e)[:60]}")
                time.sleep(3)
        inserted = 0
        for code, trade_date, close, chg in rows:
            sid = code_to_id.get(code)
            if not sid:
                continue
            inserted += db.execute(
                "INSERT OR IGNORE INTO stock_daily_quotes(security_id, trade_date, close_price, change_pct) VALUES(?,?,?,?)",
                (sid, trade_date, close, chg),
            ).rowcount
        db.commit()
        total_rows += inserted
        log(f"  批次 {i // args.batch_size + 1}/{batches}: 解析 {len(rows)} 行，新增 {inserted} 行")
        time.sleep(1)
    log(f"回填完成：累计新增 {total_rows} 行")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
