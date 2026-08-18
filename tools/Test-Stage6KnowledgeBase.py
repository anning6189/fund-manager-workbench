#!/usr/bin/env python3
"""End-to-end acceptance suite for Stage 6 knowledge infrastructure."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PATH = PROJECT_ROOT / "tools" / "consumer_knowledge_store.py"
SEED_PATH = PROJECT_ROOT / "data" / "seed" / "stage6-consumer-core-seed.v1.json"
REVISION_PATH = PROJECT_ROOT / "tests" / "fixtures" / "stage6-revision-package.v1.json"
INVALID_PATH = PROJECT_ROOT / "tests" / "fixtures" / "stage6-invalid-fund-holdings-package.v1.json"
GATED_RAW_PATH = PROJECT_ROOT / "tests" / "fixtures" / "stage6-gildata-raw-package.v1.json"
CONFLICT_PATH = PROJECT_ROOT / "tests" / "fixtures" / "stage6-conflicting-package-id.v1.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "stage-6-acceptance-report.v1.json"


def load_runtime():
    spec = importlib.util.spec_from_file_location("consumer_knowledge_store", RUNTIME_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "tests" / "tmp-stage6-acceptance.db")
    args = parser.parse_args()
    db_path = args.db
    for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if path.exists():
            path.unlink()

    runtime = load_runtime()
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    initialized = runtime.init_store(db_path)
    check("registered_sources_loaded", initialized["sources_loaded"] == 20, initialized["sources_loaded"])
    check("metric_dictionary_loaded", initialized["metrics_loaded"] == 99, initialized["metrics_loaded"])
    check("full_consumer_taxonomy_loaded", initialized["taxonomy_nodes_loaded"] == 87, initialized["taxonomy_nodes_loaded"])

    first = runtime.ingest_package(db_path, SEED_PATH)
    second = runtime.ingest_package(db_path, SEED_PATH)
    check("seed_ingestion_success", first["status"] == "success", first)
    check("idempotent_ingestion", second["status"] == "idempotent_noop", second)

    status = runtime.store_status(db_path)
    check("sqlite_integrity", status["integrity_check"] == "ok" and not status["foreign_key_violations"], status)
    check("seed_entities_and_relations", status["counts"]["entities"] >= 10 and status["counts"]["relationships"] >= 10, status["counts"])
    check("document_knowledge_seed", status["counts"]["documents"] >= 2 and status["fts_chunks"] >= 4, status["counts"])
    check("time_series_seed", status["counts"]["observations"] >= 12, status["counts"]["observations"])

    resolved = runtime.resolve_entity(db_path, "000333.SZ", "2024-05-01T00:00:00+08:00")
    check("entity_identifier_resolution", any(item["entity_id"] == "cr:security:000333sz" for item in resolved["matches"]), resolved)

    before_publish = runtime.query_metric(
        db_path, "cr:legal_entity:midea", "CR.CO.REVENUE", "2024-03-27T23:59:59+08:00", "2023-12-31"
    )
    cross_timezone_before = runtime.query_metric(
        db_path, "cr:legal_entity:midea", "CR.CO.REVENUE", "2024-03-28T23:00:00+09:00", "2023-12-31"
    )
    after_publish = runtime.query_metric(
        db_path, "cr:legal_entity:midea", "CR.CO.REVENUE", "2024-05-01T00:00:00+08:00", "2023-12-31"
    )
    check("point_in_time_excludes_future_information", len(before_publish["observations"]) == 0, before_publish)
    check("point_in_time_normalizes_timezone", len(cross_timezone_before["observations"]) == 0, cross_timezone_before)
    check("point_in_time_includes_available_information", len(after_publish["observations"]) == 1 and after_publish["observations"][0]["value_numeric"] == 372037280000, after_publish)

    search = runtime.search_documents(db_path, "经营活动", "2024-05-01T00:00:00+08:00", "cr:legal_entity:midea", 10)
    check("full_text_retrieval", any(item["locator"] == "PDF第163页合并现金流量表" for item in search["results"]), search)

    trace = runtime.trace_evidence(db_path, "ev:midea:ar2023:financials")
    check("evidence_trace", trace["evidence"] is not None and len(trace["supported_observations"]) >= 5, trace)

    revised = runtime.ingest_package(db_path, REVISION_PATH)
    old_cutoff = runtime.query_metric(
        db_path, "cr:legal_entity:midea", "CR.CO.REVENUE", "2024-05-01T00:00:00+08:00", "2023-12-31"
    )
    new_cutoff = runtime.query_metric(
        db_path, "cr:legal_entity:midea", "CR.CO.REVENUE", "2025-04-01T00:00:00+08:00", "2023-12-31"
    )
    with sqlite3.connect(db_path) as connection:
        versions = connection.execute(
            "SELECT COUNT(*) FROM observations WHERE entity_id=? AND metric_id=? AND period_end=?",
            ("cr:legal_entity:midea", "CR.CO.REVENUE", "2023-12-31"),
        ).fetchone()[0]
    check("revision_ingestion_success", revised["status"] == "success", revised)
    check("revision_history_preserved", versions == 2, versions)
    check("old_cutoff_keeps_old_version", old_cutoff["observations"][0]["value_numeric"] == 372037280000, old_cutoff)
    check("new_cutoff_selects_new_version", new_cutoff["observations"][0]["value_numeric"] == 372037280001, new_cutoff)

    invalid = runtime.ingest_package(db_path, INVALID_PATH)
    check("forbidden_fund_holdings_quarantined", invalid["status"] == "quarantined" and any(item["code"] == "fund_holdings_forbidden" for item in invalid["issues"]), invalid)

    gated_raw = runtime.ingest_package(db_path, GATED_RAW_PATH, "raw")
    gated_curated = runtime.ingest_package(db_path, GATED_RAW_PATH, "curated")
    with sqlite3.connect(db_path) as connection:
        raw_gate_rows = connection.execute("SELECT COUNT(*) FROM raw_packages WHERE package_id=? AND gate_status='license_gate'", ("CR.TEST.STAGE6.GILDATA.RAW.001",)).fetchone()[0]
    check("license_gated_raw_storage", gated_raw["status"] == "raw_stored" and raw_gate_rows == 1, gated_raw)
    check("license_gated_curated_blocked", gated_curated["status"] == "quarantined" and any(item["code"] == "license_not_curatable" for item in gated_curated["issues"]), gated_curated)
    conflict = runtime.ingest_package(db_path, CONFLICT_PATH, "curated")
    check("package_id_hash_conflict_quarantined", conflict["status"] == "quarantined" and any(item["code"] == "package_id_content_conflict" for item in conflict["issues"]), conflict)

    refresh_plan = runtime.refresh_plan(db_path)
    check("incremental_refresh_plan", refresh_plan["task_count"] == 22 and refresh_plan["license_gated_due_count"] == 8 and refresh_plan["curated_due_count"] == 14, refresh_plan)
    freshness = runtime.freshness_report(db_path)
    check("freshness_monitoring", freshness["summary"]["streams"] == 22, freshness)

    final_status = runtime.store_status(db_path)
    failed = [item for item in checks if not item["passed"]]
    report = {
        "suite_id": "CR.TEST.STAGE6.KNOWLEDGE_BASE.001",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "passed": not failed,
        "decision": "accept_stage6_reference_knowledge_infrastructure_with_incremental_population_boundary" if not failed else "reject_stage6",
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks": checks,
        "store_status": final_status,
        "declared_boundaries": [
            "全市场公司、品牌、产品和SKU需要按来源许可持续回填和人工审核",
            "商业渠道、消费者面板与另类数据未采购",
            "Gildata记录在合同确认前不得进入curated层",
            "本地FTS5已运行，外部向量索引需另行审批"
        ],
        "failures": failed,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
