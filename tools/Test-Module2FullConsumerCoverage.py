#!/usr/bin/env python3
"""Acceptance suite for module 2: full-consumer research coverage."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import full_consumer_coverage as engine  # noqa: E402


SNAPSHOTS = [
    PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-consumer-universe-2026-08-12.json",
    PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-culture-education-universe-2026-08-12.json",
]
REPORT = PROJECT_ROOT / "tests" / "module-2-full-consumer-coverage-acceptance.v1.json"


def main() -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    with TemporaryDirectory(prefix="consumer-coverage-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        db = root / "coverage.db"
        output = root / "output"
        initialized = engine.init_coverage(db)
        check("eleven_research_sector_packs", initialized["sector_packs"] == 11, initialized)
        check("all_73_leaf_nodes_in_taxonomy", initialized["leaf_nodes"] == 73, initialized)
        check("thirty_five_common_metrics", initialized["common_metrics"] == 35, initialized)
        check("sixty_four_sector_metrics", initialized["sector_metrics"] == 64, initialized)
        check("ninety_nine_metric_definitions", initialized["metric_definitions"] == 99, initialized)
        check("ten_research_templates", initialized["research_templates"] == 10, initialized)
        check("vendor_mapping_rules_loaded", initialized["vendor_mapping_rules"] == 16, initialized)

        coverage_spec = engine.spec()
        common_metrics, sector_metrics = engine.metric_contract()
        names, parents, leaves = engine.domain_index()
        sectors = list(coverage_spec["sector_research_contracts"])
        check("all_sector_packs_have_dedicated_metrics", set(sector_metrics) == set(sectors) and all(sector_metrics.values()), sector_metrics)
        check("all_sector_packs_have_research_theses", all(coverage_spec["sector_research_contracts"][code]["thesis"] for code in sectors), sectors)
        check("all_sector_packs_have_cycle_drivers", all(coverage_spec["sector_research_contracts"][code]["cycle_drivers"] for code in sectors), sectors)
        check("all_sector_packs_have_value_chains", all(coverage_spec["sector_research_contracts"][code]["value_chain"] for code in sectors), sectors)
        check("all_leaf_nodes_reachable_from_level_two_packs", all(any(leaf.startswith(code + ".") for code in sectors) for leaf in leaves), leaves)
        check("all_ten_required_streams_declared", len(coverage_spec["required_streams"]) == 10, coverage_spec["required_streams"])
        check("no_fund_holdings_quality_gate", "no_fund_holdings_or_position_inference" in coverage_spec["quality_gates"], coverage_spec["quality_gates"])

        universe = engine.build_universe(db, SNAPSHOTS, "2026-08-12", output)
        check("two_real_source_snapshots_used", len(universe["source_snapshot_ids"]) == 2, universe["source_snapshot_ids"])
        check("source_rows_total_1297", universe["source_record_count"] == 1297, universe["source_record_count"])
        check("deduplicated_security_count_1275", universe["unique_security_count"] == 1275, universe["unique_security_count"])
        check("mapped_security_count_1169", universe["mapped_security_count"] == 1169, universe["mapped_security_count"])
        check("review_required_count_106", universe["review_required_count"] == 106, universe["review_required_count"])
        check("unmapped_breakdown_is_explicit", sum(universe["unmapped_breakdown"].values()) == 106, universe["unmapped_breakdown"])
        check("many_to_many_mapping_active", universe["multi_mapped_security_count"] == 46 and universe["membership_count"] > universe["unique_security_count"], universe)
        check("all_11_sectors_have_a_share_members", set(universe["sector_security_counts"]) == set(sectors) and min(universe["sector_security_counts"].values()) > 0, universe["sector_security_counts"])
        check("pet_sector_has_specific_coverage", universe["sector_security_counts"]["CR.S.PT"] == 9, universe["sector_security_counts"])
        check("culture_education_sector_expanded", universe["sector_security_counts"]["CR.V.CE"] == 168, universe["sector_security_counts"])
        check("universe_remains_at_license_gate", universe["status"] == "staged_license_gate", universe["status"])
        check("universe_artifact_written", Path(universe["artifact_path"]).is_file(), universe["artifact_path"])

        matrix = engine.build_coverage_matrix(db, universe["universe_snapshot_id"], "2026-08-12", output)
        cells = matrix["matrix"]
        a_cells = [item for item in cells if item["market"] == "A_SHARE"]
        hk_cells = [item for item in cells if item["market"] == "HK_SHARE"]
        check("coverage_matrix_has_22_cells", len(cells) == 22, len(cells))
        check("a_share_has_11_sector_cells", len(a_cells) == 11 and all(item["security_count"] > 0 for item in a_cells), a_cells)
        check("hk_share_gap_has_11_explicit_cells", len(hk_cells) == 11 and all(item["security_count"] == 0 for item in hk_cells), hk_cells)
        check("all_research_packs_ready", all(item["research_pack_status"] == "ready" for item in cells), cells)
        check("definitions_never_count_as_population", all(item["metric_population_status"] == "definitions_only" and item["populated_stream_count"] == 0 for item in cells), cells)
        check("all_missing_streams_explicit", all(len(item["missing_streams"]) == 10 for item in cells), cells)
        check("a_share_license_blocker_explicit", all(any(blocker["code"] == "source_license_pending" for blocker in item["blockers"]) for item in a_cells), a_cells)
        check("hk_universe_blocker_explicit", all(any(blocker["code"] == "security_universe_not_populated" for blocker in item["blockers"]) for item in hk_cells), hk_cells)
        check("coverage_matrix_artifact_written", Path(matrix["artifact_path"]).is_file(), matrix["artifact_path"])

        cutoff = "2026-08-12T23:59:59+08:00"
        tasks = engine.generate_task_packages(db, cutoff, output)
        check("generated_110_research_tasks", tasks["task_package_count"] == 110, tasks["task_package_count"])
        check("every_sector_has_ten_tasks", all(sum(p["sector_code"] == code for p in tasks["packages"]) == 10 for code in sectors), sectors)
        check("every_template_has_eleven_tasks", all(sum(p["template_id"] == template for p in tasks["packages"]) == 11 for template in coverage_spec["required_templates"]), coverage_spec["required_templates"])
        check("all_tasks_use_exact_cutoff", all(p["cutoff_timestamp"] == cutoff for p in tasks["packages"]), cutoff)
        check("all_tasks_route_ten_streams", all(len(p["source_routes"]) == 10 and all(route["registered_source_routes"] > 0 for route in p["source_routes"]) for p in tasks["packages"]), tasks["packages"][0]["source_routes"])
        check("all_tasks_have_common_and_sector_metrics", all(set(common_metrics + sector_metrics[p["sector_code"]]) == set(p["metric_queries"]) for p in tasks["packages"]), len(common_metrics))
        check("tasks_truthfully_ready_with_gaps", all(p["status"] == "ready_with_data_gaps" for p in tasks["packages"]), tasks["packages"][0]["status"])
        check("task_payload_has_no_forbidden_fund_fields", all(not engine.production.scan_forbidden(p) for p in tasks["packages"]), "clean")
        check("task_artifact_written", Path(tasks["artifact_path"]).is_file(), tasks["artifact_path"])
        second_tasks = engine.generate_task_packages(db, cutoff, output)
        check("task_generation_idempotent", [p["task_package_id"] for p in second_tasks["packages"]] == [p["task_package_id"] for p in tasks["packages"]], second_tasks["task_package_count"])

        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            table_names = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            task_count = connection.execute("SELECT COUNT(*) AS count FROM research_task_packages").fetchone()["count"]
            review_rows = connection.execute("SELECT COUNT(DISTINCT security_id) AS count FROM research_universe_members WHERE mapping_status='review_required' AND sector_code IS NULL").fetchone()["count"]
        expected_tables = {"research_sector_packs", "taxonomy_vendor_mappings", "research_universe_snapshots", "research_universe_members", "research_coverage_status", "research_task_packages"}
        check("coverage_audit_schema_complete", expected_tables <= table_names, sorted(expected_tables))
        check("database_keeps_110_idempotent_tasks", task_count == 110, task_count)
        check("database_keeps_106_explicit_review_rows", review_rows == 106, review_rows)

    passed = sum(1 for item in checks if item["passed"])
    result = {
        "module": 2, "name": "全消费行业研究覆盖扩展",
        "status": "passed" if passed == len(checks) else "failed",
        "passed": passed, "total": len(checks), "checks": checks,
    }
    engine.write_json(REPORT, result)
    print(json.dumps({"status": result["status"], "passed": passed, "total": len(checks)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
