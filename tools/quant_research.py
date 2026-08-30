from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
FACTOR_VERSION = "quant-factor-v2-long-term-quality"
STRATEGY_VERSION = "quant-strategy-v2-long-term-quality"
HORIZONS = (1, 5, 20, 60)
FACTOR_NAMES = (
    "investment_score",
    "quality_score",
    "long_term_stability_score",
    "valuation_score",
    "catalyst_score",
    "risk_control_score",
    "market_fit_score",
)

BASE_LONG_TERM_WEIGHTS = {
    "investment_score": 0.12,
    "quality_score": 0.30,
    "long_term_stability_score": 0.15,
    "valuation_score": 0.13,
    "catalyst_score": 0.04,
    "risk_control_score": 0.22,
    "market_fit_score": 0.04,
}

WEIGHT_BOUNDS = {
    "quality_score": (0.25, 0.45),
    "risk_control_score": (0.18, 0.35),
    "long_term_stability_score": (0.10, 0.25),
    "valuation_score": (0.08, 0.22),
    "investment_score": (0.10, 0.25),
    "catalyst_score": (0.00, 0.08),
    "market_fit_score": (0.00, 0.06),
}


def _utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sx = math.sqrt(sum(x * x for x in dx))
    sy = math.sqrt(sum(y * y for y in dy))
    if sx == 0 or sy == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / (sx * sy)


def _rank(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[ordered[k][0]] = avg
        i = j + 1
    return ranks


def _rank_ic(xs: list[float], ys: list[float]) -> float | None:
    return _pearson(_rank(xs), _rank(ys))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS quant_factor_snapshots (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            subsector TEXT,
            factor_name TEXT NOT NULL,
            raw_value REAL,
            factor_value REAL,
            factor_zscore REAL,
            factor_rank_pct REAL,
            factor_group TEXT,
            factor_version TEXT,
            data_quality TEXT,
            created_at TEXT,
            PRIMARY KEY (trade_date, stock_code, factor_name, factor_version)
        );

        CREATE TABLE IF NOT EXISTS quant_forward_returns (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            base_price REAL,
            future_trade_date TEXT,
            future_price REAL,
            raw_return REAL,
            benchmark_return REAL,
            excess_return REAL,
            is_valid INTEGER,
            invalid_reason TEXT,
            created_at TEXT,
            PRIMARY KEY (trade_date, stock_code, horizon)
        );

        CREATE TABLE IF NOT EXISTS quant_factor_ic (
            factor_name TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            window_size INTEGER NOT NULL,
            start_date TEXT,
            end_date TEXT,
            sample_size INTEGER,
            ic_mean REAL,
            rank_ic_mean REAL,
            ic_ir REAL,
            positive_ic_ratio REAL,
            status TEXT,
            created_at TEXT,
            PRIMARY KEY (factor_name, horizon, window_size, end_date)
        );

        CREATE TABLE IF NOT EXISTS quant_group_backtest (
            factor_name TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            stock_count INTEGER,
            avg_return REAL,
            median_return REAL,
            win_rate REAL,
            benchmark_return REAL,
            excess_return REAL,
            monotonicity TEXT,
            created_at TEXT,
            PRIMARY KEY (factor_name, horizon, group_name, end_date)
        );

        CREATE TABLE IF NOT EXISTS quant_rolling_validation (
            window_end_date TEXT NOT NULL,
            window_size INTEGER NOT NULL,
            factor_name TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            sample_size INTEGER,
            rank_ic REAL,
            ic_ir REAL,
            q1_return REAL,
            q5_return REAL,
            q1_minus_q5 REAL,
            positive_ic_ratio REAL,
            status TEXT,
            created_at TEXT,
            PRIMARY KEY (window_end_date, window_size, factor_name, horizon)
        );

        CREATE TABLE IF NOT EXISTS quant_strategy_versions (
            version_id TEXT PRIMARY KEY,
            version_name TEXT,
            effective_date TEXT,
            factor_weights_json TEXT,
            gate_rules_json TEXT,
            portfolio_constraints_json TEXT,
            description TEXT,
            is_active INTEGER,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS quant_portfolio_optimization (
            run_date TEXT PRIMARY KEY,
            selected_count INTEGER,
            candidate_count INTEGER,
            objective_score REAL,
            expected_alpha REAL,
            risk_penalty REAL,
            turnover_penalty REAL,
            cost_penalty REAL,
            constraint_status TEXT,
            factor_weights_json TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS quant_strategy_experiments (
            experiment_id TEXT PRIMARY KEY,
            base_version TEXT,
            candidate_version TEXT,
            start_date TEXT,
            end_date TEXT,
            metric_json TEXT,
            passed INTEGER,
            decision TEXT,
            created_at TEXT
        );
        """
    )
    connection.execute(
        """INSERT OR REPLACE INTO quant_strategy_versions
           (version_id,version_name,effective_date,factor_weights_json,gate_rules_json,
            portfolio_constraints_json,description,is_active,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            STRATEGY_VERSION,
            "中长期好公司优先的因子重构版本",
            connection.execute("SELECT MAX(rating_date) FROM daily_stock_ratings").fetchone()[0],
            json.dumps(BASE_LONG_TERM_WEIGHTS, ensure_ascii=False),
            json.dumps(
                {
                    "hard_gate": "沿用当前每日主推/买入候选准入规则",
                    "primary_horizon": "T+20/T+60",
                    "factor_policy": "质量/风控/稳定性为核心Alpha，估值作为买点约束，催化/市场适配仅作辅助修正。",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "holdings": 30,
                    "single_weight": "1%-6%",
                    "subsector_weight_max": "25%",
                    "weekly_turnover_max": "30%",
                    "transaction_cost": "已在 AI基金经理净值中扣减",
                },
                ensure_ascii=False,
            ),
            "基于 IC、Rank IC、分组回测结果重构权重：提高质量、风险控制和中长期稳定性，降低短期催化与市场适配。",
            1,
            _utc_now(),
        ),
    )
    connection.execute(
        "UPDATE quant_strategy_versions SET is_active=0 WHERE version_id<>?",
        (STRATEGY_VERSION,),
    )


@dataclass
class FactorPoint:
    trade_date: str
    stock_code: str
    stock_name: str
    subsector: str
    factor_name: str
    raw_value: float
    data_quality: str


def _extract_factor_points(connection: sqlite3.Connection) -> list[FactorPoint]:
    rows = [dict(r) for r in connection.execute(
        """SELECT r.rating_date,r.security_id,r.security_name,r.sector_code,r.total_score,
                  r.invest_score,r.stability_score,r.valuation_score,r.event_score,r.momentum_score,
                  r.close_price,r.change_pct,r.pe_ttm,r.event_hits,r.components_json,p.sector_name
           FROM daily_stock_ratings r
           LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
           WHERE r.rating_date IS NOT NULL AND r.security_id IS NOT NULL"""
    ).fetchall()]
    points: list[FactorPoint] = []
    for row in rows:
        components = _json_loads(row.get("components_json"), {})
        investment = _safe_float(row.get("invest_score")) or _safe_float(row.get("total_score")) or 0.0
        valuation = _safe_float(row.get("valuation_score")) or _safe_float(components.get("valuation")) or 50.0
        catalyst = _safe_float(row.get("event_score")) or _safe_float(components.get("catalyst")) or min(100.0, 45.0 + float(row.get("event_hits") or 0) * 8)
        market_fit = _safe_float(row.get("momentum_score")) or _safe_float(components.get("timing")) or 50.0
        quality = _safe_float(row.get("stability_score")) or _safe_float(components.get("stability")) or max(0.0, investment - 5)
        long_term_stability = max(0.0, min(100.0, 0.70 * quality + 0.30 * valuation))
        risk_control = 100.0
        if row.get("pe_ttm") is None or float(row.get("pe_ttm") or 0) <= 0:
            risk_control -= 8
        if abs(float(row.get("change_pct") or 0)) >= 8:
            risk_control -= 8
        if "ST" in str(row.get("security_name") or "").upper():
            risk_control -= 50
        values = {
            "investment_score": investment,
            "quality_score": quality,
            "long_term_stability_score": long_term_stability,
            "valuation_score": valuation,
            "catalyst_score": catalyst,
            "risk_control_score": max(0.0, min(100.0, risk_control)),
            "market_fit_score": market_fit,
        }
        for name, value in values.items():
            points.append(
                FactorPoint(
                    trade_date=str(row["rating_date"]),
                    stock_code=str(row["security_id"]),
                    stock_name=str(row.get("security_name") or ""),
                    subsector=str(row.get("sector_name") or row.get("sector_code") or ""),
                    factor_name=name,
                    raw_value=float(max(0.0, min(100.0, value))),
                    data_quality="from_daily_stock_ratings",
                )
            )
    return points


def _extract_quote_proxy_points(connection: sqlite3.Connection, existing_dates: set[str]) -> list[FactorPoint]:
    """Use available historical quotes to backfill price-derived factor history.

    This is intentionally marked as historical_quote_proxy. It is not a replacement
    for point-in-time fundamentals; it lets the quant page use the long price
    history that already exists while keeping data provenance explicit.
    """
    universe = [dict(r) for r in connection.execute(
        """SELECT security_id,security_name,sector_code,sector_name,
                  COALESCE(invest_score,total_score,50) investment_score,
                  COALESCE(stability_score,50) quality_score,
                  COALESCE(valuation_score,50) valuation_score
           FROM (
             SELECT r.*,p.sector_name,
                    ROW_NUMBER() OVER (PARTITION BY r.security_id ORDER BY r.rating_date DESC) rn
             FROM daily_stock_ratings r
             LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
           ) WHERE rn=1"""
    ).fetchall()]
    if not universe:
        return []
    meta = {r["security_id"]: r for r in universe}
    rows = [dict(r) for r in connection.execute(
        f"""SELECT security_id,trade_date,close_price,change_pct
            FROM stock_daily_quotes
            WHERE close_price > 0
              AND security_id IN ({",".join("?" for _ in meta)})
            ORDER BY security_id,trade_date""",
        tuple(meta),
    ).fetchall()]
    by_stock: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_stock.setdefault(row["security_id"], []).append(row)

    per_date_values: dict[str, dict[str, dict[str, float]]] = {}
    for sid, series in by_stock.items():
        closes = [float(r["close_price"]) for r in series]
        for i, row in enumerate(series):
            trade_date = row["trade_date"]
            if trade_date in existing_dates:
                continue
            if i < 20:
                continue
            ret_5 = closes[i] / closes[max(0, i - 5)] - 1 if closes[max(0, i - 5)] else 0.0
            ret_20 = closes[i] / closes[i - 20] - 1 if closes[i - 20] else 0.0
            window = closes[i - 20 : i + 1]
            returns = [(window[j] / window[j - 1] - 1) for j in range(1, len(window)) if window[j - 1]]
            vol = _std(returns) or 0.0
            peak = max(window) if window else closes[i]
            drawdown = closes[i] / peak - 1 if peak else 0.0
            per_date_values.setdefault(trade_date, {})[sid] = {
                "ret_5": ret_5,
                "ret_20": ret_20,
                "vol_20": vol,
                "drawdown_20": drawdown,
            }

    points: list[FactorPoint] = []
    for trade_date, stock_values in per_date_values.items():
        if len(stock_values) < 50:
            continue

        def percentile_scores(key: str, reverse: bool = False) -> dict[str, float]:
            ordered = sorted(stock_values.items(), key=lambda item: item[1][key], reverse=reverse)
            n = max(1, len(ordered) - 1)
            return {sid: 100.0 * (1.0 - i / n) for i, (sid, _) in enumerate(ordered)}

        mom20 = percentile_scores("ret_20", reverse=True)
        mom5 = percentile_scores("ret_5", reverse=True)
        low_vol = percentile_scores("vol_20", reverse=False)
        low_dd = percentile_scores("drawdown_20", reverse=True)
        for sid, vals in stock_values.items():
            m = meta[sid]
            quality = 0.55 * float(m["quality_score"] or 50) + 0.45 * low_vol[sid]
            valuation = float(m["valuation_score"] or 50)
            catalyst = mom5[sid]
            risk = 0.65 * low_vol[sid] + 0.35 * low_dd[sid]
            market_fit = mom20[sid]
            long_term_stability = 0.45 * quality + 0.35 * risk + 0.20 * low_dd[sid]
            investment = (
                0.30 * quality
                + 0.22 * risk
                + 0.15 * long_term_stability
                + 0.13 * valuation
                + 0.12 * float(m["investment_score"] or 50)
                + 0.04 * catalyst
                + 0.04 * market_fit
            )
            values = {
                "investment_score": investment,
                "quality_score": quality,
                "long_term_stability_score": long_term_stability,
                "valuation_score": valuation,
                "catalyst_score": catalyst,
                "risk_control_score": risk,
                "market_fit_score": market_fit,
            }
            for factor, value in values.items():
                points.append(
                    FactorPoint(
                        trade_date=trade_date,
                        stock_code=sid,
                        stock_name=str(m.get("security_name") or ""),
                        subsector=str(m.get("sector_name") or m.get("sector_code") or ""),
                        factor_name=factor,
                        raw_value=float(max(0.0, min(100.0, value))),
                        data_quality="historical_quote_proxy",
                    )
                )
    return points


def refresh_factor_snapshots(connection: sqlite3.Connection) -> int:
    points = _extract_factor_points(connection)
    existing_dates = {p.trade_date for p in points}
    points.extend(_extract_quote_proxy_points(connection, existing_dates))
    grouped: dict[tuple[str, str], list[FactorPoint]] = {}
    for point in points:
        grouped.setdefault((point.trade_date, point.factor_name), []).append(point)
    now = _utc_now()
    written = 0
    for (_trade_date, _factor_name), items in grouped.items():
        values = [p.raw_value for p in items]
        avg = _mean(values) or 0.0
        sd = _std(values) or 0.0
        ordered = sorted(items, key=lambda p: p.raw_value, reverse=True)
        rank_pct: dict[str, float] = {}
        n = max(1, len(ordered) - 1)
        for i, point in enumerate(ordered):
            rank_pct[point.stock_code] = 1.0 - i / n if n else 1.0
        for point in items:
            pct = rank_pct.get(point.stock_code, 0.0)
            group_index = min(5, max(1, int((1.0 - pct) * 5) + 1))
            connection.execute(
                """INSERT OR REPLACE INTO quant_factor_snapshots
                   (trade_date,stock_code,stock_name,subsector,factor_name,raw_value,factor_value,
                    factor_zscore,factor_rank_pct,factor_group,factor_version,data_quality,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    point.trade_date,
                    point.stock_code,
                    point.stock_name,
                    point.subsector,
                    point.factor_name,
                    point.raw_value,
                    point.raw_value,
                    (point.raw_value - avg) / sd if sd else 0.0,
                    pct,
                    f"Q{group_index}",
                    FACTOR_VERSION,
                    point.data_quality,
                    now,
                ),
            )
            written += 1
    return written


def _quote_dates(connection: sqlite3.Connection) -> list[str]:
    return [r[0] for r in connection.execute("SELECT DISTINCT trade_date FROM stock_daily_quotes ORDER BY trade_date").fetchall()]


def refresh_forward_returns(connection: sqlite3.Connection) -> int:
    dates = _quote_dates(connection)
    date_index = {d: i for i, d in enumerate(dates)}
    rating_dates = [r[0] for r in connection.execute("SELECT DISTINCT trade_date FROM quant_factor_snapshots ORDER BY trade_date").fetchall()]
    quote_map = {
        (r["security_id"], r["trade_date"]): float(r["close_price"])
        for r in connection.execute("SELECT security_id,trade_date,close_price FROM stock_daily_quotes WHERE close_price > 0")
    }
    stocks_by_date: dict[str, set[str]] = {}
    for r in connection.execute("SELECT DISTINCT trade_date,stock_code FROM quant_factor_snapshots"):
        stocks_by_date.setdefault(r["trade_date"], set()).add(r["stock_code"])
    now = _utc_now()
    written = 0
    for trade_date in rating_dates:
        if trade_date not in date_index:
            continue
        base_i = date_index[trade_date]
        for stock_code in stocks_by_date.get(trade_date, set()):
            base = quote_map.get((stock_code, trade_date))
            for horizon in HORIZONS:
                future_i = base_i + horizon
                future_date = dates[future_i] if future_i < len(dates) else None
                future = quote_map.get((stock_code, future_date)) if future_date else None
                valid = bool(base and future and base > 0 and future > 0)
                raw_return = (future / base - 1) * 100 if valid else None
                invalid_reason = None if valid else "future_price_pending_or_missing"
                connection.execute(
                    """INSERT OR REPLACE INTO quant_forward_returns
                       (trade_date,stock_code,horizon,base_price,future_trade_date,future_price,
                        raw_return,benchmark_return,excess_return,is_valid,invalid_reason,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trade_date,
                        stock_code,
                        horizon,
                        base,
                        future_date,
                        future,
                        raw_return,
                        None,
                        raw_return,
                        1 if valid else 0,
                        invalid_reason,
                        now,
                    ),
                )
                written += 1
    return written


def _status(rank_ic: float | None, sample_size: int) -> str:
    if sample_size < 30:
        return "样本不足"
    if rank_ic is None:
        return "不可计算"
    if rank_ic >= 0.06:
        return "稳定有效"
    if rank_ic >= 0.02:
        return "偏正"
    if rank_ic <= -0.02:
        return "反向/衰减"
    return "偏弱"


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {k: max(0.0, float(v or 0)) for k, v in weights.items() if k in FACTOR_NAMES}
    total = sum(cleaned.values()) or 1.0
    return {k: round(cleaned.get(k, 0.0) / total, 4) for k in FACTOR_NAMES}


def _bounded_adaptive_weights(ic_rows: list[dict[str, Any]]) -> dict[str, float]:
    """Use T+20/T+60 Rank IC evidence to tilt the long-term base weights.

    Negative/weak short-term factors are not allowed to dominate. This keeps the
    model aligned with the project's stated medium/long-term objective.
    """
    score_by_factor: dict[str, float] = {}
    for factor in FACTOR_NAMES:
        related = [r for r in ic_rows if r.get("factor_name") == factor]
        evidence = 0.0
        for row in related:
            horizon = int(row.get("horizon") or 0)
            if horizon not in (20, 60):
                continue
            weight = 0.45 if horizon == 20 else 0.55
            rank_ic = float(row.get("rank_ic_mean") or 0)
            positive_ratio = float(row.get("positive_ic_ratio") or 0) / 100
            evidence += weight * max(0.0, rank_ic) * max(0.0, positive_ratio)
        score_by_factor[factor] = evidence
    total_evidence = sum(score_by_factor.values())
    tilted = dict(BASE_LONG_TERM_WEIGHTS)
    if total_evidence > 0:
        for factor in FACTOR_NAMES:
            evidence_weight = score_by_factor.get(factor, 0.0) / total_evidence
            tilted[factor] = 0.65 * BASE_LONG_TERM_WEIGHTS.get(factor, 0.0) + 0.35 * evidence_weight
    for factor, bounds in WEIGHT_BOUNDS.items():
        lo, hi = bounds
        tilted[factor] = min(hi, max(lo, tilted.get(factor, 0.0)))
    return _normalize_weights(tilted)


def _diagnose_model(connection: sqlite3.Connection) -> dict[str, Any]:
    latest = connection.execute("SELECT MAX(end_date) FROM quant_factor_ic").fetchone()[0]
    if not latest:
        return {}
    rows = [dict(r) for r in connection.execute(
        """SELECT factor_name,horizon,sample_size,rank_ic_mean,ic_ir,positive_ic_ratio,status
           FROM quant_factor_ic
           WHERE end_date=? AND window_size=0 AND horizon IN (20,60)
           ORDER BY factor_name,horizon""",
        (latest,),
    ).fetchall()]
    factor_summary = []
    for factor in FACTOR_NAMES:
        related = [r for r in rows if r["factor_name"] == factor]
        if not related:
            continue
        avg_rank_ic = _mean([float(r["rank_ic_mean"] or 0) for r in related]) or 0.0
        avg_positive = _mean([float(r["positive_ic_ratio"] or 0) for r in related]) or 0.0
        status = "稳定有效" if avg_rank_ic >= 0.06 and avg_positive >= 60 else "偏正" if avg_rank_ic >= 0.02 else "反向/衰减" if avg_rank_ic <= -0.02 else "偏弱"
        factor_summary.append(
            {
                "factor_name": factor,
                "avg_rank_ic": avg_rank_ic,
                "avg_positive_ic_ratio": avg_positive,
                "status": status,
                "suggestion": (
                    "保留并提高权重" if status == "稳定有效"
                    else "保留但作为辅助" if status == "偏正"
                    else "降权并重构定义" if status == "反向/衰减"
                    else "保留为约束，不单独主导排序"
                ),
            }
        )
    adaptive_weights = _bounded_adaptive_weights(rows)
    stable = [x for x in factor_summary if x["status"] == "稳定有效"]
    weak = [x for x in factor_summary if x["status"] in {"偏弱", "反向/衰减"}]
    return {
        "as_of": latest,
        "primary_horizon": "T+20/T+60",
        "overall": "当前模型的中长期方向有效，质量、风控和稳定性应作为主轴；短期催化和市场适配表现偏弱，不能主导推荐排序。",
        "stable_factors": [x["factor_name"] for x in stable],
        "weak_factors": [x["factor_name"] for x in weak],
        "factor_summary": factor_summary,
        "recommended_weights": adaptive_weights,
        "next_actions": [
            "主排序继续以质量、风险控制、中长期稳定性为核心。",
            "估值改为安全边际和买点约束，不作为单独强Alpha。",
            "催化因子拆分为基本面催化、交易型催化和风险型催化。",
            "市场适配因子加入拥挤度反向约束，避免追涨。",
        ],
    }


def refresh_factor_ic(connection: sqlite3.Connection) -> int:
    dates = [r[0] for r in connection.execute("SELECT DISTINCT trade_date FROM quant_factor_snapshots ORDER BY trade_date").fetchall()]
    if not dates:
        return 0
    latest = dates[-1]
    now = _utc_now()
    written = 0
    for factor in FACTOR_NAMES:
        for horizon in HORIZONS:
            for window in (0, 20, 60, 120):
                window_dates = dates[-window:] if window else dates
                if not window_dates:
                    continue
                placeholders = ",".join("?" for _ in window_dates)
                rows = [dict(r) for r in connection.execute(
                    f"""SELECT f.trade_date,f.stock_code,f.factor_value,fr.raw_return
                        FROM quant_factor_snapshots f
                        JOIN quant_forward_returns fr
                          ON fr.trade_date=f.trade_date AND fr.stock_code=f.stock_code
                         AND fr.horizon=? AND fr.is_valid=1
                        WHERE f.factor_name=? AND f.trade_date IN ({placeholders})""",
                    (horizon, factor, *window_dates),
                ).fetchall()]
                xs = [_safe_float(r["factor_value"]) for r in rows]
                ys = [_safe_float(r["raw_return"]) for r in rows]
                pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
                if not pairs:
                    continue
                xvals = [p[0] for p in pairs]
                yvals = [p[1] for p in pairs]
                daily_ics: list[float] = []
                for d in sorted(set(r["trade_date"] for r in rows)):
                    day_pairs = [
                        (float(r["factor_value"]), float(r["raw_return"]))
                        for r in rows
                        if r["trade_date"] == d and r["factor_value"] is not None and r["raw_return"] is not None
                    ]
                    if len(day_pairs) >= 5:
                        value = _rank_ic([p[0] for p in day_pairs], [p[1] for p in day_pairs])
                        if value is not None:
                            daily_ics.append(value)
                ic = _pearson(xvals, yvals)
                ric = _rank_ic(xvals, yvals)
                ic_sd = _std(daily_ics)
                ic_avg = _mean(daily_ics)
                connection.execute(
                    """INSERT OR REPLACE INTO quant_factor_ic
                       (factor_name,horizon,window_size,start_date,end_date,sample_size,ic_mean,
                        rank_ic_mean,ic_ir,positive_ic_ratio,status,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        factor,
                        horizon,
                        window,
                        window_dates[0],
                        latest,
                        len(pairs),
                        ic,
                        ric,
                        (ic_avg / ic_sd) if ic_avg is not None and ic_sd else None,
                        (sum(1 for v in daily_ics if v > 0) / len(daily_ics) * 100) if daily_ics else None,
                        _status(ric, len(pairs)),
                        now,
                    ),
                )
                written += 1
    return written


def refresh_group_backtest(connection: sqlite3.Connection) -> int:
    now = _utc_now()
    latest = connection.execute("SELECT MAX(trade_date) FROM quant_factor_snapshots").fetchone()[0]
    written = 0
    for factor in FACTOR_NAMES:
        for horizon in HORIZONS:
            rows = [dict(r) for r in connection.execute(
                """SELECT f.factor_rank_pct,fr.raw_return
                   FROM quant_factor_snapshots f
                   JOIN quant_forward_returns fr
                     ON fr.trade_date=f.trade_date AND fr.stock_code=f.stock_code
                    AND fr.horizon=? AND fr.is_valid=1
                   WHERE f.factor_name=?""",
                (horizon, factor),
            ).fetchall()]
            groups: dict[str, list[float]] = {f"Q{i}": [] for i in range(1, 6)}
            for row in rows:
                pct = float(row["factor_rank_pct"] or 0)
                group_index = min(5, max(1, int((1.0 - pct) * 5) + 1))
                groups[f"Q{group_index}"].append(float(row["raw_return"]))
            means = {g: _mean(v) for g, v in groups.items()}
            monotonicity = "强单调" if all((means[f"Q{i}"] or -999) >= (means[f"Q{i+1}"] or -999) for i in range(1, 5)) else ("弱单调" if (means["Q1"] or -999) > (means["Q5"] or 999) else "无效/反向")
            for group, values in groups.items():
                ordered = sorted(values)
                median = ordered[len(ordered) // 2] if ordered else None
                avg = _mean(values)
                connection.execute(
                    """INSERT OR REPLACE INTO quant_group_backtest
                       (factor_name,horizon,group_name,start_date,end_date,stock_count,avg_return,
                        median_return,win_rate,benchmark_return,excess_return,monotonicity,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        factor,
                        horizon,
                        group,
                        connection.execute("SELECT MIN(trade_date) FROM quant_factor_snapshots").fetchone()[0],
                        latest,
                        len(values),
                        avg,
                        median,
                        (sum(1 for v in values if v > 0) / len(values) * 100) if values else None,
                        None,
                        avg,
                        monotonicity,
                        now,
                    ),
                )
                written += 1
    return written


def refresh_rolling_validation(connection: sqlite3.Connection) -> int:
    latest = connection.execute("SELECT MAX(end_date) FROM quant_factor_ic").fetchone()[0]
    if not latest:
        return 0
    now = _utc_now()
    written = 0
    for row in connection.execute("SELECT * FROM quant_factor_ic WHERE end_date=? AND window_size IN (20,60,120)", (latest,)):
        item = dict(row)
        q1q5 = connection.execute(
            """SELECT group_name,avg_return FROM quant_group_backtest
               WHERE factor_name=? AND horizon=? AND end_date=? AND group_name IN ('Q1','Q5')""",
            (item["factor_name"], item["horizon"], latest),
        ).fetchall()
        means = {r["group_name"]: r["avg_return"] for r in q1q5}
        q1 = means.get("Q1")
        q5 = means.get("Q5")
        connection.execute(
            """INSERT OR REPLACE INTO quant_rolling_validation
               (window_end_date,window_size,factor_name,horizon,sample_size,rank_ic,ic_ir,
                q1_return,q5_return,q1_minus_q5,positive_ic_ratio,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                latest,
                item["window_size"],
                item["factor_name"],
                item["horizon"],
                item["sample_size"],
                item["rank_ic_mean"],
                item["ic_ir"],
                q1,
                q5,
                (q1 - q5) if q1 is not None and q5 is not None else None,
                item["positive_ic_ratio"],
                item["status"],
                now,
            ),
        )
        written += 1
    return written


def refresh_portfolio_optimization(connection: sqlite3.Connection) -> int:
    latest = connection.execute("SELECT MAX(rating_date) FROM daily_stock_ratings").fetchone()[0]
    if not latest:
        return 0
    ic_rows = [dict(r) for r in connection.execute(
        """SELECT factor_name,horizon,rank_ic_mean,positive_ic_ratio
           FROM quant_factor_ic
           WHERE horizon IN (20,60) AND window_size=0
           ORDER BY factor_name,horizon"""
    ).fetchall()]
    base_weights = _bounded_adaptive_weights(ic_rows) if ic_rows else _normalize_weights(BASE_LONG_TERM_WEIGHTS)
    selected = connection.execute("SELECT COUNT(*) FROM ai_fund_portfolio_positions").fetchone()[0] if connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ai_fund_portfolio_positions'").fetchone() else 0
    candidate = connection.execute("SELECT COUNT(*) FROM daily_stock_ratings WHERE rating_date=?", (latest,)).fetchone()[0]
    connection.execute(
        """INSERT OR REPLACE INTO quant_portfolio_optimization
           (run_date,selected_count,candidate_count,objective_score,expected_alpha,risk_penalty,
            turnover_penalty,cost_penalty,constraint_status,factor_weights_json,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            latest,
            selected,
            candidate,
            100.0 * (
                base_weights.get("quality_score", 0)
                + base_weights.get("risk_control_score", 0)
                + base_weights.get("long_term_stability_score", 0)
            ),
            100.0 * (
                base_weights.get("quality_score", 0)
                + base_weights.get("risk_control_score", 0)
                + base_weights.get("long_term_stability_score", 0)
                + base_weights.get("valuation_score", 0)
            ),
            100.0 * base_weights.get("risk_control_score", 0),
            30.0,
            0.12,
            "V2中长期导向优化：T+20/T+60 Rank IC 自适应权重 + 质量/风控/稳定性主轴 + 单票/行业/换手/成本约束",
            json.dumps(base_weights, ensure_ascii=False),
            _utc_now(),
        ),
    )
    return 1


def refresh_all(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    with connection:
        ensure_schema(connection)
        out = {
            "factor_snapshots": refresh_factor_snapshots(connection),
            "forward_returns": refresh_forward_returns(connection),
            "factor_ic": refresh_factor_ic(connection),
            "group_backtest": refresh_group_backtest(connection),
            "rolling_validation": refresh_rolling_validation(connection),
            "portfolio_optimization": refresh_portfolio_optimization(connection),
        }
    connection.close()
    return out


def get_dashboard(connection: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(connection)
    latest = connection.execute("SELECT MAX(trade_date) FROM quant_factor_snapshots").fetchone()[0]
    if not latest:
        refresh_factor_snapshots(connection)
        refresh_forward_returns(connection)
        refresh_factor_ic(connection)
        refresh_group_backtest(connection)
        refresh_rolling_validation(connection)
        refresh_portfolio_optimization(connection)
        latest = connection.execute("SELECT MAX(trade_date) FROM quant_factor_snapshots").fetchone()[0]
    summary = dict(connection.execute(
        """SELECT COUNT(DISTINCT stock_code) stocks, COUNT(DISTINCT factor_name) factors,
                  MIN(trade_date) start_date, MAX(trade_date) end_date
           FROM quant_factor_snapshots"""
    ).fetchone())
    factor_ic = [dict(r) for r in connection.execute(
        """SELECT factor_name,horizon,window_size,sample_size,ic_mean,rank_ic_mean,ic_ir,
                  positive_ic_ratio,status
           FROM quant_factor_ic
           WHERE end_date=(SELECT MAX(end_date) FROM quant_factor_ic)
             AND window_size=0
           ORDER BY factor_name,horizon"""
    ).fetchall()]
    group_rows = [dict(r) for r in connection.execute(
        """SELECT factor_name,horizon,group_name,stock_count,avg_return,median_return,
                  win_rate,excess_return,monotonicity
           FROM quant_group_backtest
           WHERE end_date=(SELECT MAX(end_date) FROM quant_group_backtest)
           ORDER BY factor_name,horizon,group_name"""
    ).fetchall()]
    rolling = [dict(r) for r in connection.execute(
        """SELECT window_size,factor_name,horizon,sample_size,rank_ic,ic_ir,q1_return,q5_return,
                  q1_minus_q5,positive_ic_ratio,status
           FROM quant_rolling_validation
           WHERE window_end_date=(SELECT MAX(window_end_date) FROM quant_rolling_validation)
           ORDER BY factor_name,horizon,window_size"""
    ).fetchall()]
    strategy = [dict(r) for r in connection.execute(
        "SELECT * FROM quant_strategy_versions ORDER BY is_active DESC, created_at DESC LIMIT 5"
    ).fetchall()]
    optimization = dict(connection.execute(
        "SELECT * FROM quant_portfolio_optimization ORDER BY run_date DESC LIMIT 1"
    ).fetchone() or {})
    return {
        "latest_date": latest,
        "summary": summary,
        "factor_labels": {
            "investment_score": "综合Alpha",
            "quality_score": "质量",
            "long_term_stability_score": "中长期稳定性",
            "valuation_score": "估值",
            "catalyst_score": "催化",
            "risk_control_score": "风险控制",
            "market_fit_score": "市场适配",
        },
        "factor_ic": factor_ic,
        "group_backtest": group_rows,
        "rolling_validation": rolling,
        "model_diagnosis": _diagnose_model(connection),
        "strategy_versions": strategy,
        "portfolio_optimization": optimization,
        "explain": {
            "primary_horizon": "本项目定位中期/中长期选股，T+20/T+60 是主观察周期；T+1/T+5 只用于短期反馈。",
            "rank_ic": "Rank IC 衡量因子排名与未来收益排名的相关性，越稳定为正，说明排序越有预测力。",
            "group_backtest": "Q1 是因子最高分组，Q5 是最低分组；Q1-Q5 为正且收益单调，说明因子区分度更好。",
            "portfolio": "AI基金经理第一版采用启发式组合优化：因子有效性加权 + 单票/行业/换手/成本约束。",
        },
    }


if __name__ == "__main__":
    print(json.dumps(refresh_all(), ensure_ascii=False, indent=2))
