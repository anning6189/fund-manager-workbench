#!/usr/bin/env python3
"""Product-grade research task library for the full consumer agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import consumer_data_production as production
import consumer_knowledge_store as knowledge
import consumer_realtime_monitor as monitoring
import consumer_workflow_engine as workflow
import full_consumer_coverage as coverage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
SPEC_PATH = PROJECT_ROOT / "specs" / "products" / "consumer-research-task-library.v1.json"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "007_consumer_research_task_library.sql"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "task-library" / "module4-product"
ENGINE_VERSION = "1.1.0"


class TaskLibraryValidationError(ValueError):
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


def task_library_spec() -> dict[str, Any]:
    return read_json(SPEC_PATH)


def parameter_schema(template: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "cutoff_timestamp": {"type": "string", "format": "date-time-with-timezone"},
        "research_question": {"type": "string", "minLength": 5},
        "entities": {"type": "array", "items": {"required": ["entity_id"], "optional": ["security_id", "display_name"]}},
        "period_ends": {"type": "array", "items": {"type": "string", "format": "date"}},
        "metric_queries": {"type": "array", "items": {"required": ["metric_id"], "optional": ["period_end"]}},
        "markets": {"type": "array", "items": {"enum": ["A_SHARE", "HK_SHARE"]}},
        "geographies": {"type": "array", "items": {"type": "string"}},
        "lookback_days": {"type": "integer", "minimum": 1, "maximum": 3650},
        "decision_rule": {"type": ["object", "null"]},
        "event_id": {"type": ["string", "null"]},
        "monitor_alert_id": {"type": ["string", "null"]},
        "valuation_methods": {"type": "array", "items": {"type": "string"}},
        "scenario_assumptions": {"type": "object"},
    }
    required = ["cutoff_timestamp", "research_question"]
    if template["entity_requirement"] in {"one_or_more", "two_or_more"}:
        required.append("entities")
    return {
        "type": "object", "required": required,
        "allowed": template["parameter_fields"],
        "properties": {key: properties[key] for key in set(template["parameter_fields"] + ["entities", "metric_queries", "decision_rule"]) if key in properties},
        "additionalProperties": False,
    }


def init_library(db_path: Path, output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    monitoring.init_monitor(db_path)
    spec = task_library_spec()
    names, _, _ = coverage.domain_index()
    now = knowledge.utc_now()
    template_by_id = {item["template_id"]: item for item in spec["templates"]}
    with knowledge.connect(db_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for template in spec["templates"]:
            schema = parameter_schema(template)
            connection.execute(
                """INSERT INTO task_library_templates(
                       template_id,name,category,description,entity_requirement,parameter_schema_json,
                       output_contract_json,default_priority,expected_minutes,tags_json,version,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'internal_active',?,?)
                   ON CONFLICT(template_id) DO UPDATE SET
                       name=excluded.name,category=excluded.category,description=excluded.description,
                       entity_requirement=excluded.entity_requirement,parameter_schema_json=excluded.parameter_schema_json,
                       output_contract_json=excluded.output_contract_json,default_priority=excluded.default_priority,
                       expected_minutes=excluded.expected_minutes,tags_json=excluded.tags_json,updated_at=excluded.updated_at""",
                (
                    template["template_id"], template["name"], template["category"], template["description"],
                    template["entity_requirement"], canonical(schema), canonical(template["output_sections"]),
                    template["default_priority"], template["expected_minutes"], canonical(template["tags"]),
                    spec["version"], now, now,
                ),
            )
        for role, capabilities in spec["roles"].items():
            for capability in capabilities:
                connection.execute(
                    "INSERT OR REPLACE INTO task_library_role_permissions(role_id,capability,allowed,created_at) VALUES(?,?,1,?)",
                    (role, capability, now),
                )
        for view in spec["saved_views"]:
            connection.execute(
                """INSERT OR REPLACE INTO task_library_saved_views(
                       view_id,name,owner_type,owner_id,filter_json,is_system,status,created_at,updated_at
                   ) VALUES(?,?,'system','research_product',?,1,'active',?,?)""",
                (view["view_id"], view["name"], canonical(view["filter"]), now, now),
            )
        packages = connection.execute("SELECT * FROM research_task_packages ORDER BY sector_code,template_id").fetchall()
        if len(packages) != 110:
            raise TaskLibraryValidationError([issue("source_task_count_invalid", f"Expected 110 module-2 task packages, got {len(packages)}")])
        connection.execute("DELETE FROM task_library_products_fts")
        for package in packages:
            template = template_by_id[package["template_id"]]
            sector_name = names[package["sector_code"]]
            product_id = stable_id("product", package["sector_code"], package["template_id"])
            title = f"{sector_name} · {template['name']}"
            tags = sorted(set(template["tags"] + [sector_name, package["sector_code"], template["category"]]))
            search_text = " ".join([title, template["description"], package["research_question"], *tags])
            schema = parameter_schema(template)
            data_readiness = "ready_with_data_gaps" if package["status"] == "ready_with_data_gaps" else package["status"]
            product_snapshot = {
                "product_id": product_id, "task_package_id": package["task_package_id"],
                "template_id": package["template_id"], "sector_code": package["sector_code"],
                "title": title, "description": template["description"],
                "research_question_template": package["research_question"],
                "metrics": json.loads(package["metric_queries_json"]),
                "source_routes": json.loads(package["source_routes_json"]),
                "quality_gates": json.loads(package["quality_gates_json"]),
                "parameter_schema": schema, "tags": tags, "data_readiness": data_readiness,
                "portfolio_boundary": "no_fund_holdings_or_position_inference",
            }
            content_hash = knowledge.sha256_json(product_snapshot)
            connection.execute(
                """INSERT INTO task_library_products(
                       product_id,task_package_id,template_id,sector_code,title,short_description,
                       research_question_template,metric_ids_json,source_routes_json,quality_gates_json,
                       parameter_schema_json,tags_json,search_text,version,status,visibility,data_readiness,
                       owner_role,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'internal_active','internal',?,'research_operator',?,?)
                   ON CONFLICT(product_id) DO UPDATE SET
                       task_package_id=excluded.task_package_id,
                       title=CASE WHEN task_library_products.version=excluded.version THEN excluded.title ELSE task_library_products.title END,
                       short_description=CASE WHEN task_library_products.version=excluded.version THEN excluded.short_description ELSE task_library_products.short_description END,
                       research_question_template=CASE WHEN task_library_products.version=excluded.version THEN excluded.research_question_template ELSE task_library_products.research_question_template END,
                       metric_ids_json=excluded.metric_ids_json,source_routes_json=excluded.source_routes_json,
                       quality_gates_json=excluded.quality_gates_json,
                       parameter_schema_json=CASE WHEN task_library_products.version=excluded.version THEN excluded.parameter_schema_json ELSE task_library_products.parameter_schema_json END,
                       tags_json=CASE WHEN task_library_products.version=excluded.version THEN excluded.tags_json ELSE task_library_products.tags_json END,
                       search_text=CASE WHEN task_library_products.version=excluded.version THEN excluded.search_text ELSE task_library_products.search_text END,
                       data_readiness=excluded.data_readiness,
                       updated_at=excluded.updated_at""",
                (
                    product_id, package["task_package_id"], package["template_id"], package["sector_code"], title,
                    template["description"], package["research_question"], package["metric_queries_json"],
                    package["source_routes_json"], package["quality_gates_json"], canonical(schema), canonical(tags),
                    search_text, spec["version"], data_readiness, now, now,
                ),
            )
            version_id = stable_id("product-version", product_id, spec["version"], content_hash)
            connection.execute(
                """INSERT OR IGNORE INTO task_library_product_versions(
                       product_version_id,product_id,version,content_hash,snapshot_json,release_status,
                       released_by,released_at,release_notes
                   ) VALUES(?,?,?,?,?,'internal_active','module2_acceptance',?,'Initial productization from the accepted 110-task contract')""",
                (version_id, product_id, spec["version"], content_hash, canonical(product_snapshot), now),
            )
            connection.execute(
                "INSERT INTO task_library_products_fts(product_id,title,short_description,search_text,tags) VALUES(?,?,?,?,?)",
                (product_id, title, template["description"], search_text, " ".join(tags)),
            )
    catalog = export_catalog(db_path, output_root)
    return {
        "status": "ready", "engine_version": ENGINE_VERSION, "database": str(db_path),
        "templates": len(spec["templates"]), "sector_products": 110,
        "roles": len(spec["roles"]), "saved_views": len(spec["saved_views"]),
        "catalog": catalog, "visibility": "internal",
    }


def export_catalog(db_path: Path, output_root: Path = DEFAULT_OUTPUT) -> dict[str, str]:
    with knowledge.connect(db_path) as connection:
        products = [dict(row) for row in connection.execute(
            """SELECT p.product_id,p.title,p.sector_code,p.template_id,t.name AS template_name,
                      t.category,t.description,t.expected_minutes,p.status,p.visibility,p.data_readiness,
                      p.tags_json,p.parameter_schema_json
               FROM task_library_products p JOIN task_library_templates t ON t.template_id=p.template_id
               ORDER BY p.sector_code,p.template_id"""
        ).fetchall()]
    for item in products:
        item["tags"] = json.loads(item.pop("tags_json"))
        item["parameter_schema"] = json.loads(item.pop("parameter_schema_json"))
    payload = {
        "spec_id": task_library_spec()["spec_id"], "generated_at": knowledge.utc_now(),
        "product_count": len(products), "products": products,
        "boundaries": ["internal_only", "data_readiness_explicit", "no_fund_holdings_or_position_inference", "automatic_internal_release_after_quality_gates"],
    }
    json_path = output_root / "catalog" / "research-task-library.json"
    write_json(json_path, payload)
    md_path = output_root / "catalog" / "research-task-library.md"
    categories = Counter(item["category"] for item in products)
    lines = [
        "# 全消费行业研究任务库", "", f"> 任务产品：{len(products)} 个；仅供内部研究使用。", "",
        "## 产品分类", "", *[f"- {category}: {count}" for category, count in sorted(categories.items())], "",
        "## 使用边界", "", "- 任务上线不代表数据已齐备。", "- 研究结果在系统质量门通过后直接展示，不设置人工或外部审核。",
        "- 不使用或推断基金持仓、仓位。", "- 不自动生成交易指令。", "", "## 任务清单", "",
    ]
    lines.extend(f"- `{item['product_id']}`｜{item['title']}｜{item['category']}｜{item['data_readiness']}" for item in products)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path)}


def has_permission(connection: Any, role: str, capability: str) -> bool:
    row = connection.execute(
        "SELECT allowed FROM task_library_role_permissions WHERE role_id=? AND capability=?", (role, capability)
    ).fetchone()
    return bool(row and row["allowed"])


def log_usage(connection: Any, user_id: str, role: str, event_type: str,
              product_id: str | None, detail: dict[str, Any]) -> None:
    now = knowledge.utc_now()
    connection.execute(
        """INSERT INTO task_library_usage_events(
               usage_event_id,user_id,user_role,event_type,product_id,occurred_at,detail_json
           ) VALUES(?,?,?,?,?,?,?)""",
        (stable_id("usage", user_id, event_type, product_id or "ALL", now), user_id, role, event_type, product_id, now, canonical(detail)),
    )


def search_library(db_path: Path, query: str = "", sector_code: str | None = None,
                   category: str | None = None, template_id: str | None = None,
                   role: str = "public_fund_manager", user_id: str = "anonymous") -> dict[str, Any]:
    init_library(db_path)
    with knowledge.connect(db_path) as connection:
        if not has_permission(connection, role, "search"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot search: {role}")])
        conditions = ["p.status='internal_active'"]
        params: list[Any] = []
        if sector_code:
            conditions.append("p.sector_code=?"); params.append(sector_code)
        if category:
            conditions.append("t.category=?"); params.append(category)
        if template_id:
            conditions.append("p.template_id=?"); params.append(template_id)
        if query:
            conditions.append("(p.title LIKE ? OR p.short_description LIKE ? OR p.search_text LIKE ?)")
            term = f"%{query}%"; params.extend([term, term, term])
        rows = connection.execute(
            f"""SELECT p.product_id,p.title,p.sector_code,p.template_id,t.name AS template_name,
                       t.category,t.expected_minutes,p.data_readiness,p.tags_json,
                       CASE WHEN f.product_id IS NULL THEN 0 ELSE 1 END AS is_favorite
                FROM task_library_products p JOIN task_library_templates t ON t.template_id=p.template_id
                LEFT JOIN task_library_favorites f ON f.product_id=p.product_id AND f.user_id=?
                WHERE {' AND '.join(conditions)} ORDER BY p.sector_code,p.template_id""",
            (user_id, *params),
        ).fetchall()
        results = [dict(row) for row in rows]
        for item in results: item["tags"] = json.loads(item.pop("tags_json"))
        facets = {
            "sectors": dict(Counter(item["sector_code"] for item in results)),
            "categories": dict(Counter(item["category"] for item in results)),
            "templates": dict(Counter(item["template_id"] for item in results)),
            "data_readiness": dict(Counter(item["data_readiness"] for item in results)),
        }
        log_usage(connection, user_id, role, "search", None, {"query": query, "filters": {"sector_code": sector_code, "category": category, "template_id": template_id}, "result_count": len(results)})
    return {"query": query, "filters": {"sector_code": sector_code, "category": category, "template_id": template_id}, "result_count": len(results), "facets": facets, "results": results}


def product_detail(db_path: Path, product_id: str, role: str = "public_fund_manager",
                   user_id: str = "anonymous") -> dict[str, Any]:
    init_library(db_path)
    with knowledge.connect(db_path) as connection:
        if not has_permission(connection, role, "view"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot view: {role}")])
        row = connection.execute(
            """SELECT p.*,t.name AS template_name,t.category,t.entity_requirement,t.output_contract_json,
                      t.default_priority,t.expected_minutes
               FROM task_library_products p JOIN task_library_templates t ON t.template_id=p.template_id
               WHERE p.product_id=?""",
            (product_id,),
        ).fetchone()
        if not row:
            raise TaskLibraryValidationError([issue("product_not_found", f"Unknown product: {product_id}")])
        value = dict(row)
        for key in ("metric_ids_json", "source_routes_json", "quality_gates_json", "parameter_schema_json", "tags_json", "output_contract_json"):
            value[key.removesuffix("_json")] = json.loads(value.pop(key))
        versions = [dict(item) for item in connection.execute(
            "SELECT product_version_id,version,content_hash,release_status,released_by,released_at,release_notes FROM task_library_product_versions WHERE product_id=? ORDER BY released_at DESC",
            (product_id,),
        ).fetchall()]
        log_usage(connection, user_id, role, "view_product", product_id, {})
    return {"product": value, "versions": versions}


def validate_parameters(db_path: Path, product: dict[str, Any], params: dict[str, Any]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    schema = product["parameter_schema"]
    for key in schema["required"]:
        if key not in params or params[key] in (None, "", []):
            problems.append(issue("parameter_required", f"Required parameter is missing: {key}", f"$.{key}"))
    allowed = set(schema["allowed"]) | {"entities", "metric_queries", "decision_rule", "priority"}
    for key in params:
        if key not in allowed:
            problems.append(issue("parameter_not_allowed", f"Parameter is not allowed for this template: {key}", f"$.{key}"))
    for path in production.scan_forbidden(params):
        problems.append(issue("portfolio_field_forbidden", "Fund holdings and position inference are forbidden", path))
    try:
        cutoff = datetime.fromisoformat(str(params.get("cutoff_timestamp", "")).replace("Z", "+00:00"))
        if cutoff.tzinfo is None: raise ValueError
    except ValueError:
        problems.append(issue("cutoff_invalid", "cutoff_timestamp must be ISO-8601 with timezone", "$.cutoff_timestamp"))
    entities = params.get("entities", [])
    requirement = product["entity_requirement"]
    minimum = 2 if requirement == "two_or_more" else 1 if requirement == "one_or_more" else 0
    if len(entities) < minimum:
        problems.append(issue("entity_count_invalid", f"Template requires at least {minimum} entities", "$.entities"))
    ids = [item.get("entity_id") for item in entities if isinstance(item, dict)]
    if len(ids) != len(entities) or any(not item for item in ids):
        problems.append(issue("entity_invalid", "Every entity requires entity_id", "$.entities"))
    if ids:
        with knowledge.connect(db_path) as connection:
            known = {row["entity_id"] for row in connection.execute(
                f"SELECT entity_id FROM entities WHERE entity_id IN ({','.join('?' for _ in ids)})", ids
            ).fetchall()}
        for entity_id in sorted(set(ids) - known):
            problems.append(issue("entity_not_registered", f"Unknown entity: {entity_id}", "$.entities"))
    return problems


def compile_workflow_request(product: dict[str, Any], params: dict[str, Any], job_id: str) -> dict[str, Any]:
    period_ends = params.get("period_ends", [])
    metric_queries = params.get("metric_queries")
    if metric_queries is None:
        if period_ends:
            metric_queries = [{"metric_id": metric_id, "period_end": period} for metric_id in product["metric_ids"] for period in period_ends]
        else:
            metric_queries = [{"metric_id": metric_id} for metric_id in product["metric_ids"]]
    return {
        "package_id": f"CR.TASKLIB.{job_id.replace(':', '.').upper()}",
        "workflow_id": workflow.WORKFLOW_ID, "template_id": product["template_id"],
        "cutoff_timestamp": params["cutoff_timestamp"], "reader": "public_fund_manager",
        "research_question": params.get("research_question") or product["research_question_template"],
        "scope": {
            "sector_codes": [product["sector_code"]], "geographies": params.get("geographies", ["CN"]),
            "markets": params.get("markets", ["A_SHARE"]), "accounting_standard": "CAS",
            "consolidation_scope": "consolidated", "currency": "CNY",
        },
        "entities": params.get("entities", []), "metric_queries": metric_queries,
        "model_packages": [], "decision_rule": params.get("decision_rule"),
        "include_runtime_appendix": True, "output_slug": f"task-library/{job_id.replace(':', '-')}",
    }


def submit_job(db_path: Path, product_id: str, parameters_path: Path, submitted_by: str,
               role: str, output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    if not submitted_by.strip() or submitted_by.strip().lower() in {"ai", "agent", "anonymous", "unassigned"}:
        raise TaskLibraryValidationError([issue("named_submitter_required", "A named internal submitter is required")])
    initialized = init_library(db_path, output_root)
    parameters = read_json(parameters_path)
    detail = product_detail(db_path, product_id, role, submitted_by)
    product = detail["product"]
    with knowledge.connect(db_path) as connection:
        if not has_permission(connection, role, "submit"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot submit: {role}")])
    problems = validate_parameters(db_path, product, parameters)
    if problems:
        raise TaskLibraryValidationError(problems)
    request_hash = knowledge.sha256_json({"product_id": product_id, "parameters": parameters})
    job_id = stable_id("job", product_id, submitted_by.strip(), request_hash)
    request = compile_workflow_request(product, parameters, job_id)
    directory = output_root / "jobs" / job_id.replace(":", "-")
    request_path = directory / "workflow-request.json"
    write_json(request_path, request)
    now = knowledge.utc_now()
    priority = parameters.get("priority", product["default_priority"])
    with knowledge.connect(db_path) as connection:
        existing = connection.execute("SELECT * FROM task_library_jobs WHERE job_id=?", (job_id,)).fetchone()
        if existing:
            return {"job": dict(existing), "idempotent_replay": True, "catalog": initialized["catalog"]}
        connection.execute(
            """INSERT INTO task_library_jobs(
                   job_id,product_id,submitted_by,submitter_role,request_hash,cutoff_timestamp,
                   parameters_json,priority,status,data_readiness,workflow_request_path,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,'queued',?,?,?,?)""",
            (
                job_id, product_id, submitted_by.strip(), role, request_hash,
                knowledge.normalize_timestamp(parameters["cutoff_timestamp"]), canonical(parameters), priority,
                product["data_readiness"], str(request_path), now, now,
            ),
        )
        connection.execute(
            "INSERT INTO task_library_job_events(job_event_id,job_id,event_type,from_status,to_status,actor,occurred_at,detail_json) VALUES(?,?,'submitted',NULL,'queued',?,?,?)",
            (stable_id("job-event", job_id, "submitted"), job_id, submitted_by.strip(), now, canonical({"request_hash": request_hash})),
        )
        log_usage(connection, submitted_by.strip(), role, "submit_job", product_id, {"job_id": job_id})
        job = dict(connection.execute("SELECT * FROM task_library_jobs WHERE job_id=?", (job_id,)).fetchone())
    return {"job": job, "idempotent_replay": False, "workflow_request": request, "catalog": initialized["catalog"]}


def run_job(db_path: Path, job_id: str, actor: str, actor_role: str = "research_operator",
            output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    init_library(db_path, output_root)
    with knowledge.connect(db_path) as connection:
        row = connection.execute("SELECT * FROM task_library_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise TaskLibraryValidationError([issue("job_not_found", f"Unknown job: {job_id}")])
        if not has_permission(connection, actor_role, "manage_queue"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot manage queue: {actor_role}")])
        if row["status"] not in {"queued", "blocked", "failed"}:
            return {"job": dict(row), "idempotent_replay": True}
        now = knowledge.utc_now()
        connection.execute("UPDATE task_library_jobs SET status='validating',started_at=?,updated_at=? WHERE job_id=?", (now, now, job_id))
        connection.execute(
            "INSERT INTO task_library_job_events(job_event_id,job_id,event_type,from_status,to_status,actor,occurred_at,detail_json) VALUES(?,?,'validation_started','queued','validating',?,?,'{}')",
            (stable_id("job-event", job_id, "validating", now), job_id, actor, now),
        )
        request_path = Path(row["workflow_request_path"])
    request = read_json(request_path)
    problems = workflow.validate_request(db_path, request)
    if problems:
        with knowledge.connect(db_path) as connection:
            now = knowledge.utc_now()
            connection.execute("UPDATE task_library_jobs SET status='blocked',error_json=?,updated_at=? WHERE job_id=?", (canonical(problems), now, job_id))
            connection.execute(
                "INSERT INTO task_library_job_events(job_event_id,job_id,event_type,from_status,to_status,actor,occurred_at,detail_json) VALUES(?,?,'validation_blocked','validating','blocked',?,?,?)",
                (stable_id("job-event", job_id, "blocked", now), job_id, actor, now, canonical({"issues": problems})),
            )
        return {"job_id": job_id, "status": "blocked", "issues": problems}
    with knowledge.connect(db_path) as connection:
        now = knowledge.utc_now()
        connection.execute("UPDATE task_library_jobs SET status='running',updated_at=? WHERE job_id=?", (now, job_id))
    result = workflow.run_workflow(db_path, request_path, output_root / "workflow-runs")
    run = result["run"]
    mapped = "completed" if run["status"] == "completed" else "blocked" if run["status"] == "blocked" else "running"
    with knowledge.connect(db_path) as connection:
        now = knowledge.utc_now()
        report = next((item["path"] for item in result.get("artifacts", []) if item["artifact_type"] == "research_report"), None)
        connection.execute(
            "UPDATE task_library_jobs SET status=?,workflow_run_id=?,result_artifact_path=?,updated_at=?,completed_at=? WHERE job_id=?",
            (mapped, run["run_id"], report, now, now if mapped in {"completed", "blocked"} else None, job_id),
        )
        connection.execute(
            "INSERT INTO task_library_job_events(job_event_id,job_id,event_type,from_status,to_status,actor,occurred_at,detail_json) VALUES(?,?,'workflow_status','running',?,?,?,?)",
            (stable_id("job-event", job_id, mapped, now), job_id, mapped, actor, now, canonical({"workflow_run_id": run["run_id"]})),
        )
    return {"job_id": job_id, "status": mapped, "workflow": result}


def propose_product_update(db_path: Path, product_id: str, patch_path: Path,
                           version: str, proposed_by: str, role: str) -> dict[str, Any]:
    init_library(db_path)
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise TaskLibraryValidationError([issue("version_invalid", "version must use semantic form X.Y.Z")])
    patch = read_json(patch_path)
    allowed = {"title", "short_description", "research_question_template", "tags", "parameter_schema"}
    forbidden = set(patch) - allowed
    if forbidden:
        raise TaskLibraryValidationError([issue("product_patch_field_forbidden", f"Unsupported patch fields: {sorted(forbidden)}")])
    for path in production.scan_forbidden(patch):
        raise TaskLibraryValidationError([issue("portfolio_field_forbidden", "Fund holdings and position inference are forbidden", path)])
    with knowledge.connect(db_path) as connection:
        if not has_permission(connection, role, "manage_catalog"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot manage catalog: {role}")])
        row = connection.execute("SELECT * FROM task_library_products WHERE product_id=?", (product_id,)).fetchone()
        if not row:
            raise TaskLibraryValidationError([issue("product_not_found", f"Unknown product: {product_id}")])
        snapshot = {
            "product_id": product_id, "title": row["title"], "short_description": row["short_description"],
            "research_question_template": row["research_question_template"],
            "tags": json.loads(row["tags_json"]), "parameter_schema": json.loads(row["parameter_schema_json"]),
        }
        snapshot.update(patch)
        content_hash = knowledge.sha256_json(snapshot)
        version_id = stable_id("product-version", product_id, version, content_hash)
        now = knowledge.utc_now()
        connection.execute(
            """INSERT INTO task_library_product_versions(
                   product_version_id,product_id,version,content_hash,snapshot_json,release_status,
                   released_by,released_at,release_notes
               ) VALUES(?,?,?,?,?,'draft',?,?,?)""",
            (version_id, product_id, version, content_hash, canonical(snapshot), proposed_by, now, "Pending named research reviewer approval"),
        )
        review_id = stable_id("product-review", version_id)
        connection.execute(
            """INSERT INTO task_library_reviews(
                   review_id,product_id,product_version_id,review_type,reviewer,decision,notes,created_at
               ) VALUES(?,?,?,'product_release','unassigned','pending','Named research reviewer required',?)""",
            (review_id, product_id, version_id, now),
        )
        log_usage(connection, proposed_by, role, "propose_product_update", product_id, {"version": version})
    return {"product_id": product_id, "product_version_id": version_id, "version": version, "release_status": "draft", "review_id": review_id}


def approve_product_version(db_path: Path, product_version_id: str, reviewer: str,
                            role: str, notes: str = "") -> dict[str, Any]:
    init_library(db_path)
    if not reviewer.strip() or reviewer.strip().lower() in {"ai", "agent", "unassigned"}:
        raise TaskLibraryValidationError([issue("named_human_reviewer_required", "A named human research reviewer is required")])
    with knowledge.connect(db_path) as connection:
        if not has_permission(connection, role, "review_release"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot review releases: {role}")])
        version_row = connection.execute("SELECT * FROM task_library_product_versions WHERE product_version_id=?", (product_version_id,)).fetchone()
        if not version_row:
            raise TaskLibraryValidationError([issue("product_version_not_found", f"Unknown product version: {product_version_id}")])
        if version_row["release_status"] != "draft":
            return {"product_version_id": product_version_id, "release_status": version_row["release_status"], "idempotent_replay": True}
        snapshot = json.loads(version_row["snapshot_json"])
        now = knowledge.utc_now()
        connection.execute(
            """UPDATE task_library_products SET title=?,short_description=?,research_question_template=?,
                   tags_json=?,parameter_schema_json=?,version=?,updated_at=? WHERE product_id=?""",
            (snapshot["title"], snapshot["short_description"], snapshot["research_question_template"], canonical(snapshot["tags"]), canonical(snapshot["parameter_schema"]), version_row["version"], now, version_row["product_id"]),
        )
        connection.execute("UPDATE task_library_product_versions SET release_status='internal_active',released_by=?,released_at=?,release_notes=? WHERE product_version_id=?", (reviewer.strip(), now, notes, product_version_id))
        connection.execute("UPDATE task_library_reviews SET reviewer=?,decision='approved',notes=?,created_at=? WHERE product_version_id=? AND decision='pending'", (reviewer.strip(), notes, now, product_version_id))
        connection.execute("DELETE FROM task_library_products_fts WHERE product_id=?", (version_row["product_id"],))
        product = connection.execute("SELECT * FROM task_library_products WHERE product_id=?", (version_row["product_id"],)).fetchone()
        search_text = " ".join([product["title"], product["short_description"], product["research_question_template"], *json.loads(product["tags_json"])])
        connection.execute("UPDATE task_library_products SET search_text=? WHERE product_id=?", (search_text, version_row["product_id"]))
        connection.execute("INSERT INTO task_library_products_fts(product_id,title,short_description,search_text,tags) VALUES(?,?,?,?,?)", (product["product_id"], product["title"], product["short_description"], search_text, " ".join(json.loads(product["tags_json"]))))
        log_usage(connection, reviewer.strip(), role, "approve_product_release", version_row["product_id"], {"version": version_row["version"]})
    return {"product_id": version_row["product_id"], "product_version_id": product_version_id, "version": version_row["version"], "release_status": "internal_active", "reviewer": reviewer.strip(), "idempotent_replay": False}


def favorite_product(db_path: Path, product_id: str, user_id: str, role: str) -> dict[str, Any]:
    init_library(db_path)
    with knowledge.connect(db_path) as connection:
        if not has_permission(connection, role, "favorite"):
            raise TaskLibraryValidationError([issue("permission_denied", f"Role cannot favorite: {role}")])
        if not connection.execute("SELECT 1 FROM task_library_products WHERE product_id=?", (product_id,)).fetchone():
            raise TaskLibraryValidationError([issue("product_not_found", f"Unknown product: {product_id}")])
        connection.execute("INSERT OR IGNORE INTO task_library_favorites(user_id,product_id,created_at) VALUES(?,?,?)", (user_id, product_id, knowledge.utc_now()))
        log_usage(connection, user_id, role, "favorite", product_id, {})
    return {"user_id": user_id, "product_id": product_id, "favorite": True}


def list_jobs(db_path: Path, user_id: str | None = None, status: str | None = None) -> dict[str, Any]:
    init_library(db_path)
    conditions: list[str] = []; params: list[Any] = []
    if user_id: conditions.append("submitted_by=?"); params.append(user_id)
    if status: conditions.append("status=?"); params.append(status)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with knowledge.connect(db_path) as connection:
        rows = [dict(row) for row in connection.execute(f"SELECT * FROM task_library_jobs{where} ORDER BY created_at DESC", params).fetchall()]
    return {"count": len(rows), "jobs": rows}


def library_status(db_path: Path) -> dict[str, Any]:
    initialized = init_library(db_path)
    with knowledge.connect(db_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in ("task_library_templates", "task_library_products", "task_library_product_versions", "task_library_role_permissions", "task_library_saved_views", "task_library_favorites", "task_library_jobs", "task_library_job_events", "task_library_usage_events")
        }
        readiness = {row["data_readiness"]: row["count"] for row in connection.execute("SELECT data_readiness,COUNT(*) AS count FROM task_library_products GROUP BY data_readiness").fetchall()}
    return {**initialized, "table_counts": counts, "data_readiness": readiness}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    search = sub.add_parser("search")
    search.add_argument("--query", default=""); search.add_argument("--sector-code"); search.add_argument("--category"); search.add_argument("--template-id")
    search.add_argument("--role", default="public_fund_manager"); search.add_argument("--user-id", default="anonymous")
    detail = sub.add_parser("detail"); detail.add_argument("--product-id", required=True); detail.add_argument("--role", default="public_fund_manager"); detail.add_argument("--user-id", default="anonymous")
    submit = sub.add_parser("submit"); submit.add_argument("--product-id", required=True); submit.add_argument("--parameters", type=Path, required=True); submit.add_argument("--submitted-by", required=True); submit.add_argument("--role", required=True)
    run = sub.add_parser("run-job"); run.add_argument("--job-id", required=True); run.add_argument("--actor", required=True); run.add_argument("--actor-role", default="research_operator")
    fav = sub.add_parser("favorite"); fav.add_argument("--product-id", required=True); fav.add_argument("--user-id", required=True); fav.add_argument("--role", required=True)
    jobs = sub.add_parser("jobs"); jobs.add_argument("--user-id"); jobs.add_argument("--status")
    propose = sub.add_parser("propose-update"); propose.add_argument("--product-id", required=True); propose.add_argument("--patch", type=Path, required=True); propose.add_argument("--version", required=True); propose.add_argument("--proposed-by", required=True); propose.add_argument("--role", required=True)
    approve = sub.add_parser("approve-version"); approve.add_argument("--product-version-id", required=True); approve.add_argument("--reviewer", required=True); approve.add_argument("--role", required=True); approve.add_argument("--notes", default="")
    sub.add_parser("status")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init": result = init_library(args.db)
        elif args.command == "search": result = search_library(args.db, args.query, args.sector_code, args.category, args.template_id, args.role, args.user_id)
        elif args.command == "detail": result = product_detail(args.db, args.product_id, args.role, args.user_id)
        elif args.command == "submit": result = submit_job(args.db, args.product_id, args.parameters, args.submitted_by, args.role)
        elif args.command == "run-job": result = run_job(args.db, args.job_id, args.actor, args.actor_role)
        elif args.command == "favorite": result = favorite_product(args.db, args.product_id, args.user_id, args.role)
        elif args.command == "jobs": result = list_jobs(args.db, args.user_id, args.status)
        elif args.command == "propose-update": result = propose_product_update(args.db, args.product_id, args.patch, args.version, args.proposed_by, args.role)
        elif args.command == "approve-version": result = approve_product_version(args.db, args.product_version_id, args.reviewer, args.role, args.notes)
        else: result = library_status(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except TaskLibraryValidationError as exc:
        print(json.dumps({"status": "blocked", "issues": exc.issues}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
