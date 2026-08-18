#!/usr/bin/env python3
"""Production backfill and incremental data-control plane for consumer research.

The module does not pretend that a planned or license-gated partition is
populated.  Commercial snapshots remain in raw_license_gate until a named
human records an approved licence decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import consumer_knowledge_store as knowledge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
SPEC_PATH = PROJECT_ROOT / "specs" / "production" / "consumer-data-production.v1.json"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "004_consumer_data_production.sql"
DOMAIN_PATH = PROJECT_ROOT / "specs" / "consumer-domain-model.v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "production" / "module1-data-production"
ENGINE_VERSION = "1.0.0"

FORBIDDEN_KEYS = {
    "fund_holdings", "fund_holding", "fund_position", "fund_positions",
    "portfolio_holdings", "portfolio_exposure", "portfolio_position",
    "position_inference", "holding_inference", "trade_instruction",
}


class ProductionValidationError(ValueError):
    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        super().__init__("; ".join(item["message"] for item in issues))


def issue(code: str, message: str, path: str = "$") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def scan_forbidden(value: Any, path: str = "$") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in FORBIDDEN_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(scan_forbidden(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(scan_forbidden(child, f"{path}[{index}]"))
    return hits


def production_spec() -> dict[str, Any]:
    return read_json(SPEC_PATH)


def level_two_sectors() -> list[str]:
    domain = read_json(DOMAIN_PATH)
    return [child["code"] for root in domain["taxonomy"] for child in root.get("children", [])]


def init_production(db_path: Path) -> dict[str, Any]:
    knowledge.init_store(db_path)
    spec = production_spec()
    now = knowledge.utc_now()
    with knowledge.connect(db_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for contract in spec["dataset_contracts"]:
            connection.execute(
                """INSERT OR REPLACE INTO production_dataset_contracts(
                       stream_name,description,history_start,partition_strategy,dependencies_json,
                       quality_gates_json,service_level_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    contract["stream_name"], contract["description"], contract["history_start"],
                    contract["partition_strategy"], canonical(contract["dependencies"]),
                    canonical(contract["quality_gates"]), canonical(contract["service_level"]), now,
                ),
            )
        sources = connection.execute(
            "SELECT source_id,license_status,status FROM source_catalog ORDER BY source_id"
        ).fetchall()
        for source in sources:
            public = str(source["license_status"]).startswith("public_")
            decision = "public_official" if public else "pending"
            targets = ["raw", "curated"] if public else ["raw_license_gate"]
            connection.execute(
                """INSERT OR IGNORE INTO source_license_decisions(
                       source_id,decision,allowed_targets_json,redistribution_allowed,cache_policy,
                       decided_by,decided_at,notes,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    source["source_id"], decision, canonical(targets), int(public),
                    "official_public_research_use" if public else "pending_contract_review",
                    "source_registry" if public else None, now if public else None,
                    "Initialized from the stage-5 source registry; legal approval can only be recorded by a named human.", now,
                ),
            )
    return {
        "status": "ready", "engine_version": ENGINE_VERSION, "database": str(db_path),
        "dataset_contracts": len(spec["dataset_contracts"]), "markets": spec["markets"],
        "sector_packs": len(level_two_sectors()), "source_licenses_initialized": len(sources),
    }


def source_streams(db_path: Path) -> list[dict[str, Any]]:
    """Return the 22 stage-6 refresh routes as structured records."""
    plan = knowledge.refresh_plan(db_path)
    return plan["tasks"]


def year_range(start: str | None, cutoff: str) -> list[int | None]:
    if not start:
        return [None]
    first = int(start[:4])
    last = int(cutoff[:4])
    return list(range(first, last + 1))


def build_backfill_plan(db_path: Path, cutoff_date: str, output_root: Path = DEFAULT_OUTPUT,
                        mode: str = "full") -> dict[str, Any]:
    init_production(db_path)
    try:
        cutoff = date.fromisoformat(cutoff_date)
    except ValueError as exc:
        raise ProductionValidationError([issue("cutoff_date_invalid", "cutoff_date must be YYYY-MM-DD")]) from exc
    spec = production_spec()
    contracts = {item["stream_name"]: item for item in spec["dataset_contracts"]}
    sector_codes = level_two_sectors()
    routes = source_streams(db_path)
    plan_signature = knowledge.sha256_json({"cutoff_date": cutoff_date, "mode": mode, "engine": ENGINE_VERSION})
    run_id = stable_id("bf", cutoff_date, mode, plan_signature)
    partitions: list[dict[str, Any]] = []
    order = {name: index for index, name in enumerate(spec["execution_order"])}
    with knowledge.connect(db_path) as connection:
        existing = connection.execute("SELECT status FROM backfill_runs WHERE backfill_run_id=?", (run_id,)).fetchone()
        if not existing:
            connection.execute(
                "INSERT INTO backfill_runs(backfill_run_id,plan_id,cutoff_date,status,mode,started_at) VALUES(?,?,?,?,?,?)",
                (run_id, spec["spec_id"], cutoff_date, "planned", mode, knowledge.utc_now()),
            )
        license_by_source = {
            row["source_id"]: row["decision"] for row in connection.execute(
                "SELECT source_id,decision FROM source_license_decisions"
            ).fetchall()
        }
        for route in sorted(routes, key=lambda item: (order.get(item["stream_name"], 99), item["source_id"])):
            stream = route["stream_name"]
            contract = contracts[stream]
            strategy = contract["partition_strategy"]
            markets = spec["markets"] if "market" in strategy else [None]
            sectors = sector_codes if "sector" in strategy else [None]
            years = year_range(contract["history_start"], cutoff_date)
            for market in markets:
                for sector_code in sectors:
                    for year in years:
                        period_start = f"{year}-01-01" if year else None
                        period_end = min(date(year, 12, 31), cutoff).isoformat() if year else cutoff_date
                        parts = [stream, route["source_id"], market or "ALL", sector_code or "ALL", str(year or "SNAPSHOT")]
                        partition_id = "bp:" + ":".join(parts)
                        decision = license_by_source.get(route["source_id"], "pending")
                        target = "curated" if decision in {"approved", "public_official"} else "raw_license_gate"
                        status = "ready" if target == "curated" else "external_gate"
                        dependencies = contract["dependencies"]
                        record = {
                            "partition_id": partition_id, "stream_name": stream,
                            "source_id": route["source_id"], "market": market, "sector_code": sector_code,
                            "period_start": period_start, "period_end": period_end,
                            "depends_on": dependencies, "target_layer": target, "status": status,
                            "blocking_gate": None if target == "curated" else "license_not_confirmed_for_curated_use",
                        }
                        partitions.append(record)
                        connection.execute(
                            """INSERT OR IGNORE INTO backfill_partitions(
                                   backfill_run_id,partition_id,stream_name,source_id,market,sector_code,
                                   period_start,period_end,depends_on_json,target_layer,status
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                run_id, partition_id, stream, route["source_id"], market, sector_code,
                                period_start, period_end, canonical(dependencies), target, status,
                            ),
                        )
    counts = Counter(item["status"] for item in partitions)
    result = {
        "backfill_run_id": run_id, "cutoff_date": cutoff_date, "mode": mode,
        "execution_order": spec["execution_order"], "partition_count": len(partitions),
        "partition_status_counts": dict(counts), "partitions": partitions,
        "completion_statement": "Production plan created. Only completed partitions count as populated; external_gate and ready do not.",
    }
    path = output_root / run_id.replace(":", "-") / "backfill-plan.json"
    write_json(path, result)
    result["artifact_path"] = str(path)
    return result


def snapshot_records(snapshot: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    collections = [(key, snapshot.get(key)) for key in ("securities", "observations", "documents", "records") if key in snapshot]
    if len(collections) != 1 or not isinstance(collections[0][1], list):
        raise ProductionValidationError([issue("snapshot_collection_invalid", "Snapshot must contain exactly one list: securities, observations, documents, or records")])
    return collections[0][0], collections[0][1]


def validate_snapshot(db_path: Path, snapshot: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    required = [
        "snapshot_id", "source_id", "stream_name", "provider_as_of_date", "retrieved_at",
        "license_status", "ingestion_target", "publication_allowed", "source_reported_count",
        "projected_count", "requested_fields", "no_fund_holdings_or_positions",
    ]
    for key in required:
        if key not in snapshot:
            problems.append(issue("required_field_missing", f"Missing snapshot field: {key}", f"$.{key}"))
    for path in scan_forbidden(snapshot):
        problems.append(issue("portfolio_field_forbidden", "Fund holdings, positions, and trade instructions are forbidden", path))
    if problems:
        return problems
    try:
        date.fromisoformat(snapshot["provider_as_of_date"])
        knowledge.parse_timestamp(snapshot["retrieved_at"])
    except (ValueError, TypeError):
        problems.append(issue("snapshot_time_invalid", "provider_as_of_date and retrieved_at must be valid dates with timezone"))
    try:
        collection_name, records = snapshot_records(snapshot)
    except ProductionValidationError as exc:
        return problems + exc.issues
    if snapshot["projected_count"] != len(records):
        problems.append(issue("projected_count_mismatch", f"projected_count={snapshot['projected_count']} but records={len(records)}"))
    if snapshot["source_reported_count"] < snapshot["projected_count"]:
        problems.append(issue("source_count_below_projection", "source_reported_count cannot be below projected_count"))
    if snapshot.get("no_fund_holdings_or_positions") is not True:
        problems.append(issue("portfolio_boundary_not_confirmed", "Snapshot must confirm that no fund holdings or positions are included"))
    requested_fields = set(snapshot["requested_fields"])
    for index, record in enumerate(records):
        extra = set(record) - requested_fields
        if extra:
            problems.append(issue("unprojected_fields_present", f"Unrequested fields remain: {sorted(extra)}", f"$.{collection_name}[{index}]"))
            break
    if collection_name == "securities":
        ids = [item.get("security_id") for item in records]
        if None in ids or len(ids) != len(set(ids)):
            problems.append(issue("security_key_invalid", "security_id must be present and unique"))
        if any(item.get("market_mic") not in {"XSHG", "XSHE", "XBSE", "XHKG"} for item in records):
            problems.append(issue("market_mic_invalid", "Every security must have a supported market_mic"))
        if any(not item.get("vendor_industry_l1") for item in records):
            problems.append(issue("classification_missing", "Every security must have a vendor industry classification"))
    with knowledge.connect(db_path) as connection:
        source = connection.execute(
            "SELECT source_id FROM source_catalog WHERE source_id=?", (snapshot["source_id"],)
        ).fetchone()
        contract = connection.execute(
            "SELECT stream_name FROM production_dataset_contracts WHERE stream_name=?", (snapshot["stream_name"],)
        ).fetchone()
        license_row = connection.execute(
            "SELECT decision FROM source_license_decisions WHERE source_id=?", (snapshot["source_id"],)
        ).fetchone()
    if not source:
        problems.append(issue("source_not_registered", f"Unknown source: {snapshot['source_id']}"))
    if not contract:
        problems.append(issue("stream_not_registered", f"Unknown stream: {snapshot['stream_name']}"))
    decision = license_row["decision"] if license_row else "pending"
    if decision not in {"approved", "public_official"}:
        if snapshot["ingestion_target"] != "raw_license_gate" or snapshot["publication_allowed"] is not False:
            problems.append(issue("license_gate_bypass", "Pending commercial data must remain non-publishable in raw_license_gate"))
    return problems


def quality_event(connection: sqlite3.Connection, snapshot_id: str, gate_name: str,
                  status: str, observed: Any, expected: Any, detail: dict[str, Any] | None = None) -> None:
    quality_id = stable_id("pqr", snapshot_id, gate_name)
    connection.execute(
        """INSERT OR REPLACE INTO production_quality_results(
               quality_result_id,snapshot_id,gate_name,status,observed_value,expected_value,detail_json,checked_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (quality_id, snapshot_id, gate_name, status, str(observed), str(expected), canonical(detail or {}), knowledge.utc_now()),
    )


def attach_snapshot_to_partitions(connection: sqlite3.Connection, snapshot: dict[str, Any],
                                  collection: str, records: list[dict[str, Any]]) -> int:
    rows = connection.execute(
        """SELECT backfill_run_id,partition_id,period_start,period_end,market,sector_code,target_layer
           FROM backfill_partitions WHERE source_id=? AND stream_name=?""",
        (snapshot["source_id"], snapshot["stream_name"]),
    ).fetchall()
    matched = 0
    for row in rows:
        if row["market"] and snapshot.get("market") and row["market"] != snapshot["market"]:
            continue
        count = 0
        if collection == "securities":
            count = len(records) if not row["market"] or row["market"] == snapshot.get("market") else 0
        else:
            for record in records:
                period = str(record.get("date") or record.get("period_end") or record.get("published_at") or "")[:10]
                if (not row["period_start"] or period >= row["period_start"]) and (not row["period_end"] or period <= row["period_end"]):
                    count += 1
        if count == 0:
            continue
        status = "completed" if row["target_layer"] == "curated" else "staged_raw_license_gate"
        connection.execute(
            """UPDATE backfill_partitions SET status=?,record_count=?,snapshot_id=?,checkpoint=?,completed_at=?
               WHERE backfill_run_id=? AND partition_id=?""",
            (
                status, count, snapshot["snapshot_id"], snapshot["provider_as_of_date"], knowledge.utc_now(),
                row["backfill_run_id"], row["partition_id"],
            ),
        )
        matched += 1
    return matched


def register_snapshot(db_path: Path, snapshot_path: Path) -> dict[str, Any]:
    init_production(db_path)
    snapshot = read_json(snapshot_path)
    problems = validate_snapshot(db_path, snapshot)
    if problems:
        raise ProductionValidationError(problems)
    collection, records = snapshot_records(snapshot)
    content_hash = file_hash(snapshot_path)
    discarded = snapshot.get("over_retrieval_fields_discarded", [])
    with knowledge.connect(db_path) as connection:
        existing = connection.execute(
            "SELECT content_hash FROM production_snapshots WHERE snapshot_id=?", (snapshot["snapshot_id"],)
        ).fetchone()
        if existing and existing["content_hash"] != content_hash:
            raise ProductionValidationError([issue("snapshot_id_content_conflict", "snapshot_id already exists with different content")])
        connection.execute(
            """INSERT OR IGNORE INTO production_snapshots(
                   snapshot_id,source_id,stream_name,market,provider_as_of_date,retrieved_at,
                   source_reported_count,projected_count,content_hash,storage_path,license_status,
                   ingestion_target,publication_allowed,schema_status,quality_status,
                   field_projection_json,discarded_fields_json,registered_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'passed','passed',?,?,?)""",
            (
                snapshot["snapshot_id"], snapshot["source_id"], snapshot["stream_name"], snapshot.get("market"),
                snapshot["provider_as_of_date"], knowledge.normalize_timestamp(snapshot["retrieved_at"]),
                snapshot["source_reported_count"], snapshot["projected_count"], content_hash, str(snapshot_path.resolve()),
                snapshot["license_status"], snapshot["ingestion_target"], int(bool(snapshot["publication_allowed"])),
                canonical(snapshot["requested_fields"]), canonical(discarded), knowledge.utc_now(),
            ),
        )
        quality_event(connection, snapshot["snapshot_id"], "schema_projection", "passed", len(snapshot["requested_fields"]), "requested fields only")
        quality_event(connection, snapshot["snapshot_id"], "record_count", "passed", len(records), snapshot["projected_count"])
        quality_event(connection, snapshot["snapshot_id"], "no_fund_holdings", "passed", True, True)
        license_decision = connection.execute(
            "SELECT decision FROM source_license_decisions WHERE source_id=?", (snapshot["source_id"],)
        ).fetchone()["decision"]
        quality_event(
            connection, snapshot["snapshot_id"], "license_gate",
            "passed" if license_decision in {"approved", "public_official"} else "blocked",
            license_decision, "approved or public_official",
            {"ingestion_target": snapshot["ingestion_target"], "publication_allowed": snapshot["publication_allowed"]},
        )
        attached = attach_snapshot_to_partitions(connection, snapshot, collection, records)
        earliest = min((str(item.get("date") or item.get("period_end") or snapshot["provider_as_of_date"])[:10] for item in records), default=None)
        latest = max((str(item.get("date") or item.get("period_end") or snapshot["provider_as_of_date"])[:10] for item in records), default=None)
        connection.execute(
            """INSERT OR REPLACE INTO production_coverage_watermarks(
                   stream_name,source_id,market,sector_code,earliest_period,latest_period,record_count,
                   completeness_status,license_gate,last_snapshot_id,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot["stream_name"], snapshot["source_id"], snapshot.get("market") or "ALL", "ALL",
                earliest, latest, len(records),
                "staged_not_publishable" if license_decision not in {"approved", "public_official"} else "populated",
                None if license_decision in {"approved", "public_official"} else "license_not_confirmed_for_curated_use",
                snapshot["snapshot_id"], knowledge.utc_now(),
            ),
        )
    return {
        "status": "registered", "snapshot_id": snapshot["snapshot_id"], "collection": collection,
        "record_count": len(records), "source_reported_count": snapshot["source_reported_count"],
        "ingestion_target": snapshot["ingestion_target"], "publication_allowed": snapshot["publication_allowed"],
        "partitions_attached": attached, "content_hash": content_hash,
    }


def decide_license(db_path: Path, source_id: str, decision: str, decided_by: str,
                   notes: str, redistribution_allowed: bool = False) -> dict[str, Any]:
    init_production(db_path)
    if not decided_by.strip() or decided_by.strip().lower() in {"ai", "agent", "unassigned"}:
        raise ProductionValidationError([issue("named_human_required", "A named legal/procurement reviewer is required")])
    if decision not in {"approved", "rejected"}:
        raise ProductionValidationError([issue("license_decision_invalid", "decision must be approved or rejected")])
    with knowledge.connect(db_path) as connection:
        row = connection.execute("SELECT source_id FROM source_catalog WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            raise ProductionValidationError([issue("source_not_registered", f"Unknown source: {source_id}")])
        targets = ["raw", "curated"] if decision == "approved" else ["raw_license_gate"]
        connection.execute(
            """UPDATE source_license_decisions SET decision=?,allowed_targets_json=?,redistribution_allowed=?,
               decided_by=?,decided_at=?,notes=?,updated_at=? WHERE source_id=?""",
            (
                decision, canonical(targets), int(redistribution_allowed), decided_by.strip(), knowledge.utc_now(),
                notes, knowledge.utc_now(), source_id,
            ),
        )
    return {"source_id": source_id, "decision": decision, "decided_by": decided_by.strip(), "redistribution_allowed": redistribution_allowed}


def promote_snapshot(db_path: Path, snapshot_id: str, decided_by: str, reason: str) -> dict[str, Any]:
    if not decided_by.strip() or decided_by.strip().lower() in {"ai", "agent", "unassigned"}:
        raise ProductionValidationError([issue("named_human_required", "A named human reviewer is required")])
    with knowledge.connect(db_path) as connection:
        snapshot = connection.execute("SELECT * FROM production_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if not snapshot:
            raise ProductionValidationError([issue("snapshot_not_found", f"Unknown snapshot: {snapshot_id}")])
        license_row = connection.execute(
            "SELECT decision FROM source_license_decisions WHERE source_id=?", (snapshot["source_id"],)
        ).fetchone()
        if license_row["decision"] not in {"approved", "public_official"}:
            promotion_id = stable_id("prom", snapshot_id, "blocked", knowledge.utc_now())
            connection.execute(
                "INSERT INTO snapshot_promotion_events VALUES(?,?,?,?,?,?,?,?)",
                (promotion_id, snapshot_id, snapshot["ingestion_target"], "curated", "blocked", decided_by.strip(), reason, knowledge.utc_now()),
            )
            raise ProductionValidationError([issue("license_not_approved", "Snapshot cannot be promoted until its source licence is approved")])
        connection.execute(
            "UPDATE production_snapshots SET ingestion_target='curated',publication_allowed=1 WHERE snapshot_id=?",
            (snapshot_id,),
        )
        promotion_id = stable_id("prom", snapshot_id, "approved", knowledge.utc_now())
        connection.execute(
            "INSERT INTO snapshot_promotion_events VALUES(?,?,?,?,?,?,?,?)",
            (promotion_id, snapshot_id, snapshot["ingestion_target"], "curated", "approved", decided_by.strip(), reason, knowledge.utc_now()),
        )
    return {"snapshot_id": snapshot_id, "status": "promoted", "decided_by": decided_by.strip()}


def finalize_backfill(db_path: Path, run_id: str, output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    with knowledge.connect(db_path) as connection:
        run = connection.execute("SELECT * FROM backfill_runs WHERE backfill_run_id=?", (run_id,)).fetchone()
        if not run:
            raise ProductionValidationError([issue("backfill_run_not_found", f"Unknown run: {run_id}")])
        partitions = [dict(row) for row in connection.execute(
            "SELECT * FROM backfill_partitions WHERE backfill_run_id=? ORDER BY stream_name,source_id,partition_id", (run_id,)
        ).fetchall()]
        counts = Counter(item["status"] for item in partitions)
        if counts.get("ready", 0) or counts.get("planned", 0):
            status = "partially_complete"
        elif counts.get("external_gate", 0) or counts.get("staged_raw_license_gate", 0):
            status = "complete_with_external_gates"
        elif counts.get("blocked", 0) or counts.get("failed", 0):
            status = "blocked"
        else:
            status = "completed"
        summary = {
            "partition_count": len(partitions), "status_counts": dict(counts),
            "populated_partition_count": counts.get("completed", 0),
            "license_staged_partition_count": counts.get("staged_raw_license_gate", 0),
            "external_gate_partition_count": counts.get("external_gate", 0),
            "unpopulated_public_partition_count": counts.get("ready", 0),
        }
        connection.execute(
            "UPDATE backfill_runs SET status=?,completed_at=?,summary_json=? WHERE backfill_run_id=?",
            (status, knowledge.utc_now(), canonical(summary), run_id),
        )
        snapshots = [dict(row) for row in connection.execute(
            "SELECT * FROM production_snapshots ORDER BY stream_name,source_id,provider_as_of_date"
        ).fetchall()]
        quality = [dict(row) for row in connection.execute(
            "SELECT * FROM production_quality_results ORDER BY snapshot_id,gate_name"
        ).fetchall()]
        watermarks = [dict(row) for row in connection.execute(
            "SELECT * FROM production_coverage_watermarks ORDER BY stream_name,source_id,market,sector_code"
        ).fetchall()]
        gates = [dict(row) for row in connection.execute(
            "SELECT * FROM source_license_decisions WHERE decision IN ('pending','rejected') ORDER BY source_id"
        ).fetchall()]
    root = output_root / run_id.replace(":", "-")
    artifacts = {
        "snapshot_registry": root / "snapshot-registry.json",
        "quality_report": root / "quality-report.json",
        "coverage_watermarks": root / "coverage-watermarks.json",
        "external_gates": root / "external-gates.json",
    }
    write_json(artifacts["snapshot_registry"], {"snapshots": snapshots})
    write_json(artifacts["quality_report"], {"quality_results": quality})
    write_json(artifacts["coverage_watermarks"], {"watermarks": watermarks})
    write_json(artifacts["external_gates"], {"license_gates": gates})
    return {
        "backfill_run_id": run_id, "status": status, **summary,
        "artifacts": {key: str(path) for key, path in artifacts.items()},
        "completion_interpretation": (
            "Pipeline and audit controls are operational, but full data population is not complete."
            if status == "partially_complete" else
            "All technically available partitions are complete; named external licence gates remain."
            if status == "complete_with_external_gates" else status
        ),
    }


def production_status(db_path: Path) -> dict[str, Any]:
    initialized = init_production(db_path)
    with knowledge.connect(db_path) as connection:
        table_counts = {}
        for table in (
            "production_dataset_contracts", "source_license_decisions", "production_snapshots",
            "backfill_runs", "backfill_partitions", "production_quality_results",
            "production_coverage_watermarks", "snapshot_promotion_events",
        ):
            table_counts[table] = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        license_counts = {
            row["decision"]: row["count"] for row in connection.execute(
                "SELECT decision,COUNT(*) AS count FROM source_license_decisions GROUP BY decision"
            ).fetchall()
        }
        run_counts = {
            row["status"]: row["count"] for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM backfill_runs GROUP BY status"
            ).fetchall()
        }
    return {**initialized, "table_counts": table_counts, "license_counts": license_counts, "run_counts": run_counts}


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consumer research production backfill control plane")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    plan = sub.add_parser("plan")
    plan.add_argument("--cutoff-date", required=True)
    plan.add_argument("--mode", choices=["full", "incremental", "reconciliation"], default="full")
    plan.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    register = sub.add_parser("register-snapshot")
    register.add_argument("--snapshot", type=Path, required=True)
    decide = sub.add_parser("decide-license")
    decide.add_argument("--source-id", required=True)
    decide.add_argument("--decision", choices=["approved", "rejected"], required=True)
    decide.add_argument("--decided-by", required=True)
    decide.add_argument("--notes", required=True)
    decide.add_argument("--redistribution-allowed", action="store_true")
    promote = sub.add_parser("promote")
    promote.add_argument("--snapshot-id", required=True)
    promote.add_argument("--decided-by", required=True)
    promote.add_argument("--reason", required=True)
    final = sub.add_parser("finalize")
    final.add_argument("--run-id", required=True)
    final.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    sub.add_parser("status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = init_production(args.db)
        elif args.command == "plan":
            result = build_backfill_plan(args.db, args.cutoff_date, args.output_root, args.mode)
        elif args.command == "register-snapshot":
            result = register_snapshot(args.db, args.snapshot)
        elif args.command == "decide-license":
            result = decide_license(args.db, args.source_id, args.decision, args.decided_by, args.notes, args.redistribution_allowed)
        elif args.command == "promote":
            result = promote_snapshot(args.db, args.snapshot_id, args.decided_by, args.reason)
        elif args.command == "finalize":
            result = finalize_backfill(args.db, args.run_id, args.output_root)
        else:
            result = production_status(args.db)
        emit(result)
        return 0
    except ProductionValidationError as exc:
        emit({"status": "blocked", "issues": exc.issues})
        return 2


if __name__ == "__main__":
    sys.exit(main())
