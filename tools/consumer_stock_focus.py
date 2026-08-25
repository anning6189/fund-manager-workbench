# -*- coding: utf-8 -*-
"""全消费 A 股每日股票评级引擎（AutoInvest Agent）。

核心目标是中期 / 中长期持有推荐，不是每日短线排行榜。
当日动量只是买入时点因子；长期好公司和核心候选必须具备跨日状态记忆。
结果写入 daily_stock_ratings（按 rating_date+security_id 幂等）。
"""
import argparse
import json
import os
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
MCP_URL = os.environ.get("GILDATA_MCP_URL") or (
    f"https://api.gildata.com/mcp-servers/aidata-assistant-srv-tool?token={os.environ.get('GILDATA_MCP_TOKEN', '')}"
)
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
  invest_score REAL,
  stability_score REAL,
  board_status TEXT,
  holding_label TEXT,
  state_reason TEXT,
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
                price = float(cells[4])
                if price <= 0:
                    continue
                out[cells[1]] = {
                    "quote_time": cells[2],
                    "price": price,
                    "change_pct": float(cells[7]),
                    "turnover": float(cells[16]),
                    "volratio": float(cells[17]),
                    "market_cap": float(cells[28]) if len(cells) > 28 and cells[28] not in ("", "None") else None,
                    "pe_ttm": float(cells[32]) if len(cells) > 32 and cells[32] not in ("", "None") else None,
                }
            except (ValueError, IndexError):
                continue
    return out


def valid_quote(q: dict | None) -> bool:
    if not q:
        return False
    try:
        return float(q.get("price") or 0) > 0
    except (TypeError, ValueError):
        return False


def quote_move_is_reasonable(db: sqlite3.Connection, security_id: str, price: float, trade_date: str, max_move: float = 0.35) -> bool:
    previous = db.execute(
        """SELECT close_price
           FROM stock_daily_quotes
           WHERE security_id=? AND trade_date < ? AND close_price IS NOT NULL AND close_price > 0
           ORDER BY trade_date DESC LIMIT 1""",
        (security_id, trade_date),
    ).fetchone()
    if not previous or not previous[0]:
        return True
    prev = float(previous[0])
    if prev <= 0:
        return True
    return abs(float(price) / prev - 1) <= max_move


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


def fetch_event_hits(db: sqlite3.Connection, names: list[str], days: int = 3, now: datetime | None = None) -> dict[str, int]:
    """近 N 天事件库中按股票名命中计数（正 +1 / 负 -2）。"""
    now = now or datetime.now(BJ)
    since = (now - timedelta(days=days)).isoformat()
    until = now.isoformat()
    rows = db.execute(
        "SELECT title, summary FROM monitor_events WHERE available_at >= ? AND available_at <= ?", (since, until)
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


def is_risk_name(name: str | None) -> bool:
    value = str(name or "").upper()
    return "ST" in value or "退" in value


def score_stability(history: dict | None, valuation_score: float) -> float:
    """跨日稳定分：长期池不能每天跟着行情排名消失。"""
    if not history:
        return round(max(45.0, min(75.0, valuation_score)), 1)
    seen = max(1, int(history.get("seen") or 1))
    avg_score = float(history.get("avg_invest_score") or history.get("avg_total_score") or valuation_score)
    long_days = int(history.get("long_days") or 0)
    risk_days = int(history.get("risk_days") or 0)
    continuity = min(20.0, long_days / seen * 25.0)
    penalty = min(35.0, risk_days * 12.0)
    return round(clamp(avg_score * 0.75 + continuity - penalty), 1)


def build_state_reason(r: dict, history: dict | None) -> str:
    parts = []
    if history:
        parts.append(f"近{int(history.get('seen') or 0)}个评级日有状态记忆")
        if history.get("long_days"):
            parts.append(f"长期池命中{int(history['long_days'])}日")
    parts.append(f"投资分{r['invest_score']:.1f}")
    if r.get("event_hits"):
        parts.append(f"事件{r['event_hits']}条")
    if r.get("pe_ttm") and r["pe_ttm"] > 0:
        parts.append(f"PE {r['pe_ttm']:.0f}倍")
    return "；".join(parts)


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


def run(limit: int = 0, batch_size: int = 30, workers: int = 2, target_date: str | None = None, historical_local: bool = False) -> dict:
    now_bj = datetime.now(BJ)
    if target_date:
        now_bj = datetime.fromisoformat(f"{target_date}T16:30:00+08:00")
    today = now_bj.strftime("%Y-%m-%d")
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute(DDL)
    db.execute(PROGRESS_DDL)
    rating_columns = {row[1] for row in db.execute("PRAGMA table_info(daily_stock_ratings)").fetchall()}
    if "quote_trade_date" not in rating_columns:
        db.execute("ALTER TABLE daily_stock_ratings ADD COLUMN quote_trade_date TEXT")
    for column, ddl in {
        "invest_score": "ALTER TABLE daily_stock_ratings ADD COLUMN invest_score REAL",
        "stability_score": "ALTER TABLE daily_stock_ratings ADD COLUMN stability_score REAL",
        "board_status": "ALTER TABLE daily_stock_ratings ADD COLUMN board_status TEXT",
        "holding_label": "ALTER TABLE daily_stock_ratings ADD COLUMN holding_label TEXT",
        "state_reason": "ALTER TABLE daily_stock_ratings ADD COLUMN state_reason TEXT",
    }.items():
        if column not in rating_columns:
            db.execute(ddl)
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

    quotes: dict[str, dict] = {}
    if historical_local:
        fallback_rows = db.execute(
            """SELECT p.*
               FROM stock_quote_sync_progress p
               JOIN (
                   SELECT security_id, MAX(updated_at) AS updated_at
                   FROM stock_quote_sync_progress
                   GROUP BY security_id
               ) latest ON latest.security_id=p.security_id AND latest.updated_at=p.updated_at"""
        ).fetchall()
        fallback_by_sid = {row["security_id"]: dict(row) for row in fallback_rows}
        local_rows = db.execute(
            """SELECT q.security_id,q.close_price,q.change_pct
               FROM stock_daily_quotes q
               WHERE q.trade_date=?""",
            (today,),
        ).fetchall()
        id_to_code = {s["security_id"]: s["security_code"] for s in universe}
        quotes = {
            id_to_code[row["security_id"]]: {
                "quote_time": f"{today} 15:00:00",
                "price": row["close_price"],
                "change_pct": row["change_pct"],
                "turnover": (fallback_by_sid.get(row["security_id"]) or {}).get("turnover_rate") or 0.0,
                "volratio": (fallback_by_sid.get(row["security_id"]) or {}).get("volume_ratio") or 1.0,
                "market_cap": (fallback_by_sid.get(row["security_id"]) or {}).get("market_cap_yi"),
                "pe_ttm": (fallback_by_sid.get(row["security_id"]) or {}).get("pe_ttm"),
            }
            for row in local_rows
            if row["security_id"] in id_to_code and row["close_price"] is not None and row["close_price"] > 0
        }
        log(f"  历史补跑: 从本地 stock_daily_quotes 读取 {len(quotes)} 只 {today} 行情，补充估值 {sum(1 for q in quotes.values() if q.get('pe_ttm'))} 只")
    if not quotes:
        quotes = {
        row["security_code"]: {
            "quote_time": row["quote_time"], "price": row["close_price"],
            "change_pct": row["change_pct"], "turnover": row["turnover_rate"],
            "volratio": row["volume_ratio"], "market_cap": row["market_cap_yi"],
            "pe_ttm": row["pe_ttm"],
        }
        for row in db.execute("SELECT * FROM stock_quote_sync_progress WHERE run_key=? AND close_price > 0", (run_key,)).fetchall()
        }
    pending = [] if historical_local else [s for s in universe if s["security_code"] not in quotes]
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
            completed += 1
            batch_by_code = {s["security_code"]: s for s in batch}
            clean_got: dict[str, dict] = {}
            skipped_bad = 0
            for code, q in got.items():
                if not valid_quote(q):
                    skipped_bad += 1
                    continue
                security = batch_by_code.get(code)
                if not security:
                    continue
                if not quote_move_is_reasonable(db, security["security_id"], float(q.get("price")), quote_trade_date):
                    skipped_bad += 1
                    continue
                clean_got[code] = q
                db.execute(
                    """INSERT OR REPLACE INTO stock_quote_sync_progress(
                           run_key,security_id,security_code,quote_time,close_price,change_pct,
                           turnover_rate,volume_ratio,market_cap_yi,pe_ttm,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_key, security["security_id"], code, q.get("quote_time"), q.get("price"),
                     q.get("change_pct"), q.get("turnover"), q.get("volratio"),
                     q.get("market_cap"), q.get("pe_ttm"), now_utc),
                )
            quotes.update(clean_got)
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

    name_hits = fetch_event_hits(db, [s["security_name"] for s in universe], now=now_bj)
    log(f"行情覆盖 {len(quotes)}/{len(universe)}；事件命中股票 {len(name_hits)} 只")
    minimum_coverage = max(1, int(len(universe) * 0.80))
    if len(quotes) < minimum_coverage:
        db.close()
        raise RuntimeError(f"行情覆盖不足80%（{len(quotes)}/{len(universe)}），已保存断点，保留原评级批次")

    # 全池行情快照落库：板块热力图（日/周/月）的数据基础
    db.execute(QUOTES_DDL)
    code_to_id = {s["security_code"]: s["security_id"] for s in universe}
    for code, q in quotes.items():
        if not valid_quote(q):
            continue
        sid = code_to_id.get(code)
        if sid:
            if not quote_move_is_reasonable(db, sid, float(q.get("price")), quote_trade_date):
                continue
            db.execute(
                "INSERT OR REPLACE INTO stock_daily_quotes(security_id, trade_date, close_price, change_pct) VALUES(?,?,?,?)",
                (sid, quote_trade_date, q["price"], q["change_pct"]),
            )
    db.commit()
    log(f"行情快照实际交易日: {quote_trade_date}")

    history_rows = db.execute(
        """SELECT security_id,tier,total_score,invest_score,board_status,rating_date
           FROM daily_stock_ratings
           WHERE rating_date < ?
           ORDER BY rating_date DESC""",
        (today,),
    ).fetchall()
    history: dict[str, dict] = {}
    for row in history_rows:
        sid = row["security_id"]
        stat = history.setdefault(sid, {"seen": 0, "long_days": 0, "risk_days": 0, "scores": []})
        if stat["seen"] >= 20:
            continue
        stat["seen"] += 1
        score_value = row["invest_score"] if row["invest_score"] is not None else row["total_score"]
        if score_value is not None:
            stat["scores"].append(float(score_value))
        if row["board_status"] in ("长期好公司", "核心候选", "主推") or row["tier"] in ("中性", "增持观察", "重点关注"):
            stat["long_days"] += 1
        if row["board_status"] == "行业扫描" or row["tier"] == "回避":
            stat["risk_days"] += 1
    for stat in history.values():
        stat["avg_invest_score"] = sum(stat["scores"]) / len(stat["scores"]) if stat["scores"] else 50.0

    records = []
    for s in universe:
        code = s["security_code"]
        q = quotes.get(code)
        if not q:
            continue
        pe = q.get("pe_ttm")
        hits = name_hits.get(s["security_name"], 0)
        timing = score_momentum(q)
        valuation = score_valuation(pe)
        catalyst = score_events(hits)
        hist = history.get(s["security_id"])
        stable = score_stability(hist, valuation)
        risk_penalty = 35 if is_risk_name(s["security_name"]) else 20 if hits <= -2 else 0
        invest = round(clamp(valuation * 0.42 + stable * 0.33 + catalyst * 0.18 + timing * 0.07 - risk_penalty), 1)
        records.append({
            "security_id": s["security_id"], "security_name": s["security_name"],
            "sector_code": s["sector_code"], "close_price": q["price"], "change_pct": q["change_pct"],
            "pe_ttm": pe, "turnover_rate": q["turnover"], "volume_ratio": q["volratio"],
            "market_cap_yi": q.get("market_cap"), "event_hits": hits, "event_score": catalyst,
            "momentum_score": timing, "valuation_score": valuation, "total_score": invest,
            "invest_score": invest, "stability_score": stable,
            "rationale": build_rationale(q, pe, hits, ""),
            "quote_trade_date": quote_trade_date,
            "components_json": json.dumps({"timing": timing, "valuation": valuation, "catalyst": catalyst,
                                             "stability": stable, "invest": invest,
                                             "quote_trade_date": quote_trade_date}, ensure_ascii=False),
        })

    for r in records:
        hist = history.get(r["security_id"])
        if is_risk_name(r["security_name"]) or r["event_hits"] <= -2 or r["invest_score"] < 35:
            r["tier"] = "回避"
            r["board_status"] = "行业扫描"
            r["holding_label"] = ""
        elif r["invest_score"] >= 76 and r["stability_score"] >= 58:
            r["tier"] = "重点关注"
            r["board_status"] = "核心候选"
            r["holding_label"] = "中长期" if r["event_hits"] <= 0 else "中长期·中期可建仓"
        elif r["invest_score"] >= 66:
            r["tier"] = "增持观察"
            r["board_status"] = "重点跟踪"
            r["holding_label"] = "中期" if r["event_hits"] > 0 else ""
        elif r["invest_score"] >= 50 or (hist and int(hist.get("long_days") or 0) > 0 and r["invest_score"] >= 42):
            r["tier"] = "中性"
            r["board_status"] = "长期好公司"
            r["holding_label"] = "中长期·暂不建仓"
        else:
            r["tier"] = "回避"
            r["board_status"] = "行业扫描"
            r["holding_label"] = ""
        r["state_reason"] = build_state_reason(r, hist)

    # 页面口径：每日主推 5 个从核心候选中挑出；剩余“可以考虑买入”显示 25 个。
    status_limits = {"核心候选": 30, "重点跟踪": 45, "长期好公司": 80, "行业扫描": 40}
    display = []
    for status, limit_n in status_limits.items():
        bucket = [r for r in records if r["board_status"] == status]
        bucket.sort(key=lambda r: (r["invest_score"], r["stability_score"], r["valuation_score"]), reverse=True)
        display.extend(bucket[:limit_n])

    db.execute("DELETE FROM daily_stock_ratings WHERE rating_date=?", (today,))
    records = display
    for r in records:
        db.execute(
            """INSERT OR REPLACE INTO daily_stock_ratings(
                   rating_date,security_id,security_name,sector_code,close_price,change_pct,pe_ttm,
                   turnover_rate,volume_ratio,market_cap_yi,event_hits,event_score,momentum_score,
                   valuation_score,total_score,tier,rationale,invest_score,stability_score,board_status,
                   holding_label,state_reason,quote_trade_date,components_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today, r["security_id"], r["security_name"], r["sector_code"], r["close_price"],
             r["change_pct"], r["pe_ttm"], r["turnover_rate"], r["volume_ratio"], r["market_cap_yi"],
             r["event_hits"], r["event_score"], r["momentum_score"], r["valuation_score"],
             r["total_score"], r["tier"], r["rationale"], r["invest_score"], r["stability_score"],
             r["board_status"], r["holding_label"], r["state_reason"], r["quote_trade_date"],
             r["components_json"], now_utc),
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
    ap.add_argument("--date", default=None, help="补跑指定日期 YYYY-MM-DD")
    ap.add_argument("--historical-local", action="store_true", help="历史补跑：使用本地 stock_daily_quotes，不联网拉实时行情")
    args = ap.parse_args()
    run(limit=args.limit, batch_size=args.batch_size, workers=args.workers, target_date=args.date, historical_local=args.historical_local)
