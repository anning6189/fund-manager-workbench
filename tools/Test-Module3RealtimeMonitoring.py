#!/usr/bin/env python3
"""Acceptance suite for module 3: realtime research and continuous monitoring."""

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

import consumer_realtime_monitor as engine  # noqa: E402
import full_consumer_coverage as coverage  # noqa: E402


SNAPSHOTS = [
    PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-consumer-universe-2026-08-12.json",
    PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata" / "a-share-culture-education-universe-2026-08-12.json",
]
REPORT = PROJECT_ROOT / "tests" / "module-3-realtime-monitoring-acceptance.v1.json"
CUTOFF = "2026-08-12T23:59:59+08:00"


def main() -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    with TemporaryDirectory(prefix="consumer-monitor-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        db = root / "monitor.db"
        output = root / "output"
        initialized = engine.init_monitor(db)
        universe = coverage.build_universe(db, SNAPSHOTS, "2026-08-12", root / "coverage")
        coverage.build_coverage_matrix(db, universe["universe_snapshot_id"], "2026-08-12", root / "coverage")
        coverage.generate_task_packages(db, CUTOFF, root / "coverage")

        check("five_default_monitor_rules", initialized["rules"] == 5, initialized)
        check("ten_event_types", initialized["event_types"] == 10, initialized)
        check("internal_delivery_only", initialized["delivery"] == "internal_only", initialized)
        spec = engine.monitor_spec()
        check("event_and_scheduled_modes_declared", set(spec["monitoring_modes"]) == {"event_driven", "scheduled_scan", "system_health"}, spec["monitoring_modes"])
        check("alert_state_machine_complete", set(spec["alert_state_machine"]["states"]) == {"open", "acknowledged", "resolved", "suppressed"}, spec["alert_state_machine"])
        check("no_automatic_external_message", spec["delivery_policy"]["automatic_external_message"] is False, spec["delivery_policy"])
        check("no_automatic_report_publication", spec["delivery_policy"]["automatic_report_publication"] is False, spec["delivery_policy"])
        check("no_automatic_trade_instruction", spec["delivery_policy"]["automatic_trade_instruction"] is False, spec["delivery_policy"])
        check("fund_boundary_quality_gate", "no_fund_holdings_or_position_inference" in spec["quality_gates"], spec["quality_gates"])

        event = {
            "source_id": "CR.SRC.NBS", "event_type": "industry_data_release",
            "event_time": "2026-08-12T10:00:00+08:00", "available_at": "2026-08-12T10:05:00+08:00",
            "sector_code": "CR.D.AU", "title": "测试用官方行业数据发布事件",
            "summary": "只用于模块三验收，不代表真实市场数据。", "materiality_score": 0.82,
            "source_url": "https://www.stats.gov.cn/test-fixture", "locator": "test fixture row 1",
            "license_status": "public_government_information", "no_fund_holdings_or_positions": True,
        }
        event_path = root / "event.json"
        engine.write_json(event_path, event)
        ingested = engine.ingest_event(db, event_path, CUTOFF)
        check("official_event_accepted", ingested["status"] == "accepted", ingested)
        same = engine.ingest_event(db, event_path, CUTOFF)
        check("event_ingestion_idempotent", same["monitor_event_id"] == ingested["monitor_event_id"], same)

        future = copy.deepcopy(event)
        future["event_time"] = "2026-08-13T00:00:00+08:00"
        future["available_at"] = "2026-08-13T00:00:01+08:00"
        problems = engine.validate_event(db, future, CUTOFF)
        check("future_information_blocked", any(item["code"] == "future_information_blocked" for item in problems), problems)
        unlocatable = copy.deepcopy(event)
        unlocatable.pop("locator")
        problems = engine.validate_event(db, unlocatable, CUTOFF)
        check("material_event_requires_locator", any(item["code"] == "material_event_not_locatable" for item in problems), problems)
        forbidden = copy.deepcopy(event)
        forbidden["fund_holdings"] = []
        problems = engine.validate_event(db, forbidden, CUTOFF)
        check("fund_holdings_event_blocked", any(item["code"] == "portfolio_field_forbidden" for item in problems), problems)
        unknown = copy.deepcopy(event)
        unknown["source_id"] = "CR.SRC.UNKNOWN"
        problems = engine.validate_event(db, unknown, CUTOFF)
        check("unknown_source_blocked", any(item["code"] == "source_not_registered" for item in problems), problems)
        no_boundary = copy.deepcopy(event)
        no_boundary["no_fund_holdings_or_positions"] = False
        problems = engine.validate_event(db, no_boundary, CUTOFF)
        check("portfolio_boundary_required", any(item["code"] == "portfolio_boundary_not_confirmed" for item in problems), problems)

        run = engine.run_monitor(db, CUTOFF, "scheduled", output)
        check("monitor_run_truthfully_partial", run["status"] == "partially_complete", run)
        check("five_rules_evaluated", run["summary"]["rules_evaluated"] == 5, run["summary"])
        check("fifty_two_signals_evaluated", run["summary"]["signals_evaluated"] == 52, run["summary"])
        check("fifty_two_alerts_created", run["summary"]["alerts_created"] == 52, run["summary"])
        check("twenty_three_task_suggestions", run["summary"]["triggered_task_suggestions"] == 23, run["summary"])
        check("all_22_streams_unhealthy_explicit", run["summary"]["freshness_truth"]["fresh"] == 0 and run["summary"]["freshness_truth"]["stale"] + run["summary"]["freshness_truth"]["not_started"] == 22, run["summary"])
        check("no_external_delivery", run["summary"]["external_delivery"] == 0, run["summary"])
        check("no_automatic_execution", run["summary"]["automatic_execution"] == 0, run["summary"])
        check("brief_json_written", Path(run["brief"]["json_path"]).is_file(), run["brief"])
        check("brief_markdown_written", Path(run["brief"]["markdown_path"]).is_file(), run["brief"])

        replay = engine.run_monitor(db, CUTOFF, "scheduled", output)
        check("same_cutoff_run_idempotent", replay["idempotent_replay"] is True and replay["monitor_run_id"] == run["monitor_run_id"], replay)
        second = engine.run_monitor(db, "2026-08-13T00:00:00+08:00", "scheduled", output)
        check("next_run_deduplicates_alerts", second["summary"]["alerts_created"] == 0 and second["summary"]["alerts_deduplicated"] == 52, second["summary"])
        check("dedup_does_not_duplicate_tasks", second["summary"]["triggered_task_suggestions"] == 0, second["summary"])

        ai_blocked = False
        try:
            engine.acknowledge_alert(db, run["alert_ids"][0], "AI")
        except engine.MonitorValidationError as exc:
            ai_blocked = any(item["code"] == "named_human_required" for item in exc.issues)
        check("agent_cannot_acknowledge_alert", ai_blocked, ai_blocked)
        acknowledged = engine.acknowledge_alert(db, run["alert_ids"][0], "测试研究员", "已知悉数据缺口")
        check("named_human_can_acknowledge", acknowledged["state"] == "acknowledged", acknowledged)

        with closing(sqlite3.connect(db)) as connection:
            connection.row_factory = sqlite3.Row
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            event_count = connection.execute("SELECT COUNT(*) AS count FROM monitor_events").fetchone()["count"]
            alert_count = connection.execute("SELECT COUNT(*) AS count FROM monitor_alerts").fetchone()["count"]
            trigger_count = connection.execute("SELECT COUNT(*) AS count FROM monitor_task_triggers").fetchone()["count"]
            auto_count = connection.execute("SELECT COUNT(*) AS count FROM monitor_task_triggers WHERE automatic_execution<>0").fetchone()["count"]
            external_count = connection.execute("SELECT COUNT(*) AS count FROM monitor_delivery_outbox WHERE external_delivery<>0").fetchone()["count"]
            outbox_count = connection.execute("SELECT COUNT(*) AS count FROM monitor_delivery_outbox").fetchone()["count"]
            publication = {row["publication_status"] for row in connection.execute("SELECT publication_status FROM monitor_alerts")}
        expected = {"monitor_rules", "monitor_subscriptions", "monitor_runs", "monitor_events", "monitor_alerts", "monitor_alert_events", "monitor_task_triggers", "monitor_delivery_outbox", "monitor_briefs"}
        check("monitor_audit_schema_complete", expected <= tables, sorted(expected))
        check("one_event_after_idempotent_ingest", event_count == 1, event_count)
        check("alert_deduplication_keeps_52", alert_count == 52, alert_count)
        check("task_deduplication_keeps_23", trigger_count == 23, trigger_count)
        check("all_task_triggers_nonautomatic", auto_count == 0, auto_count)
        check("all_deliveries_internal", external_count == 0 and outbox_count == 104, {"external": external_count, "outbox": outbox_count})
        check("all_alerts_internal_only", publication == {"internal_only"}, sorted(publication))

    passed = sum(1 for item in checks if item["passed"])
    result = {"module": 3, "name": "实时研究与持续监控", "status": "passed" if passed == len(checks) else "failed", "passed": passed, "total": len(checks), "checks": checks}
    engine.write_json(REPORT, result)
    print(json.dumps({"status": result["status"], "passed": passed, "total": len(checks)}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
