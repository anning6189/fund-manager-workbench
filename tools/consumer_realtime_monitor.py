#!/usr/bin/env python3
"""Realtime research monitoring for the full consumer research agent.

This engine evaluates event-time signals and the system's actual freshness,
coverage, licence and workflow state.  Alerts are internal research signals;
task triggers are suggestions only and never auto-publish or trade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import consumer_data_production as production
import consumer_knowledge_store as knowledge
import consumer_workflow_engine as workflow
import full_consumer_coverage as coverage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
SPEC_PATH = PROJECT_ROOT / "specs" / "monitoring" / "consumer-realtime-monitoring.v1.json"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "006_consumer_realtime_monitoring.sql"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "monitoring" / "module3-realtime-research"
ENGINE_VERSION = "1.1.1"
SEVERITY_ORDER = {"info": 0, "watch": 1, "important": 2, "critical": 3}


class MonitorValidationError(ValueError):
    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        super().__init__("; ".join(item["message"] for item in issues))


def issue(code: str, message: str, path: str = "$") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: str) -> str:
    return production.stable_id(prefix, *parts)


def monitor_spec() -> dict[str, Any]:
    return read_json(SPEC_PATH)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


def init_monitor(db_path: Path) -> dict[str, Any]:
    coverage.init_coverage(db_path)
    workflow.init_engine(db_path)
    spec = monitor_spec()
    now = knowledge.utc_now()
    with knowledge.connect(db_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute(
            """UPDATE monitor_alerts
               SET state='resolved',resolved_at=COALESCE(resolved_at,?),resolution_reason='human_review_gate_removed'
               WHERE rule_code='CR.MON.WORKFLOW.BACKLOG'
                 AND detail_json LIKE '%pending_human_review%'
                 AND state IN ('open','acknowledged')""",
            (now,),
        )
        for rule in spec["default_rules"]:
            task_template = rule.get("task_template_by_event") or rule.get("task_template_id")
            connection.execute(
                """INSERT INTO monitor_rules(
                       rule_code,name,signal_type,condition_json,severity,cooldown_minutes,
                       task_template_json,enabled,version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,1,?,?,?)
                   ON CONFLICT(rule_code) DO UPDATE SET
                       name=excluded.name,signal_type=excluded.signal_type,
                       condition_json=excluded.condition_json,severity=excluded.severity,
                       cooldown_minutes=excluded.cooldown_minutes,task_template_json=excluded.task_template_json,
                       version=excluded.version,updated_at=excluded.updated_at""",
                (
                    rule["rule_code"], rule["name"], rule["signal_type"],
                    canonical({"condition": rule["condition"]}), rule["severity"],
                    rule["cooldown_minutes"], canonical(task_template) if task_template else None,
                    spec["version"], now, now,
                ),
            )
        subscription_id = "CR.MON.SUB.FULL_CONSUMER.DEFAULT"
        connection.execute(
            """INSERT OR IGNORE INTO monitor_subscriptions(
                   subscription_id,subscription_name,subscriber_type,subscriber_id,sector_code,
                   event_types_json,minimum_severity,delivery_channels_json,status,created_at,updated_at
               ) VALUES(?,?,'role','public_fund_manager',NULL,?,'watch',?,'active',?,?)""",
            (
                subscription_id, "全消费行业默认研究监控", canonical(spec["event_types"]),
                canonical(spec["delivery_policy"]["channels"]), now, now,
            ),
        )
    return {
        "status": "ready", "engine_version": ENGINE_VERSION, "database": str(db_path),
        "rules": len(spec["default_rules"]), "event_types": len(spec["event_types"]),
        "default_subscription_id": subscription_id, "delivery": "internal_only",
    }


def validate_event(db_path: Path, event: dict[str, Any], cutoff_timestamp: str) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    required = [
        "source_id", "event_type", "event_time", "available_at", "title",
        "materiality_score", "license_status", "no_fund_holdings_or_positions",
    ]
    for key in required:
        if key not in event or event[key] in (None, ""):
            problems.append(issue("required_field_missing", f"Missing event field: {key}", f"$.{key}"))
    for path in production.scan_forbidden(event):
        problems.append(issue("portfolio_field_forbidden", "Fund holdings and position inference are forbidden", path))
    if problems:
        return problems
    try:
        cutoff = parse_time(cutoff_timestamp)
        event_time = parse_time(event["event_time"])
        available = parse_time(event["available_at"])
        if event_time > cutoff or available > cutoff:
            problems.append(issue("future_information_blocked", "event_time and available_at must not be later than cutoff_timestamp"))
        if available < event_time:
            problems.append(issue("available_before_event", "available_at cannot be earlier than event_time"))
    except (ValueError, TypeError):
        problems.append(issue("event_time_invalid", "event_time, available_at and cutoff_timestamp require ISO-8601 timezones"))
    spec = monitor_spec()
    if event["event_type"] not in spec["event_types"]:
        problems.append(issue("event_type_invalid", f"Unsupported event_type: {event['event_type']}"))
    try:
        score = float(event["materiality_score"])
        if score < 0 or score > 1:
            raise ValueError
    except (ValueError, TypeError):
        problems.append(issue("materiality_invalid", "materiality_score must be between 0 and 1"))
    if event.get("no_fund_holdings_or_positions") is not True:
        problems.append(issue("portfolio_boundary_not_confirmed", "Event must confirm the fund-data boundary"))
    if float(event.get("materiality_score", 0) or 0) >= 0.70 and (not event.get("source_url") or not event.get("locator")):
        problems.append(issue("material_event_not_locatable", "Material events require source_url and locator"))
    with knowledge.connect(db_path) as connection:
        source = connection.execute("SELECT source_id FROM source_catalog WHERE source_id=?", (event["source_id"],)).fetchone()
        sector = connection.execute("SELECT node_code FROM taxonomy_nodes WHERE node_code=?", (event.get("sector_code"),)).fetchone() if event.get("sector_code") else True
        entity = connection.execute("SELECT entity_id FROM entities WHERE entity_id=?", (event.get("entity_id"),)).fetchone() if event.get("entity_id") else True
    if not source:
        problems.append(issue("source_not_registered", f"Unknown source: {event['source_id']}"))
    if not sector:
        problems.append(issue("sector_not_registered", f"Unknown sector: {event.get('sector_code')}"))
    if not entity:
        problems.append(issue("entity_not_registered", f"Unknown entity: {event.get('entity_id')}"))
    return problems


def ingest_event(db_path: Path, event_path: Path, cutoff_timestamp: str) -> dict[str, Any]:
    init_monitor(db_path)
    event = read_json(event_path)
    problems = validate_event(db_path, event, cutoff_timestamp)
    if problems:
        raise MonitorValidationError(problems)
    payload_hash = "sha256:" + hashlib.sha256(canonical(event).encode("utf-8")).hexdigest()
    event_id = event.get("monitor_event_id") or stable_id(
        "me", event["source_id"], event["event_type"], event["available_at"], payload_hash,
    )
    with knowledge.connect(db_path) as connection:
        license_row = connection.execute(
            "SELECT decision FROM source_license_decisions WHERE source_id=?", (event["source_id"],)
        ).fetchone()
        pending = not license_row or license_row["decision"] not in {"approved", "public_official"}
        status = "license_gated" if pending else "accepted"
        connection.execute(
            """INSERT OR IGNORE INTO monitor_events(
                   monitor_event_id,source_id,event_type,event_time,available_at,detected_at,sector_code,
                   entity_id,security_id,title,summary,materiality_score,source_url,locator,content_hash,
                   license_status,status,raw_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, event["source_id"], event["event_type"], knowledge.normalize_timestamp(event["event_time"]),
                knowledge.normalize_timestamp(event["available_at"]), knowledge.utc_now(), event.get("sector_code"),
                event.get("entity_id"), event.get("security_id"), event["title"], event.get("summary"),
                float(event["materiality_score"]), event.get("source_url"), event.get("locator"), payload_hash,
                event["license_status"], status, canonical(event),
            ),
        )
    return {"status": status, "monitor_event_id": event_id, "content_hash": payload_hash}


def active_subscriptions(connection: Any, severity: str, sector_code: str | None, event_type: str) -> list[Any]:
    rows = connection.execute("SELECT * FROM monitor_subscriptions WHERE status='active'").fetchall()
    return [
        row for row in rows
        if SEVERITY_ORDER[severity] >= SEVERITY_ORDER[row["minimum_severity"]]
        and (row["sector_code"] is None or row["sector_code"] == sector_code)
        and event_type in json.loads(row["event_types_json"])
    ]


def matching_task(connection: Any, sector_code: str | None, template_id: str | None) -> str | None:
    if not sector_code or not template_id:
        return None
    row = connection.execute(
        """SELECT task_package_id FROM research_task_packages
           WHERE sector_code=? AND template_id=? ORDER BY cutoff_timestamp DESC LIMIT 1""",
        (sector_code, template_id),
    ).fetchone()
    return row["task_package_id"] if row else None


def emit_alert(connection: Any, run_id: str, rule: dict[str, Any], signal: dict[str, Any],
               cutoff_timestamp: str) -> tuple[str, bool, bool]:
    dedup_key = stable_id("dedup", rule["rule_code"], signal["scope_key"])
    alert_id = stable_id("alert", dedup_key)
    existing = connection.execute("SELECT * FROM monitor_alerts WHERE deduplication_key=?", (dedup_key,)).fetchone()
    created = existing is None
    now = knowledge.normalize_timestamp(cutoff_timestamp)
    if created:
        connection.execute(
            """INSERT INTO monitor_alerts(
                   alert_id,deduplication_key,rule_code,monitor_run_id,monitor_event_id,sector_code,
                   entity_id,severity,title,detail_json,first_detected_at,last_detected_at,
                   occurrence_count,state,publication_status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,'open','internal_only')""",
            (
                alert_id, dedup_key, rule["rule_code"], run_id, signal.get("monitor_event_id"),
                signal.get("sector_code"), signal.get("entity_id"), signal.get("severity", rule["severity"]),
                signal["title"], canonical(signal["detail"]), now, now,
            ),
        )
        connection.execute(
            "INSERT INTO monitor_alert_events(alert_event_id,alert_id,event_type,actor,occurred_at,detail_json) VALUES(?,?,'created','monitor_engine',?,?)",
            (stable_id("mae", alert_id, run_id, "created"), alert_id, now, canonical({"monitor_run_id": run_id})),
        )
    else:
        alert_id = existing["alert_id"]
        connection.execute(
            """UPDATE monitor_alerts SET monitor_run_id=?,monitor_event_id=COALESCE(?,monitor_event_id),
                   last_detected_at=?,occurrence_count=occurrence_count+1,
                   detail_json=?,state=CASE WHEN state='resolved' THEN 'open' ELSE state END
               WHERE alert_id=?""",
            (run_id, signal.get("monitor_event_id"), now, canonical(signal["detail"]), alert_id),
        )
        connection.execute(
            "INSERT OR IGNORE INTO monitor_alert_events(alert_event_id,alert_id,event_type,actor,occurred_at,detail_json) VALUES(?,?,'deduplicated','monitor_engine',?,?)",
            (stable_id("mae", alert_id, run_id, "deduplicated"), alert_id, now, canonical({"monitor_run_id": run_id})),
        )

    task_created = False
    template_id = signal.get("template_id")
    package_id = matching_task(connection, signal.get("sector_code"), template_id)
    if template_id and signal.get("sector_code"):
        trigger_id = stable_id("mtt", alert_id, template_id)
        before = connection.total_changes
        connection.execute(
            """INSERT OR IGNORE INTO monitor_task_triggers(
                   trigger_id,alert_id,research_task_package_id,sector_code,template_id,trigger_reason,
                   priority,status,automatic_execution,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'suggested',0,?,?)""",
            (
                trigger_id, alert_id, package_id, signal["sector_code"], template_id,
                signal["title"], "urgent" if signal.get("severity", rule["severity"]) == "critical" else "high",
                now, now,
            ),
        )
        task_created = connection.total_changes > before

    if created:
        event_type = signal.get("event_type", "data_quality")
        for subscription in active_subscriptions(connection, signal.get("severity", rule["severity"]), signal.get("sector_code"), event_type):
            for channel in json.loads(subscription["delivery_channels_json"]):
                connection.execute(
                    """INSERT OR IGNORE INTO monitor_delivery_outbox(
                           delivery_id,alert_id,subscription_id,channel,status,external_delivery,payload_json,created_at
                       ) VALUES(?,?,?,?,'pending',0,?,?)""",
                    (
                        stable_id("delivery", alert_id, subscription["subscription_id"], channel), alert_id,
                        subscription["subscription_id"], channel,
                        canonical({"title": signal["title"], "severity": signal.get("severity", rule["severity"]), "internal_only": True}), now,
                    ),
                )
    return alert_id, created, task_created


def collect_system_signals(db_path: Path) -> list[tuple[str, dict[str, Any]]]:
    signals: list[tuple[str, dict[str, Any]]] = []
    fresh = knowledge.freshness_report(db_path)
    for item in fresh["streams"]:
        if item["freshness_status"] in {"stale", "not_started"}:
            signals.append(("CR.MON.FRESHNESS.STALE", {
                "scope_key": f"{item['source_id']}:{item['stream_name']}",
                "title": f"数据流未达到新鲜度要求：{item['source_id']} / {item['stream_name']}",
                "detail": item, "event_type": "source_freshness",
            }))
    with knowledge.connect(db_path) as connection:
        for row in connection.execute("SELECT * FROM research_coverage_status ORDER BY sector_code,market").fetchall():
            if row["metric_population_status"] != "populated" or row["security_count"] == 0:
                signals.append(("CR.MON.COVERAGE.GAP", {
                    "scope_key": f"{row['sector_code']}:{row['market']}", "sector_code": row["sector_code"],
                    "title": f"研究覆盖尚未完整：{row['sector_code']} / {row['market']}",
                    "detail": dict(row), "event_type": "data_quality", "template_id": "P0_10_RISK_CATALYST_MONITOR",
                }))
        for row in connection.execute("SELECT source_id,decision,notes FROM source_license_decisions WHERE decision='pending' ORDER BY source_id").fetchall():
            signals.append(("CR.MON.LICENSE.GATE", {
                "scope_key": row["source_id"], "title": f"商业数据许可仍待确认：{row['source_id']}",
                "detail": dict(row), "event_type": "data_quality",
            }))
        for row in connection.execute(
            "SELECT run_id,package_id,status,publication_status,started_at FROM workflow_runs WHERE status IN ('blocked','failed') ORDER BY run_id"
        ).fetchall():
            signals.append(("CR.MON.WORKFLOW.BACKLOG", {
                "scope_key": row["run_id"], "title": f"研究工作流需要处理：{row['package_id']} / {row['status']}",
                "detail": dict(row), "event_type": "data_quality",
            }))
    return signals


def event_signals(db_path: Path, cutoff_timestamp: str) -> list[tuple[str, dict[str, Any]]]:
    mapping = monitor_spec()["default_rules"][-1]["task_template_by_event"]
    with knowledge.connect(db_path) as connection:
        rows = connection.execute(
            """SELECT * FROM monitor_events
               WHERE julianday(available_at)<=julianday(?) AND materiality_score>=0.70
               ORDER BY available_at,monitor_event_id""",
            (knowledge.normalize_timestamp(cutoff_timestamp),),
        ).fetchall()
    return [("CR.MON.EVENT.MATERIAL", {
        "scope_key": row["monitor_event_id"], "monitor_event_id": row["monitor_event_id"],
        "sector_code": row["sector_code"], "entity_id": row["entity_id"], "title": row["title"],
        "severity": "critical" if row["materiality_score"] >= 0.90 else "important",
        "detail": {**dict(row), "raw_json": "omitted_from_alert"}, "event_type": row["event_type"],
        "template_id": mapping.get(row["event_type"]),
    }) for row in rows]


def render_brief(db_path: Path, run_id: str, cutoff_timestamp: str, output_root: Path) -> dict[str, Any]:
    cutoff = parse_time(cutoff_timestamp)
    local = cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
    with knowledge.connect(db_path) as connection:
        alerts = [dict(row) for row in connection.execute(
            """SELECT alert_id,rule_code,sector_code,severity,title,state,first_detected_at,last_detected_at,
                      occurrence_count,publication_status FROM monitor_alerts WHERE monitor_run_id=?
               ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'important' THEN 2 WHEN 'watch' THEN 1 ELSE 0 END DESC,title""",
            (run_id,),
        ).fetchall()]
        tasks = [dict(row) for row in connection.execute(
            """SELECT t.trigger_id,t.alert_id,t.sector_code,t.template_id,t.priority,t.status,t.automatic_execution
               FROM monitor_task_triggers t JOIN monitor_alerts a ON a.alert_id=t.alert_id WHERE a.monitor_run_id=? ORDER BY t.priority,t.trigger_id""",
            (run_id,),
        ).fetchall()]
    directory = output_root / local.strftime("%Y-%m-%d") / run_id.replace(":", "-")
    json_path = directory / "monitoring-brief.json"
    payload = {
        "monitor_run_id": run_id, "cutoff_timestamp": knowledge.normalize_timestamp(cutoff_timestamp),
        "research_timezone": "Asia/Shanghai", "brief_date": local.date().isoformat(),
        "alert_count": len(alerts), "task_suggestion_count": len(tasks), "alerts": alerts, "task_suggestions": tasks,
        "boundaries": ["internal_research_only", "no_automatic_publication", "no_automatic_trade_instruction", "no_fund_holdings_or_position_inference"],
    }
    write_json(json_path, payload)
    md_path = directory / "monitoring-brief.md"
    counts = Counter(item["severity"] for item in alerts)
    lines = [
        "# 全消费行业实时研究监控简报", "", f"> 截止时间：{cutoff_timestamp}",
        "> 用途：内部研究预警；不自动发布，不构成交易指令。", "",
        "## 概览", "", f"- 告警：{len(alerts)} 条（严重 {counts['critical']}、重要 {counts['important']}、关注 {counts['watch']}）",
        f"- 建议研究任务：{len(tasks)} 条；全部需要人工确认后才可进入执行队列。", "", "## 告警", "",
    ]
    lines.extend(f"- [{item['severity']}] {item['title']}（{item['state']}）" for item in alerts)
    lines.extend(["", "## 边界", "", "- 数据缺口和许可闸门保留原状态。", "- 不使用或推断基金持仓与仓位。", "- 不自动生成买卖指令。", ""])
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
    content_hash = "sha256:" + hashlib.sha256(json_path.read_bytes()).hexdigest()
    brief_id = stable_id("brief", run_id)
    with knowledge.connect(db_path) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO monitor_briefs(
                   brief_id,brief_date,cutoff_timestamp,monitor_run_id,status,alert_count,
                   task_suggestion_count,artifact_path,content_hash,created_at
               ) VALUES(?,?,?,?,'internal_draft',?,?,?,?,?)""",
            (brief_id, local.date().isoformat(), knowledge.normalize_timestamp(cutoff_timestamp), run_id, len(alerts), len(tasks), str(md_path), content_hash, knowledge.utc_now()),
        )
    return {"brief_id": brief_id, "json_path": str(json_path), "markdown_path": str(md_path), "content_hash": content_hash}


def run_monitor(db_path: Path, cutoff_timestamp: str, mode: str = "scheduled",
                output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    initialized = init_monitor(db_path)
    parse_time(cutoff_timestamp)
    normalized = knowledge.normalize_timestamp(cutoff_timestamp)
    run_id = stable_id("monitor", normalized, mode, ENGINE_VERSION)
    with knowledge.connect(db_path) as connection:
        existing = connection.execute("SELECT * FROM monitor_runs WHERE monitor_run_id=?", (run_id,)).fetchone()
    if existing and existing["status"] in {"completed", "partially_complete"}:
        with knowledge.connect(db_path) as connection:
            brief = connection.execute("SELECT * FROM monitor_briefs WHERE monitor_run_id=?", (run_id,)).fetchone()
        return {"monitor_run_id": run_id, "status": existing["status"], "idempotent_replay": True, "summary": json.loads(existing["summary_json"]), "brief": dict(brief) if brief else None}
    with knowledge.connect(db_path) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO monitor_runs(
                   monitor_run_id,cutoff_timestamp,mode,status,started_at
               ) VALUES(?,?,?,'running',?)""",
            (run_id, normalized, mode, knowledge.utc_now()),
        )
        rules = {row["rule_code"]: dict(row) for row in connection.execute("SELECT * FROM monitor_rules WHERE enabled=1").fetchall()}
    signals = collect_system_signals(db_path) + event_signals(db_path, cutoff_timestamp)
    freshness_truth = knowledge.freshness_report(db_path)["summary"]
    created = deduplicated = tasks = 0
    alert_ids: list[str] = []
    with knowledge.connect(db_path) as connection:
        for rule_code, signal in signals:
            alert_id, was_created, task_created = emit_alert(connection, run_id, rules[rule_code], signal, cutoff_timestamp)
            alert_ids.append(alert_id)
            created += int(was_created)
            deduplicated += int(not was_created)
            tasks += int(task_created)
        # A state alert must disappear when the underlying signal disappears.
        # Without this reconciliation, a successfully recovered stream remains
        # visible forever because the workbench reads all open alerts, not only
        # alerts from the latest monitor run.
        active_keys_by_rule: dict[str, set[str]] = {}
        for rule_code, signal in signals:
            active_keys_by_rule.setdefault(rule_code, set()).add(
                stable_id("dedup", rule_code, signal["scope_key"])
            )
        resolved = 0
        for rule_code in ("CR.MON.FRESHNESS.STALE", "CR.MON.LICENSE.GATE"):
            active_keys = active_keys_by_rule.get(rule_code, set())
            candidates = connection.execute(
                "SELECT alert_id,deduplication_key FROM monitor_alerts WHERE rule_code=? AND state IN ('open','acknowledged')",
                (rule_code,),
            ).fetchall()
            for candidate in candidates:
                if candidate["deduplication_key"] in active_keys:
                    continue
                now = knowledge.utc_now()
                connection.execute(
                    "UPDATE monitor_alerts SET state='resolved',resolved_at=?,resolution_reason='signal_cleared_automatically' WHERE alert_id=?",
                    (now, candidate["alert_id"]),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO monitor_alert_events(alert_event_id,alert_id,event_type,actor,occurred_at,detail_json) VALUES(?,?,'resolved','monitor_engine',?,?)",
                    (stable_id("mae", candidate["alert_id"], run_id, "resolved"), candidate["alert_id"], now,
                     canonical({"monitor_run_id": run_id, "reason": "signal_cleared_automatically"})),
                )
                resolved += 1
        summary = {
            "rules_evaluated": len(rules), "signals_evaluated": len(signals),
            "alerts_created": created, "alerts_deduplicated": deduplicated,
            "triggered_task_suggestions": tasks, "freshness_truth": freshness_truth,
            "alerts_resolved": resolved,
            "coverage_truth": "definitions_only_until_production_watermarks_are_complete",
            "external_delivery": 0, "automatic_execution": 0,
        }
        status = "partially_complete" if summary["freshness_truth"].get("not_started", 0) or summary["freshness_truth"].get("stale", 0) else "completed"
        connection.execute(
            """UPDATE monitor_runs SET status=?,rules_evaluated=?,signals_evaluated=?,alerts_created=?,
                   alerts_deduplicated=?,triggered_tasks=?,completed_at=?,summary_json=? WHERE monitor_run_id=?""",
            (status, len(rules), len(signals), created, deduplicated, tasks, knowledge.utc_now(), canonical(summary), run_id),
        )
    brief = render_brief(db_path, run_id, cutoff_timestamp, output_root)
    return {"monitor_run_id": run_id, "status": status, "idempotent_replay": False, "summary": summary, "brief": brief, "alert_ids": alert_ids}


def acknowledge_alert(db_path: Path, alert_id: str, reviewer: str, notes: str = "") -> dict[str, Any]:
    if not reviewer.strip() or reviewer.strip().lower() in {"ai", "agent", "unassigned"}:
        raise MonitorValidationError([issue("named_human_required", "Acknowledgement requires a named human")])
    with knowledge.connect(db_path) as connection:
        alert = connection.execute("SELECT * FROM monitor_alerts WHERE alert_id=?", (alert_id,)).fetchone()
        if not alert:
            raise MonitorValidationError([issue("alert_not_found", f"Unknown alert: {alert_id}")])
        now = knowledge.utc_now()
        connection.execute(
            "UPDATE monitor_alerts SET state='acknowledged',acknowledged_by=?,acknowledged_at=? WHERE alert_id=?",
            (reviewer.strip(), now, alert_id),
        )
        connection.execute(
            "INSERT INTO monitor_alert_events(alert_event_id,alert_id,event_type,actor,occurred_at,detail_json) VALUES(?,?,'acknowledged',?,?,?)",
            (stable_id("mae", alert_id, now, "acknowledged"), alert_id, reviewer.strip(), now, canonical({"notes": notes})),
        )
    return {"alert_id": alert_id, "state": "acknowledged", "acknowledged_by": reviewer.strip()}


def monitor_status(db_path: Path) -> dict[str, Any]:
    initialized = init_monitor(db_path)
    with knowledge.connect(db_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in ("monitor_rules", "monitor_subscriptions", "monitor_runs", "monitor_events", "monitor_alerts", "monitor_task_triggers", "monitor_delivery_outbox", "monitor_briefs")
        }
        states = {row["state"]: row["count"] for row in connection.execute("SELECT state,COUNT(*) AS count FROM monitor_alerts GROUP BY state").fetchall()}
    return {**initialized, "table_counts": counts, "alert_states": states}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    event = sub.add_parser("ingest-event")
    event.add_argument("--event", type=Path, required=True)
    event.add_argument("--cutoff", required=True)
    run = sub.add_parser("run")
    run.add_argument("--cutoff", required=True)
    run.add_argument("--mode", choices=("scheduled", "manual", "event"), default="scheduled")
    ack = sub.add_parser("acknowledge")
    ack.add_argument("--alert-id", required=True)
    ack.add_argument("--reviewer", required=True)
    ack.add_argument("--notes", default="")
    sub.add_parser("status")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init": result = init_monitor(args.db)
        elif args.command == "ingest-event": result = ingest_event(args.db, args.event, args.cutoff)
        elif args.command == "run": result = run_monitor(args.db, args.cutoff, args.mode)
        elif args.command == "acknowledge": result = acknowledge_alert(args.db, args.alert_id, args.reviewer, args.notes)
        else: result = monitor_status(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except MonitorValidationError as exc:
        print(json.dumps({"status": "blocked", "issues": exc.issues}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
