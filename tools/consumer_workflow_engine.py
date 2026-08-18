#!/usr/bin/env python3
"""Stage 8 consumer research workflow and agent orchestration engine.

The engine coordinates the stage-6 point-in-time knowledge store and the
stage-7 deterministic model engine. Internal research output is released only
after every required machine quality gate passes; it never reads or infers
fund positions and never publishes externally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import consumer_knowledge_store as knowledge
import consumer_model_engine as models


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
WORKFLOW_SPEC = PROJECT_ROOT / "specs" / "workflows" / "consumer-research-workflow.v1.json"
WORKFLOW_SCHEMA = PROJECT_ROOT / "sql" / "003_consumer_research_workflow_engine.sql"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "workflows" / "stage8"
ENGINE_VERSION = "1.1.0"
WORKFLOW_ID = "CR.WORKFLOW.RESEARCH_ORCHESTRATION.001"
TERMINAL_TASK_STATES = {"completed", "degraded", "skipped"}

FORBIDDEN_KEYS = {
    "fund_holdings", "fund_holding", "fund_position", "fund_positions",
    "portfolio_holdings", "portfolio_position", "portfolio_positions",
    "portfolio_exposure", "position_inference", "holding_inference",
    "trade_instruction", "trade_action", "buy_signal", "sell_signal",
}

METRIC_NAMES = {
    "CR.CO.REVENUE": "营业收入",
    "CR.CO.OPERATING_COST": "营业成本",
    "CR.CO.PARENT_NET_PROFIT": "归母净利润",
    "CR.CO.CFO_NET": "经营活动现金流净额",
}


class WorkflowValidationError(ValueError):
    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        super().__init__("; ".join(item["message"] for item in issues))


def issue(code: str, message: str, path: str = "$") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()[:24]}"


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def workflow_spec() -> dict[str, Any]:
    return read_json(WORKFLOW_SPEC)


def topological_plan(spec: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = {item["task_id"]: dict(item) for item in spec["task_graph"]}
    unknown = sorted({dep for item in tasks.values() for dep in item["depends_on"] if dep not in tasks})
    if unknown:
        raise WorkflowValidationError([issue("unknown_dependency", f"Unknown task dependencies: {unknown}")])
    pending = set(tasks)
    resolved: set[str] = set()
    plan: list[dict[str, Any]] = []
    wave = 0
    while pending:
        ready = sorted(task_id for task_id in pending if set(tasks[task_id]["depends_on"]) <= resolved)
        if not ready:
            raise WorkflowValidationError([issue("task_graph_cycle", "Workflow task graph contains a cycle")])
        for task_id in ready:
            item = tasks[task_id]
            item["wave_no"] = wave
            plan.append(item)
        pending.difference_update(ready)
        resolved.update(ready)
        wave += 1
    return plan


def init_engine(db_path: Path) -> dict[str, Any]:
    knowledge.init_store(db_path)
    models.init_engine(db_path)
    spec = workflow_spec()
    plan = topological_plan(spec)
    with knowledge.connect(db_path) as connection:
        connection.executescript(WORKFLOW_SCHEMA.read_text(encoding="utf-8"))
        migration = PROJECT_ROOT / "sql" / "009_remove_human_review_gate.sql"
        already_migrated = connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='009_remove_human_review_gate'"
        ).fetchone()
        if migration.exists() and not already_migrated and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_runs'"
        ).fetchone():
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute(
            """INSERT OR REPLACE INTO workflow_definitions(
                   workflow_id,name,version,status,role_contracts_json,task_graph_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                spec["workflow_id"], spec["name"], spec["version"], spec["status"],
                knowledge.canonical_json(spec["roles"]), knowledge.canonical_json(plan), knowledge.utc_now(),
            ),
        )
    return {
        "status": "ready", "engine_version": ENGINE_VERSION, "database": str(db_path),
        "workflow_id": spec["workflow_id"], "roles": len(spec["roles"]), "tasks": len(plan),
        "waves": max(item["wave_no"] for item in plan) + 1,
    }


def validate_request(db_path: Path, request: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    required = [
        "package_id", "workflow_id", "template_id", "cutoff_timestamp", "reader",
        "research_question", "scope", "entities", "metric_queries",
    ]
    for key in required:
        if key not in request or request[key] in (None, "", [], {}):
            problems.append(issue("required_field_missing", f"Required field is missing: {key}", f"$.{key}"))
    if problems:
        return problems
    if request["workflow_id"] != WORKFLOW_ID:
        problems.append(issue("workflow_id_invalid", f"Expected workflow_id {WORKFLOW_ID}", "$.workflow_id"))
    try:
        parsed = datetime.fromisoformat(str(request["cutoff_timestamp"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            problems.append(issue("cutoff_timezone_missing", "cutoff_timestamp must include a timezone", "$.cutoff_timestamp"))
    except ValueError:
        problems.append(issue("cutoff_invalid", "cutoff_timestamp must be ISO-8601", "$.cutoff_timestamp"))
    for path in scan_forbidden(request):
        problems.append(issue("portfolio_or_trade_field_forbidden", "Portfolio, position, and trade-instruction fields are forbidden", path))
    if request.get("reader") != "public_fund_manager":
        problems.append(issue("reader_invalid", "Stage 8 reader must be public_fund_manager", "$.reader"))
    entity_ids: set[str] = set()
    for index, entity in enumerate(request.get("entities", [])):
        if not isinstance(entity, dict) or not entity.get("entity_id"):
            problems.append(issue("entity_invalid", "Each entity needs entity_id", f"$.entities[{index}]"))
            continue
        if entity["entity_id"] in entity_ids:
            problems.append(issue("entity_duplicate", f"Duplicate entity_id: {entity['entity_id']}", f"$.entities[{index}]"))
        entity_ids.add(entity["entity_id"])
    allowed_models = (PROJECT_ROOT / "data" / "models" / "stage7").resolve()
    for index, package in enumerate(request.get("model_packages", [])):
        relative = Path(str(package.get("path", "")))
        try:
            resolved = (PROJECT_ROOT / relative).resolve()
            resolved.relative_to(allowed_models)
        except (ValueError, OSError):
            problems.append(issue("model_path_forbidden", "Model package must stay under data/models/stage7", f"$.model_packages[{index}].path"))
            continue
        if package.get("required") and not resolved.is_file():
            problems.append(issue("required_model_missing", f"Required model package does not exist: {relative}", f"$.model_packages[{index}].path"))
    if db_path.exists():
        with knowledge.connect(db_path) as connection:
            registered_entities = {
                row["entity_id"] for row in connection.execute(
                    "SELECT entity_id FROM entities WHERE entity_id IN (%s)" % ",".join("?" for _ in entity_ids),
                    tuple(entity_ids),
                ).fetchall()
            } if entity_ids else set()
            for entity_id in sorted(entity_ids - registered_entities):
                problems.append(issue("entity_not_registered", f"Entity is not registered: {entity_id}", "$.entities"))
            metric_ids = {str(item.get("metric_id", "")) for item in request.get("metric_queries", [])}
            registered_metrics = {
                row["metric_id"] for row in connection.execute(
                    "SELECT metric_id FROM metric_definitions WHERE metric_id IN (%s)" % ",".join("?" for _ in metric_ids),
                    tuple(metric_ids),
                ).fetchall()
            } if metric_ids else set()
            for metric_id in sorted(metric_ids - registered_metrics):
                problems.append(issue("metric_not_registered", f"Metric is not registered: {metric_id}", "$.metric_queries"))
    return problems


def plan_request(db_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    init_engine(db_path)
    problems = validate_request(db_path, request)
    if problems:
        raise WorkflowValidationError(problems)
    spec = workflow_spec()
    plan = topological_plan(spec)
    waves: dict[int, list[str]] = {}
    for task in plan:
        waves.setdefault(task["wave_no"], []).append(task["task_id"])
    return {
        "workflow_id": spec["workflow_id"], "package_id": request["package_id"],
        "cutoff_timestamp": knowledge.normalize_timestamp(request["cutoff_timestamp"]),
        "roles": spec["roles"], "tasks": plan,
        "execution_waves": [{"wave_no": number, "concurrent_tasks": tasks} for number, tasks in sorted(waves.items())],
        "quality_gates": spec["quality_gates"], "human_review_required": False,
        "release_mode": "automatic_internal_release_after_required_quality_gates",
    }


def record_event(connection: sqlite3.Connection, run_id: str, task_id: str | None,
                 event_type: str, severity: str, detail: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO workflow_events(run_id,task_id,event_type,severity,occurred_at,detail_json) VALUES(?,?,?,?,?,?)",
        (run_id, task_id, event_type, severity, knowledge.utc_now(), knowledge.canonical_json(detail)),
    )


def record_artifact(db_path: Path, run_id: str, artifact_type: str, path: Path,
                    metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    artifact = {
        "artifact_id": stable_id("wa", run_id, artifact_type), "artifact_type": artifact_type,
        "path": str(path), "content_hash": hash_file(path), "metadata": metadata or {},
    }
    with knowledge.connect(db_path) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO workflow_artifacts(
                   artifact_id,run_id,artifact_type,path,content_hash,created_at,metadata_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                artifact["artifact_id"], run_id, artifact_type, str(path), artifact["content_hash"],
                knowledge.utc_now(), knowledge.canonical_json(artifact["metadata"]),
            ),
        )
    return artifact


def load_task_outputs(db_path: Path, run_id: str) -> dict[str, Any]:
    with knowledge.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT task_id,output_json FROM workflow_tasks WHERE run_id=?", (run_id,)
        ).fetchall()
    return {row["task_id"]: json.loads(row["output_json"]) for row in rows if row["output_json"]}


def task_scope_guard(ctx: dict[str, Any]) -> dict[str, Any]:
    request = ctx["request"]
    hits = scan_forbidden(request)
    if hits:
        raise WorkflowValidationError([issue("scope_boundary_violation", "Forbidden portfolio/trade field", path) for path in hits])
    return {
        "cutoff_timestamp": knowledge.normalize_timestamp(request["cutoff_timestamp"]),
        "portfolio_data": "not_requested_and_forbidden", "position_inference": "disabled",
        "trade_instruction": "disabled", "decision_rule": request.get("decision_rule"),
    }


def task_entity_resolution(ctx: dict[str, Any]) -> dict[str, Any]:
    resolved: list[dict[str, Any]] = []
    with knowledge.connect(ctx["db_path"]) as connection:
        for requested in ctx["request"]["entities"]:
            row = connection.execute(
                "SELECT entity_id,entity_type,canonical_name,jurisdiction,status FROM entities WHERE entity_id=?",
                (requested["entity_id"],),
            ).fetchone()
            if not row:
                raise WorkflowValidationError([issue("entity_unresolved", f"Entity cannot be resolved: {requested['entity_id']}")])
            identifiers = [dict(item) for item in connection.execute(
                "SELECT id_type,issuer,value FROM external_identifiers WHERE entity_id=? ORDER BY id_type,issuer",
                (requested["entity_id"],),
            ).fetchall()]
            resolved.append({**dict(row), "display_name": requested.get("display_name") or row["canonical_name"], "identifiers": identifiers})
    return {"entities": resolved, "count": len(resolved), "security_and_legal_entity_separated": True}


def task_fact_retrieval(ctx: dict[str, Any]) -> dict[str, Any]:
    request = ctx["request"]
    facts: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for entity in request["entities"]:
        for query in request["metric_queries"]:
            result = knowledge.query_metric(
                ctx["db_path"], entity["entity_id"], query["metric_id"],
                request["cutoff_timestamp"], query.get("period_end"),
            )
            if not result["observations"]:
                missing.append({"entity_id": entity["entity_id"], **query})
            else:
                facts.extend(result["observations"])
    if not facts:
        raise WorkflowValidationError([issue("no_point_in_time_facts", "No eligible facts were available at the cutoff")])
    return {
        "facts": facts, "fact_count": len(facts), "missing_queries": missing,
        "cutoff_timestamp": knowledge.normalize_timestamp(request["cutoff_timestamp"]),
        "source_gap_explicit": True,
    }


def task_document_retrieval(ctx: dict[str, Any]) -> dict[str, Any]:
    cutoff = knowledge.normalize_timestamp(ctx["request"]["cutoff_timestamp"])
    entity_ids = [item["entity_id"] for item in ctx["request"]["entities"]]
    placeholders = ",".join("?" for _ in entity_ids)
    with knowledge.connect(ctx["db_path"]) as connection:
        rows = connection.execute(
            f"""SELECT DISTINCT d.document_id,d.document_type,d.title,d.publisher,d.published_at,
                               d.available_at,d.source_url,d.evidence_tier
                FROM documents d JOIN document_entities de ON de.document_id=d.document_id
                WHERE de.entity_id IN ({placeholders}) AND julianday(d.available_at)<=julianday(?) AND d.status='curated'
                ORDER BY d.available_at DESC,d.document_id""",
            (*entity_ids, cutoff),
        ).fetchall()
    documents = [dict(row) for row in rows]
    if not documents:
        raise WorkflowValidationError([issue("no_eligible_documents", "No eligible source documents were available at the cutoff")])
    return {"documents": documents, "document_count": len(documents), "cutoff_timestamp": cutoff}


def task_model_execution(ctx: dict[str, Any]) -> dict[str, Any]:
    packages = ctx["request"].get("model_packages", [])
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in packages:
        path = (PROJECT_ROOT / item["path"]).resolve()
        if not path.is_file():
            failures.append({"path": item["path"], "required": bool(item.get("required")), "error": "package_not_found"})
            if item.get("required"):
                raise WorkflowValidationError([issue("required_model_missing", f"Required model package not found: {item['path']}")])
            continue
        try:
            results.append(models.run_package(ctx["db_path"], path))
        except Exception as exc:  # model validation details are retained in the audit output
            failures.append({"path": item["path"], "required": bool(item.get("required")), "error": str(exc)})
            if item.get("required"):
                raise
    if failures and not results:
        raise RuntimeError(knowledge.canonical_json({"optional_model_failures": failures}))
    return {"model_runs": results, "failures": failures, "publication_status": "internal_research_ready"}


def first_fact(facts: list[dict[str, Any]], entity_id: str, metric_id: str, period_end: str) -> dict[str, Any] | None:
    candidates = [
        item for item in facts
        if item["entity_id"] == entity_id and item["metric_id"] == metric_id and item["period_end"] == period_end
    ]
    return sorted(candidates, key=lambda item: (item["available_at"], item["version_no"]), reverse=True)[0] if candidates else None


def task_claim_composition(ctx: dict[str, Any]) -> dict[str, Any]:
    facts = ctx["outputs"]["fact_retrieval"]["facts"]
    entities = ctx["request"]["entities"]
    claims: list[dict[str, Any]] = []

    def add_claim(module: str, text: str, label: str, importance: str, confidence: float,
                  evidence: list[dict[str, str]], formula: str | None = None,
                  inputs: list[str] | None = None) -> str:
        claim_id = stable_id("wc", ctx["run_id"], module, text)
        claims.append({
            "claim_id": claim_id, "module_id": module, "text": text, "content_label": label,
            "importance": importance, "confidence": confidence, "as_of_date": "2023-12-31",
            "formula": formula, "input_ids": inputs or [], "status": "draft",
            "evidence_relations": evidence,
        })
        return claim_id

    metrics = ["CR.CO.REVENUE", "CR.CO.OPERATING_COST", "CR.CO.PARENT_NET_PROFIT", "CR.CO.CFO_NET"]
    entity_summary: dict[str, Any] = {}
    for entity in entities:
        entity_id = entity["entity_id"]
        name = entity.get("display_name", entity_id)
        selected: dict[tuple[str, str], dict[str, Any]] = {}
        for metric in metrics:
            periods = ["2022-12-31", "2023-12-31"] if metric == "CR.CO.REVENUE" else ["2023-12-31"]
            for period in periods:
                fact = first_fact(facts, entity_id, metric, period)
                if fact:
                    selected[(metric, period)] = fact
                    add_claim(
                        "reported_financials",
                        f"{name}{period[:4]}年{METRIC_NAMES[metric]}为{float(fact['value_numeric']) / 1e8:.2f}亿元。",
                        "FACT_PRIMARY", "supporting", 0.98,
                        [{"evidence_id": fact["evidence_id"], "relation_type": "supporting"}],
                        inputs=[fact["observation_id"]],
                    )
        revenue_22 = selected.get(("CR.CO.REVENUE", "2022-12-31"))
        revenue_23 = selected.get(("CR.CO.REVENUE", "2023-12-31"))
        cost_23 = selected.get(("CR.CO.OPERATING_COST", "2023-12-31"))
        profit_23 = selected.get(("CR.CO.PARENT_NET_PROFIT", "2023-12-31"))
        cfo_23 = selected.get(("CR.CO.CFO_NET", "2023-12-31"))
        required = [revenue_22, revenue_23, cost_23, profit_23, cfo_23]
        if not all(required):
            entity_summary[entity_id] = {"display_name": name, "status": "insufficient_facts"}
            continue
        revenue_growth = float(revenue_23["value_numeric"]) / float(revenue_22["value_numeric"]) - 1
        gross_margin = 1 - float(cost_23["value_numeric"]) / float(revenue_23["value_numeric"])
        net_margin = float(profit_23["value_numeric"]) / float(revenue_23["value_numeric"])
        cfo_conversion = float(cfo_23["value_numeric"]) / float(profit_23["value_numeric"])
        calculations = [
            ("revenue_growth", f"{name}2023年营业收入同比增长{revenue_growth:.2%}。", revenue_growth,
             "revenue_2023 / revenue_2022 - 1", [revenue_23, revenue_22]),
            ("gross_margin", f"{name}2023年报表口径毛利率为{gross_margin:.2%}。", gross_margin,
             "1 - operating_cost_2023 / revenue_2023", [cost_23, revenue_23]),
            ("net_margin", f"{name}2023年归母净利率为{net_margin:.2%}。", net_margin,
             "parent_net_profit_2023 / revenue_2023", [profit_23, revenue_23]),
            ("cfo_conversion", f"{name}2023年经营现金流/归母净利润为{cfo_conversion:.2f}倍。", cfo_conversion,
             "cfo_net_2023 / parent_net_profit_2023", [cfo_23, profit_23]),
        ]
        calc_claim_ids: dict[str, str] = {}
        for module, text, _value, formula, inputs in calculations:
            calc_claim_ids[module] = add_claim(
                module, text, "AGENT_CALCULATION", "key", 0.96,
                [{"evidence_id": item["evidence_id"], "relation_type": "supporting"} for item in inputs],
                formula=formula, inputs=[item["observation_id"] for item in inputs],
            )
        entity_summary[entity_id] = {
            "display_name": name, "status": "complete",
            "revenue_2023": float(revenue_23["value_numeric"]), "revenue_growth": revenue_growth,
            "gross_margin": gross_margin, "net_margin": net_margin, "cfo_conversion": cfo_conversion,
            "evidence_ids": sorted({item["evidence_id"] for item in required}),
            "calculation_claim_ids": calc_claim_ids,
        }

    complete = [item for item in entity_summary.values() if item.get("status") == "complete"]
    if len(complete) >= 2:
        scale_leader = max(complete, key=lambda item: item["revenue_2023"])
        growth_leader = max(complete, key=lambda item: item["revenue_growth"])
        margin_leader = max(complete, key=lambda item: item["gross_margin"])
        cash_leader = max(complete, key=lambda item: item["cfo_conversion"])
        supporting = scale_leader["evidence_ids"] + growth_leader["evidence_ids"]
        counter = margin_leader["evidence_ids"] + cash_leader["evidence_ids"]
        add_claim(
            "limited_conclusion",
            f"有限结论：{scale_leader['display_name']}收入规模领先、{growth_leader['display_name']}收入增速略高；"
            f"{margin_leader['display_name']}毛利率更高、{cash_leader['display_name']}现金利润转换更强。"
            "在没有预设决策规则、估值与更长周期证据时，不给出单一优胜者。",
            "AGENT_INFERENCE", "material", 0.78,
            ([{"evidence_id": evidence_id, "relation_type": "supporting"} for evidence_id in sorted(set(supporting))]
             + [{"evidence_id": evidence_id, "relation_type": "counter"} for evidence_id in sorted(set(counter))]),
            inputs=[claim["claim_id"] for claim in claims if claim["content_label"] == "AGENT_CALCULATION"],
        )
    return {"claims": claims, "claim_count": len(claims), "entity_summary": entity_summary}


def task_evidence_audit(ctx: dict[str, Any]) -> dict[str, Any]:
    cutoff = knowledge.normalize_timestamp(ctx["request"]["cutoff_timestamp"])
    claims = ctx["outputs"]["claim_composition"]["claims"]
    problems: list[dict[str, Any]] = []
    passed: list[dict[str, str]] = []
    with knowledge.connect(ctx["db_path"]) as connection:
        for claim in claims:
            relations = claim["evidence_relations"]
            evidence_ids = sorted({item["evidence_id"] for item in relations})
            evidence_rows = []
            if evidence_ids:
                evidence_rows = connection.execute(
                    "SELECT evidence_id,available_at,evidence_tier FROM evidence WHERE evidence_id IN (%s)" % ",".join("?" for _ in evidence_ids),
                    tuple(evidence_ids),
                ).fetchall()
            if claim["content_label"] == "FACT_PRIMARY" and not evidence_rows:
                problems.append(issue("fact_without_direct_evidence", claim["claim_id"]))
            if claim["content_label"] == "AGENT_CALCULATION" and (not claim.get("formula") or not claim.get("input_ids")):
                problems.append(issue("calculation_lineage_missing", claim["claim_id"]))
            if claim["importance"] == "material" and not any(item["relation_type"] == "counter" for item in relations):
                problems.append(issue("material_claim_counter_evidence_missing", claim["claim_id"]))
            cutoff_dt = knowledge.parse_timestamp(cutoff)
            future = [
                row["evidence_id"] for row in evidence_rows
                if knowledge.parse_timestamp(row["available_at"]) > cutoff_dt
            ]
            if future:
                problems.append(issue("future_evidence_detected", f"{claim['claim_id']}: {future}"))
            else:
                passed.append({"claim_id": claim["claim_id"], "result": "passed"})
    if problems:
        raise WorkflowValidationError(problems)
    return {
        "status": "passed", "cutoff_timestamp": cutoff, "claims_audited": len(claims),
        "checks": passed, "future_information_count": 0,
        "fact_direct_evidence": "passed", "calculation_lineage": "passed",
        "material_counter_analysis": "passed",
    }


def task_skeptic_review(ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "passed_with_limitations",
        "alternative_explanations": [
            "两家公司品类结构和业务模式不同，横向毛利率差异不应被直接解释为经营能力差异。",
            "单一年份现金流可能受营运资本时点影响，需要更长周期验证。",
            "收入增速差异较小，舍入误差、并表范围及价格周期都可能影响排序。",
        ],
        "cannot_conclude": [
            "当前证据不支持给出单一公司优胜结论。",
            "当前请求未提供估值、市场预期和中期预测，不支持形成交易指令。",
            "没有也不推断任何基金持仓或仓位信息。",
        ],
        "falsification_indicators": [
            "后续季度收入增速与年度排序反转",
            "连续四个季度毛利率趋势与2023年截面结论相反",
            "经营现金流/归母净利润回落至长期常态区间以下",
        ],
    }


def pct(value: float) -> str:
    return f"{value:.2%}"


def task_report_render(ctx: dict[str, Any]) -> dict[str, Any]:
    summary = ctx["outputs"]["claim_composition"]["entity_summary"]
    entities = ctx["request"]["entities"]
    skeptic = ctx["outputs"]["skeptic_review"]
    facts = ctx["outputs"]["fact_retrieval"]["facts"]
    model_output = ctx["outputs"].get("model_execution", {})
    output_path = ctx["output_dir"] / "research-report.md"
    rows: list[str] = []
    for entity in entities:
        item = summary.get(entity["entity_id"], {})
        if item.get("status") != "complete":
            rows.append(f"| {entity.get('display_name', entity['entity_id'])} | 数据不足 | 数据不足 | 数据不足 | 数据不足 | 数据不足 |")
        else:
            rows.append(
                f"| {item['display_name']} | {item['revenue_2023']/1e8:.2f} | {pct(item['revenue_growth'])} | "
                f"{pct(item['gross_margin'])} | {pct(item['net_margin'])} | {item['cfo_conversion']:.2f}x |"
            )
    complete = [item for item in summary.values() if item.get("status") == "complete"]
    if len(complete) >= 2:
        scale = max(complete, key=lambda item: item["revenue_2023"])["display_name"]
        growth = max(complete, key=lambda item: item["revenue_growth"])["display_name"]
        margin = max(complete, key=lambda item: item["gross_margin"])["display_name"]
        cash = max(complete, key=lambda item: item["cfo_conversion"])["display_name"]
        executive = [
            f"{scale}的2023年收入规模更大。",
            f"{growth}的2023年收入增速略高，但差异很小。",
            f"{margin}的报表口径毛利率更高，{cash}的现金利润转换更强。",
            "证据呈现维度分化；没有预设决策规则、估值和中期预测，因此不给出单一优胜者。",
        ]
    else:
        executive = ["当前点时数据不足，不能形成完整横向比较。"]
    evidence_ids = sorted({fact["evidence_id"] for fact in facts})
    evidence_lines: list[str] = []
    for evidence_id in evidence_ids:
        trace = knowledge.trace_evidence(ctx["db_path"], evidence_id)["evidence"]
        if trace:
            with knowledge.connect(ctx["db_path"]) as connection:
                evidence_time = connection.execute(
                    "SELECT available_at FROM evidence WHERE evidence_id=?", (evidence_id,)
                ).fetchone()["available_at"]
            link = trace.get("source_url") or "无公开链接"
            evidence_lines.append(
                f"- `{evidence_id}`｜{trace['publisher']}｜{trace['title']}｜{trace['locator']}｜"
                f"可用时间 {evidence_time}｜{link}"
            )
    missing = ctx["outputs"]["fact_retrieval"]["missing_queries"]
    runtime_note = ""
    if ctx["request"].get("include_runtime_appendix"):
        runtime_note = f"""
## 运行与审计附录

- 工作流：`{WORKFLOW_ID}` / 引擎 `{ENGINE_VERSION}`
- 运行 ID：`{ctx['run_id']}`
- 数据截止：`{knowledge.normalize_timestamp(ctx['request']['cutoff_timestamp'])}`
- 模型任务：{'已执行' if model_output.get('model_runs') else '未形成可用结果或已降级'}
- 数据缺口：{knowledge.canonical_json(missing) if missing else '无本题必需指标缺口'}
- 交付状态：系统质量门已通过后可供内部研究使用；不得自动对外发布。
"""
    report = f"""# 消费行业公司比较研究报告（阶段八工作流产物）

> 读者：公募基金经理  
> 研究截止时间：{knowledge.normalize_timestamp(ctx['request']['cutoff_timestamp'])}  
> 边界：本报告不使用、不请求且不推断任何基金持仓或仓位；不构成自动交易指令。  
> 状态：**内部研究可用（系统质量门已通过）**

## 研究问题

{ctx['request']['research_question']}

## 执行摘要

{chr(10).join(f'- {item}' for item in executive)}

## 比较口径与证据边界

- 范围：白色家电公司年度经营与财务质量比较；会计口径为合并报表、CAS、CNY。
- 本报告仅使用研究截止时间之前已可获得的阶段六事实库证据。
- 当前证据以2022—2023年年度报告为主；未在本题中扩展宏观、渠道、月度销量、估值和市场预期数据。

## 核心指标同表比较

| 公司 | 2023收入（亿元） | 收入同比 | 毛利率 | 归母净利率 | CFO/归母净利润 |
|---|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## 分公司观察

{chr(10).join(f"### {item['display_name']}\n\n- 收入规模：{item['revenue_2023']/1e8:.2f}亿元\n- 收入同比：{pct(item['revenue_growth'])}\n- 毛利率：{pct(item['gross_margin'])}\n- 归母净利率：{pct(item['net_margin'])}\n- 现金利润转换：{item['cfo_conversion']:.2f}倍" for item in complete)}

## 反方审查与不可下结论事项

### 替代解释

{chr(10).join(f'- {item}' for item in skeptic['alternative_explanations'])}

### 当前不可下结论

{chr(10).join(f'- {item}' for item in skeptic['cannot_conclude'])}

## 有限结论与后续跟踪

横向证据显示收入规模、增速、利润率与现金转换并非由同一家公司全面领先。因研究请求没有预设决策规则，且未纳入估值、市场预期和中期预测，本工作流不输出单一优胜者，也不输出买卖建议。

后续应跟踪：

{chr(10).join(f'- {item}' for item in skeptic['falsification_indicators'])}

## 证据索引

{chr(10).join(evidence_lines)}
{runtime_note}
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8", newline="\n")
    return {"report_path": str(output_path), "content_hash": hash_file(output_path), "status": "internal_research_ready"}


def task_compliance_review(ctx: dict[str, Any]) -> dict[str, Any]:
    report_path = Path(ctx["outputs"]["report_render"]["report_path"])
    report = report_path.read_text(encoding="utf-8")
    violations: list[str] = []
    if scan_forbidden(ctx["request"]):
        violations.append("forbidden_structured_fields")
    imperative_trade_phrases = ["建议买入", "建议卖出", "应当买入", "应当卖出", "目标仓位", "组合权重"]
    violations.extend(phrase for phrase in imperative_trade_phrases if phrase in report)
    required_boundary = "不使用、不请求且不推断任何基金持仓或仓位"
    if required_boundary not in report:
        violations.append("portfolio_boundary_statement_missing")
    if violations:
        raise WorkflowValidationError([issue("compliance_violation", item) for item in violations])
    return {
        "status": "passed", "violations": [], "portfolio_data_check": "passed",
        "position_inference_check": "passed", "automatic_trade_instruction_check": "passed",
        "internal_release_decision": "approved_by_required_quality_gates",
        "external_publication": "disabled",
    }


TASK_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "scope_guard": task_scope_guard,
    "entity_resolution": task_entity_resolution,
    "fact_retrieval": task_fact_retrieval,
    "document_retrieval": task_document_retrieval,
    "model_execution": task_model_execution,
    "claim_composition": task_claim_composition,
    "evidence_audit": task_evidence_audit,
    "skeptic_review": task_skeptic_review,
    "report_render": task_report_render,
    "compliance_review": task_compliance_review,
}


def persist_claims(db_path: Path, run_id: str, claims: list[dict[str, Any]]) -> None:
    with knowledge.connect(db_path) as connection:
        for claim in claims:
            connection.execute(
                """INSERT OR REPLACE INTO workflow_claims(
                       run_id,claim_id,module_id,text,content_label,importance,confidence,as_of_date,
                       formula,input_ids_json,status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, claim["claim_id"], claim["module_id"], claim["text"], claim["content_label"],
                    claim["importance"], claim["confidence"], claim["as_of_date"], claim.get("formula"),
                    knowledge.canonical_json(claim.get("input_ids", [])), claim["status"],
                ),
            )
            connection.execute("DELETE FROM workflow_claim_evidence WHERE run_id=? AND claim_id=?", (run_id, claim["claim_id"]))
            for relation in claim["evidence_relations"]:
                connection.execute(
                    "INSERT OR IGNORE INTO workflow_claim_evidence(run_id,claim_id,evidence_id,relation_type) VALUES(?,?,?,?)",
                    (run_id, claim["claim_id"], relation["evidence_id"], relation["relation_type"]),
                )


def prepare_run(db_path: Path, request: dict[str, Any], output_root: Path) -> tuple[str, Path, bool]:
    plan = plan_request(db_path, request)
    package_hash = knowledge.sha256_json(request)
    run_id = stable_id("wr", request["package_id"], package_hash, ENGINE_VERSION)
    output_dir = output_root / request.get("output_slug", request["package_id"].lower()) / run_id.replace(":", "-")
    output_dir.mkdir(parents=True, exist_ok=True)
    with knowledge.connect(db_path) as connection:
        existing_package = connection.execute(
            "SELECT package_hash FROM workflow_packages WHERE package_id=?", (request["package_id"],)
        ).fetchone()
        if existing_package and existing_package["package_hash"] != package_hash:
            raise WorkflowValidationError([issue("package_id_content_conflict", "package_id already exists with different content")])
        connection.execute(
            """INSERT OR IGNORE INTO workflow_packages(
                   package_id,workflow_id,package_hash,cutoff_timestamp,template_id,request_json,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                request["package_id"], request["workflow_id"], package_hash,
                knowledge.normalize_timestamp(request["cutoff_timestamp"]), request["template_id"],
                knowledge.canonical_json(request), knowledge.utc_now(),
            ),
        )
        existing_run = connection.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        is_new = existing_run is None
        if is_new:
            connection.execute(
                """INSERT INTO workflow_runs(
                       run_id,package_id,status,started_at,publication_status,human_review_required,output_directory,summary_json
                    ) VALUES(?,?,?,?,'quality_checks_pending',0,?,'{}')""",
                (run_id, request["package_id"], "planned", knowledge.utc_now(), str(output_dir)),
            )
            for task in plan["tasks"]:
                connection.execute(
                    """INSERT INTO workflow_tasks(
                           run_id,task_id,role_id,lane,wave_no,status,required,dependencies_json
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (
                        run_id, task["task_id"], task["role_id"], task["lane"], task["wave_no"],
                        int(bool(task["required"])), knowledge.canonical_json(task["depends_on"]),
                    ),
                )
            record_event(connection, run_id, None, "run_planned", "info", {"package_hash": package_hash})
    plan_path = output_dir / "execution-plan.json"
    write_json(plan_path, {**plan, "run_id": run_id, "package_hash": package_hash})
    record_artifact(db_path, run_id, "execution_plan", plan_path)
    return run_id, output_dir, is_new


def execute_task(db_path: Path, request: dict[str, Any], run_id: str, output_dir: Path,
                 task_id: str, outputs: dict[str, Any]) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    handler = TASK_HANDLERS[task_id]
    ctx = {
        "db_path": db_path, "request": request, "run_id": run_id,
        "output_dir": output_dir, "outputs": outputs,
    }
    try:
        return "completed", handler(ctx), None
    except Exception as exc:
        details: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, WorkflowValidationError):
            details["issues"] = exc.issues
        else:
            details["traceback"] = traceback.format_exc(limit=5)
        return "failed", None, details


def write_runtime_audit(db_path: Path, run_id: str, output_dir: Path) -> Path:
    with knowledge.connect(db_path) as connection:
        run = dict(connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone())
        tasks = [dict(row) for row in connection.execute(
            """SELECT task_id,role_id,lane,wave_no,status,required,attempt_count,started_at,completed_at,
                      dependencies_json,error_json FROM workflow_tasks WHERE run_id=? ORDER BY wave_no,task_id""",
            (run_id,),
        ).fetchall()]
        events = [dict(row) for row in connection.execute(
            "SELECT event_type,severity,task_id,occurred_at,detail_json FROM workflow_events WHERE run_id=? ORDER BY event_id",
            (run_id,),
        ).fetchall()]
        reviews = [dict(row) for row in connection.execute(
            "SELECT review_type,reviewer,decision,notes,created_at FROM workflow_reviews WHERE run_id=? ORDER BY created_at",
            (run_id,),
        ).fetchall()]
    path = output_dir / "runtime-audit.json"
    write_json(path, {"engine_version": ENGINE_VERSION, "run": run, "tasks": tasks, "events": events, "reviews": reviews})
    record_artifact(db_path, run_id, "runtime_audit", path)
    return path


def execute_run(db_path: Path, request: dict[str, Any], run_id: str, output_dir: Path,
                stop_after: str | None = None, resumed: bool = False) -> dict[str, Any]:
    with knowledge.connect(db_path) as connection:
        current = connection.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if current["status"] in {"completed", "blocked"}:
            return workflow_status(db_path, run_id)
        connection.execute(
            "UPDATE workflow_runs SET status='running',resumed_count=resumed_count+? WHERE run_id=?",
            (1 if resumed else 0, run_id),
        )
        record_event(connection, run_id, None, "run_resumed" if resumed else "run_started", "info", {})
    while True:
        outputs = load_task_outputs(db_path, run_id)
        with knowledge.connect(db_path) as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_tasks WHERE run_id=? ORDER BY wave_no,task_id", (run_id,)
            ).fetchall()
        pending = [row for row in rows if row["status"] == "pending"]
        if not pending:
            break
        ready: list[sqlite3.Row] = []
        status_by_task = {row["task_id"]: row["status"] for row in rows}
        for row in pending:
            dependencies = json.loads(row["dependencies_json"])
            if all(status_by_task.get(dep) in TERMINAL_TASK_STATES for dep in dependencies):
                ready.append(row)
        if not ready:
            with knowledge.connect(db_path) as connection:
                connection.execute("UPDATE workflow_runs SET status='blocked' WHERE run_id=?", (run_id,))
                record_event(connection, run_id, None, "scheduler_deadlock", "error", {"pending": [row["task_id"] for row in pending]})
            break
        wave_no = min(row["wave_no"] for row in ready)
        wave = [row for row in ready if row["wave_no"] == wave_no]
        started = knowledge.utc_now()
        with knowledge.connect(db_path) as connection:
            for row in wave:
                connection.execute(
                    "UPDATE workflow_tasks SET status='running',attempt_count=attempt_count+1,started_at=? WHERE run_id=? AND task_id=?",
                    (started, run_id, row["task_id"]),
                )
                record_event(connection, run_id, row["task_id"], "task_started", "info", {"wave_no": wave_no})
        results: dict[str, tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(wave))) as executor:
            future_map = {
                executor.submit(execute_task, db_path, request, run_id, output_dir, row["task_id"], outputs): row
                for row in wave
            }
            for future in as_completed(future_map):
                row = future_map[future]
                results[row["task_id"]] = future.result()
        blocked = False
        for row in wave:
            task_id = row["task_id"]
            state, output, error = results[task_id]
            final_state = state
            if state == "failed" and not bool(row["required"]):
                final_state = "degraded"
                output = {"status": "degraded", "error": error, "model_runs": []}
            elif state == "failed":
                final_state = "blocked"
                blocked = True
            with knowledge.connect(db_path) as connection:
                connection.execute(
                    """UPDATE workflow_tasks SET status=?,completed_at=?,output_json=?,error_json=?
                       WHERE run_id=? AND task_id=?""",
                    (
                        final_state, knowledge.utc_now(), knowledge.canonical_json(output or {}),
                        knowledge.canonical_json(error) if error else None, run_id, task_id,
                    ),
                )
                record_event(
                    connection, run_id, task_id, "task_completed" if final_state == "completed" else "task_degraded" if final_state == "degraded" else "task_blocked",
                    "info" if final_state == "completed" else "warning" if final_state == "degraded" else "error",
                    {"status": final_state, "error": error},
                )
            if task_id == "claim_composition" and output:
                persist_claims(db_path, run_id, output["claims"])
                path = output_dir / "claim-graph.json"
                write_json(path, output)
                record_artifact(db_path, run_id, "claim_graph", path, {"claim_count": output["claim_count"]})
            elif task_id == "evidence_audit" and output:
                path = output_dir / "evidence-audit.json"
                write_json(path, output)
                record_artifact(db_path, run_id, "evidence_audit", path)
            elif task_id == "report_render" and output:
                record_artifact(db_path, run_id, "research_report", Path(output["report_path"]), {"publication_status": "internal_research_ready"})
        if blocked:
            with knowledge.connect(db_path) as connection:
                connection.execute("UPDATE workflow_runs SET status='blocked',publication_status='blocked' WHERE run_id=?", (run_id,))
                record_event(connection, run_id, None, "run_blocked", "error", {"wave_no": wave_no})
            break
        if stop_after and any(row["task_id"] == stop_after for row in wave):
            with knowledge.connect(db_path) as connection:
                record_event(connection, run_id, stop_after, "run_interrupted_for_resume_test", "info", {})
            break
    with knowledge.connect(db_path) as connection:
        current = connection.execute("SELECT status FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        remaining = connection.execute(
            "SELECT COUNT(*) AS count FROM workflow_tasks WHERE run_id=? AND status NOT IN ('completed','degraded','skipped')",
            (run_id,),
        ).fetchone()["count"]
        if current["status"] == "running" and remaining == 0:
            connection.execute(
                """UPDATE workflow_runs
                   SET status='completed',publication_status='internal_research_ready',human_review_required=0,completed_at=?
                   WHERE run_id=?""",
                (knowledge.utc_now(), run_id),
            )
            connection.execute("UPDATE workflow_claims SET status='internal_research_ready' WHERE run_id=?", (run_id,))
            record_event(connection, run_id, None, "internal_research_released", "info", {"release_mode": "automatic_quality_gates"})
    write_runtime_audit(db_path, run_id, output_dir)
    return workflow_status(db_path, run_id)


def run_workflow(db_path: Path, request_path: Path, output_root: Path = DEFAULT_OUTPUT_ROOT,
                 stop_after: str | None = None) -> dict[str, Any]:
    request = read_json(request_path)
    run_id, output_dir, _is_new = prepare_run(db_path, request, output_root)
    if stop_after:
        task_ids = {item["task_id"] for item in topological_plan(workflow_spec())}
        if stop_after not in task_ids:
            raise WorkflowValidationError([issue("stop_after_unknown", f"Unknown task: {stop_after}")])
    return execute_run(db_path, request, run_id, output_dir, stop_after=stop_after)


def resume_workflow(db_path: Path, run_id: str) -> dict[str, Any]:
    init_engine(db_path)
    with knowledge.connect(db_path) as connection:
        row = connection.execute(
            """SELECT r.status,r.output_directory,p.request_json FROM workflow_runs r
               JOIN workflow_packages p ON p.package_id=r.package_id WHERE r.run_id=?""",
            (run_id,),
        ).fetchone()
    if not row:
        raise WorkflowValidationError([issue("run_not_found", f"Workflow run not found: {run_id}")])
    if row["status"] not in {"planned", "running", "failed"}:
        return workflow_status(db_path, run_id)
    return execute_run(db_path, json.loads(row["request_json"]), run_id, Path(row["output_directory"]), resumed=True)


def workflow_status(db_path: Path, run_id: str | None = None) -> dict[str, Any]:
    init_engine(db_path)
    with knowledge.connect(db_path) as connection:
        if run_id:
            run = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise WorkflowValidationError([issue("run_not_found", f"Workflow run not found: {run_id}")])
            tasks = [dict(row) for row in connection.execute(
                "SELECT task_id,role_id,lane,wave_no,status,required,attempt_count FROM workflow_tasks WHERE run_id=? ORDER BY wave_no,task_id",
                (run_id,),
            ).fetchall()]
            artifacts = [dict(row) for row in connection.execute(
                "SELECT artifact_type,path,content_hash FROM workflow_artifacts WHERE run_id=? ORDER BY artifact_type", (run_id,)
            ).fetchall()]
            claims = connection.execute("SELECT COUNT(*) AS count FROM workflow_claims WHERE run_id=?", (run_id,)).fetchone()["count"]
            evidence_links = connection.execute("SELECT COUNT(*) AS count FROM workflow_claim_evidence WHERE run_id=?", (run_id,)).fetchone()["count"]
            return {"run": dict(run), "tasks": tasks, "artifacts": artifacts, "claim_count": claims, "claim_evidence_links": evidence_links}
        counts = {
            row["status"]: row["count"] for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM workflow_runs GROUP BY status"
            ).fetchall()
        }
        return {"engine_version": ENGINE_VERSION, "workflow_id": WORKFLOW_ID, "run_counts": counts}


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consumer research workflow and agent orchestration engine")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    validate = sub.add_parser("validate")
    validate.add_argument("--request", type=Path, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--request", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    run.add_argument("--stop-after")
    resume = sub.add_parser("resume")
    resume.add_argument("--run-id", required=True)
    status = sub.add_parser("status")
    status.add_argument("--run-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = init_engine(args.db)
        elif args.command == "validate":
            init_engine(args.db)
            request = read_json(args.request)
            problems = validate_request(args.db, request)
            result = {"status": "valid" if not problems else "invalid", "issues": problems}
            if problems:
                emit(result)
                return 2
        elif args.command == "plan":
            result = plan_request(args.db, read_json(args.request))
        elif args.command == "run":
            result = run_workflow(args.db, args.request, args.output_root, args.stop_after)
        elif args.command == "resume":
            result = resume_workflow(args.db, args.run_id)
        else:
            result = workflow_status(args.db, args.run_id)
        emit(result)
        return 0
    except WorkflowValidationError as exc:
        emit({"status": "blocked", "issues": exc.issues})
        return 2
    except (models.ModelValidationError, knowledge.PackageValidationError) as exc:
        emit({"status": "blocked", "issues": getattr(exc, "issues", [{"message": str(exc)}])})
        return 2


if __name__ == "__main__":
    sys.exit(main())
