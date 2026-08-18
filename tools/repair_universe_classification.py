#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair research universe to one primary sector per security and enforce uniqueness."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import consumer_data_production as production
import full_consumer_coverage as coverage

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "curated" / "consumer-research.db"


def run(db_path: Path = DB) -> dict:
    rules = coverage.spec()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        before = db.execute(
            "SELECT COUNT(*) rows_n,COUNT(DISTINCT security_id) ids,"
            "SUM(CASE WHEN sector_code IS NULL THEN 1 ELSE 0 END) unmapped FROM research_universe_members"
        ).fetchone()
        snapshots = [row[0] for row in db.execute("SELECT universe_snapshot_id FROM research_universe_snapshots")]
        for snapshot_id in snapshots:
            rows = db.execute(
                """SELECT * FROM research_universe_members WHERE universe_snapshot_id=?
                   ORDER BY security_id,mapping_confidence DESC,membership_id""", (snapshot_id,)
            ).fetchall()
            securities = {}
            for row in rows:
                securities.setdefault(row["security_id"], dict(row))
            db.execute("DELETE FROM research_universe_members WHERE universe_snapshot_id=?", (snapshot_id,))
            inserts = []
            for security_id, security in sorted(securities.items()):
                mappings = coverage.security_mappings(security, rules)
                sector_code, confidence = mappings[0] if mappings else (None, 0.0)
                status = "mapped" if sector_code else "review_required"
                membership_id = production.stable_id("member", snapshot_id, security_id, sector_code or "UNMAPPED")
                inserts.append((membership_id, snapshot_id, security_id, security["security_code"],
                                security["security_name"], security["market_mic"], security["trading_status"],
                                security.get("vendor_industry_l1"), security.get("vendor_industry_l2"),
                                security.get("vendor_industry_l3"), sector_code, status, confidence))
            db.executemany(
                """INSERT INTO research_universe_members(
                   membership_id,universe_snapshot_id,security_id,security_code,security_name,market_mic,
                   trading_status,vendor_industry_l1,vendor_industry_l2,vendor_industry_l3,
                   sector_code,mapping_status,mapping_confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", inserts)
            mapped = sum(1 for row in inserts if row[10])
            db.execute(
                "UPDATE research_universe_snapshots SET security_count=?,mapped_security_count=?,review_required_count=? WHERE universe_snapshot_id=?",
                (len(inserts), mapped, len(inserts) - mapped, snapshot_id))

        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_research_universe_security ON research_universe_members(universe_snapshot_id,security_id)")
        latest = db.execute("SELECT universe_snapshot_id,as_of_date FROM research_universe_snapshots ORDER BY as_of_date DESC LIMIT 1").fetchone()
        if latest:
            db.execute(
                """UPDATE daily_stock_ratings SET sector_code=(
                       SELECT m.sector_code FROM research_universe_members m
                       WHERE m.universe_snapshot_id=? AND m.security_id=daily_stock_ratings.security_id)
                   WHERE EXISTS(SELECT 1 FROM research_universe_members m
                                WHERE m.universe_snapshot_id=? AND m.security_id=daily_stock_ratings.security_id)""",
                (latest["universe_snapshot_id"], latest["universe_snapshot_id"]),
            )
            for row in db.execute("SELECT sector_code,market FROM research_coverage_status").fetchall():
                mics = ("XSHG", "XSHE", "XBSE") if row["market"] == "A_SHARE" else ("XHKG",)
                placeholders = ",".join("?" for _ in mics)
                count = db.execute(
                    f"SELECT COUNT(*) FROM research_universe_members WHERE universe_snapshot_id=? AND sector_code=? AND market_mic IN ({placeholders})",
                    (latest["universe_snapshot_id"], row["sector_code"], *mics),
                ).fetchone()[0]
                db.execute("UPDATE research_coverage_status SET security_count=?,as_of_date=? WHERE sector_code=? AND market=?",
                           (count, latest["as_of_date"], row["sector_code"], row["market"]))
        db.commit()
        after = db.execute(
            "SELECT COUNT(*) rows_n,COUNT(DISTINCT security_id) ids,"
            "SUM(CASE WHEN sector_code IS NULL THEN 1 ELSE 0 END) unmapped FROM research_universe_members"
        ).fetchone()
        duplicates = db.execute(
            "SELECT COUNT(*) FROM (SELECT universe_snapshot_id,security_id FROM research_universe_members GROUP BY 1,2 HAVING COUNT(*)>1)"
        ).fetchone()[0]
    return {"before": dict(before), "after": dict(after), "duplicate_security_ids": duplicates}


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False))
