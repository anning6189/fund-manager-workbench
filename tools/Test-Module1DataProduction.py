#!/usr/bin/env python3
"""Acceptance suite for module 1: data production and full backfill control plane."""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import consumer_data_production as engine  # noqa: E402
import consumer_knowledge_store as knowledge  # noqa: E402


SEED = PROJECT_ROOT / "data" / "seed" / "stage6-consumer-core-seed.v1.json"
SNAPSHOT = PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-consumer-universe-2026-08-12.json"
REPORT = PROJECT_ROOT / "tests" / "module-1-data-production-acceptance.v1.json"


def main() -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    with TemporaryDirectory(prefix="consumer-production-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        db = root / "production.db"
        output = root / "output"
        initialized = engine.init_production(db)
        knowledge.ingest_package(db, SEED)
        check("ten_dataset_contracts_loaded", initialized["dataset_contracts"] == 10, initialized)
        check("a_and_hk_markets_declared", set(initialized["markets"]) == {"A_SHARE", "HK_SHARE"}, initialized)
        check("eleven_full_consumer_sector_packs_declared", initialized["sector_packs"] == 11, initialized)
        check("twenty_source_licence_decisions_initialized", initialized["source_licenses_initialized"] == 20, initialized)

        spec = engine.production_spec()
        check("execution_starts_with_master_data", spec["execution_order"][0] == "master_data", spec["execution_order"])
        check("market_and_financial_history_start_2015", all(
            next(item for item in spec["dataset_contracts"] if item["stream_name"] == stream)["history_start"] == "2015-01-01"
            for stream in ("market_daily", "financials", "announcements")
        ), spec["dataset_contracts"])
        check("macro_history_start_2010", next(item for item in spec["dataset_contracts"] if item["stream_name"] == "macro")["history_start"] == "2010-01-01", spec["dataset_contracts"])
        check("portfolio_fields_prohibited", "fund_holdings" in spec["prohibited_fields"] and "position_inference" in spec["prohibited_fields"], spec["prohibited_fields"])

        plan = engine.build_backfill_plan(db, "2026-08-12", output)
        check("full_backfill_plan_has_1313_partitions", plan["partition_count"] == 1313, plan["partition_count"])
        check("public_partitions_ready", plan["partition_status_counts"].get("ready") == 1078, plan["partition_status_counts"])
        check("commercial_partitions_external_gated", plan["partition_status_counts"].get("external_gate") == 235, plan["partition_status_counts"])
        check("backfill_plan_artifact_written", Path(plan["artifact_path"]).is_file(), plan["artifact_path"])
        check("plan_never_counts_ready_as_populated", "Only completed partitions count" in plan["completion_statement"], plan["completion_statement"])

        snapshot = engine.read_json(SNAPSHOT)
        check("real_a_share_snapshot_has_1159_securities", len(snapshot["securities"]) == 1159, len(snapshot["securities"]))
        check("provider_and_projection_counts_match", snapshot["source_reported_count"] == snapshot["projected_count"] == 1159, {key:snapshot[key] for key in ("source_reported_count","projected_count")})
        check("eleven_overretrieved_fields_discarded", len(snapshot["over_retrieval_fields_discarded"]) == 11, snapshot["over_retrieval_fields_discarded"])
        check("nine_vendor_industry_families_covered", len({item["vendor_industry_l1"] for item in snapshot["securities"]}) == 9, sorted({item["vendor_industry_l1"] for item in snapshot["securities"]}))
        check("all_security_ids_unique", len({item["security_id"] for item in snapshot["securities"]}) == 1159, len({item["security_id"] for item in snapshot["securities"]}))
        check("no_unrequested_market_values_retained", all(set(item) <= set(snapshot["requested_fields"]) for item in snapshot["securities"]), snapshot["requested_fields"])
        check("snapshot_confirms_no_fund_data", snapshot["no_fund_holdings_or_positions"] is True, snapshot["no_fund_holdings_or_positions"])
        check("commercial_snapshot_nonpublishable", snapshot["ingestion_target"] == "raw_license_gate" and snapshot["publication_allowed"] is False, {"target":snapshot["ingestion_target"],"publication":snapshot["publication_allowed"]})
        validation = engine.validate_snapshot(db, snapshot)
        check("real_snapshot_passes_schema_quality", validation == [], validation)

        registered = engine.register_snapshot(db, SNAPSHOT)
        check("real_snapshot_registered", registered["status"] == "registered", registered)
        check("one_master_partition_staged", registered["partitions_attached"] == 1, registered)
        check("snapshot_content_hash_recorded", registered["content_hash"].startswith("sha256:"), registered["content_hash"])

        identical = engine.register_snapshot(db, SNAPSHOT)
        check("snapshot_registration_idempotent", identical["snapshot_id"] == registered["snapshot_id"], identical)

        invalid = copy.deepcopy(snapshot)
        invalid["snapshot_id"] = "CR.TEST.BAD.PROJECTION"
        invalid["securities"][0]["closing_price"] = 1
        invalid_issues = engine.validate_snapshot(db, invalid)
        check("unprojected_field_blocked", any(item["code"] == "unprojected_fields_present" for item in invalid_issues), invalid_issues)
        portfolio = copy.deepcopy(snapshot)
        portfolio["snapshot_id"] = "CR.TEST.BAD.PORTFOLIO"
        portfolio["fund_holdings"] = []
        portfolio_issues = engine.validate_snapshot(db, portfolio)
        check("fund_holdings_field_blocked", any(item["code"] == "portfolio_field_forbidden" for item in portfolio_issues), portfolio_issues)
        bypass = copy.deepcopy(snapshot)
        bypass["snapshot_id"] = "CR.TEST.BAD.LICENCE"
        bypass["ingestion_target"] = "curated"
        bypass["publication_allowed"] = True
        bypass_issues = engine.validate_snapshot(db, bypass)
        check("licence_gate_bypass_blocked", any(item["code"] == "license_gate_bypass" for item in bypass_issues), bypass_issues)

        promotion_blocked = False
        try:
            engine.promote_snapshot(db, snapshot["snapshot_id"], "法务测试员", "许可未批准，预期阻断")
        except engine.ProductionValidationError as exc:
            promotion_blocked = any(item["code"] == "license_not_approved" for item in exc.issues)
        check("pending_licence_blocks_promotion", promotion_blocked, promotion_blocked)
        agent_decision_blocked = False
        try:
            engine.decide_license(db, snapshot["source_id"], "approved", "AI", "")
        except engine.ProductionValidationError as exc:
            agent_decision_blocked = any(item["code"] == "named_human_required" for item in exc.issues)
        check("agent_cannot_approve_commercial_licence", agent_decision_blocked, agent_decision_blocked)

        final = engine.finalize_backfill(db, plan["backfill_run_id"], output)
        check("run_truthfully_partially_complete", final["status"] == "partially_complete", final)
        check("staged_partition_not_counted_as_populated", final["license_staged_partition_count"] == 1 and final["populated_partition_count"] == 0, final)
        check("1078_public_partitions_remain_unpopulated", final["unpopulated_public_partition_count"] == 1078, final)
        check("234_other_commercial_partitions_remain_gated", final["external_gate_partition_count"] == 234, final)
        check("four_audit_artifacts_written", len(final["artifacts"]) == 4 and all(Path(path).is_file() for path in final["artifacts"].values()), final["artifacts"])

        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name LIKE 'production_%' OR name LIKE 'backfill_%' OR name LIKE 'source_license%' OR name LIKE 'snapshot_promotion%')")}
            quality = {row["gate_name"]:row["status"] for row in connection.execute("SELECT gate_name,status FROM production_quality_results WHERE snapshot_id=?", (snapshot["snapshot_id"],))}
            license_row = dict(connection.execute("SELECT decision,allowed_targets_json FROM source_license_decisions WHERE source_id=?", (snapshot["source_id"],)).fetchone())
        expected_tables = {"production_dataset_contracts","source_license_decisions","production_snapshots","backfill_runs","backfill_partitions","production_quality_results","production_coverage_watermarks","snapshot_promotion_events"}
        check("production_audit_schema_complete", expected_tables <= tables, sorted(tables))
        check("schema_count_and_boundary_quality_passed", quality.get("schema_projection") == "passed" and quality.get("record_count") == "passed" and quality.get("no_fund_holdings") == "passed", quality)
        check("licence_quality_is_blocked_not_failed", quality.get("license_gate") == "blocked", quality)
        check("gildata_allowed_target_raw_only", license_row["decision"] == "pending" and json.loads(license_row["allowed_targets_json"]) == ["raw_license_gate"], license_row)

    passed = sum(1 for item in checks if item["passed"])
    result = {"module":1,"name":"数据生产化与全量回填","status":"passed" if passed == len(checks) else "failed","passed":passed,"total":len(checks),"checks":checks}
    engine.write_json(REPORT, result)
    print(json.dumps({"status":result["status"],"passed":passed,"total":len(checks)},ensure_ascii=False,indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
