#!/usr/bin/env python3
"""同步 AI基金经理公开指数基准行情。

写入 stock_daily_quotes，供 /api/ai-fund/nav 对比：
- 沪深300：000300.SH
- 中证消费指数：000990.SH（中证全指主要消费）
- 800消费指数：000932.SH（中证800消费/中证主要消费）
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "curated" / "consumer-research.db"

BENCHMARKS = [
    ("沪深300", "000300.SH", "1.000300"),
    ("中证消费指数", "000990.SH", "1.000990"),
    ("800消费指数", "000932.SH", "1.000932"),
]


def fetch_eastmoney_daily(secid: str, begin: str = "20250101", end: str = "20500101") -> list[dict]:
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": begin,
        "end": end,
        "lmt": "10000",
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:
            last_error = exc
            if attempt >= 3:
                raise
            time.sleep(attempt * 1.5)
    else:
        raise RuntimeError(f"指数行情请求失败: {last_error}")
    klines = (payload.get("data") or {}).get("klines") or []
    rows: list[dict] = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 10:
            continue
        try:
            close_price = float(parts[2])
            change_pct = float(parts[8])
        except ValueError:
            continue
        rows.append({
            "trade_date": parts[0],
            "close_price": close_price,
            "change_pct": change_pct,
        })
    return rows


def main() -> int:
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}", file=sys.stderr)
        return 1
    total = 0
    with sqlite3.connect(DB_PATH) as connection:
        for name, security_id, eastmoney_secid in BENCHMARKS:
            rows = fetch_eastmoney_daily(eastmoney_secid)
            for row in rows:
                connection.execute(
                    """INSERT INTO stock_daily_quotes(security_id, trade_date, close_price, change_pct)
                       VALUES(?,?,?,?)
                       ON CONFLICT(security_id, trade_date) DO UPDATE SET
                         close_price=excluded.close_price,
                         change_pct=excluded.change_pct""",
                    (security_id, row["trade_date"], row["close_price"], row["change_pct"]),
                )
            total += len(rows)
            latest = rows[-1]["trade_date"] if rows else "无"
            print(f"{name} {security_id}: {len(rows)} 条，最新 {latest}")
        connection.commit()
    print(f"指数基准同步完成: {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
