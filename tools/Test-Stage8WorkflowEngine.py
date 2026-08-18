#!/usr/bin/env python3
"""End-to-end acceptance tests for Stage 8 workflow orchestration."""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PROJECT_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))

import consumer_knowledge_store as knowledge  # noqa: E402
import consumer_workflow_engine as engine  # noqa: E402


SEED_PATH = PROJECT_ROOT / "data" / "seed" / "stage6-consumer-core-seed.v1.json"
REQUEST_PATH = PROJECT_ROOT / "tests" / "fixtures" / "stage8-research-workflow-request.v1.json"
REPORT_PATH = PROJECT_ROOT / "tests" / "stage-8-acceptance-report.v1.json"


def main() -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    # sqlite/FTS can briefly retain a Windows file handle after worker threads
    # exit; ignore deferred temp cleanup errors because the OS reclaims it.
    with TemporaryDirectory(prefix="consumer-stage8-", ignore_cleanup_errors=True) as temp:
        temp_root = Path(temp)
        db_path = temp_root / "stage8.db"
        output_root = temp_root / "outputs"
        initialized = engine.init_engine(db_path)
        seed = knowledge.ingest_package(db_path, SEED_PATH)
        check("workflow_engine_initialized", initialized["status"] == "ready", initialized)
        check("nine_agent_roles_registered", initialized["roles"] == 9, initialized)
        check("ten_orchestrated_tasks_registered", initialized["tasks"] == 10, initialized)
        check("stage6_seed_ingested", seed["status"] == "success", seed)

        spec = engine.workflow_spec()
        role_ids = {item["role_id"] for item in spec["roles"]}
        required_roles = {
            "research_director", "entity_scope_agent", "evidence_retrieval_agent", "model_agent",
            "claim_agent", "evidence_auditor", "skeptic_agent", "report_agent",
            "compliance_agent",
        }
        check("all_role_contracts_present", required_roles <= role_ids, sorted(role_ids))
        check("portfolio_data_explicitly_forbidden", spec["boundaries"]["portfolio_data"] == "forbidden", spec["boundaries"])
        check("position_inference_explicitly_forbidden", spec["boundaries"]["portfolio_position_inference"] == "forbidden", spec["boundaries"])
        check("automatic_trade_instruction_forbidden", spec["boundaries"]["automatic_trade_instruction"] == "forbidden", spec["boundaries"])
        plan_tasks = engine.topological_plan(spec)
        waves = {item["task_id"]: item["wave_no"] for item in plan_tasks}
        graph_order_valid = all(
            waves[dependency] < waves[item["task_id"]]
            for item in plan_tasks for dependency in item["depends_on"]
        )
        check("dag_is_acyclic_and_topological", graph_order_valid, waves)
        check(
            "quality_tasks_run_concurrently",
            waves["evidence_audit"] == waves["skeptic_review"],
            {key: waves[key] for key in ("evidence_audit", "skeptic_review")},
        )

        request = engine.read_json(REQUEST_PATH)
        validation = engine.validate_request(db_path, request)
        check("canonical_request_valid", validation == [], validation)
        plan = engine.plan_request(db_path, request)
        check("execution_plan_has_eight_waves", len(plan["execution_waves"]) == 8, plan["execution_waves"])
        check("human_review_not_planned", plan["human_review_required"] is False, plan["human_review_required"])
        check("automatic_internal_release_planned", plan["release_mode"] == "automatic_internal_release_after_required_quality_gates", plan["release_mode"])

        no_timezone = copy.deepcopy(request)
        no_timezone["cutoff_timestamp"] = "2024-05-01T00:00:00"
        no_timezone_issues = engine.validate_request(db_path, no_timezone)
        check(
            "cutoff_without_timezone_blocked",
            any(item["code"] == "cutoff_timezone_missing" for item in no_timezone_issues),
            no_timezone_issues,
        )
        holdings = copy.deepcopy(request)
        holdings["fund_holdings"] = [{"security_id": "000333.SZ", "weight": 0.01}]
        holdings_issues = engine.validate_request(db_path, holdings)
        check(
            "fund_holdings_request_blocked",
            any(item["code"] == "portfolio_or_trade_field_forbidden" for item in holdings_issues),
            holdings_issues,
        )
        inference = copy.deepcopy(request)
        inference["scope"]["position_inference"] = True
        inference_issues = engine.validate_request(db_path, inference)
        check(
            "position_inference_request_blocked",
            any(item["code"] == "portfolio_or_trade_field_forbidden" for item in inference_issues),
            inference_issues,
        )
        traversal = copy.deepcopy(request)
        traversal["model_packages"] = [{"path": "../../outside.json", "required": False}]
        traversal_issues = engine.validate_request(db_path, traversal)
        check(
            "model_path_traversal_blocked",
            any(item["code"] == "model_path_forbidden" for item in traversal_issues),
            traversal_issues,
        )

        interrupted = engine.run_workflow(db_path, REQUEST_PATH, output_root, stop_after="fact_retrieval")
        run_id = interrupted["run"]["run_id"]
        interrupted_statuses = {item["task_id"]: item["status"] for item in interrupted["tasks"]}
        check("controlled_interruption_keeps_run_resumable", interrupted["run"]["status"] == "running", interrupted["run"])
        check("interruption_completed_whole_parallel_wave", interrupted_statuses["document_retrieval"] == "completed", interrupted_statuses)
        check("downstream_tasks_not_started_before_resume", interrupted_statuses["claim_composition"] == "pending", interrupted_statuses)

        resumed = engine.resume_workflow(db_path, run_id)
        statuses = {item["task_id"]: item["status"] for item in resumed["tasks"]}
        check("resume_completes_and_releases_directly", resumed["run"]["status"] == "completed", resumed["run"])
        check("resume_counter_incremented", resumed["run"]["resumed_count"] == 1, resumed["run"])
        check("all_required_tasks_complete", all(value in {"completed", "degraded"} for value in statuses.values()), statuses)
        check("human_review_task_absent", "human_review_gate" not in statuses, statuses)

        output_dir = Path(resumed["run"]["output_directory"])
        artifact_paths = {
            item["artifact_type"]: Path(item["path"]) for item in resumed["artifacts"]
        }
        expected_artifacts = {"execution_plan", "claim_graph", "evidence_audit", "research_report", "runtime_audit"}
        check("five_required_artifacts_registered", expected_artifacts <= set(artifact_paths), sorted(artifact_paths))
        check(
            "all_registered_artifacts_exist",
            all(path.is_file() for path in artifact_paths.values()),
            {key: str(value) for key, value in artifact_paths.items()},
        )

        claim_graph = engine.read_json(artifact_paths["claim_graph"])
        summary = claim_graph["entity_summary"]
        check("both_comparison_entities_complete", all(item["status"] == "complete" for item in summary.values()), summary)
        check("nineteen_claims_composed", claim_graph["claim_count"] == 19, claim_graph["claim_count"])
        check(
            "fact_claims_have_direct_evidence",
            all(item["evidence_relations"] for item in claim_graph["claims"] if item["content_label"] == "FACT_PRIMARY"),
            [item["claim_id"] for item in claim_graph["claims"] if item["content_label"] == "FACT_PRIMARY"],
        )
        check(
            "calculation_claims_have_formula_and_inputs",
            all(item.get("formula") and len(item.get("input_ids", [])) >= 2 for item in claim_graph["claims"] if item["content_label"] == "AGENT_CALCULATION"),
            [item for item in claim_graph["claims"] if item["content_label"] == "AGENT_CALCULATION"],
        )
        material = [item for item in claim_graph["claims"] if item["importance"] == "material"]
        check(
            "material_conclusion_has_counter_evidence",
            bool(material) and all(any(rel["relation_type"] == "counter" for rel in item["evidence_relations"]) for item in material),
            material,
        )
        gree = summary["cr:legal_entity:gree"]
        check("timezone_safe_point_in_time_query", abs(gree["revenue_2023"] - 203_979_266_387.09) < 0.01, gree)

        audit = engine.read_json(artifact_paths["evidence_audit"])
        check("evidence_audit_passed", audit["status"] == "passed", audit)
        check("no_future_information_detected", audit["future_information_count"] == 0, audit)
        check("calculation_lineage_audit_passed", audit["calculation_lineage"] == "passed", audit)
        check("counter_analysis_audit_passed", audit["material_counter_analysis"] == "passed", audit)

        report_text = artifact_paths["research_report"].read_text(encoding="utf-8")
        required_sections = [
            "## 研究问题", "## 执行摘要", "## 比较口径与证据边界", "## 核心指标同表比较",
            "## 分公司观察", "## 反方审查与不可下结论事项", "## 有限结论与后续跟踪",
            "## 证据索引", "## 运行与审计附录",
        ]
        check("fund_manager_report_structure_complete", all(section in report_text for section in required_sections), required_sections)
        check("same_row_company_comparison_present", "| 美的集团 |" in report_text and "| 格力电器 |" in report_text, report_text[:1500])
        check("evidence_index_has_source_urls", "https://static.cninfo.com.cn/" in report_text, report_text[-2000:])
        check("no_single_winner_without_rule", "不给出单一优胜者" in report_text, report_text)
        check("no_automatic_buy_or_sell_instruction", "建议买入" not in report_text and "建议卖出" not in report_text, report_text)
        check("portfolio_boundary_disclosed", "不使用、不请求且不推断任何基金持仓或仓位" in report_text, report_text[:500])
        check("report_is_immediately_internal_ready", "内部研究可用（系统质量门已通过）" in report_text, report_text[:500])
        check("report_contains_no_human_review_wording", "人工复核" not in report_text, report_text[:500])

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            task_attempts = {
                row["task_id"]: row["attempt_count"] for row in connection.execute(
                    "SELECT task_id,attempt_count FROM workflow_tasks WHERE run_id=?", (run_id,)
                ).fetchall()
            }
            orphan_links = connection.execute(
                """SELECT COUNT(*) AS count FROM workflow_claim_evidence ce
                   LEFT JOIN evidence e ON e.evidence_id=ce.evidence_id
                   WHERE ce.run_id=? AND e.evidence_id IS NULL""",
                (run_id,),
            ).fetchone()["count"]
        check("completed_tasks_not_repeated_on_resume", task_attempts["scope_guard"] == 1 and task_attempts["fact_retrieval"] == 1, task_attempts)
        check("claim_evidence_foreign_keys_intact", orphan_links == 0, orphan_links)

        idempotent = engine.run_workflow(db_path, REQUEST_PATH, output_root)
        check("identical_package_is_idempotent", idempotent["run"]["run_id"] == run_id and idempotent["run"]["status"] == "completed", idempotent["run"])

        conflict = copy.deepcopy(request)
        conflict["research_question"] += " 冲突版本"
        conflict_path = temp_root / "conflict.json"
        engine.write_json(conflict_path, conflict)
        conflict_blocked = False
        try:
            engine.run_workflow(db_path, conflict_path, output_root)
        except engine.WorkflowValidationError as exc:
            conflict_blocked = any(item["code"] == "package_id_content_conflict" for item in exc.issues)
        check("package_id_hash_conflict_blocked", conflict_blocked, conflict_blocked)

        optional = copy.deepcopy(request)
        optional["package_id"] = "CR.WORKFLOW.PKG.STAGE8.OPTIONAL.MODEL.001"
        optional["output_slug"] = "optional-model-degrade"
        optional["model_packages"] = [{"path": "data/models/stage7/not-present.v1.json", "required": False}]
        optional_path = temp_root / "optional.json"
        engine.write_json(optional_path, optional)
        optional_result = engine.run_workflow(db_path, optional_path, output_root)
        optional_statuses = {item["task_id"]: item["status"] for item in optional_result["tasks"]}
        check("optional_model_failure_degrades_not_blocks", optional_statuses["model_execution"] == "degraded", optional_statuses)
        check("degraded_optional_lane_still_releases", optional_result["run"]["status"] == "completed", optional_result["run"])
        check("publication_status_is_internal_ready", optional_result["run"]["publication_status"] == "internal_research_ready", optional_result["run"])

        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'workflow_%'"
                ).fetchall()
            }
            review_count = connection.execute(
                "SELECT COUNT(*) FROM workflow_reviews WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            direct_release_events = connection.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE run_id=? AND event_type='internal_research_released'", (run_id,)
            ).fetchone()[0]
        expected_tables = {
            "workflow_definitions", "workflow_packages", "workflow_runs", "workflow_tasks",
            "workflow_claims", "workflow_claim_evidence", "workflow_artifacts", "workflow_reviews",
            "workflow_events",
        }
        check("workflow_audit_schema_complete", expected_tables <= tables, sorted(tables))
        check("no_human_review_records_created", review_count == 0, review_count)
        check("direct_internal_release_audited", direct_release_events == 1, direct_release_events)

    passed = sum(1 for item in checks if item["passed"])
    result = {
        "stage": 8,
        "name": "研究工作流与Agent编排",
        "status": "passed" if passed == len(checks) else "failed",
        "passed": passed,
        "total": len(checks),
        "checks": checks,
    }
    engine.write_json(REPORT_PATH, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
