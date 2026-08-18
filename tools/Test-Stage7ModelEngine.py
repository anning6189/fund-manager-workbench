#!/usr/bin/env python3
"""End-to-end acceptance tests for Stage 7 research models."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_PATH = PROJECT_ROOT / "tools" / "consumer_knowledge_store.py"
ENGINE_PATH = PROJECT_ROOT / "tools" / "consumer_model_engine.py"
SEED_PATH = PROJECT_ROOT / "data" / "seed" / "stage6-consumer-core-seed.v1.json"
MODEL_ROOT = PROJECT_ROOT / "data" / "models" / "stage7"
REPORT_PATH = PROJECT_ROOT / "tests" / "stage-7-acceptance-report.v1.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def close_enough(left: float, right: float, relative: float = 1e-10, absolute: float = 1e-8) -> bool:
    return math.isclose(float(left), float(right), rel_tol=relative, abs_tol=absolute)


def output_role(result: dict, role: str) -> float:
    row = next(item for item in result["outputs"] if item["output_role"] == role)
    return float(row["value_numeric"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "tests" / "tmp-stage7-acceptance.db")
    args = parser.parse_args()
    db_path = args.db
    for path in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if path.exists():
            path.unlink()

    knowledge = load_module("consumer_knowledge_store", KNOWLEDGE_PATH)
    engine = load_module("consumer_model_engine", ENGINE_PATH)
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    initialized = engine.init_engine(db_path)
    check("five_model_definitions_loaded", initialized["models_loaded"] == 5, initialized)
    seed = knowledge.ingest_package(db_path, SEED_PATH)
    check("stage6_seed_available", seed["status"] == "success", seed)

    package_paths = sorted(
        path for path in MODEL_ROOT.glob("*.json")
        if path.name != "stage7-model-suite.manifest.v1.json"
    )
    packages = {path.name: engine.read_json(path) for path in package_paths}
    validation = {name: engine.validate_package(db_path, package) for name, package in packages.items()}
    check("five_model_packages_present", len(packages) == 5, sorted(packages))
    check("all_model_packages_static_valid", all(not issues for issues in validation.values()), validation)

    results = {path.name: engine.run_package(db_path, path) for path in package_paths}
    check("all_five_model_types_execute", all(result["status"] == "completed" for result in results.values()), {key: value["status"] for key, value in results.items()})
    check("demonstration_results_not_publishable", all(result["publication_status"] == "demonstration_only" for result in results.values()), {key: value["publication_status"] for key, value in results.items()})

    market = results["market-size-dual.v1.json"]
    market_runs = {item["scenario_id"]: item for item in market["runs"]}
    base_market = market_runs["base"]
    check("market_size_dual_path_calculation", close_enough(output_role(base_market, "market_size_top_down"), 52_800_000_000) and close_enough(output_role(base_market, "market_size_bottom_up"), 51_000_000_000), base_market["outputs"])
    check("market_size_cross_method_reconciliation", close_enough(output_role(base_market, "market_size_midpoint"), 51_900_000_000) and 0 <= output_role(base_market, "cross_check_gap") < 0.05, base_market["outputs"])
    market_order = [output_role(market_runs[key], "market_size_midpoint") for key in ("bear", "base", "bull")]
    check("market_size_bear_base_bull_order", market_order[0] <= market_order[1] <= market_order[2], market_order)
    check("market_size_sensitivity_3x3", len(base_market["sensitivity_results"]) == 9, base_market["sensitivity_results"])

    competition = results["competition-concentration.v1.json"]["runs"][0]
    check("competition_cr3_cr5", close_enough(output_role(competition, "cr3"), 0.55) and close_enough(output_role(competition, "cr5"), 0.70), competition["outputs"])
    check("competition_market_reconciles", close_enough(output_role(competition, "cr5") + output_role(competition, "long_tail_share"), 1), competition["outputs"])

    bridge = results["company-operating-bridge.v1.json"]["runs"][0]
    expected_growth = 372_037_280_000 / 343_917_531_000 - 1
    check("operating_bridge_uses_stage6_actuals", close_enough(output_role(bridge, "actual_revenue_growth"), expected_growth), bridge["outputs"])
    check("operating_bridge_reconciles", abs(output_role(bridge, "bridge_residual")) < 1e-12, bridge["outputs"])

    forecast = results["financial-forecast.v1.json"]
    forecast_runs = {item["scenario_id"]: item for item in forecast["runs"]}
    base_forecast = forecast_runs["base"]
    expected_revenue = 372_037_280_000 * 1.08
    expected_profit = expected_revenue * (0.27 - 0.14) * (1 - 0.19)
    check("financial_forecast_formula_accuracy", close_enough(output_role(base_forecast, "forecast_revenue"), expected_revenue) and close_enough(output_role(base_forecast, "forecast_net_profit"), expected_profit), base_forecast["outputs"])
    forecast_order = [output_role(forecast_runs[key], "forecast_net_profit") for key in ("bear", "base", "bull")]
    check("financial_forecast_bear_base_bull_order", forecast_order[0] <= forecast_order[1] <= forecast_order[2], forecast_order)
    check("financial_cash_flow_reconciliation", output_role(base_forecast, "forecast_fcf") < output_role(base_forecast, "forecast_cfo"), base_forecast["outputs"])
    check("financial_sensitivity_3x3", len(base_forecast["sensitivity_results"]) == 9, base_forecast["sensitivity_results"])
    check("historical_financial_ratios_calculated", close_enough(output_role(base_forecast, "historical_gross_margin"), 1 - 273_481_373_000 / 372_037_280_000), base_forecast["outputs"])

    valuation = results["valuation-expectations.v1.json"]
    valuation_runs = {item["scenario_id"]: item for item in valuation["runs"]}
    base_valuation = valuation_runs["base"]
    expected_val_profit = 33_719_935_000 * 1.09
    check("valuation_forward_pe_accuracy", close_enough(output_role(base_valuation, "forward_pe"), 420_000_000_000 / expected_val_profit), base_valuation["outputs"])
    check("reverse_valuation_accuracy", close_enough(output_role(base_valuation, "reverse_implied_earnings"), 30_000_000_000), base_valuation["outputs"])
    check("valuation_expectation_gap_present", close_enough(output_role(base_valuation, "forecast_vs_consensus"), expected_val_profit / 37_000_000_000 - 1), base_valuation["outputs"])
    valuation_order = [output_role(valuation_runs[key], "implied_market_cap") for key in ("bear", "base", "bull")]
    check("valuation_bear_base_bull_order", valuation_order[0] <= valuation_order[1] <= valuation_order[2], valuation_order)
    check("valuation_sensitivity_3x3", len(base_valuation["sensitivity_results"]) == 9, base_valuation["sensitivity_results"])

    status_before = engine.engine_status(db_path)
    expected_counts = {"model_definitions": 5, "model_packages": 5, "model_runs": 11, "model_outputs": 76, "model_sensitivity_results": 27}
    check("model_audit_tables_populated", all(status_before["counts"][key] == value for key, value in expected_counts.items()), status_before["counts"])
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        fact_lineage = connection.execute(
            "SELECT COUNT(*) AS total,SUM(CASE WHEN observation_id IS NOT NULL AND evidence_id IS NOT NULL THEN 1 ELSE 0 END) AS traced FROM model_inputs WHERE input_kind='FACT'"
        ).fetchone()
        labels = {
            "fact": connection.execute("SELECT COUNT(*) FROM model_inputs WHERE input_kind='FACT' AND content_label<>'FACT_OBSERVATION'").fetchone()[0],
            "assumption": connection.execute("SELECT COUNT(*) FROM model_inputs WHERE input_kind='ASSUMPTION' AND content_label<>'SCENARIO_ASSUMPTION'").fetchone()[0],
            "output": connection.execute("SELECT COUNT(*) FROM model_outputs WHERE content_label<>'AGENT_CALCULATION'").fetchone()[0],
        }
        direct_delivery = connection.execute(
            """SELECT COUNT(*) FROM model_runs r JOIN model_packages p ON p.package_id=r.package_id
               JOIN model_definitions d ON d.model_id=p.model_id
               WHERE d.model_type IN ('financial_forecast','valuation_expectations')
                 AND r.human_review_required=0 AND r.publication_status='demonstration_only'"""
        ).fetchone()[0]
        fact_observations_before = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    check("fact_inputs_have_observation_and_evidence", fact_lineage["total"] == fact_lineage["traced"] and fact_lineage["total"] == 20, dict(fact_lineage))
    check("fact_assumption_calculation_labels_separated", all(value == 0 for value in labels.values()), labels)
    check("forecast_valuation_no_human_review_gate", direct_delivery == 6, direct_delivery)

    reproducible = True
    reproducibility_detail: dict[str, object] = {}
    for package_name, result in results.items():
        package = packages[package_name]
        for run in result["runs"]:
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                stored_inputs = {
                    row["input_id"]: {
                        "value": row["value_numeric"],
                    }
                    for row in connection.execute("SELECT input_id,value_numeric FROM model_inputs WHERE run_id=?", (run["run_id"],)).fetchall()
                }
            recalculated = engine.calculate_outputs(package["calculations"], stored_inputs)
            recalculated_values = {item["output_id"]: item["value"] for item in recalculated}
            stored_values = {item["output_id"]: item["value_numeric"] for item in run["outputs"]}
            run_ok = recalculated_values.keys() == stored_values.keys() and all(close_enough(recalculated_values[key], stored_values[key]) for key in stored_values)
            reproducibility_detail[run["run_id"]] = run_ok
            reproducible = reproducible and run_ok
    check("all_outputs_reproducible_from_stored_inputs", reproducible, reproducibility_detail)

    unsafe = copy.deepcopy(packages["competition-concentration.v1.json"])
    unsafe["calculations"][0]["formula"] = "__import__('os').system('echo unsafe')"
    unsafe_issues = engine.validate_package(db_path, unsafe)
    check("unsafe_formula_blocked", any(item["code"] == "unsafe_formula" for item in unsafe_issues), unsafe_issues)

    future_assumption = copy.deepcopy(packages["competition-concentration.v1.json"])
    future_assumption["scenarios"][0]["assumptions"][0]["available_at"] = "2024-05-02T00:00:00Z"
    future_issues = engine.validate_package(db_path, future_assumption)
    check("future_assumption_leakage_blocked", any(item["code"] == "future_assumption_leakage" for item in future_issues), future_issues)

    wrong_label = copy.deepcopy(packages["competition-concentration.v1.json"])
    wrong_label["scenarios"][0]["assumptions"][0]["content_label"] = "FACT_OBSERVATION"
    label_issues = engine.validate_package(db_path, wrong_label)
    check("assumption_cannot_masquerade_as_fact", any(item["code"] == "assumption_label_invalid" for item in label_issues), label_issues)

    scope_mismatch = copy.deepcopy(packages["competition-concentration.v1.json"])
    scope_mismatch["scenarios"][0]["assumptions"][0]["scope_key"] = "WRONG:SCOPE"
    scope_issues = engine.validate_package(db_path, scope_mismatch)
    check("market_denominator_scope_mismatch_blocked", any(item["code"] == "scope_mismatch" for item in scope_issues), scope_issues)

    valuation_mismatch = copy.deepcopy(packages["valuation-expectations.v1.json"])
    valuation_mismatch["controls"]["forecast_snapshot_timestamp"] = "2024-04-29T16:00:00Z"
    valuation_issues = engine.validate_package(db_path, valuation_mismatch)
    check("valuation_forecast_timestamp_mismatch_blocked", any(item["code"] == "valuation_timestamp_mismatch" for item in valuation_issues), valuation_issues)

    boundary = copy.deepcopy(packages["competition-concentration.v1.json"])
    boundary["fund_holdings"] = []
    boundary_issues = engine.validate_package(db_path, boundary)
    check("fund_holdings_boundary_enforced", any(item["code"] == "research_boundary_violation" for item in boundary_issues), boundary_issues)

    historical = copy.deepcopy(packages["company-operating-bridge.v1.json"])
    historical["package_id"] = "CR.MODEL.PKG.STAGE7.POINT_IN_TIME.NEGATIVE"
    historical["as_of_timestamp"] = "2024-03-27T23:59:59+08:00"
    historical["scenarios"][0]["assumptions"][0]["available_at"] = "2024-03-27T15:59:59Z"
    historical_issues = engine.validate_package(db_path, historical)
    historical_blocked = False
    if not historical_issues:
        with TemporaryDirectory() as temp_root:
            historical_path = Path(temp_root) / "historical.json"
            historical_path.write_text(json.dumps(historical, ensure_ascii=False), encoding="utf-8")
            try:
                engine.run_package(db_path, historical_path)
            except engine.ModelValidationError as exc:
                historical_blocked = any(item["code"] == "fact_unavailable_at_cutoff" for item in exc.issues)
                historical_issues = exc.issues
    check("point_in_time_fact_future_leakage_blocked", historical_blocked, historical_issues)

    conflict = copy.deepcopy(packages["competition-concentration.v1.json"])
    conflict["scope"]["note"] = "same id, different content"
    conflict_blocked = False
    with TemporaryDirectory() as temp_root:
        conflict_path = Path(temp_root) / "conflict.json"
        conflict_path.write_text(json.dumps(conflict, ensure_ascii=False), encoding="utf-8")
        try:
            engine.run_package(db_path, conflict_path)
        except engine.ModelValidationError as exc:
            conflict_blocked = any(item["code"] == "package_id_content_conflict" for item in exc.issues)
            conflict_detail = exc.issues
    check("model_package_id_hash_conflict_blocked", conflict_blocked, conflict_detail if conflict_blocked else "not blocked")

    rerun = engine.run_package(db_path, MODEL_ROOT / "financial-forecast.v1.json")
    status_after = engine.engine_status(db_path)
    check("model_run_idempotency", rerun["status"] == "completed" and status_after["counts"] == status_before["counts"], {"before": status_before["counts"], "after": status_after["counts"]})
    with sqlite3.connect(db_path) as connection:
        fact_observations_after = connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    check("assumptions_do_not_pollute_fact_store", fact_observations_after == fact_observations_before, {"before": fact_observations_before, "after": fact_observations_after})
    check("sqlite_integrity_and_foreign_keys", status_after["integrity_check"] == "ok" and not status_after["foreign_key_violations"], status_after)

    failed = [item for item in checks if not item["passed"]]
    report = {
        "suite_id": "CR.TEST.STAGE7.MODEL_ENGINE.001",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "passed": not failed,
        "decision": "accept_stage7_research_model_engine" if not failed else "reject_stage7",
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks": checks,
        "engine_status": status_after,
        "declared_boundaries": [
            "演示模型的市场、市值、一致预期和预测参数均为 SCENARIO_ASSUMPTION，不是实时事实或投资意见。",
            "正式研究需把阶段五数据源持续写入阶段六事实仓，再按同一模型合同运行。",
            "财务预测和估值结果通过全部必需系统质量门后直接进入内部研究输出。",
            "系统不接收基金持仓，不推断基金仓位，不自动发出买卖指令。"
        ],
        "failures": failed,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("suite_id", "passed", "decision", "checks_passed", "checks_total")}, ensure_ascii=False, indent=2))
    if failed:
        print(json.dumps(failed, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
