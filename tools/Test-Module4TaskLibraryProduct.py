#!/usr/bin/env python3
"""Acceptance suite for module 4: product-grade research task library."""

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

import consumer_task_library as engine  # noqa: E402
import full_consumer_coverage as coverage  # noqa: E402


SNAPSHOTS = [
    PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-consumer-universe-2026-08-12.json",
    PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-culture-education-universe-2026-08-12.json",
]
REPORT = PROJECT_ROOT / "tests" / "module-4-task-library-product-acceptance.v1.json"
SEED = PROJECT_ROOT / "data" / "seed" / "stage6-consumer-core-seed.v1.json"
CUTOFF = "2026-08-12T23:59:59+08:00"


def main() -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    with TemporaryDirectory(prefix="consumer-task-library-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        db = root / "library.db"
        output = root / "output"
        coverage.init_coverage(db)
        engine.knowledge.ingest_package(db, SEED)
        universe = coverage.build_universe(db, SNAPSHOTS, "2026-08-12", root / "coverage")
        coverage.build_coverage_matrix(db, universe["universe_snapshot_id"], "2026-08-12", root / "coverage")
        coverage.generate_task_packages(db, CUTOFF, root / "coverage")
        initialized = engine.init_library(db, output)

        check("ten_product_templates", initialized["templates"] == 10, initialized)
        check("one_hundred_ten_sector_products", initialized["sector_products"] == 110, initialized)
        check("four_internal_roles", initialized["roles"] == 4, initialized)
        check("five_saved_views", initialized["saved_views"] == 5, initialized)
        check("catalog_internal_only", initialized["visibility"] == "internal", initialized)
        check("catalog_json_written", Path(initialized["catalog"]["json_path"]).is_file(), initialized["catalog"])
        check("catalog_markdown_written", Path(initialized["catalog"]["markdown_path"]).is_file(), initialized["catalog"])
        spec = engine.task_library_spec()
        check("all_templates_have_output_contracts", all(item["output_sections"] for item in spec["templates"]), spec["templates"])
        check("all_templates_have_parameter_fields", all(item["parameter_fields"] for item in spec["templates"]), spec["templates"])
        check("human_release_policy", spec["release_policy"]["new_or_changed_template"] == "named_human_research_reviewer_required", spec["release_policy"])
        check("research_output_direct_release", spec["release_policy"]["research_output"] == "stage8_automatic_internal_release_after_quality_gates", spec["release_policy"])
        check("external_publication_disabled", spec["release_policy"]["external_publication"] is False, spec["release_policy"])
        check("portfolio_boundary_quality_gate", "no_fund_holdings_or_position_inference" in spec["quality_gates"], spec["quality_gates"])

        all_results = engine.search_library(db, role="public_fund_manager", user_id="基金经理甲")
        check("search_all_returns_110", all_results["result_count"] == 110, all_results["result_count"])
        check("search_facets_cover_11_sectors", len(all_results["facets"]["sectors"]) == 11, all_results["facets"])
        check("search_facets_cover_10_templates", len(all_results["facets"]["templates"]) == 10, all_results["facets"])
        search = engine.search_library(db, "景气", category="monitoring", role="public_fund_manager", user_id="基金经理甲")
        check("keyword_and_category_search_works", search["result_count"] == 11 and all(item["template_id"] == "P0_02_PROSPERITY_TRACKING" for item in search["results"]), search["result_count"])
        food = engine.search_library(db, sector_code="CR.S.FB", role="public_fund_manager", user_id="基金经理甲")
        check("sector_filter_returns_ten", food["result_count"] == 10, food["result_count"])
        compare = engine.search_library(db, sector_code="CR.D.AP", template_id="P0_04_COMPANY_COMPARISON", role="public_fund_manager", user_id="基金经理甲")
        check("exact_product_filter_returns_one", compare["result_count"] == 1, compare)
        product_id = compare["results"][0]["product_id"]
        detail = engine.product_detail(db, product_id, "public_fund_manager", "基金经理甲")
        check("product_detail_has_metrics", len(detail["product"]["metric_ids"]) > 35, len(detail["product"]["metric_ids"]))
        check("product_detail_routes_ten_streams", len(detail["product"]["source_routes"]) == 10, detail["product"]["source_routes"])
        check("product_detail_has_version", len(detail["versions"]) == 1 and detail["versions"][0]["release_status"] == "internal_active", detail["versions"])
        check("data_readiness_is_explicit", detail["product"]["data_readiness"] == "ready_with_data_gaps", detail["product"]["data_readiness"])
        favorite = engine.favorite_product(db, product_id, "基金经理甲", "public_fund_manager")
        check("fund_manager_can_favorite", favorite["favorite"] is True, favorite)
        favorite_search = engine.search_library(db, sector_code="CR.D.AP", template_id="P0_04_COMPANY_COMPARISON", role="public_fund_manager", user_id="基金经理甲")
        check("favorite_visible_in_search", favorite_search["results"][0]["is_favorite"] == 1, favorite_search["results"][0])
        denied = False
        try:
            engine.search_library(db, role="unknown_role", user_id="x")
        except engine.TaskLibraryValidationError as exc:
            denied = any(item["code"] == "permission_denied" for item in exc.issues)
        check("unknown_role_search_denied", denied, denied)

        parameters = {
            "cutoff_timestamp": "2024-05-01T00:00:00+08:00",
            "research_question": "比较美的集团和格力电器2023年度经营与财务质量，给出有限结论和验证点。",
            "entities": [
                {"entity_id": "cr:legal_entity:midea", "security_id": "000333.SZ", "display_name": "美的集团"},
                {"entity_id": "cr:legal_entity:gree", "security_id": "000651.SZ", "display_name": "格力电器"}
            ],
            "metric_queries": [
                {"metric_id": "CR.CO.REVENUE", "period_end": "2022-12-31"},
                {"metric_id": "CR.CO.REVENUE", "period_end": "2023-12-31"},
                {"metric_id": "CR.CO.OPERATING_COST", "period_end": "2023-12-31"},
                {"metric_id": "CR.CO.PARENT_NET_PROFIT", "period_end": "2023-12-31"},
                {"metric_id": "CR.CO.CFO_NET", "period_end": "2023-12-31"}
            ],
            "decision_rule": None, "priority": "normal"
        }
        params_path = root / "parameters.json"
        engine.write_json(params_path, parameters)
        submitted = engine.submit_job(db, product_id, params_path, "基金经理甲", "public_fund_manager", output)
        check("fund_manager_can_submit_job", submitted["job"]["status"] == "queued", submitted["job"])
        check("stage8_workflow_request_compiled", submitted["workflow_request"]["workflow_id"] == engine.workflow.WORKFLOW_ID, submitted["workflow_request"])
        check("compiled_request_keeps_exact_cutoff", submitted["workflow_request"]["cutoff_timestamp"] == parameters["cutoff_timestamp"], submitted["workflow_request"])
        check("compiled_request_has_no_fund_fields", not engine.production.scan_forbidden(submitted["workflow_request"]), "clean")
        replay = engine.submit_job(db, product_id, params_path, "基金经理甲", "public_fund_manager", output)
        check("job_submission_idempotent", replay["idempotent_replay"] is True and replay["job"]["job_id"] == submitted["job"]["job_id"], replay["job"])
        agent_blocked = False
        try:
            engine.submit_job(db, product_id, params_path, "AI", "public_fund_manager", output)
        except engine.TaskLibraryValidationError as exc:
            agent_blocked = any(item["code"] == "named_submitter_required" for item in exc.issues)
        check("agent_cannot_be_named_submitter", agent_blocked, agent_blocked)
        one_entity = copy.deepcopy(parameters)
        one_entity["entities"] = one_entity["entities"][:1]
        problems = engine.validate_parameters(db, detail["product"], one_entity)
        check("company_comparison_requires_two_entities", any(item["code"] == "entity_count_invalid" for item in problems), problems)
        forbidden = copy.deepcopy(parameters)
        forbidden["fund_holdings"] = []
        problems = engine.validate_parameters(db, detail["product"], forbidden)
        check("fund_holdings_parameter_blocked", any(item["code"] == "portfolio_field_forbidden" for item in problems), problems)

        unauthorized_run = False
        try:
            engine.run_job(db, submitted["job"]["job_id"], "基金经理甲", "public_fund_manager", output)
        except engine.TaskLibraryValidationError as exc:
            unauthorized_run = any(item["code"] == "permission_denied" for item in exc.issues)
        check("fund_manager_cannot_manage_queue", unauthorized_run, unauthorized_run)
        executed = engine.run_job(db, submitted["job"]["job_id"], "研究运营员", "research_operator", output)
        check("operator_can_run_job", executed["status"] == "completed", executed["status"])
        check("job_releases_directly_after_quality_gates", executed["workflow"]["run"]["publication_status"] == "internal_research_ready", executed["workflow"]["run"])
        check("job_has_research_report", any(item["artifact_type"] == "research_report" for item in executed["workflow"]["artifacts"]), executed["workflow"]["artifacts"])

        patch_path = root / "product-patch.json"
        engine.write_json(patch_path, {"short_description": "更新后的内部比较研究说明，仅用于验收。", "tags": detail["product"]["tags"] + ["验收更新"]})
        manager_denied = False
        try:
            engine.propose_product_update(db, product_id, patch_path, "1.1.0", "基金经理甲", "public_fund_manager")
        except engine.TaskLibraryValidationError as exc:
            manager_denied = any(item["code"] == "permission_denied" for item in exc.issues)
        check("fund_manager_cannot_manage_catalog", manager_denied, manager_denied)
        proposed = engine.propose_product_update(db, product_id, patch_path, "1.1.0", "研究运营员", "research_operator")
        check("operator_update_is_draft", proposed["release_status"] == "draft", proposed)
        ai_review_blocked = False
        try:
            engine.approve_product_version(db, proposed["product_version_id"], "AI", "research_reviewer")
        except engine.TaskLibraryValidationError as exc:
            ai_review_blocked = any(item["code"] == "named_human_reviewer_required" for item in exc.issues)
        check("agent_cannot_approve_product_version", ai_review_blocked, ai_review_blocked)
        approved = engine.approve_product_version(db, proposed["product_version_id"], "研究复核员乙", "research_reviewer", "验收批准")
        check("named_reviewer_can_release_internal_version", approved["release_status"] == "internal_active", approved)
        updated_detail = engine.product_detail(db, product_id, "public_fund_manager", "基金经理甲")
        check("approved_version_visible", updated_detail["product"]["version"] == "1.1.0" and len(updated_detail["versions"]) == 2, updated_detail["versions"])

        jobs = engine.list_jobs(db, "基金经理甲")
        check("own_job_list_available", jobs["count"] == 1 and jobs["jobs"][0]["status"] == "completed", jobs)
        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"] for table in ("task_library_templates", "task_library_products", "task_library_product_versions", "task_library_saved_views", "task_library_jobs", "task_library_job_events", "task_library_reviews", "task_library_usage_events")}
            fts_count = connection.execute("SELECT COUNT(*) AS count FROM task_library_products_fts").fetchone()["count"]
            external_visibility = connection.execute("SELECT COUNT(*) AS count FROM task_library_products WHERE visibility<>'internal'").fetchone()["count"]
        expected_tables = {"task_library_templates", "task_library_products", "task_library_product_versions", "task_library_role_permissions", "task_library_saved_views", "task_library_favorites", "task_library_jobs", "task_library_job_events", "task_library_reviews", "task_library_usage_events"}
        check("task_library_audit_schema_complete", expected_tables <= tables, sorted(expected_tables))
        check("database_counts_correct", counts["task_library_templates"] == 10 and counts["task_library_products"] == 110 and counts["task_library_product_versions"] == 111 and counts["task_library_saved_views"] == 5 and counts["task_library_jobs"] == 1, counts)
        check("fts_catalog_has_110_products", fts_count == 110, fts_count)
        check("no_external_product_visibility", external_visibility == 0, external_visibility)
        check("job_events_audited", counts["task_library_job_events"] >= 3, counts)
        check("product_release_review_audited", counts["task_library_reviews"] == 1, counts)
        check("usage_events_audited", counts["task_library_usage_events"] >= 10, counts)

    passed = sum(1 for item in checks if item["passed"])
    result = {"module": 4, "name": "研究任务库的全面产品化", "status": "passed" if passed == len(checks) else "failed", "passed": passed, "total": len(checks), "checks": checks}
    engine.write_json(REPORT, result)
    print(json.dumps({"status": result["status"], "passed": passed, "total": len(checks)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
