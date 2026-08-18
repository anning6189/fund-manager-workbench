#!/usr/bin/env python3
"""Consumer research knowledge store reference runtime.

Only Python's standard library is used. The reference store is deliberately
portable: SQLite/FTS5 can later be replaced by PostgreSQL, object storage and a
licensed vector index without changing the JSON ingestion contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "001_consumer_research_warehouse.sql"
DOMAIN_MODEL_PATH = PROJECT_ROOT / "specs" / "consumer-domain-model.v1.json"
METRIC_PATH = PROJECT_ROOT / "specs" / "consumer-metric-dictionary.v1.json"
SOURCE_PATH = PROJECT_ROOT / "specs" / "connectors" / "source-registry.v1.json"
WAREHOUSE_SPEC_PATH = PROJECT_ROOT / "specs" / "knowledge" / "consumer-research-warehouse.v1.json"
QUARANTINE_ROOT = PROJECT_ROOT / "data" / "quarantine"

HASH_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
FORBIDDEN_KEYS = {
    "fund_holdings",
    "fund_holding",
    "portfolio_holdings",
    "portfolio_exposure",
    "portfolio_position",
    "fund_position",
    "position_inference",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include UTC offset: {value}")
    return parsed.astimezone(timezone.utc)


def normalize_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    return parse_timestamp(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def iter_metric_definitions(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "metric_id" in value and "definition" in value:
            yield value
        for child in value.values():
            yield from iter_metric_definitions(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_metric_definitions(child)


def flatten_taxonomy(nodes: list[dict[str, Any]], parent: str | None = None, level: int = 1) -> Iterable[dict[str, Any]]:
    for node in nodes:
        code = node["code"]
        yield {
            "node_code": code,
            "parent_code": parent,
            "name": node["name"],
            "level": int(node.get("level", level)),
            "status": node.get("status", "active"),
            "valid_from": node.get("valid_from"),
            "valid_to": node.get("valid_to"),
        }
        yield from flatten_taxonomy(node.get("children", []), code, level + 1)


def iter_sources(registry: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("sources", "registered_sources", "source_registry"):
        values = registry.get(key)
        if isinstance(values, list):
            yield from values
            return


def source_is_curatable(source: sqlite3.Row) -> bool:
    license_status = source["license_status"].lower()
    status = source["status"].lower()
    if "pending" in license_status or "license_gate" in status:
        return False
    allowed_markers = ("public", "confirmed", "licensed", "internal_approved")
    return any(marker in license_status for marker in allowed_markers)


def init_store(db_path: Path) -> dict[str, Any]:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    domain = read_json(DOMAIN_MODEL_PATH)
    metric_payload = read_json(METRIC_PATH)
    registry = read_json(SOURCE_PATH)
    warehouse_spec = read_json(WAREHOUSE_SPEC_PATH)
    metrics = {item["metric_id"]: item for item in iter_metric_definitions(metric_payload)}
    taxonomy = list(flatten_taxonomy(domain["taxonomy"]))
    sources = list(iter_sources(registry))

    with connect(db_path) as connection:
        connection.executescript(schema)
        for source in sources:
            connection.execute(
                """INSERT INTO source_catalog(
                       source_id,name,source_family,evidence_tier,license_status,
                       access_class,status,point_in_time_support,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                     name=excluded.name, source_family=excluded.source_family,
                     evidence_tier=excluded.evidence_tier, license_status=excluded.license_status,
                     access_class=excluded.access_class, status=excluded.status,
                     point_in_time_support=excluded.point_in_time_support, raw_json=excluded.raw_json""",
                (
                    source["source_id"], source["name"], source["source_family"],
                    source["evidence_tier"], source["license_status"], source["access_class"],
                    source["status"], source.get("point_in_time_support"), canonical_json(source),
                ),
            )
        for metric in metrics.values():
            connection.execute(
                """INSERT INTO metric_definitions(
                       metric_id,name,definition,unit,frequency,grain,time_semantics,
                       preferred_source_tier,raw_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(metric_id) DO UPDATE SET
                     name=excluded.name, definition=excluded.definition, unit=excluded.unit,
                     frequency=excluded.frequency, grain=excluded.grain,
                     time_semantics=excluded.time_semantics,
                     preferred_source_tier=excluded.preferred_source_tier,
                     raw_json=excluded.raw_json""",
                (
                    metric["metric_id"], metric["name"], metric["definition"], metric["unit"],
                    metric["frequency"], metric["grain"], metric["time_semantics"],
                    metric["preferred_source_tier"], canonical_json(metric),
                ),
            )
        for node in taxonomy:
            connection.execute(
                """INSERT INTO taxonomy_nodes(
                       node_code,parent_code,name,level,status,valid_from,valid_to
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(node_code) DO UPDATE SET
                     parent_code=excluded.parent_code,name=excluded.name,level=excluded.level,
                     status=excluded.status,valid_from=excluded.valid_from,valid_to=excluded.valid_to""",
                (
                    node["node_code"], node["parent_code"], node["name"], node["level"],
                    node["status"], node["valid_from"], node["valid_to"],
                ),
            )
        refresh = warehouse_spec["refresh_classes"]
        default_source_streams = {
            "CR.SRC.GILDATA.FINQUERY": ["market_daily", "financials"],
            "CR.SRC.GILDATA.ANNOUNCEMENT": ["announcements"],
            "CR.SRC.GILDATA.NEWS": ["news_leads"],
            "CR.SRC.GILDATA.MACRO_INDUSTRY": ["macro"],
            "CR.SRC.GILDATA.RESEARCH": ["research_metadata"],
            "CR.SRC.GILDATA.ENTERPRISE": ["enterprise_risk"],
            "CR.SRC.GILDATA.STOCK_UNIVERSE": ["master_data"],
            "CR.SRC.CNINFO": ["announcements", "financials"],
            "CR.SRC.SZSE": ["announcements"],
            "CR.SRC.SSE": ["announcements"],
            "CR.SRC.BSE": ["announcements"],
            "CR.SRC.NBS": ["macro"],
            "CR.SRC.CUSTOMS": ["official_industry_releases"],
            "CR.SRC.MOFCOM": ["official_industry_releases"],
            "CR.SRC.MIIT": ["official_industry_releases"],
            "CR.SRC.GOVCN": ["official_policy_documents"],
            "CR.SRC.NDRC": ["official_policy_documents"],
            "CR.SRC.SAMR": ["official_policy_documents"],
            "CR.SRC.PBOC": ["macro"],
            "CR.SRC.MCT": ["official_industry_releases"],
        }
        now = utc_now()
        for source_id, streams in default_source_streams.items():
            if connection.execute("SELECT 1 FROM source_catalog WHERE source_id=?", (source_id,)).fetchone() is None:
                continue
            for stream in streams:
                lag = float(refresh[stream]["maximum_lag_hours"])
                connection.execute(
                    """INSERT OR IGNORE INTO source_cursors(
                           source_id,stream_name,status,metadata_json
                       ) VALUES(?,?,?,?)""",
                    (source_id, stream, "not_started", canonical_json({"schedule": refresh[stream]["schedule"]})),
                )
                connection.execute(
                    """INSERT INTO freshness_state(
                           source_id,stream_name,expected_max_lag_hours,checked_at,status
                       ) VALUES(?,?,?,?,?)
                       ON CONFLICT(source_id,stream_name) DO UPDATE SET
                         expected_max_lag_hours=excluded.expected_max_lag_hours,
                         checked_at=excluded.checked_at""",
                    (source_id, stream, lag, now, "not_started"),
                )
    return {
        "status": "initialized",
        "database": str(db_path),
        "sources_loaded": len(sources),
        "metrics_loaded": len(metrics),
        "taxonomy_nodes_loaded": len(taxonomy),
    }


def scan_forbidden_keys(value: Any, path: str = "$") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_normalized = str(key).strip().lower()
            if key_normalized in FORBIDDEN_KEYS:
                matches.append(f"{path}.{key}")
            matches.extend(scan_forbidden_keys(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(scan_forbidden_keys(child, f"{path}[{index}]"))
    return matches


class PackageValidationError(Exception):
    def __init__(self, issues: list[dict[str, str]]):
        self.issues = issues
        super().__init__("; ".join(issue["message"] for issue in issues))


def validate_raw_package(connection: sqlite3.Connection, package: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append({"code": code, "path": path, "message": message})

    for required in ("package_id", "source_id", "retrieved_at", "license_tag"):
        if not package.get(required):
            issue("required_field_missing", f"$.{required}", f"缺少必填字段 {required}")
    source = None
    if package.get("source_id"):
        source = connection.execute("SELECT * FROM source_catalog WHERE source_id=?", (package["source_id"],)).fetchone()
        if source is None:
            issue("source_not_registered", "$.source_id", "来源未在阶段五注册表中登记")
    for forbidden_path in scan_forbidden_keys(package):
        issue("fund_holdings_forbidden", forbidden_path, "阶段六禁止基金持仓、组合暴露或仓位推断字段")
    try:
        if package.get("retrieved_at"):
            parse_timestamp(package["retrieved_at"])
    except ValueError as exc:
        issue("timestamp_invalid", "$.retrieved_at", str(exc))
    return issues


def validate_package(connection: sqlite3.Connection, package: dict[str, Any]) -> None:
    issues = validate_raw_package(connection, package)

    def issue(code: str, path: str, message: str) -> None:
        issues.append({"code": code, "path": path, "message": message})

    source = connection.execute("SELECT * FROM source_catalog WHERE source_id=?", (package.get("source_id"),)).fetchone() if package.get("source_id") else None
    if source is not None and not source_is_curatable(source):
        issue("license_not_curatable", "$.source_id", "来源仍处许可闸门后，禁止进入curated层；可使用raw目标保存原始包")

    domain = read_json(DOMAIN_MODEL_PATH)
    allowed_entity_types = set(domain["entity_types"].keys())
    allowed_predicates = set(domain["relationship_types"])
    entity_ids = {row[0] for row in connection.execute("SELECT entity_id FROM entities")}
    entity_ids.update(item.get("entity_id") for item in package.get("entities", []) if item.get("entity_id"))
    for index, entity in enumerate(package.get("entities", [])):
        if entity.get("entity_type") not in allowed_entity_types:
            issue("entity_type_invalid", f"$.entities[{index}].entity_type", "实体类型不在阶段三白名单")
        if not str(entity.get("entity_id", "")).startswith("cr:"):
            issue("entity_id_invalid", f"$.entities[{index}].entity_id", "实体ID必须以cr:开头")

    taxonomy_codes = {row[0] for row in connection.execute("SELECT node_code FROM taxonomy_nodes")}
    for index, relation in enumerate(package.get("relationships", [])):
        if relation.get("predicate") not in allowed_predicates:
            issue("relationship_predicate_invalid", f"$.relationships[{index}].predicate", "关系谓词不在阶段三白名单")
        for field in ("subject_id", "object_id"):
            if relation.get(field) not in entity_ids:
                issue("relationship_entity_missing", f"$.relationships[{index}].{field}", "关系引用的实体不存在")
    for index, assignment in enumerate(package.get("classifications", [])):
        if assignment.get("entity_id") not in entity_ids:
            issue("classification_entity_missing", f"$.classifications[{index}].entity_id", "分类主体不存在")
        if assignment.get("node_code") not in taxonomy_codes:
            issue("classification_node_missing", f"$.classifications[{index}].node_code", "分类节点不存在")

    document_ids = {row[0] for row in connection.execute("SELECT document_id FROM documents")}
    document_ids.update(item.get("document_id") for item in package.get("documents", []) if item.get("document_id"))
    for index, document in enumerate(package.get("documents", [])):
        for required in ("document_id", "document_type", "title", "publisher", "published_at", "available_at", "content_hash", "document_version", "license_tag", "access_class", "evidence_tier"):
            if document.get(required) in (None, ""):
                issue("document_field_missing", f"$.documents[{index}].{required}", f"文档缺少 {required}")
        if document.get("content_hash") and not HASH_PATTERN.match(str(document["content_hash"])):
            issue("document_hash_invalid", f"$.documents[{index}].content_hash", "文档哈希必须为sha256:加64位十六进制")
        if document.get("license_tag") and document["license_tag"] != package.get("license_tag"):
            issue("license_tag_mismatch", f"$.documents[{index}].license_tag", "文档许可标签与摄取包不一致")
        try:
            if document.get("available_at") and document.get("published_at") and parse_timestamp(document["available_at"]) < parse_timestamp(document["published_at"]):
                issue("document_available_before_publish", f"$.documents[{index}].available_at", "available_at不能早于published_at")
        except ValueError as exc:
            issue("timestamp_invalid", f"$.documents[{index}]", str(exc))

    chunk_ids = {row[0] for row in connection.execute("SELECT chunk_id FROM document_chunks")}
    chunk_ids.update(item.get("chunk_id") for item in package.get("chunks", []) if item.get("chunk_id"))
    for index, chunk in enumerate(package.get("chunks", [])):
        if chunk.get("document_id") not in document_ids:
            issue("chunk_document_missing", f"$.chunks[{index}].document_id", "文档切片引用的文档不存在")
        for required in ("chunk_id", "sequence_no", "chunk_type", "locator", "text_content", "content_hash", "available_at"):
            if chunk.get(required) in (None, ""):
                issue("chunk_field_missing", f"$.chunks[{index}].{required}", f"切片缺少 {required}")
        if chunk.get("content_hash") and not HASH_PATTERN.match(str(chunk["content_hash"])):
            issue("chunk_hash_invalid", f"$.chunks[{index}].content_hash", "切片哈希格式无效")
        try:
            if chunk.get("available_at"):
                parse_timestamp(chunk["available_at"])
        except ValueError as exc:
            issue("timestamp_invalid", f"$.chunks[{index}].available_at", str(exc))

    evidence_ids = {row[0] for row in connection.execute("SELECT evidence_id FROM evidence")}
    evidence_ids.update(item.get("evidence_id") for item in package.get("evidence", []) if item.get("evidence_id"))
    for index, evidence in enumerate(package.get("evidence", [])):
        if evidence.get("document_id") not in document_ids:
            issue("evidence_document_missing", f"$.evidence[{index}].document_id", "证据引用的文档不存在")
        if evidence.get("chunk_id") and evidence["chunk_id"] not in chunk_ids:
            issue("evidence_chunk_missing", f"$.evidence[{index}].chunk_id", "证据引用的切片不存在")
        if not evidence.get("locator"):
            issue("evidence_locator_missing", f"$.evidence[{index}].locator", "证据必须有原文定位")
        if evidence.get("license_tag") and evidence["license_tag"] != package.get("license_tag"):
            issue("license_tag_mismatch", f"$.evidence[{index}].license_tag", "证据许可标签与摄取包不一致")
        try:
            if evidence.get("available_at") and evidence.get("published_at") and parse_timestamp(evidence["available_at"]) < parse_timestamp(evidence["published_at"]):
                issue("evidence_available_before_publish", f"$.evidence[{index}].available_at", "证据available_at不能早于published_at")
        except ValueError as exc:
            issue("timestamp_invalid", f"$.evidence[{index}]", str(exc))

    metric_ids = {row[0] for row in connection.execute("SELECT metric_id FROM metric_definitions")}
    value_status_values = set(read_json(METRIC_PATH)["observation_schema"]["value_status_values"])
    for index, observation in enumerate(package.get("observations", [])):
        path = f"$.observations[{index}]"
        if observation.get("metric_id") not in metric_ids:
            issue("metric_not_registered", f"{path}.metric_id", "指标未进入阶段四字典")
        if observation.get("entity_id") not in entity_ids:
            issue("observation_entity_missing", f"{path}.entity_id", "指标主体不存在")
        if observation.get("evidence_id") not in evidence_ids:
            issue("observation_evidence_missing", f"{path}.evidence_id", "指标证据不存在")
        if observation.get("value_status") not in value_status_values:
            issue("value_status_invalid", f"{path}.value_status", "指标值状态不在白名单")
        for required in ("observation_id", "metric_id", "entity_id", "unit", "period_start", "period_end", "as_of_date", "published_at", "available_at", "evidence_id", "value_status"):
            if observation.get(required) in (None, ""):
                issue("observation_field_missing", f"{path}.{required}", f"指标缺少 {required}")
        if observation.get("period_start") and observation.get("period_end") and observation["period_start"] > observation["period_end"]:
            issue("observation_period_invalid", f"{path}.period_end", "period_end早于period_start")
        try:
            if observation.get("available_at") and observation.get("published_at") and parse_timestamp(observation["available_at"]) < parse_timestamp(observation["published_at"]):
                issue("observation_available_before_publish", f"{path}.available_at", "指标available_at不能早于published_at")
        except ValueError as exc:
            issue("timestamp_invalid", path, str(exc))
        if str(observation.get("metric_id", "")).startswith("CR.CO."):
            for required in ("statement_scope", "consolidation_scope", "accounting_standard", "currency", "scale", "restatement_status", "fiscal_period_type"):
                if observation.get(required) in (None, ""):
                    issue("financial_scope_missing", f"{path}.{required}", f"公司财务指标缺少 {required}")

    if issues:
        raise PackageValidationError(issues)


def quarantine_package(package_path: Path, package: dict[str, Any], issues: list[dict[str, str]]) -> Path:
    QUARANTINE_ROOT.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(package.get("package_id", package_path.stem)))
    destination = QUARANTINE_ROOT / f"{safe_id}.json"
    report = QUARANTINE_ROOT / f"{safe_id}.issues.json"
    shutil.copyfile(package_path, destination)
    report.write_text(json.dumps({"package_id": package.get("package_id"), "issues": issues}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def ingest_package(db_path: Path, package_path: Path, target: str = "curated") -> dict[str, Any]:
    package = read_json(package_path)
    input_hash = sha256_json(package)
    now = utc_now()
    run_package_id = package.get("package_id") if target == "curated" else f"{package.get('package_id')}#raw"
    inserted = 0
    updated = 0
    with connect(db_path) as connection:
        existing = connection.execute("SELECT * FROM ingestion_runs WHERE package_id=? AND status='success'", (run_package_id,)).fetchone()
        if existing is not None:
            if existing["input_hash"] != input_hash:
                conflict = [{"code": "package_id_content_conflict", "path": "$.package_id", "message": "同一package_id对应了不同内容哈希"}]
                report_path = quarantine_package(package_path, package, conflict)
                return {"status": "quarantined", "package_id": package.get("package_id"), "issues": conflict, "issue_report": str(report_path)}
            return {
                "status": "idempotent_noop",
                "package_id": package.get("package_id"),
                "run_id": existing["run_id"],
                "inserted_records": 0,
                "updated_records": 0,
            }
        raw_issues = validate_raw_package(connection, package)
        if raw_issues:
            report_path = quarantine_package(package_path, package, raw_issues)
            return {"status": "quarantined", "package_id": package.get("package_id"), "issues": raw_issues, "issue_report": str(report_path)}
        raw_existing = connection.execute("SELECT input_hash FROM raw_packages WHERE package_id=?", (package["package_id"],)).fetchone()
        if raw_existing is not None and raw_existing["input_hash"] != input_hash:
            conflict = [{"code": "package_id_content_conflict", "path": "$.package_id", "message": "原始层已有同ID不同哈希的摄取包"}]
            report_path = quarantine_package(package_path, package, conflict)
            return {"status": "quarantined", "package_id": package.get("package_id"), "issues": conflict, "issue_report": str(report_path)}
        source = connection.execute("SELECT * FROM source_catalog WHERE source_id=?", (package["source_id"],)).fetchone()
        gate_status = "raw" if source_is_curatable(source) else "license_gate"
        connection.execute(
            """INSERT OR IGNORE INTO raw_packages(
                   package_id,source_id,retrieved_at,input_hash,license_tag,gate_status,raw_json,stored_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (package["package_id"], package["source_id"], normalize_timestamp(package["retrieved_at"]), input_hash, package["license_tag"], gate_status, canonical_json(package), now),
        )
        if target == "raw":
            run_id = f"ingest:{hashlib.sha256((run_package_id + now).encode()).hexdigest()[:20]}"
            connection.execute(
                """INSERT INTO ingestion_runs(
                       run_id,package_id,source_id,started_at,completed_at,status,input_hash,inserted_records
                   ) VALUES(?,?,?,?,?,'success',?,1)""",
                (run_id, run_package_id, package["source_id"], now, now, input_hash),
            )
            return {"status": "raw_stored", "package_id": package["package_id"], "run_id": run_id, "gate_status": gate_status, "inserted_records": 1, "updated_records": 0}
        connection.commit()
        try:
            validate_package(connection, package)
        except PackageValidationError as exc:
            report_path = quarantine_package(package_path, package, exc.issues)
            source_exists = connection.execute("SELECT 1 FROM source_catalog WHERE source_id=?", (package.get("source_id"),)).fetchone()
            if source_exists:
                run_id = f"ingest:{hashlib.sha256((str(package.get('package_id')) + now).encode()).hexdigest()[:20]}"
                connection.execute(
                    """INSERT OR REPLACE INTO ingestion_runs(
                           run_id,package_id,source_id,started_at,completed_at,status,input_hash,
                           inserted_records,updated_records,rejected_records,error_summary
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, run_package_id, package["source_id"], now, now, "quarantined", input_hash, 0, 0, len(exc.issues), canonical_json(exc.issues)),
                )
                for problem in exc.issues:
                    connection.execute(
                        """INSERT INTO ingestion_errors(
                               run_id,package_id,record_type,error_code,message,raw_json,created_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (run_id, run_package_id, problem["path"], problem["code"], problem["message"], canonical_json(problem), now),
                    )
            return {
                "status": "quarantined",
                "package_id": package.get("package_id"),
                "issues": exc.issues,
                "issue_report": str(report_path),
            }

        run_id = f"ingest:{hashlib.sha256((package['package_id'] + now).encode()).hexdigest()[:20]}"
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """INSERT OR REPLACE INTO ingestion_runs(
                       run_id,package_id,source_id,started_at,status,input_hash
                   ) VALUES(?,?,?,?,?,?)""",
                (run_id, run_package_id, package["source_id"], now, "running", input_hash),
            )
            for entity in package.get("entities", []):
                exists = connection.execute("SELECT 1 FROM entities WHERE entity_id=?", (entity["entity_id"],)).fetchone()
                connection.execute(
                    """INSERT INTO entities(
                           entity_id,entity_type,canonical_name,jurisdiction,status,valid_from,valid_to,
                           attributes_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(entity_id) DO UPDATE SET
                         canonical_name=excluded.canonical_name,jurisdiction=excluded.jurisdiction,
                         status=excluded.status,valid_from=excluded.valid_from,valid_to=excluded.valid_to,
                         attributes_json=excluded.attributes_json,updated_at=excluded.updated_at""",
                    (
                        entity["entity_id"], entity["entity_type"], entity["canonical_name"],
                        entity.get("jurisdiction"), entity.get("status", "active"), entity.get("valid_from"),
                        entity.get("valid_to"), canonical_json(entity.get("attributes", {})), now, now,
                    ),
                )
                inserted += int(exists is None)
                updated += int(exists is not None)
            for alias in package.get("aliases", []):
                connection.execute(
                    """INSERT INTO entity_aliases(
                           entity_id,alias,language,alias_type,valid_from,valid_to,source_id,confidence,review_status
                       ) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(entity_id,alias,source_id) DO UPDATE SET
                         valid_from=excluded.valid_from,valid_to=excluded.valid_to,
                         confidence=excluded.confidence,review_status=excluded.review_status""",
                    (
                        alias["entity_id"], alias["alias"], alias.get("language", "zh-CN"), alias["alias_type"],
                        alias.get("valid_from"), alias.get("valid_to"), package["source_id"],
                        float(alias.get("confidence", 1)), alias.get("review_status", "reviewed"),
                    ),
                )
                inserted += 1
            for identifier in package.get("identifiers", []):
                connection.execute(
                    """INSERT INTO external_identifiers(
                           entity_id,id_type,issuer,value,valid_from,valid_to,is_primary
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(id_type,issuer,value) DO UPDATE SET
                         entity_id=excluded.entity_id,valid_from=excluded.valid_from,
                         valid_to=excluded.valid_to,is_primary=excluded.is_primary""",
                    (
                        identifier["entity_id"], identifier["id_type"], identifier["issuer"], identifier["value"],
                        identifier.get("valid_from"), identifier.get("valid_to"), int(bool(identifier.get("is_primary", False))),
                    ),
                )
                inserted += 1
            for relation in package.get("relationships", []):
                connection.execute(
                    """INSERT INTO relationships(
                           relationship_id,subject_id,predicate,object_id,valid_from,valid_to,observed_at,
                           source_id,confidence,review_status,attributes_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(relationship_id) DO UPDATE SET
                         valid_from=excluded.valid_from,valid_to=excluded.valid_to,observed_at=excluded.observed_at,
                         confidence=excluded.confidence,review_status=excluded.review_status,
                         attributes_json=excluded.attributes_json""",
                    (
                        relation["relationship_id"], relation["subject_id"], relation["predicate"], relation["object_id"],
                        relation.get("valid_from"), relation.get("valid_to"), normalize_timestamp(relation["observed_at"]), package["source_id"],
                        float(relation.get("confidence", 1)), relation.get("review_status", "reviewed"),
                        canonical_json(relation.get("attributes", {})),
                    ),
                )
                inserted += 1
            for assignment in package.get("classifications", []):
                connection.execute(
                    """INSERT INTO entity_classifications(
                           assignment_id,entity_id,node_code,assignment_type,exposure_ratio,valid_from,valid_to,
                           observed_at,source_id,confidence,review_status
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(assignment_id) DO UPDATE SET
                         node_code=excluded.node_code,assignment_type=excluded.assignment_type,
                         exposure_ratio=excluded.exposure_ratio,valid_from=excluded.valid_from,
                         valid_to=excluded.valid_to,observed_at=excluded.observed_at,
                         confidence=excluded.confidence,review_status=excluded.review_status""",
                    (
                        assignment["assignment_id"], assignment["entity_id"], assignment["node_code"],
                        assignment.get("assignment_type", "primary"), assignment.get("exposure_ratio"),
                        assignment.get("valid_from"), assignment.get("valid_to"), normalize_timestamp(assignment["observed_at"]),
                        package["source_id"], float(assignment.get("confidence", 1)), assignment.get("review_status", "reviewed"),
                    ),
                )
                inserted += 1
            for document in package.get("documents", []):
                connection.execute(
                    """INSERT INTO documents(
                           document_id,source_id,source_record_id,document_type,title,publisher,source_url,
                           local_object_path,published_at,available_at,as_of_date,retrieved_at,content_hash,
                           mime_type,language,document_version,license_tag,access_class,evidence_tier,status,metadata_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(document_id) DO UPDATE SET
                         source_url=excluded.source_url,local_object_path=excluded.local_object_path,
                         retrieved_at=excluded.retrieved_at,status=excluded.status,metadata_json=excluded.metadata_json""",
                    (
                        document["document_id"], package["source_id"], document.get("source_record_id"), document["document_type"],
                        document["title"], document["publisher"], document.get("source_url"), document.get("local_object_path"),
                        normalize_timestamp(document["published_at"]), normalize_timestamp(document["available_at"]), document.get("as_of_date"),
                        normalize_timestamp(document.get("retrieved_at", package["retrieved_at"])), document["content_hash"], document.get("mime_type"),
                        document.get("language", "zh-CN"), document["document_version"], document["license_tag"],
                        document["access_class"], document["evidence_tier"], document.get("status", "curated"),
                        canonical_json(document.get("metadata", {})),
                    ),
                )
                inserted += 1
            for link in package.get("document_entities", []):
                connection.execute(
                    """INSERT INTO document_entities(document_id,entity_id,relation_type,confidence,review_status)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(document_id,entity_id,relation_type) DO UPDATE SET
                         confidence=excluded.confidence,review_status=excluded.review_status""",
                    (link["document_id"], link["entity_id"], link.get("relation_type", "about"), float(link.get("confidence", 1)), link.get("review_status", "reviewed")),
                )
                inserted += 1
            for chunk in package.get("chunks", []):
                connection.execute(
                    """INSERT INTO document_chunks(
                           chunk_id,document_id,sequence_no,page_start,page_end,section_path,chunk_type,
                           locator,text_content,table_json,token_count,content_hash,available_at,metadata_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(chunk_id) DO UPDATE SET
                         section_path=excluded.section_path,locator=excluded.locator,text_content=excluded.text_content,
                         table_json=excluded.table_json,token_count=excluded.token_count,
                         content_hash=excluded.content_hash,metadata_json=excluded.metadata_json""",
                    (
                        chunk["chunk_id"], chunk["document_id"], int(chunk["sequence_no"]), chunk.get("page_start"),
                        chunk.get("page_end"), chunk.get("section_path"), chunk["chunk_type"], chunk["locator"],
                        chunk["text_content"], canonical_json(chunk["table"]) if chunk.get("table") is not None else None,
                        chunk.get("token_count"), chunk["content_hash"], normalize_timestamp(chunk["available_at"]), canonical_json(chunk.get("metadata", {})),
                    ),
                )
                inserted += 1
            for evidence in package.get("evidence", []):
                connection.execute(
                    """INSERT INTO evidence(
                           evidence_id,document_id,chunk_id,source_id,locator,support_type,evidence_tier,
                           published_at,available_at,content_hash,license_tag,access_class
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(evidence_id) DO UPDATE SET
                         chunk_id=excluded.chunk_id,locator=excluded.locator,support_type=excluded.support_type""",
                    (
                        evidence["evidence_id"], evidence["document_id"], evidence.get("chunk_id"), package["source_id"],
                        evidence["locator"], evidence.get("support_type", "direct"), evidence["evidence_tier"],
                        normalize_timestamp(evidence["published_at"]), normalize_timestamp(evidence["available_at"]), evidence["content_hash"],
                        evidence["license_tag"], evidence["access_class"],
                    ),
                )
                inserted += 1
            for observation in package.get("observations", []):
                if observation.get("supersedes_observation_id"):
                    connection.execute(
                        "UPDATE observations SET is_current=0 WHERE observation_id=?",
                        (observation["supersedes_observation_id"],),
                    )
                value = observation.get("value")
                numeric = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
                text_value = None if numeric is not None else (str(value) if value is not None else None)
                connection.execute(
                    """INSERT INTO observations(
                           observation_id,metric_id,entity_id,security_id,value_numeric,value_text,unit,
                           period_start,period_end,as_of_date,observed_at,published_at,available_at,ingested_at,
                           source_id,evidence_id,value_status,statement_scope,consolidation_scope,accounting_standard,
                           currency,scale,restatement_status,fiscal_period_type,version_no,is_current,
                           supersedes_observation_id,quality_status,attributes_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observation["observation_id"], observation["metric_id"], observation["entity_id"],
                        observation.get("security_id"), numeric, text_value, observation["unit"],
                        observation["period_start"], observation["period_end"], observation["as_of_date"],
                        normalize_timestamp(observation.get("observed_at")), normalize_timestamp(observation["published_at"]), normalize_timestamp(observation["available_at"]), now,
                        package["source_id"], observation["evidence_id"], observation["value_status"],
                        observation.get("statement_scope"), observation.get("consolidation_scope"),
                        observation.get("accounting_standard"), observation.get("currency"), observation.get("scale"),
                        observation.get("restatement_status", "original"), observation.get("fiscal_period_type"),
                        int(observation.get("version_no", 1)), int(bool(observation.get("is_current", True))),
                        observation.get("supersedes_observation_id"), "curated", canonical_json(observation.get("attributes", {})),
                    ),
                )
                inserted += 1
            cursor = package.get("source_cursor")
            if cursor:
                connection.execute(
                    """INSERT INTO source_cursors(
                           source_id,stream_name,cursor_value,watermark_available_at,last_success_at,next_due_at,status,metadata_json
                       ) VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id,stream_name) DO UPDATE SET
                         cursor_value=excluded.cursor_value,watermark_available_at=excluded.watermark_available_at,
                         last_success_at=excluded.last_success_at,next_due_at=excluded.next_due_at,
                         status=excluded.status,metadata_json=excluded.metadata_json""",
                    (
                        package["source_id"], cursor["stream_name"], cursor.get("cursor_value"),
                        normalize_timestamp(cursor.get("watermark_available_at")), now, normalize_timestamp(cursor.get("next_due_at")), "success",
                        canonical_json(cursor.get("metadata", {})),
                    ),
                )
            connection.execute(
                """UPDATE ingestion_runs SET completed_at=?,status='success',inserted_records=?,updated_records=?
                   WHERE run_id=?""",
                (utc_now(), inserted, updated, run_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {
        "status": "success",
        "package_id": package["package_id"],
        "run_id": run_id,
        "inserted_records": inserted,
        "updated_records": updated,
    }


def rows_as_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def resolve_entity(db_path: Path, query: str, cutoff: str | None) -> dict[str, Any]:
    cutoff = normalize_timestamp(cutoff) if cutoff else "9999-12-31T23:59:59.999Z"
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT DISTINCT e.entity_id,e.entity_type,e.canonical_name,e.jurisdiction,e.status,
                              a.alias,i.id_type,i.issuer,i.value AS identifier
               FROM entities e
               LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
                 AND COALESCE(a.valid_from,'0000-01-01') <= substr(?,1,10)
                 AND COALESCE(a.valid_to,'9999-12-31') >= substr(?,1,10)
               LEFT JOIN external_identifiers i ON i.entity_id=e.entity_id
                 AND COALESCE(i.valid_from,'0000-01-01') <= substr(?,1,10)
                 AND COALESCE(i.valid_to,'9999-12-31') >= substr(?,1,10)
               WHERE lower(e.canonical_name)=lower(?) OR lower(a.alias)=lower(?) OR lower(i.value)=lower(?)
               ORDER BY e.canonical_name""",
            (cutoff, cutoff, cutoff, cutoff, query, query, query),
        ).fetchall()
        return {"query": query, "cutoff": cutoff, "matches": rows_as_dicts(rows)}


def query_metric(db_path: Path, entity_id: str, metric_id: str, cutoff: str, period_end: str | None) -> dict[str, Any]:
    cutoff = normalize_timestamp(cutoff)
    # ISO-8601 timestamps can carry different offsets.  A lexical TEXT
    # comparison (for example +08:00 versus Z) is not a chronological one.
    filters = "o.entity_id=? AND o.metric_id=? AND julianday(o.available_at)<=julianday(?) AND o.quality_status='curated'"
    parameters: list[Any] = [entity_id, metric_id, cutoff]
    if period_end:
        filters += " AND o.period_end=?"
        parameters.append(period_end)
    sql = f"""
      WITH eligible AS (
        SELECT o.*,
               ROW_NUMBER() OVER (
                 PARTITION BY o.metric_id,o.entity_id,o.period_start,o.period_end,o.source_id
                 ORDER BY o.available_at DESC,o.version_no DESC,o.ingested_at DESC
               ) AS rn
        FROM observations o WHERE {filters}
      )
      SELECT e.observation_id,e.metric_id,m.name AS metric_name,e.entity_id,n.canonical_name,e.source_id,
             e.value_numeric,e.value_text,e.unit,e.period_start,e.period_end,e.as_of_date,
             e.published_at,e.available_at,e.value_status,e.restatement_status,e.version_no,
             e.evidence_id,v.document_id,v.title,v.publisher,v.source_url,v.locator,v.chunk_id
      FROM eligible e
      JOIN metric_definitions m ON m.metric_id=e.metric_id
      JOIN entities n ON n.entity_id=e.entity_id
      JOIN v_evidence_trace v ON v.evidence_id=e.evidence_id
      WHERE e.rn=1 ORDER BY e.period_end,e.available_at
    """
    with connect(db_path) as connection:
        rows = connection.execute(sql, parameters).fetchall()
        return {
            "entity_id": entity_id,
            "metric_id": metric_id,
            "cutoff_timestamp": cutoff,
            "period_end": period_end,
            "observations": rows_as_dicts(rows),
        }


def search_documents(db_path: Path, query: str, cutoff: str, entity_id: str | None, limit: int) -> dict[str, Any]:
    cutoff = normalize_timestamp(cutoff)
    with connect(db_path) as connection:
        entity_clause = ""
        parameters: list[Any] = [query, cutoff]
        if entity_id:
            entity_clause = "AND EXISTS (SELECT 1 FROM document_entities de WHERE de.document_id=d.document_id AND de.entity_id=?)"
            parameters.append(entity_id)
        parameters.append(limit)
        sql = f"""
          SELECT f.chunk_id,f.document_id,d.title,d.publisher,d.published_at,d.available_at,
                 c.section_path,c.locator,c.text_content,d.source_url,bm25(document_chunks_fts) AS rank
          FROM document_chunks_fts f
          JOIN document_chunks c ON c.chunk_id=f.chunk_id
          JOIN documents d ON d.document_id=f.document_id
          WHERE document_chunks_fts MATCH ? AND julianday(d.available_at)<=julianday(?) AND d.status='curated'
          {entity_clause}
          ORDER BY rank LIMIT ?
        """
        try:
            rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError:
            like = f"%{query}%"
            fallback_params: list[Any] = [like, like, cutoff]
            fallback_entity = ""
            if entity_id:
                fallback_entity = "AND EXISTS (SELECT 1 FROM document_entities de WHERE de.document_id=d.document_id AND de.entity_id=?)"
                fallback_params.append(entity_id)
            fallback_params.append(limit)
            rows = connection.execute(
                f"""SELECT c.chunk_id,c.document_id,d.title,d.publisher,d.published_at,d.available_at,
                            c.section_path,c.locator,c.text_content,d.source_url,NULL AS rank
                     FROM document_chunks c JOIN documents d ON d.document_id=c.document_id
                     WHERE (c.text_content LIKE ? OR d.title LIKE ?) AND julianday(d.available_at)<=julianday(?) AND d.status='curated'
                     {fallback_entity} ORDER BY d.available_at DESC LIMIT ?""",
                fallback_params,
            ).fetchall()
        if not rows:
            fallback_params = [f"%{query}%", f"%{query}%", cutoff]
            fallback_entity = ""
            if entity_id:
                fallback_entity = "AND EXISTS (SELECT 1 FROM document_entities de WHERE de.document_id=d.document_id AND de.entity_id=?)"
                fallback_params.append(entity_id)
            fallback_params.append(limit)
            rows = connection.execute(
                f"""SELECT c.chunk_id,c.document_id,d.title,d.publisher,d.published_at,d.available_at,
                            c.section_path,c.locator,c.text_content,d.source_url,NULL AS rank
                     FROM document_chunks c JOIN documents d ON d.document_id=c.document_id
                     WHERE (c.text_content LIKE ? OR d.title LIKE ?) AND julianday(d.available_at)<=julianday(?) AND d.status='curated'
                     {fallback_entity} ORDER BY d.available_at DESC LIMIT ?""",
                fallback_params,
            ).fetchall()
        return {"query": query, "cutoff_timestamp": cutoff, "entity_id": entity_id, "results": rows_as_dicts(rows)}


def trace_evidence(db_path: Path, evidence_id: str) -> dict[str, Any]:
    with connect(db_path) as connection:
        evidence = connection.execute("SELECT * FROM v_evidence_trace WHERE evidence_id=?", (evidence_id,)).fetchone()
        observations = connection.execute(
            """SELECT observation_id,metric_id,entity_id,value_numeric,value_text,unit,period_end,available_at
               FROM observations WHERE evidence_id=? ORDER BY metric_id,period_end""",
            (evidence_id,),
        ).fetchall()
        return {"evidence": dict(evidence) if evidence else None, "supported_observations": rows_as_dicts(observations)}


def freshness_report(db_path: Path) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT f.source_id,s.name,f.stream_name,f.expected_max_lag_hours,
                      c.watermark_available_at,c.last_success_at,c.next_due_at,c.status AS cursor_status
               FROM freshness_state f
               JOIN source_catalog s ON s.source_id=f.source_id
               LEFT JOIN source_cursors c ON c.source_id=f.source_id AND c.stream_name=f.stream_name
               ORDER BY f.stream_name,f.source_id"""
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            watermark = row["watermark_available_at"]
            lag = None
            status = "not_started"
            if watermark:
                parsed = datetime.fromisoformat(watermark.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                lag = (now - parsed.astimezone(timezone.utc)).total_seconds() / 3600
                status = "fresh" if lag <= float(row["expected_max_lag_hours"]) else "stale"
            item["lag_hours"] = round(lag, 3) if lag is not None else None
            item["freshness_status"] = status
            results.append(item)
            connection.execute(
                """UPDATE freshness_state SET latest_available_at=?,checked_at=?,lag_hours=?,status=?
                   WHERE source_id=? AND stream_name=?""",
                (watermark, utc_now(), lag, status, row["source_id"], row["stream_name"]),
            )
        summary = {
            "streams": len(results),
            "fresh": sum(1 for item in results if item["freshness_status"] == "fresh"),
            "stale": sum(1 for item in results if item["freshness_status"] == "stale"),
            "not_started": sum(1 for item in results if item["freshness_status"] == "not_started"),
        }
        return {"checked_at": utc_now(), "summary": summary, "streams": results}


def refresh_plan(db_path: Path) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT c.source_id,s.name,s.status AS status,s.license_status,s.access_class,
                      c.stream_name,c.cursor_value,c.watermark_available_at,c.last_success_at,
                      c.next_due_at,c.status AS cursor_status,c.metadata_json,s.raw_json
               FROM source_cursors c JOIN source_catalog s ON s.source_id=c.source_id
               ORDER BY c.stream_name,c.source_id"""
        ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            due = row["cursor_status"] in ("not_started", "failed", "stale") or not row["next_due_at"]
            if row["next_due_at"]:
                parsed = datetime.fromisoformat(row["next_due_at"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                due = parsed.astimezone(timezone.utc) <= now
            source = json.loads(row["raw_json"])
            curatable = source_is_curatable(row)
            tasks.append(
                {
                    "task_id": f"refresh:{row['source_id']}:{row['stream_name']}",
                    "source_id": row["source_id"],
                    "source_name": row["name"],
                    "stream_name": row["stream_name"],
                    "due": due,
                    "cursor": row["cursor_value"],
                    "watermark_available_at": row["watermark_available_at"],
                    "endpoint_or_tool": source.get("endpoint_or_tool"),
                    "adapter_id": source.get("adapter_id"),
                    "ingestion_target": "curated" if curatable else "raw_license_gate",
                    "blocking_gate": None if curatable else "license_not_confirmed_for_curated_use",
                    "required_post_filters": source.get("quality_gates", []),
                }
            )
        return {
            "generated_at": utc_now(),
            "task_count": len(tasks),
            "due_count": sum(1 for task in tasks if task["due"]),
            "curated_due_count": sum(1 for task in tasks if task["due"] and task["ingestion_target"] == "curated"),
            "license_gated_due_count": sum(1 for task in tasks if task["due"] and task["ingestion_target"] == "raw_license_gate"),
            "tasks": tasks,
        }


def store_status(db_path: Path) -> dict[str, Any]:
    tables = [
        "source_catalog", "raw_packages", "metric_definitions", "taxonomy_nodes", "entities", "entity_aliases",
        "external_identifiers", "relationships", "entity_classifications", "documents", "document_entities",
        "document_chunks", "evidence", "observations", "ingestion_runs", "ingestion_errors",
        "source_cursors", "freshness_state", "quality_events",
    ]
    with connect(db_path) as connection:
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        fts = connection.execute("SELECT COUNT(*) FROM document_chunks_fts").fetchone()[0]
        curated_sources = connection.execute(
            """SELECT COUNT(*) FROM source_catalog
               WHERE lower(license_status) NOT LIKE '%pending%' AND lower(status) NOT LIKE '%license_gate%'"""
        ).fetchone()[0]
        database_bytes = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
        return {
            "database": str(db_path),
            "database_bytes": database_bytes,
            "counts": counts,
            "fts_chunks": fts,
            "curatable_sources": curated_sources,
            "foreign_key_violations": rows_as_dicts(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="消费行业研究知识库参考运行时")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--package", type=Path, required=True)
    ingest.add_argument("--target", choices=("raw", "curated"), default="curated")
    entity = sub.add_parser("resolve-entity")
    entity.add_argument("--query", required=True)
    entity.add_argument("--cutoff")
    metric = sub.add_parser("query-metric")
    metric.add_argument("--entity", required=True)
    metric.add_argument("--metric", required=True)
    metric.add_argument("--cutoff", required=True)
    metric.add_argument("--period-end")
    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--cutoff", required=True)
    search.add_argument("--entity")
    search.add_argument("--limit", type=int, default=10)
    trace = sub.add_parser("trace")
    trace.add_argument("--evidence", required=True)
    sub.add_parser("freshness")
    sub.add_parser("refresh-plan")
    sub.add_parser("status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "init":
        emit(init_store(args.db))
    elif args.command == "ingest":
        result = ingest_package(args.db, args.package, args.target)
        emit(result)
        return 0 if result["status"] in ("success", "raw_stored", "idempotent_noop") else 2
    elif args.command == "resolve-entity":
        emit(resolve_entity(args.db, args.query, args.cutoff))
    elif args.command == "query-metric":
        emit(query_metric(args.db, args.entity, args.metric, args.cutoff, args.period_end))
    elif args.command == "search":
        emit(search_documents(args.db, args.query, args.cutoff, args.entity, args.limit))
    elif args.command == "trace":
        emit(trace_evidence(args.db, args.evidence))
    elif args.command == "freshness":
        emit(freshness_report(args.db))
    elif args.command == "refresh-plan":
        emit(refresh_plan(args.db))
    elif args.command == "status":
        emit(store_status(args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
