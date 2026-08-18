# -*- coding: utf-8 -*-
"""全消费 A 股每日股票评级引擎（数据驱动）。

评分 = 动量 40%（日涨跌幅/量比/换手）+ 估值 30%（PE TTM 分档）+ 事件催化 30%（近 3 日新闻公告命中）。
四档：重点关注（前 10）、增持观察（次 15）、中性、回避（负面事件或末 15）。
结果写入 daily_stock_ratings（按 rating_date+security_id 幂等）。
"""
import argparse
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
MCP_URL = "https://api.gildata.com/mcp-servers/aidata-assistant-srv-tool?token=ed82c6584c824d9ba18aeee99d852317"
BJ = timezone(timedelta(hours=8))

DDL = """
CREATE TABLE IF NOT EXISTS daily_stock_ratings (
  rating_date TEXT NOT NULL,
  security_id TEXT NOT NULL,
  security_name TEXT NOT NULL,
  sector_code TEXT,
  close_price REAL, change_pct REAL, pe_ttm REAL,
  turnover_rate REAL, volume_ratio REAL, market_cap_yi REAL,
  event_hits INTEGER, event_score REAL,
  momentum_score REAL, valuation_score REAL,
  total_score REAL, tier TEXT NOT NULL, rationale TEXT,
  quote_trade_date TEXT,
  components_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY (rating_date, security_id)
);
"""

QUOTES_DDL = """
CREATE TABLE IF NOT EXISTS stock_daily_quotes (
  security_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  close_price REAL,
  change_pct REAL,
  PRIMARY KEY (security_id, trade_date)
);
"""

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS stock_quote_sync_progress (
  run_key TEXT NOT NULL,
  security_id TEXT NOT NULL,
  security_code TEXT NOT NULL,
  quote_time TEXT,
  close_price REAL,
  change_pct REAL,
  turnover_rate REAL,
  volume_ratio REAL,
  market_cap_yi REAL,
  pe_ttm REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_key, security_id)
);
"""

POS_HINT = re.compile(r"预增|提价|中标|扩产|新高|增长|获批|回购|增持|突破|回暖|超预期")
NEG_HINT = re.compile(r"预亏|减持|处罚|下滑|亏损|违规|退市|警示|质押|暴雷|低于预期")


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


def load_universe(db: sqlite3.Connection, limit: int = 0) -> list[dict]:
    sql = """SELECT security_id, MIN(security_code) AS security_code,
                    MIN(security_name) AS security_name, MIN(sector_code) AS sector_code
             FROM research_universe_members WHERE trading_status='正常交易'
             GROUP BY security_id ORDER BY security_id"""
    if limit:
        sql += f" LIMIT {limit}"
    return [dict(r) for r in db.execute(sql).fetchall()]


ROW_RE = re.compile(
    r"\|(?P<name>[^|]+)\|(?P<code>\d{6})\|(?P<time>[^|]+)\|(?P<status>[^|]+)\|(?P<price>[\d.]+)\|[^|]*\|[^|]*\|(?P<chg>-?[\d.]+)\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|(?P<turnover>[\d.]+)\|(?P<volratio>[\d.]+)\|"
)


def parse_realtime(text: str) -> dict[str, dict]:
    """从 A股实时行情 markdown 表解析每只股票；再补 PE TTM 与总市值。"""
    out: dict[str, dict] = {}
    if isinstance(text, str):
        try:
            data = json.loads(text)
            tables = [r.get("table_markdown", "") for r in data.get("results", []) if isinstance(r, dict)]
        except json.JSONDecodeError:
            tables = [text]
    else:
        tables = [t.get("table_markdown", "") for t in text]
    for table in tables:
        for line in table.splitlines():
            if not line.startswith("|") or "股票名称" in line or line.startswith("|-"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 20 or not re.fullmatch(r"\d{6}", cells[1] or ""):
                continue
            try:
                out[cells[1]] = {
                    "quote_time": cells[2],
                    "price": float(cells[4]),
                    "change_pct": float(cells[7]),
                    "turnover": float(cells[16]),
                    "volratio": float(cells[17]),
                    "market_cap": float(cells[28]) if len(cells) > 28 and cells[28] not in ("", "None") else None,
                    "pe_ttm": float(cells[32]) if len(cells) > 32 and cells[32] not in ("", "None") else None,
                }
            except (ValueError, IndexError):
                continue
    return out


def fetch_quotes(names: list[str], retries: int = 3) -> dict[str, dict]:
    query = f"查询{'、'.join(names)}的最新价、涨跌幅、市盈率TTM、换手率、量比、总市值"
    for attempt in range(retries + 1):
        try:
            return parse_realtime(mcp_call("FinQuery", query))
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            wait = int(retry_after) if retry_after and retry_after.isdigit() else min(2 ** attempt, 12)
            log(f"    HTTP {e.code}，{wait}秒后重试 {attempt + 1}/{retries + 1}")
            if attempt < retries:
                time.sleep(wait)
        except Exception as e:
            wait = min(2 ** attempt, 8)
            log(f"    {type(e).__name__}，{wait}秒后重试 {attempt + 1}/{retries + 1}: {str(e)[:60]}")
            if attempt < retries:
                time.sleep(wait)
    return {}


def fetch_event_hits(db: sqlite3.Connection, names: list[str], days: int = 3) -> dict[str, int]:
    """近 N 天事件库中按股票名命中计数（正 +1 / 负 -2）。"""
    since = (datetime.now(BJ) - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT title, summary FROM monitor_events WHERE available_at >= ?", (since,)
    ).fetchall()
    hits: dict[str, int] = {}
    for title, summary in rows:
        # 先剥离否定表述，避免"无失信/无违规"被误判为负面
        text = re.sub(r"(无|没有|未|不存在|不涉及)[^，。；,;]{0,12}?(失信|违规|违法|处罚|经营异常|风险|减持|亏损)", "", f"{title} {summary or ''}")
        sign = 1 if POS_HINT.search(text) else -2 if NEG_HINT.search(text) else 0
        if sign == 0:
            continue
        for name in names:
            if len(name) >= 2 and name in text:
                hits[name] = hits.get(name, 0) + sign
    return hits


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_momentum(q: dict) -> float:
    chg = clamp((q["change_pct"] + 5) * 10)            # -5%..+5% -> 0..100
    vol = clamp((q["volratio"] - 0.4) / 2.1 * 100)     # 0.4..2.5 -> 0..100
    turn = clamp(q["turnover"] / 5 * 100)              # 0..5% -> 0..100
    return round(chg * 0.5 + vol * 0.3 + turn * 0.2, 1)


def score_valuation(pe: float | None) -> float:
    if pe is None:
        return 50.0
    if pe <= 0:
        return 25.0
    if pe < 15:
        return 100.0
    if pe < 25:
        return 80.0
    if pe < 40:
        return 60.0
    if pe < 60:
        return 40.0
    return 25.0


def score_events(net_hits: int) -> float:
    return clamp(50 + net_hits * 20)


def build_rationale(q: dict, pe: float | None, hits: int, tier: str) -> str:
    parts = []
    if q["change_pct"] >= 1:
        parts.append(f"涨{q['change_pct']:.1f}%")
    elif q["change_pct"] <= -1:
        parts.append(f"跌{abs(q['change_pct']):.1f}%")
    if q["volratio"] >= 1.5:
        parts.append(f"量比{q['volratio']:.1f}放量")
    if pe is not None and 0 < pe < 20:
        parts.append(f"PE {pe:.0f}倍低估")
    elif pe is not None and pe > 50:
        parts.append(f"PE {pe:.0f}倍偏高")
    if hits > 0:
        parts.append(f"近3日{hits}条正面催化")
    elif hits < 0:
        parts.append(f"近3日{abs(hits)}条负面事件")
    return "、".join(parts) if parts else "量价平稳，无显著催化"


def run(limit: int = 0, batch_size: int = 30, workers: int = 2) -> dict:
    now_bj = datetime.now(BJ)
    today = now_bj.strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute(DDL)
    db.execute(PROGRESS_DDL)
    rating_columns = {row[1] for row in db.execute("PRAGMA table_info(daily_stock_ratings)").fetchall()}
    if "quote_trade_date" not in rating_columns:
        db.execute("ALTER TABLE daily_stock_ratings ADD COLUMN quote_trade_date TEXT")
    universe = load_universe(db, limit)
    phase = "morning" if (now_bj.hour, now_bj.minute) < (9, 30) else "close" if (now_bj.hour, now_bj.minute) >= (15, 5) else "intraday"
    run_key = f"{today}:{phase}"
    cleanup_before = (now_bj - timedelta(days=3)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db.execute("DELETE FROM stock_quote_sync_progress WHERE updated_at < ?", (cleanup_before,))
    db.commit()
    log(f"评级对象: {len(universe)} 只（{today}，{phase}，自适应批次{batch_size}）")

    # 08:30 盘前同步拿到的是上一交易日收盘行情，不能错标为当天行情。
    quote_trade_date = today
    if (now_bj.hour, now_bj.minute) < (9, 30):
        last_close = db.execute(
            """SELECT event_time FROM monitor_events
               WHERE status='accepted' AND event_type='market_move'
               ORDER BY available_at DESC LIMIT 1"""
        ).fetchone()
        if last_close and re.match(r"\d{4}-\d{2}-\d{2}", str(last_close[0] or "")):
            quote_trade_date = str(last_close[0])[:10]

    quotes: dict[str, dict] = {
        row["security_code"]: {
            "quote_time": row["quote_time"], "price": row["close_price"],
            "change_pct": row["change_pct"], "turnover": row["turnover_rate"],
            "volratio": row["volume_ratio"], "market_cap": row["market_cap_yi"],
            "pe_ttm": row["pe_ttm"],
        }
        for row in db.execute("SELECT * FROM stock_quote_sync_progress WHERE run_key=?", (run_key,)).fetchall()
    }
    pending = [s for s in universe if s["security_code"] not in quotes]
    if quotes:
        log(f"  断点恢复: 已有 {len(quotes)} 只，仅请求剩余 {len(pending)} 只")
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]

    def fetch_adaptive(batch: list[dict]) -> dict[str, dict]:
        names = [s["security_name"] for s in batch]
        got = fetch_quotes(names)
        missing = [s for s in batch if s["security_code"] not in got]
        if missing and len(batch) > 15:
            log(f"    大批次缺失 {len(missing)} 只，自动拆分为15只以内补取")
            for j in range(0, len(missing), 15):
                supplement = missing[j:j + 15]
                got.update(fetch_quotes([s["security_name"] for s in supplement], retries=2))
        return got

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 2))) as pool:
        future_batches = {pool.submit(fetch_adaptive, batch): batch for batch in batches}
        for future in as_completed(future_batches):
            batch = future_batches[future]
            got = future.result()
            quotes.update(got)
            completed += 1
            batch_by_code = {s["security_code"]: s for s in batch}
            for code, q in got.items():
                security = batch_by_code.get(code)
                if not security:
                    continue
                db.execute(
                    """INSERT OR REPLACE INTO stock_quote_sync_progress(
                           run_key,security_id,security_code,quote_time,close_price,change_pct,
                           turnover_rate,volume_ratio,market_cap_yi,pe_ttm,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_key, security["security_id"], code, q.get("quote_time"), q.get("price"),
                     q.get("change_pct"), q.get("turnover"), q.get("volratio"),
                     q.get("market_cap"), q.get("pe_ttm"), now_utc),
                )
            db.commit()
            log(f"  行情批次 {completed}/{len(batches)}: 返回 {len(got)}/{len(batch)}，断点已保存")

    # 开盘后优先采用聚源行情时间中的实际日期；盘前强制采用最近收盘事件日期。
    vendor_dates = [
        match.group(0)
        for q in quotes.values()
        if (match := re.search(r"20\d{2}-\d{2}-\d{2}", str(q.get("quote_time") or "")))
    ]
    vendor_trade_date = max(set(vendor_dates), key=vendor_dates.count) if vendor_dates else None
    if (now_bj.hour, now_bj.minute) >= (9, 30) and vendor_trade_date and vendor_trade_date <= today:
        quote_trade_date = vendor_trade_date

    name_hits = fetch_event_hits(db, [s["security_name"] for s in universe])
    log(f"行情覆盖 {len(quotes)}/{len(universe)}；事件命中股票 {len(name_hits)} 只")
    minimum_coverage = max(1, int(len(universe) * 0.80))
    if len(quotes) < minimum_coverage:
        db.close()
        raise RuntimeError(f"行情覆盖不足80%（{len(quotes)}/{len(universe)}），已保存断点，保留原评级批次")

    # 全池行情快照落库：板块热力图（日/周/月）的数据基础
    db.execute(QUOTES_DDL)
    code_to_id = {s["security_code"]: s["security_id"] for s in universe}
    for code, q in quotes.items():
        sid = code_to_id.get(code)
        if sid:
            db.execute(
                "INSERT OR REPLACE INTO stock_daily_quotes(security_id, trade_date, close_price, change_pct) VALUES(?,?,?,?)",
                (sid, quote_trade_date, q["price"], q["change_pct"]),
            )
    db.commit()
    log(f"行情快照实际交易日: {quote_trade_date}")

    records = []
    for s in universe:
        code = s["security_code"]
        q = quotes.get(code)
        if not q:
            continue
        pe = q.get("pe_ttm")
        hits = name_hits.get(s["security_name"], 0)
        m = score_momentum(q)
        v = score_valuation(pe)
        ev = score_events(hits)
        total = round(m * 0.4 + v * 0.3 + ev * 0.3, 1)
        records.append({
            "security_id": s["security_id"], "security_name": s["security_name"],
            "sector_code": s["sector_code"], "close_price": q["price"], "change_pct": q["change_pct"],
            "pe_ttm": pe, "turnover_rate": q["turnover"], "volume_ratio": q["volratio"],
            "market_cap_yi": q.get("market_cap"), "event_hits": hits, "event_score": ev,
            "momentum_score": m, "valuation_score": v, "total_score": total,
            "rationale": build_rationale(q, pe, hits, ""),
            "quote_trade_date": quote_trade_date,
            "components_json": json.dumps({"momentum": m, "valuation": v, "event": ev,
                                             "quote_trade_date": quote_trade_date}, ensure_ascii=False),
        })

    records.sort(key=lambda r: r["total_score"], reverse=True)
    display: list[dict] = []
    for idx, r in enumerate(records):
        if r["event_hits"] <= -2 or idx >= len(records) - 15:
            r["tier"] = "回避"
            display.append(r)
        elif idx < 20:
            r["tier"] = "重点关注"
            display.append(r)
        elif idx < 60:
            r["tier"] = "增持观察"
            display.append(r)
        elif idx < 120:
            r["tier"] = "中性"
            display.append(r)
        # 120 名以外不入库展示

    db.execute("DELETE FROM daily_stock_ratings WHERE rating_date=?", (today,))
    records = display
    for r in records:
        db.execute(
            """INSERT OR REPLACE INTO daily_stock_ratings(
                   rating_date,security_id,security_name,sector_code,close_price,change_pct,pe_ttm,
                   turnover_rate,volume_ratio,market_cap_yi,event_hits,event_score,momentum_score,
                   valuation_score,total_score,tier,rationale,quote_trade_date,components_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today, r["security_id"], r["security_name"], r["sector_code"], r["close_price"],
             r["change_pct"], r["pe_ttm"], r["turnover_rate"], r["volume_ratio"], r["market_cap_yi"],
             r["event_hits"], r["event_score"], r["momentum_score"], r["valuation_score"],
             r["total_score"], r["tier"], r["rationale"], r["quote_trade_date"], r["components_json"], now_utc),
        )
    db.commit()
    tiers = {}
    for r in records:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    log(f"评级完成: {len(records)} 只入库；分布 {tiers}")
    db.close()
    return {"date": today, "market_date": quote_trade_date, "count": len(records), "tiers": tiers}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 只（测试用）")
    ap.add_argument("--batch-size", type=int, default=30)
    ap.add_argument("--workers", type=int, default=2, choices=(1, 2))
    args = ap.parse_args()
    run(limit=args.limit, batch_size=args.batch_size, workers=args.workers)
