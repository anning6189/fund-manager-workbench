"""AutoInvest Agent 自校准引擎。

全自动、零人工介入：
- 每日自我校对数据质量与推荐一致性；
- 低风险问题自动修复；
- 保存推荐快照与后验结果；
- 维护规则版本、影子规则和自动晋级/回滚事件底座。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
BJ = timezone(timedelta(hours=8))
RULE_VERSION = "autoinvest-v2.1"
SHADOW_VERSION = "autoinvest-v2.1-shadow-auto"


def now_iso() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def today_bj() -> str:
    return datetime.now(BJ).date().isoformat()


def is_expected_morning_quote_lag(rating_date: str | None, quote_date: str | None, target_date: str) -> bool:
    """08:40 早盘评级允许使用上一交易日收盘行情，16:10 收盘同步后再要求刷新。"""
    if not rating_date or not quote_date or rating_date != target_date:
        return False
    now = datetime.now(BJ)
    close_sync_time = datetime.combine(now.date(), time(16, 10), BJ)
    return now < close_sync_time and quote_date <= rating_date


def stable_event_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "agent-self-calibration:" + ":".join(parts)))


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


DDL = """
CREATE TABLE IF NOT EXISTS agent_self_audit_runs (
  run_id TEXT PRIMARY KEY,
  run_date TEXT NOT NULL,
  status TEXT NOT NULL,
  checks_json TEXT NOT NULL,
  issues_json TEXT NOT NULL,
  auto_fixes_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_recommendation_snapshots (
  snapshot_date TEXT NOT NULL,
  security_id TEXT NOT NULL,
  security_name TEXT,
  sector_code TEXT,
  sector_name TEXT,
  snapshot_group TEXT,
  board_status TEXT,
  is_main_push INTEGER NOT NULL DEFAULT 0,
  holding_label TEXT,
  invest_score REAL,
  stability_score REAL,
  valuation_score REAL,
  timing_score REAL,
  close_price REAL,
  rationale TEXT,
  rule_version TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(snapshot_date, security_id, is_main_push)
);
CREATE TABLE IF NOT EXISTS agent_recommendation_outcomes (
  snapshot_date TEXT NOT NULL,
  security_id TEXT NOT NULL,
  horizon TEXT NOT NULL,
  outcome_date TEXT,
  absolute_return REAL,
  max_drawdown REAL,
  status_after TEXT,
  risk_event_count INTEGER DEFAULT 0,
  outcome_label TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(snapshot_date, security_id, horizon)
);
CREATE TABLE IF NOT EXISTS agent_rule_versions (
  rule_version TEXT PRIMARY KEY,
  parent_version TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  activated_at TEXT,
  retired_at TEXT,
  params_json TEXT NOT NULL,
  reason_json TEXT NOT NULL,
  guardrail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_rule_events (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  from_version TEXT,
  to_version TEXT,
  reason TEXT,
  metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


GROUP_LABELS = {
    "main_push": "每日主推清单",
    "buy_candidate": "可以考虑买入",
    "watch_signal": "等待买点",
    "long_quality": "长期观察",
    "sector_scan": "暂不推荐/行业扫描",
}


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(DDL)
    columns = {row[1] for row in db.execute("PRAGMA table_info(agent_recommendation_snapshots)").fetchall()}
    if "snapshot_group" not in columns:
        db.execute("ALTER TABLE agent_recommendation_snapshots ADD COLUMN snapshot_group TEXT")
    if "entry_trade_date" not in columns:
        db.execute("ALTER TABLE agent_recommendation_snapshots ADD COLUMN entry_trade_date TEXT")
    db.execute(
        """UPDATE agent_recommendation_snapshots
           SET snapshot_group=CASE
               WHEN is_main_push=1 THEN 'main_push'
               WHEN board_status='核心候选' THEN 'buy_candidate'
               WHEN board_status='重点跟踪' THEN 'watch_signal'
               WHEN board_status='长期好公司' THEN 'long_quality'
               WHEN board_status='行业扫描' THEN 'sector_scan'
               ELSE COALESCE(snapshot_group,'other')
           END
           WHERE snapshot_group IS NULL"""
    )
    db.execute(
        """UPDATE agent_recommendation_snapshots
           SET entry_trade_date=snapshot_date
           WHERE entry_trade_date IS NULL"""
    )


def connect(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    ensure_schema(db)
    return db


def table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def latest_date(db: sqlite3.Connection, table: str, column: str) -> str | None:
    if not table_exists(db, table):
        return None
    return db.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]


def scan_isolated_outliers(db: sqlite3.Connection) -> list[dict[str, Any]]:
    outliers: list[dict[str, Any]] = []
    for (sid,) in db.execute("SELECT DISTINCT security_id FROM stock_daily_quotes"):
        rows = [dict(r) for r in db.execute(
            """SELECT trade_date,close_price FROM stock_daily_quotes
               WHERE security_id=? AND close_price>0 ORDER BY trade_date""",
            (sid,),
        )]
        for idx in range(1, len(rows) - 1):
            prev_close = float(rows[idx - 1]["close_price"] or 0)
            close = float(rows[idx]["close_price"] or 0)
            next_close = float(rows[idx + 1]["close_price"] or 0)
            if prev_close <= 0 or close <= 0 or next_close <= 0:
                continue
            neighbours_close = abs(next_close / prev_close - 1) <= 0.35
            middle_far = abs(close / prev_close - 1) >= 0.45 and abs(next_close / close - 1) >= 0.45
            if neighbours_close and middle_far:
                outliers.append({
                    "security_id": sid,
                    "trade_date": rows[idx]["trade_date"],
                    "bad_close": close,
                    "prev_close": prev_close,
                    "next_close": next_close,
                })
    return outliers


def auto_fix_quote_quality(db: sqlite3.Connection) -> list[dict[str, Any]]:
    fixes: list[dict[str, Any]] = []
    zero_quotes = db.execute(
        "SELECT COUNT(*) FROM stock_daily_quotes WHERE close_price IS NULL OR close_price<=0"
    ).fetchone()[0]
    zero_ratings = db.execute(
        "SELECT COUNT(*) FROM daily_stock_ratings WHERE close_price IS NULL OR close_price<=0"
    ).fetchone()[0]
    if zero_quotes:
        db.execute("DELETE FROM stock_daily_quotes WHERE close_price IS NULL OR close_price<=0")
        fixes.append({"type": "delete_invalid_quotes", "rows": zero_quotes})
    if zero_ratings:
        db.execute("DELETE FROM daily_stock_ratings WHERE close_price IS NULL OR close_price<=0")
        fixes.append({"type": "delete_invalid_ratings", "rows": zero_ratings})

    outliers = scan_isolated_outliers(db)
    for item in outliers:
        sid = item["security_id"]
        day = item["trade_date"]
        db.execute("DELETE FROM stock_daily_quotes WHERE security_id=? AND trade_date=?", (sid, day))
        db.execute("DELETE FROM daily_stock_ratings WHERE security_id=? AND rating_date=?", (sid, day))
        db.execute("DELETE FROM stock_quote_sync_progress WHERE security_id=? AND substr(quote_time,1,10)=?", (sid, day))
    if outliers:
        fixes.append({"type": "delete_isolated_quote_outliers", "rows": len(outliers), "sample": outliers[:10]})
    return fixes


def main_push_ids(db: sqlite3.Connection, rating_date: str) -> set[str]:
    rows = [dict(r) for r in db.execute(
        """SELECT security_id,security_name,sector_code,close_price,change_pct,pe_ttm,
                  total_score,invest_score,stability_score,valuation_score,momentum_score,
                  board_status,holding_label,state_reason,rationale
           FROM daily_stock_ratings
           WHERE rating_date=? AND board_status='核心候选' AND close_price>0
           ORDER BY COALESCE(invest_score,total_score,0) DESC""",
        (rating_date,),
    )]
    chosen: list[str] = []
    sectors: set[str] = set()
    sector_count = len({r.get("sector_code") for r in rows if r.get("sector_code")})
    for row in rows:
        sector = row.get("sector_code") or "unknown"
        if len(chosen) < 5 and sector_count >= 5 and sector in sectors:
            continue
        chosen.append(row["security_id"])
        sectors.add(sector)
        if len(chosen) >= 5:
            break
    if len(chosen) < 5:
        for row in rows:
            if row["security_id"] not in chosen:
                chosen.append(row["security_id"])
            if len(chosen) >= 5:
                break
    return set(chosen)


def save_recommendation_snapshots(db: sqlite3.Connection, rating_date: str) -> int:
    main_ids = main_push_ids(db, rating_date)
    rows = [dict(r) for r in db.execute(
        """SELECT r.security_id,r.security_name,r.sector_code,p.sector_name,r.close_price,
                  r.quote_trade_date,
                  r.total_score,r.invest_score,r.stability_score,r.valuation_score,r.momentum_score,
                  r.board_status,r.holding_label,r.state_reason,r.rationale
           FROM daily_stock_ratings r
           LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
           WHERE r.rating_date=? AND r.close_price>0
             AND (r.security_id IN ({}) OR r.board_status IN ('核心候选','重点跟踪','长期好公司','行业扫描'))
           ORDER BY COALESCE(r.invest_score,r.total_score,0) DESC""".format(",".join("?" for _ in main_ids) or "''"),
        (rating_date, *tuple(main_ids)),
    )]
    created_at = now_iso()
    count = 0
    for row in rows:
        is_main = 1 if row["security_id"] in main_ids else 0
        if is_main:
            snapshot_group = "main_push"
        elif row["board_status"] == "核心候选":
            snapshot_group = "buy_candidate"
        elif row["board_status"] == "重点跟踪":
            snapshot_group = "watch_signal"
        elif row["board_status"] == "长期好公司":
            snapshot_group = "long_quality"
        elif row["board_status"] == "行业扫描":
            snapshot_group = "sector_scan"
        else:
            snapshot_group = "other"
        db.execute(
            """INSERT OR REPLACE INTO agent_recommendation_snapshots(
                   snapshot_date,security_id,security_name,sector_code,sector_name,snapshot_group,board_status,
                   is_main_push,holding_label,invest_score,stability_score,valuation_score,timing_score,
                   close_price,rationale,rule_version,created_at,entry_trade_date
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rating_date, row["security_id"], row["security_name"], row["sector_code"], row["sector_name"],
                snapshot_group, row["board_status"], is_main, row["holding_label"], row["invest_score"], row["stability_score"],
                row["valuation_score"], row["momentum_score"], row["close_price"],
                row["state_reason"] or row["rationale"], RULE_VERSION, created_at, row.get("quote_trade_date") or rating_date,
            ),
        )
        count += 1
    return count


def quote_on_or_after(db: sqlite3.Connection, security_id: str, target: str, lag_days: int) -> sqlite3.Row | None:
    return db.execute(
        """SELECT trade_date,close_price FROM stock_daily_quotes
           WHERE security_id=? AND trade_date>=? AND trade_date<=date(?, ?)
             AND close_price>0
           ORDER BY trade_date ASC LIMIT 1""",
        (security_id, target, target, f"+{lag_days} days"),
    ).fetchone()


def max_drawdown_until(db: sqlite3.Connection, security_id: str, start: str, end: str, base_close: float) -> float | None:
    rows = [float(r[0]) for r in db.execute(
        """SELECT close_price FROM stock_daily_quotes
           WHERE security_id=? AND trade_date>=? AND trade_date<=? AND close_price>0
           ORDER BY trade_date""",
        (security_id, start, end),
    )]
    if not rows or base_close <= 0:
        return None
    trough = min(rows)
    return round((trough / base_close - 1) * 100, 2)


def update_outcomes(db: sqlite3.Connection) -> int:
    horizons = {"T+1": (1, 3), "T+5": (7, 7), "T+20": (30, 14), "T+60": (90, 21)}
    snapshots = [dict(r) for r in db.execute(
        """SELECT snapshot_date,security_id,close_price FROM agent_recommendation_snapshots
           WHERE close_price>0"""
    )]
    created_at = now_iso()
    updated = 0
    for snap in snapshots:
        base_date = snap["snapshot_date"]
        base_close = float(snap["close_price"] or 0)
        for horizon, (days, lag) in horizons.items():
            target = (datetime.fromisoformat(base_date) + timedelta(days=days)).date().isoformat()
            q = quote_on_or_after(db, snap["security_id"], target, lag)
            if not q:
                continue
            ret = round((float(q["close_price"]) / base_close - 1) * 100, 2) if base_close > 0 else None
            status = db.execute(
                """SELECT board_status FROM daily_stock_ratings
                   WHERE security_id=? AND rating_date<=?
                   ORDER BY rating_date DESC LIMIT 1""",
                (snap["security_id"], q["trade_date"]),
            ).fetchone()
            risk_count = db.execute(
                """SELECT COUNT(*) FROM monitor_events
                   WHERE status='accepted' AND available_at>=? AND available_at<=date(?, '+1 day')
                     AND (title LIKE ? OR summary LIKE ?)""",
                (base_date, q["trade_date"], f"%{snap['security_id'][:6]}%", f"%{snap['security_id'][:6]}%"),
            ).fetchone()[0] if table_exists(db, "monitor_events") else 0
            label = "positive" if ret is not None and ret > 0 else "negative" if ret is not None and ret < 0 else "flat"
            db.execute(
                """INSERT OR REPLACE INTO agent_recommendation_outcomes(
                       snapshot_date,security_id,horizon,outcome_date,absolute_return,max_drawdown,
                       status_after,risk_event_count,outcome_label,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    base_date, snap["security_id"], horizon, q["trade_date"], ret,
                    max_drawdown_until(db, snap["security_id"], base_date, q["trade_date"], base_close),
                    status[0] if status else None, risk_count, label, created_at,
                ),
            )
            updated += 1
    return updated


def ensure_rule_versions(db: sqlite3.Connection) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    params = {
        "invest_weight": {"valuation": 0.42, "stability": 0.33, "catalyst": 0.18, "timing": 0.07},
        "main_push_limit": 5,
        "auto_guardrails": {"max_weight_step": 0.03, "max_threshold_step": 2, "auto_rollback": True},
    }
    guardrail = {
        "no_manual_approval": True,
        "shadow_before_activation": True,
        "rollback_on_quality_failure": True,
        "no_invalid_price": True,
        "no_wait_buy_in_main_push": True,
    }
    now = now_iso()
    if not db.execute("SELECT 1 FROM agent_rule_versions WHERE rule_version=?", (RULE_VERSION,)).fetchone():
        db.execute(
            "INSERT INTO agent_rule_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (RULE_VERSION, None, "active", now, now, None, jdump(params), jdump({"reason": "initial_auto_calibration_baseline"}), jdump(guardrail)),
        )
        eid = stable_event_id("activate", RULE_VERSION, now[:10])
        db.execute(
            "INSERT OR REPLACE INTO agent_rule_events VALUES(?,?,?,?,?,?,?)",
            (eid, "auto_activate_baseline", None, RULE_VERSION, "首次建立全自动自校准基线规则", jdump({"rule_version": RULE_VERSION}), now),
        )
        events.append({"event_type": "auto_activate_baseline", "to_version": RULE_VERSION})
    shadow_params = dict(params)
    shadow_params["shadow_note"] = "自动影子规则：先并行观察，不阻断正式输出；达到条件自动晋级，恶化自动废弃。"
    if not db.execute("SELECT 1 FROM agent_rule_versions WHERE rule_version=?", (SHADOW_VERSION,)).fetchone():
        db.execute(
            "INSERT INTO agent_rule_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (SHADOW_VERSION, RULE_VERSION, "shadow", now, None, None, jdump(shadow_params), jdump({"reason": "auto_shadow_created_for_self_calibration"}), jdump(guardrail)),
        )
        eid = stable_event_id("shadow", SHADOW_VERSION, now[:10])
        db.execute(
            "INSERT OR REPLACE INTO agent_rule_events VALUES(?,?,?,?,?,?,?)",
            (eid, "auto_create_shadow_rule", RULE_VERSION, SHADOW_VERSION, "自动创建影子规则，进入并行观察", jdump({"shadow_version": SHADOW_VERSION}), now),
        )
        events.append({"event_type": "auto_create_shadow_rule", "to_version": SHADOW_VERSION})
    return events


def maybe_rule_event_from_outcomes(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(r) for r in db.execute(
        """SELECT COALESCE(s.snapshot_group,CASE WHEN s.is_main_push=1 THEN 'main_push' ELSE 'other' END) AS snapshot_group,
                  horizon,AVG(absolute_return) AS avg_return,COUNT(*) AS n
           FROM agent_recommendation_outcomes o
           JOIN agent_recommendation_snapshots s
             ON s.snapshot_date=o.snapshot_date AND s.security_id=o.security_id
           WHERE o.absolute_return IS NOT NULL
           GROUP BY snapshot_group,horizon"""
    )]
    if not rows:
        return []
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        group = r["snapshot_group"] or "other"
        metrics.setdefault(group, {})[r["horizon"]] = {
            "label": GROUP_LABELS.get(group, group),
            "avg_return": round(float(r["avg_return"]), 2),
            "n": r["n"],
        }
    today = today_bj()
    now = now_iso()
    event_type = "auto_observe_rule"
    reason = "样本继续积累，维持正式规则与影子规则并行观察"
    main_t5 = metrics.get("main_push", {}).get("T+5", {})
    buy_t5 = metrics.get("buy_candidate", {}).get("T+5", {})
    long_t5 = metrics.get("long_quality", {}).get("T+5", {})
    scan_t5 = metrics.get("sector_scan", {}).get("T+5", {})
    if main_t5.get("n", 0) >= 5 and main_t5.get("avg_return", 0) < -5:
        event_type = "auto_guardrail_warn"
        reason = "每日主推清单 T+5 后验收益偏弱，自动收紧短期动量影响并提高稳定分观察权重"
    elif buy_t5.get("n", 0) >= 10 and main_t5.get("n", 0) >= 5 and buy_t5.get("avg_return", 0) - main_t5.get("avg_return", 0) > 2:
        event_type = "auto_ranking_review"
        reason = "可以考虑买入 T+5 明显跑赢每日主推，自动检查主推排序是否过度偏向事件/板块分散"
    elif long_t5.get("n", 0) >= 10 and main_t5.get("n", 0) >= 5 and long_t5.get("avg_return", 0) - main_t5.get("avg_return", 0) > 2:
        event_type = "auto_stability_weight_review"
        reason = "长期观察 T+5 跑赢每日主推，自动提高稳定分与长期质量信号观察权重"
    elif scan_t5.get("n", 0) >= 10 and scan_t5.get("avg_return", 0) > 2:
        event_type = "auto_gate_review"
        reason = "暂不推荐/行业扫描 T+5 表现偏强，自动检查分类、估值阈值或风险 Gate 是否误杀"
    eid = stable_event_id(event_type, today)
    db.execute(
        "INSERT OR REPLACE INTO agent_rule_events VALUES(?,?,?,?,?,?,?)",
        (eid, event_type, RULE_VERSION, SHADOW_VERSION, reason, jdump(metrics), now),
    )
    return [{"event_type": event_type, "reason": reason, "metrics": metrics}]


def run(db_path: Path = DB, target_date: str | None = None) -> dict[str, Any]:
    target_date = target_date or today_bj()
    with connect(db_path) as db:
        auto_fixes = auto_fix_quote_quality(db)
        quote_date = latest_date(db, "stock_daily_quotes", "trade_date")
        rating_row = db.execute(
            "SELECT MAX(rating_date) FROM daily_stock_ratings WHERE rating_date<=?",
            (target_date,),
        ).fetchone()
        rating_date = rating_row[0] if rating_row else latest_date(db, "daily_stock_ratings", "rating_date")
        brief_date = latest_date(db, "daily_brief_sections", "brief_date")
        snapshot_count = save_recommendation_snapshots(db, rating_date) if rating_date else 0
        outcome_count = update_outcomes(db)
        rule_events = [*ensure_rule_versions(db), *maybe_rule_event_from_outcomes(db)]

        quote_alignment_ok = bool(
            rating_date
            and quote_date
            and (rating_date == quote_date or is_expected_morning_quote_lag(rating_date, quote_date, target_date))
        )
        quote_alignment_detail = (
            f"rating={rating_date},quote={quote_date}；08:40早盘评级允许使用上一交易日收盘行情，16:10收盘同步后刷新"
            if rating_date and quote_date and rating_date != quote_date and quote_alignment_ok
            else f"rating={rating_date},quote={quote_date}"
        )
        checks = [
            {"label": "行情无 0 价", "ok": db.execute("SELECT COUNT(*) FROM stock_daily_quotes WHERE close_price IS NULL OR close_price<=0").fetchone()[0] == 0},
            {"label": "行情无孤立尖刺", "ok": len(scan_isolated_outliers(db)) == 0},
            {"label": "评级与行情日期一致", "ok": quote_alignment_ok, "detail": quote_alignment_detail},
            {"label": "推荐快照已保存", "ok": snapshot_count > 0, "detail": f"{snapshot_count} rows"},
            {"label": "规则版本可审计", "ok": db.execute("SELECT COUNT(*) FROM agent_rule_versions").fetchone()[0] >= 1},
        ]
        issues = [c for c in checks if not c["ok"]]
        status = "ok" if not issues else "warn"
        run_id = stable_event_id("audit", target_date, now_iso())
        db.execute(
            """INSERT OR REPLACE INTO agent_self_audit_runs(
                   run_id,run_date,status,checks_json,issues_json,auto_fixes_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (run_id, target_date, status, jdump(checks), jdump(issues), jdump(auto_fixes), now_iso()),
        )
        db.commit()
        return {
            "run_id": run_id,
            "run_date": target_date,
            "status": status,
            "checks": checks,
            "issues": issues,
            "auto_fixes": auto_fixes,
            "snapshot_count": snapshot_count,
            "outcome_count": outcome_count,
            "rule_events": rule_events,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--date", default=None)
    parser.add_argument("--backfill-days", type=int, default=0, help="回填最近 N 个评级日的推荐快照与后验结果")
    args = parser.parse_args()
    if args.backfill_days:
        with sqlite3.connect(args.db) as db:
            dates = [r[0] for r in db.execute(
                "SELECT DISTINCT rating_date FROM daily_stock_ratings ORDER BY rating_date DESC LIMIT ?",
                (args.backfill_days,),
            ).fetchall()]
        results = [run(args.db, day) for day in reversed(dates)]
        print(json.dumps({"backfilled": len(results), "latest": results[-1] if results else None}, ensure_ascii=False, indent=2))
        return 0
    result = run(args.db, args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
