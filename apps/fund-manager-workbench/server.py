from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import traceback
import urllib.request
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent.parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", APP_DIR))
RUNTIME_ROOT = BUNDLE_ROOT if getattr(sys, "frozen", False) else PROJECT_ROOT
TOOLS_ROOT = RUNTIME_ROOT / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import consumer_task_library as task_library  # noqa: E402
import consumer_workflow_engine as workflow  # noqa: E402
import agent_self_calibration as self_calibration  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")
ROLE_LABELS = {
    "public_fund_manager": "公募基金经理",
    "research_analyst": "研究员",
    "research_operator": "研究运营",
}
ALLOWED_ROLES = set(ROLE_LABELS)
LOCAL_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def normalize_allowed_hosts(raw: list[str] | str) -> set[str]:
    normalized = {item.lower() for item in (split_csv(raw) if isinstance(raw, str) else raw) if item}
    if not normalized:
        return set(LOCAL_ALLOWED_HOSTS)
    if "*" in normalized:
        return {"*"}
    return normalized
CATEGORY_LABELS = {
    "industry": "行业研究",
    "monitoring": "持续监控",
    "company": "公司研究",
    "event": "事件研究",
    "model": "模型分析",
}
STATUS_LABELS = {
    "queued": "排队中",
    "validating": "正在校验",
    "running": "研究执行中",
    "completed": "已完成",
    "blocked": "受阻",
    "failed": "失败",
    "cancelled": "已取消",
    "open": "待处理",
    "acknowledged": "已确认",
    "resolved": "已解决",
}


@contextmanager
def closing_knowledge_connection(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# The original research engines use sqlite connections as context managers,
# which commit but do not close on Windows. The desktop host replaces that
# factory for its process so backups, upgrades and clean shutdowns do not retain
# database file handles.
task_library.knowledge.connect = closing_knowledge_connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def repair_mojibake(value: str | None) -> str | None:
    if not value:
        return value
    markers = ("缇", "鏍", "骞", "浜", "鎴", "鍏", "璐", "绠", "锛")
    if not any(marker in value for marker in markers):
        return value
    try:
        repaired = value.encode("gb18030").decode("utf-8")
        return repaired if repaired.count("�") <= value.count("�") else value
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def default_cutoff_date() -> str:
    return datetime.now(SHANGHAI).date().isoformat()


def cutoff_timestamp(cutoff_date: str) -> str:
    try:
        date_value = datetime.strptime(cutoff_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("研究截止日期必须使用 YYYY-MM-DD 格式") from exc
    if date_value > datetime.now(SHANGHAI).date():
        raise ValueError("正式行业研究的截止日期最晚只能是今天")
    return datetime.combine(date_value, time(8, 0, 0), SHANGHAI).isoformat()


def safe_read_text(path_value: str | None, roots: list[Path]) -> str | None:
    if not path_value:
        return None
    original = Path(path_value)
    candidates = [original if original.is_absolute() else PROJECT_ROOT / original]
    lower_parts = [part.lower() for part in original.parts]
    if "data" in lower_parts:
        data_index = lower_parts.index("data")
        relative_data_path = Path(*original.parts[data_index + 1:])
        for root in roots:
            candidates.append(root / relative_data_path)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                return repair_mojibake(resolved.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError, OSError):
                continue
    return None


@dataclass(frozen=True)
class Identity:
    name: str
    role: str

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]


class WorkbenchService:
    def __init__(
        self,
        db_path: Path,
        data_root: Path,
        identity: Identity,
        static_root: Path,
        *,
        bound_host: str = "127.0.0.1",
        deployment_mode: str = "local_single_user",
        client_scope: str = "loopback_only",
        allowed_hosts: set[str] | None = None,
    ):
        self.db_path = db_path.resolve()
        self.data_root = data_root.resolve()
        self.identity = identity
        self.static_root = static_root.resolve()
        self.bound_host = bound_host
        self.client_scope = client_scope
        self.deployment_mode = deployment_mode
        self.allowed_hosts = normalize_allowed_hosts(allowed_hosts or set())
        self.session_token = secrets.token_urlsafe(32)
        self.installation_id = stable_id("install", str(self.db_path.parent), deployment_mode)
        self.session_id = stable_id("session", self.installation_id, identity.name, utc_now())
        self._workers: dict[str, threading.Thread] = {}
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if not self.db_path.exists():
            raise FileNotFoundError(f"研究数据库不存在：{self.db_path}")
        schema_path = RUNTIME_ROOT / "sql" / "008_consumer_fund_manager_workbench.sql"
        if not schema_path.exists():
            schema_path = PROJECT_ROOT / "sql" / "008_consumer_fund_manager_workbench.sql"
        with self.connect() as connection:
            connection.executescript(schema_path.read_text(encoding="utf-8"))
            now = utc_now()
            connection.execute(
                """INSERT INTO workbench_installations(
                       installation_id,installation_name,deployment_mode,bound_host,data_root,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(installation_id) DO UPDATE SET updated_at=excluded.updated_at,data_root=excluded.data_root""",
                (
                    self.installation_id,
                    "消费行业研究工作台",
                    self.deployment_mode,
                    self.bound_host,
                    str(self.data_root),
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO workbench_sessions(
                       session_id,installation_id,actor,actor_role,started_at,client_scope,status
                   ) VALUES(?,?,?,?,?,?,?)""",
                (self.session_id, self.installation_id, self.identity.name, self.identity.role, now, self.client_scope, "active"),
            )
        task_library.init_library(self.db_path, self.data_root / "task-library")
        workflow.init_engine(self.db_path)

    def audit(self, action: str, object_type: str | None = None, object_id: str | None = None,
              outcome: str = "success", detail: dict[str, Any] | None = None) -> None:
        now = utc_now()
        event_id = stable_id("wb-audit", self.session_id, action, object_id or "", now)
        safe_detail = detail or {}
        for forbidden in ("token", "secret", "password", "holdings", "positions"):
            safe_detail.pop(forbidden, None)
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO workbench_audit_events(
                       audit_event_id,session_id,actor,actor_role,action,object_type,object_id,outcome,detail_json,occurred_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event_id, self.session_id, self.identity.name, self.identity.role, action, object_type,
                 object_id, outcome, json.dumps(safe_detail, ensure_ascii=False, separators=(",", ":")), now),
            )

    def has_permission(self, capability: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT allowed FROM task_library_role_permissions WHERE role_id=? AND capability=?",
                (self.identity.role, capability),
            ).fetchone()
        return bool(row and row["allowed"])

    def bootstrap(self) -> dict[str, Any]:
        with self.connect() as connection:
            counts = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM research_sector_packs) AS sectors,
                     (SELECT COUNT(*) FROM task_library_products WHERE status='internal_active') AS products,
                     (SELECT COUNT(*) FROM monitor_alerts WHERE state='open') AS open_alerts,
                     (SELECT COUNT(*) FROM monitor_alerts WHERE state='open' AND severity IN ('important','critical')) AS important_alerts,
                     (SELECT COUNT(*) FROM monitor_task_triggers WHERE status IN ('suggested','queued')) AS task_suggestions,
                     (SELECT COUNT(*) FROM workflow_runs WHERE status='completed' AND publication_status='internal_research_ready') AS internal_ready_reports,
                     (SELECT COUNT(*) FROM task_library_jobs WHERE submitted_by=?) AS own_jobs""",
                (self.identity.name,),
            ).fetchone()
            monitor = connection.execute(
                "SELECT cutoff_timestamp,status,signals_evaluated,alerts_created,triggered_tasks,completed_at FROM monitor_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            freshness = {row["status"]: row["count"] for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM freshness_state GROUP BY status"
            ).fetchall()}
        return {
            "product": {"name": "消费行研agent", "version": "1.3.0", "deployment": "本机单用户"},
            "identity": {"name": self.identity.name, "role": self.identity.role, "role_label": self.identity.role_label},
            "cutoff": {"date": default_cutoff_date(), "timezone": "Asia/Shanghai", "rule": "正式研究截止至当日08:00:00"},
            "counts": dict(counts),
            "monitor": dict(monitor) if monitor else None,
            "freshness": freshness,
            "truth_boundary": "研究包已就绪不等于数据已回填；缺失、过期和授权门禁均会显式展示。",
            "prohibitions": ["不接入或推断基金持仓", "不生成自动交易指令", "不自动对外发布"],
        }

    def sectors(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.sector_code,p.sector_name,p.parent_domain,p.research_thesis,
                          p.cycle_drivers_json,p.value_chain_json,p.metric_ids_json,p.required_streams_json,
                          MAX(CASE WHEN c.market='A_SHARE' THEN c.security_count END) AS a_share_count,
                          MAX(CASE WHEN c.market='HK_SHARE' THEN c.security_count END) AS hk_share_count,
                          MAX(CASE WHEN c.market='A_SHARE' THEN c.metric_definition_count END) AS metric_count,
                          MAX(CASE WHEN c.market='A_SHARE' THEN c.populated_stream_count END) AS populated_streams,
                          MAX(CASE WHEN c.market='A_SHARE' THEN c.required_stream_count END) AS required_streams,
                          MAX(CASE WHEN c.market='A_SHARE' THEN c.universe_status END) AS a_universe_status,
                          MAX(CASE WHEN c.market='HK_SHARE' THEN c.universe_status END) AS hk_universe_status,
                          MAX(c.as_of_date) AS as_of_date,
                          SUM(CASE WHEN a.state='open' THEN 1 ELSE 0 END) AS open_alerts
                   FROM research_sector_packs p
                   LEFT JOIN research_coverage_status c ON c.sector_code=p.sector_code
                   LEFT JOIN monitor_alerts a ON a.sector_code=p.sector_code
                   GROUP BY p.sector_code,p.sector_name,p.parent_domain,p.research_thesis,
                            p.cycle_drivers_json,p.value_chain_json,p.metric_ids_json,p.required_streams_json
                   ORDER BY p.sector_code"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("cycle_drivers_json", "value_chain_json", "metric_ids_json", "required_streams_json"):
                item[key.removesuffix("_json")] = json_load(item.pop(key), [])
            result.append(item)
        return result

    def alerts(self, limit: int = 100, state: str = "open") -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT a.alert_id,a.rule_code,a.sector_code,p.sector_name,a.severity,a.title,a.detail_json,
                          a.first_detected_at,a.last_detected_at,a.occurrence_count,a.state,a.acknowledged_by,
                          a.publication_status,a.monitor_event_id,
                          m.title AS event_title,m.summary AS event_summary,m.source_url AS event_source_url,
                          m.locator AS event_locator,m.materiality_score AS event_materiality_score
                   FROM monitor_alerts a LEFT JOIN research_sector_packs p ON p.sector_code=a.sector_code
                   LEFT JOIN monitor_events m ON m.monitor_event_id=a.monitor_event_id
                   WHERE (?='all' OR a.state=?)
                   ORDER BY CASE a.severity WHEN 'critical' THEN 4 WHEN 'important' THEN 3 WHEN 'watch' THEN 2 ELSE 1 END DESC,
                            a.last_detected_at DESC LIMIT ?""",
                (state, state, limit),
            ).fetchall()
            source_rows = connection.execute(
                "SELECT source_id,name,source_family,evidence_tier,license_status,access_class,status,point_in_time_support,raw_json FROM source_catalog"
            ).fetchall()
        source_catalog: dict[str, dict[str, Any]] = {}
        for row in source_rows:
            source = dict(row)
            raw = json_load(source.pop("raw_json"), {})
            endpoint = raw.get("endpoint_or_tool")
            source["endpoint_type"] = "web" if isinstance(endpoint, str) and endpoint.startswith(("https://", "http://")) else "mcp" if raw.get("connection_mode") == "mcp" else "internal"
            source["source_url"] = endpoint if source["endpoint_type"] == "web" else None
            source["operator"] = raw.get("operator")
            source["coverage"] = raw.get("coverage", [])
            source["update_frequency"] = raw.get("update_frequency")
            source["reachability_status"] = raw.get("reachability_status")
            source["reachability_note"] = raw.get("reachability_note")
            source["alternate_entry"] = raw.get("alternate_entry")
            source_catalog[source["source_id"]] = source
        result = []
        for row in rows:
            item = dict(row)
            item["title"] = repair_mojibake(item["title"])
            item["detail"] = json_load(item.pop("detail_json"), {})
            source_id = item["detail"].get("source_id")
            item["source"] = source_catalog.get(source_id)
            item["event"] = {
                "monitor_event_id": item.pop("monitor_event_id"),
                "title": repair_mojibake(item.pop("event_title")),
                "summary": repair_mojibake(item.pop("event_summary")),
                "source_url": item.pop("event_source_url"),
                "locator": item.pop("event_locator"),
                "materiality_score": item.pop("event_materiality_score"),
            }
            item["state_label"] = STATUS_LABELS.get(item["state"], item["state"])
            result.append(item)
        return result

    def acknowledge_alert(self, alert_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute("SELECT state FROM monitor_alerts WHERE alert_id=?", (alert_id,)).fetchone()
            if not row:
                raise LookupError("监控事项不存在")
            if row["state"] == "open":
                connection.execute(
                    "UPDATE monitor_alerts SET state='acknowledged',acknowledged_by=?,acknowledged_at=? WHERE alert_id=?",
                    (self.identity.name, now, alert_id),
                )
                connection.execute(
                    """INSERT INTO monitor_alert_events(alert_event_id,alert_id,event_type,actor,occurred_at,detail_json)
                       VALUES(?,?,'acknowledged',?,?,'{}')""",
                    (stable_id("alert-event", alert_id, "acknowledged", now), alert_id, self.identity.name, now),
                )
        self.audit("acknowledge_alert", "monitor_alert", alert_id)
        return {"alert_id": alert_id, "state": "acknowledged", "acknowledged_by": self.identity.name}

    def tasks(self, query: str = "", sector: str | None = None, category: str | None = None,
              favorites_only: bool = False) -> dict[str, Any]:
        result = task_library.search_library(
            self.db_path, query=query, sector_code=sector or None, category=category or None,
            role=self.identity.role, user_id=self.identity.name,
        )
        if favorites_only:
            result["results"] = [item for item in result["results"] if item["is_favorite"]]
            result["result_count"] = len(result["results"])
        for item in result["results"]:
            item["category_label"] = CATEGORY_LABELS.get(item["category"], item["category"])
        return result

    def task_detail(self, product_id: str) -> dict[str, Any]:
        return task_library.product_detail(self.db_path, product_id, self.identity.role, self.identity.name)

    def set_favorite(self, product_id: str, favorite: bool) -> dict[str, Any]:
        if not self.has_permission("favorite"):
            raise PermissionError("当前角色不能收藏研究任务")
        now = utc_now()
        with self.connect() as connection:
            exists = connection.execute("SELECT 1 FROM task_library_products WHERE product_id=?", (product_id,)).fetchone()
            if not exists:
                raise LookupError("研究任务产品不存在")
            if favorite:
                connection.execute(
                    "INSERT OR IGNORE INTO task_library_favorites(user_id,product_id,created_at) VALUES(?,?,?)",
                    (self.identity.name, product_id, now),
                )
            else:
                connection.execute(
                    "DELETE FROM task_library_favorites WHERE user_id=? AND product_id=?",
                    (self.identity.name, product_id),
                )
        self.audit("favorite_task" if favorite else "unfavorite_task", "task_product", product_id)
        return {"product_id": product_id, "favorite": favorite}

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.has_permission("submit"):
            raise PermissionError("当前角色不能提交研究任务")
        forbidden_paths = task_library.production.scan_forbidden(payload)
        if forbidden_paths:
            raise ValueError("禁止提交基金持仓、仓位或组合推断字段")
        product_id = str(payload.get("product_id", "")).strip()
        question = str(payload.get("research_question", "")).strip()
        if len(question) < 5:
            raise ValueError("研究问题至少需要5个字符")
        cutoff = cutoff_timestamp(str(payload.get("cutoff_date") or default_cutoff_date()))
        markets = payload.get("markets") or ["A_SHARE"]
        if not isinstance(markets, list) or any(item not in {"A_SHARE", "HK_SHARE"} for item in markets):
            raise ValueError("市场范围无效")
        parameters: dict[str, Any] = {
            "cutoff_timestamp": cutoff,
            "research_question": question,
            "markets": markets,
            "priority": payload.get("priority", "normal"),
        }
        detail = self.task_detail(product_id)["product"]
        allowed = set(detail["parameter_schema"]["allowed"])
        optional_payload = {
            "lookback_days": payload.get("lookback_days"),
            "geographies": payload.get("geographies"),
            "entities": payload.get("entities"),
            "event": payload.get("event"),
            "scenario": payload.get("scenario"),
        }
        for key, value in optional_payload.items():
            if value not in (None, "", []) and (key in allowed or key == "entities"):
                parameters[key] = value
        with tempfile.TemporaryDirectory(prefix="consumer-workbench-") as temp_dir:
            parameter_path = Path(temp_dir) / "parameters.json"
            parameter_path.write_text(json.dumps(parameters, ensure_ascii=False), encoding="utf-8")
            result = task_library.submit_job(
                self.db_path, product_id, parameter_path, self.identity.name, self.identity.role,
                self.data_root / "task-library",
            )
        job = result["job"]
        self.audit("submit_research_job", "task_job", job["job_id"], detail={"product_id": product_id})
        if payload.get("execute_now", True):
            self._start_job(job["job_id"])
        return {"job": job, "idempotent_replay": result["idempotent_replay"]}

    def _start_job(self, job_id: str) -> None:
        active = self._workers.get(job_id)
        if active and active.is_alive():
            return

        def worker() -> None:
            try:
                task_library.run_job(
                    self.db_path, job_id, "local-explicit-task-runner", "research_operator",
                    self.data_root / "task-library",
                )
                self.audit("execute_research_job", "task_job", job_id)
            except Exception as exc:  # pragma: no cover - last-resort worker safety
                now = utc_now()
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE task_library_jobs SET status='failed',error_json=?,updated_at=?,completed_at=? WHERE job_id=?",
                        (json.dumps({"message": str(exc)}, ensure_ascii=False), now, now, job_id),
                    )
                self.audit("execute_research_job", "task_job", job_id, "failed", {"message": str(exc)[:500]})

        thread = threading.Thread(target=worker, name=f"research-{job_id[-8:]}", daemon=True)
        self._workers[job_id] = thread
        thread.start()

    def jobs(self) -> list[dict[str, Any]]:
        own_only = self.identity.role in {"public_fund_manager", "research_analyst"}
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT j.job_id,j.product_id,p.title,j.submitted_by,j.submitter_role,j.cutoff_timestamp,
                          j.priority,j.status,j.data_readiness,j.workflow_run_id,j.result_artifact_path,
                          j.error_json,j.created_at,j.started_at,j.completed_at,j.updated_at
                   FROM task_library_jobs j JOIN task_library_products p ON p.product_id=j.product_id
                   WHERE (?=0 OR j.submitted_by=?) ORDER BY j.created_at DESC LIMIT 200""",
                (1 if own_only else 0, self.identity.name),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
            item["error"] = json_load(item.pop("error_json"), None)
            result.append(item)
        return result

    def reports(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.run_id,r.status,r.publication_status,r.human_review_required,r.output_directory,
                          r.started_at,r.completed_at,p.cutoff_timestamp,p.template_id,p.request_json,
                          (SELECT path FROM workflow_artifacts a WHERE a.run_id=r.run_id AND a.artifact_type='research_report' ORDER BY created_at DESC LIMIT 1) AS report_path,
                           (SELECT COUNT(*) FROM workflow_claims c WHERE c.run_id=r.run_id) AS claim_count,
                          (SELECT COUNT(*) FROM workbench_report_annotations n WHERE n.run_id=r.run_id AND n.status='open') AS open_annotations
                   FROM workflow_runs r JOIN workflow_packages p ON p.package_id=r.package_id
                   ORDER BY r.started_at DESC LIMIT 100"""
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            request = json_load(item.pop("request_json"), {})
            item["title"] = repair_mojibake(request.get("research_question")) or f"研究报告 {item['run_id']}"
            item["status_label"] = STATUS_LABELS.get(item["status"], item["status"])
            output.append(item)
        return output

    def report_detail(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = connection.execute(
                """SELECT r.*,p.cutoff_timestamp,p.template_id,p.request_json
                   FROM workflow_runs r JOIN workflow_packages p ON p.package_id=r.package_id WHERE r.run_id=?""",
                (run_id,),
            ).fetchone()
            if not run:
                raise LookupError("研究报告不存在")
            claims = [dict(row) for row in connection.execute(
                "SELECT claim_id,module_id,text,content_label,importance,confidence,as_of_date,formula,input_ids_json,status FROM workflow_claims WHERE run_id=? ORDER BY CASE importance WHEN 'material' THEN 1 WHEN 'key' THEN 2 ELSE 3 END,claim_id",
                (run_id,),
            ).fetchall()]
            evidence_rows = connection.execute(
                """SELECT ce.claim_id,ce.relation_type,e.evidence_id,e.locator,e.evidence_tier,e.published_at,
                          e.available_at,e.license_tag,e.access_class,d.title AS document_title,d.publisher,d.source_url
                   FROM workflow_claim_evidence ce JOIN evidence e ON e.evidence_id=ce.evidence_id
                   JOIN documents d ON d.document_id=e.document_id WHERE ce.run_id=? ORDER BY ce.claim_id,ce.relation_type""",
                (run_id,),
            ).fetchall()
            annotations = [dict(row) for row in connection.execute(
                "SELECT annotation_id,claim_id,section_name,author,author_role,note,status,created_at,resolved_at,resolved_by FROM workbench_report_annotations WHERE run_id=? ORDER BY created_at DESC",
                (run_id,),
            ).fetchall()]
            artifact = connection.execute(
                "SELECT path FROM workflow_artifacts WHERE run_id=? AND artifact_type='research_report' ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
        for row in evidence_rows:
            item = dict(row)
            item["document_title"] = repair_mojibake(item["document_title"])
            evidence_by_claim.setdefault(item["claim_id"], []).append(item)
        for claim in claims:
            claim["text"] = repair_mojibake(claim["text"])
            claim["input_ids"] = json_load(claim.pop("input_ids_json"), [])
            claim["evidence"] = evidence_by_claim.get(claim["claim_id"], [])
        run_value = dict(run)
        request = json_load(run_value.pop("request_json"), {})
        title = repair_mojibake(request.get("research_question")) or f"研究报告 {run_id}"
        roots = [PROJECT_ROOT / "data", self.data_root, Path(sys.executable).parent / "data"]
        report_content = safe_read_text(artifact["path"] if artifact else None, roots)
        self.audit("view_report", "workflow_run", run_id)
        return {
            "run": run_value,
            "title": title,
            "request": request,
            "report_markdown": report_content,
            "claims": claims,
            "annotations": annotations,
        }

    def annotate_report(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        note = str(payload.get("note", "")).strip()
        if len(note) < 2:
            raise ValueError("批注内容不能为空")
        claim_id = str(payload.get("claim_id", "")).strip() or None
        section_name = str(payload.get("section_name", "")).strip() or None
        now = utc_now()
        annotation_id = stable_id("annotation", run_id, claim_id or section_name or "report", self.identity.name, now)
        with self.connect() as connection:
            run = connection.execute("SELECT 1 FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise LookupError("研究报告不存在")
            if claim_id:
                claim = connection.execute(
                    "SELECT 1 FROM workflow_claims WHERE run_id=? AND claim_id=?", (run_id, claim_id)
                ).fetchone()
                if not claim:
                    raise LookupError("报告结论不存在")
            connection.execute(
                """INSERT INTO workbench_report_annotations(
                       annotation_id,run_id,claim_id,section_name,author,author_role,note,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,'open',?)""",
                (annotation_id, run_id, claim_id, section_name, self.identity.name, self.identity.role, note, now),
            )
        self.audit("annotate_report", "workflow_run", run_id, detail={"claim_id": claim_id})
        return {"annotation_id": annotation_id, "status": "open", "author": self.identity.name}

    def entities(self, query: str) -> list[dict[str, Any]]:
        query = query.strip()
        if len(query) < 1:
            return []
        term = f"%{query}%"
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT e.entity_id,e.entity_type,e.canonical_name,
                          GROUP_CONCAT(DISTINCT x.value) AS identifiers
                   FROM entities e LEFT JOIN external_identifiers x ON x.entity_id=e.entity_id
                   LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id
                   WHERE e.canonical_name LIKE ? OR a.alias LIKE ? OR x.value LIKE ?
                   GROUP BY e.entity_id,e.entity_type,e.canonical_name ORDER BY e.canonical_name LIMIT 30""",
                (term, term, term),
            ).fetchall()
        return [dict(row) for row in rows]

    def document_page(self, document_id: str) -> dict[str, Any] | None:
        """库内原文页数据：本地缓存全文优先，库内摘录兜底。"""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT document_id,title,publisher,published_at,as_of_date,source_url,local_object_path,evidence_tier FROM documents WHERE document_id=?",
                (document_id,),
            ).fetchone()
            if not row:
                return None
            doc = dict(row)
            chunks = [r["text_content"] for r in connection.execute(
                "SELECT text_content FROM document_chunks WHERE document_id=? ORDER BY sequence_no",
                (document_id,),
            ).fetchall()]
        doc["full_text"] = None
        local = doc.get("local_object_path")
        if local:
            path = (PROJECT_ROOT / local).resolve()
            if path.is_file() and path.is_relative_to(PROJECT_ROOT):
                doc["full_text"] = path.read_text(encoding="utf-8", errors="ignore")
        doc["excerpt"] = "\n\n".join(chunks) if chunks else None
        return doc

    def research_report_page(self, event_id: str) -> dict[str, Any] | None:
        """聚源授权研报的库内元数据与合规摘录页。"""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT monitor_event_id,title,summary,event_time,available_at,locator,raw_json
                   FROM monitor_events WHERE monitor_event_id=? AND event_type='research_report'
                     AND status='accepted'""",
                (event_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        raw = json_load(item.pop("raw_json"), {})
        metadata = raw.get("research_report", {}) if isinstance(raw, dict) else {}
        return {**item, **metadata}

    def data_status(self) -> dict[str, Any]:
        with self.connect() as connection:
            freshness = [dict(row) for row in connection.execute(
                """SELECT f.source_id,s.name AS source_name,f.stream_name,f.expected_max_lag_hours,
                          f.latest_available_at,f.checked_at,f.lag_hours,f.status
                   FROM freshness_state f LEFT JOIN source_catalog s ON s.source_id=f.source_id
                   ORDER BY CASE f.status WHEN 'fresh' THEN 1 WHEN 'stale' THEN 2 ELSE 3 END,f.source_id,f.stream_name"""
            ).fetchall()]
            licenses = [dict(row) for row in connection.execute(
                """SELECT s.source_id,s.name,s.source_family,s.license_status,s.access_class,s.status,
                          d.decision,d.redistribution_allowed,d.cache_policy,d.notes,d.updated_at
                   FROM source_catalog s LEFT JOIN source_license_decisions d ON d.source_id=s.source_id
                   ORDER BY CASE COALESCE(d.decision,'pending') WHEN 'pending' THEN 1 WHEN 'rejected' THEN 2 ELSE 3 END,s.source_id"""
            ).fetchall()]
            coverage = [dict(row) for row in connection.execute(
                """SELECT c.sector_code,p.sector_name,c.market,c.security_count,c.metric_definition_count,
                          c.required_stream_count,c.populated_stream_count,c.metric_population_status,
                          c.universe_status,c.research_pack_status,c.blockers_json,c.as_of_date
                   FROM research_coverage_status c JOIN research_sector_packs p ON p.sector_code=c.sector_code
                   ORDER BY c.sector_code,c.market"""
            ).fetchall()]
            snapshot_counts = {row["license_status"]: row["count"] for row in connection.execute(
                "SELECT license_status,COUNT(*) AS count FROM production_snapshots GROUP BY license_status"
            ).fetchall()}
        for item in coverage:
            item["blockers"] = json_load(item.pop("blockers_json"), [])
        return {
            "freshness": freshness,
            "licenses": licenses,
            "coverage": coverage,
            "snapshot_counts": snapshot_counts,
            "truth_boundary": "ready 表示研究结构可用，不代表所需数据已经完成生产回填。",
        }

    def ops_status(self) -> dict[str, Any]:
        """正式交付状态卡：自动同步、数据日期一致性、最近日志。"""
        today = datetime.now(SHANGHAI).date().isoformat()
        def next_weekday_run(hour: int, minute: int) -> datetime:
            run_at = datetime.combine(datetime.now(SHANGHAI).date(), time(hour, minute), SHANGHAI)
            if datetime.now(SHANGHAI) >= run_at:
                run_at = run_at + timedelta(days=1)
            while run_at.weekday() >= 5:
                run_at = run_at + timedelta(days=1)
            return run_at

        next_sync = next_weekday_run(8, 40)
        next_close_sync = next_weekday_run(16, 10)
        log_candidates = [
            PROJECT_ROOT / "data" / "monitoring" / "module3-realtime-research" / "server-daily-sync.log",
            self.data_root.parent.parent / "monitoring" / "module3-realtime-research" / "server-daily-sync.log",
        ]
        log_path = next((p for p in log_candidates if p.is_file()), log_candidates[0])
        log_tail: list[str] = []
        last_success_at = None
        last_failure = None
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
                log_tail = lines[-12:]
                for line in reversed(lines):
                    m = re.search(r"\[(.*?)\]", line)
                    if "每日晨报同步结束" in line and m:
                        last_success_at = m.group(1)
                        break
                handled_degradation_markers = (
                    "核心文案暂不可用",
                    "模型不可用，研报库使用真实底座兜底生成",
                    "晨报文案已入库",
                    "晨报文案撰写完成",
                )
                handled_degradation = any(marker in line for line in lines for marker in handled_degradation_markers)
                for line in reversed(lines):
                    if any(k in line for k in ("失败", "Error", "Exception", "Traceback")):
                        is_llm_optional_failure = (
                            handled_degradation
                            and any(k in line for k in ("撰写调用失败", "大事件第", "研报库第"))
                            and any(k in line for k in ("Connection refused", "积极拒绝", "Errno 111", "WinError 10061"))
                        )
                        if is_llm_optional_failure:
                            continue
                        last_failure = line[-220:]
                        break
            except Exception as exc:
                last_failure = f"日志读取失败：{exc}"
        with self.connect() as connection:
            stock_date = connection.execute(
                "SELECT MAX(rating_date) AS rating_date FROM daily_stock_ratings"
            ).fetchone()
            latest_rating_date = stock_date["rating_date"] if stock_date else None
            stock = connection.execute(
                """SELECT ? AS rating_date, MAX(quote_trade_date) AS market_date, COUNT(*) AS rows
                   FROM daily_stock_ratings WHERE rating_date=?""",
                (latest_rating_date, latest_rating_date),
            ).fetchone() if latest_rating_date else None
            brief = connection.execute("SELECT MAX(brief_date) AS brief_date FROM daily_brief_sections").fetchone()
            quote = connection.execute("SELECT MAX(trade_date) AS quote_date, COUNT(*) AS rows FROM stock_daily_quotes").fetchone()
            source_rows = [dict(row) for row in connection.execute(
                """SELECT s.source_id,s.name,MAX(c.last_success_at) AS last_sync
                   FROM source_catalog s LEFT JOIN source_cursors c ON c.source_id=s.source_id
                   GROUP BY s.source_id,s.name ORDER BY last_sync DESC LIMIT 8"""
            ).fetchall()]
        rating_date = stock["rating_date"] if stock else None
        market_date = stock["market_date"] if stock else None
        brief_date = brief["brief_date"] if brief else None
        quote_date = quote["quote_date"] if quote else None
        mismatches = []
        if rating_date and market_date and rating_date != market_date:
            now_shanghai = datetime.now(SHANGHAI)
            before_close_sync = now_shanghai < datetime.combine(now_shanghai.date(), time(16, 10), SHANGHAI)
            expected_morning_snapshot = rating_date == today and quote_date == market_date and before_close_sync
            if expected_morning_snapshot:
                pass
            else:
                mismatches.append(f"股票评级日期 {rating_date} 与行情实际交易日 {market_date} 不一致")
        if brief_date and rating_date and brief_date != rating_date:
            mismatches.append(f"晨报日期 {brief_date} 与股票评级日期 {rating_date} 不一致")
        stale = rating_date != today
        daily_sync_ok = not stale
        daily_sync_detail = last_success_at or ("今日数据已更新（未发现 systemd 完成日志，可能为手动同步）" if daily_sync_ok else "尚未发现同步完成日志")
        healthy = daily_sync_ok and not mismatches and not last_failure
        if stale:
            mismatches.insert(0, f"今日 {today} 尚未生成最新股票评级，当前最新为 {rating_date or '无'}")
        return {
            "status": "ok" if healthy else ("warn" if rating_date else "error"),
            "today": today,
            "last_success_at": last_success_at,
            "next_sync_at": next_sync.isoformat(timespec="minutes"),
            "next_close_sync_at": next_close_sync.isoformat(timespec="minutes"),
            "sync_schedule": {
                "morning": "周一至周五 08:40，更新晨报、研报、新闻、政策、企业风险、行情快照、股票评级、自校准",
                "close": "周一至周五 16:10，更新收盘行情快照与股票评级，不改写早盘晨报口径",
            },
            "dates": {
                "brief_date": brief_date,
                "rating_date": rating_date,
                "market_date": market_date,
                "quote_date": quote_date,
                "stock_rows": stock["rows"] if stock else 0,
                "quote_rows": quote["rows"] if quote else 0,
            },
            "checks": [
                {"key": "daily_sync", "label": "自动同步", "ok": daily_sync_ok, "detail": daily_sync_detail},
                {"key": "date_consistency", "label": "日期一致性", "ok": not mismatches, "detail": "一致" if not mismatches else "；".join(mismatches)},
                {"key": "market_snapshot", "label": "行情口径", "ok": True, "detail": "08:40 早盘评级允许使用上一交易日收盘行情；16:10 收盘同步后刷新为最新收盘口径" if rating_date == today and market_date and market_date != rating_date else "最新收盘口径"},
                {"key": "service_data", "label": "股票看板", "ok": bool(rating_date and stock and stock["rows"]), "detail": f"{rating_date or '无'} · {stock['rows'] if stock else 0} 条评级"},
            ],
            "last_failure": last_failure,
            "sources": source_rows,
            "log_tail": log_tail,
        }

    def self_calibration_status(self) -> dict[str, Any]:
        """Agent 自校准：自检、自动修复、快照/后验、影子规则与自动回滚事件。"""
        def parse_json(value: Any, fallback: Any) -> Any:
            if not value:
                return fallback
            try:
                return json.loads(value)
            except Exception:
                return fallback

        with self.connect() as connection:
            self_calibration.ensure_schema(connection)
            audit = connection.execute(
                """SELECT * FROM agent_self_audit_runs
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            active_rule = connection.execute(
                """SELECT * FROM agent_rule_versions
                   WHERE status='active' ORDER BY activated_at DESC,created_at DESC LIMIT 1"""
            ).fetchone()
            shadow_rule = connection.execute(
                """SELECT * FROM agent_rule_versions
                   WHERE status='shadow' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            events = [dict(row) for row in connection.execute(
                """SELECT event_type,from_version,to_version,reason,metrics_json,created_at
                   FROM agent_rule_events ORDER BY created_at DESC LIMIT 8"""
            ).fetchall()]
            snapshot_summary = connection.execute(
                """SELECT COUNT(*) AS rows,
                          COUNT(DISTINCT snapshot_date) AS days,
                          MAX(snapshot_date) AS latest_date,
                          SUM(CASE WHEN is_main_push=1 THEN 1 ELSE 0 END) AS main_rows
                   FROM agent_recommendation_snapshots"""
            ).fetchone()
            outcome_rows = [dict(row) for row in connection.execute(
                """SELECT COALESCE(s.snapshot_group,CASE WHEN s.is_main_push=1 THEN 'main_push' ELSE 'other' END) AS snapshot_group,
                          horizon,COUNT(*) AS samples,AVG(o.absolute_return) AS avg_return,
                          SUM(CASE WHEN o.absolute_return>0 THEN 1 ELSE 0 END)*1.0/COUNT(*) AS win_rate
                   FROM agent_recommendation_outcomes o
                   JOIN agent_recommendation_snapshots s
                     ON s.snapshot_date=o.snapshot_date AND s.security_id=o.security_id
                   WHERE o.absolute_return IS NOT NULL
                   GROUP BY snapshot_group,horizon
                   ORDER BY CASE snapshot_group
                       WHEN 'main_push' THEN 1
                       WHEN 'buy_candidate' THEN 2
                       WHEN 'watch_signal' THEN 3
                       WHEN 'long_quality' THEN 4
                       WHEN 'sector_scan' THEN 5
                       ELSE 9 END,
                       CASE horizon WHEN 'T+1' THEN 1 WHEN 'T+5' THEN 2 WHEN 'T+20' THEN 3 WHEN 'T+60' THEN 4 ELSE 9 END"""
            ).fetchall()]
            outcome_group_rows = [dict(row) for row in connection.execute(
                """SELECT COALESCE(s.snapshot_group,CASE WHEN s.is_main_push=1 THEN 'main_push' ELSE 'other' END) AS snapshot_group,
                          COUNT(*) AS rows,
                          COUNT(DISTINCT s.snapshot_date) AS days
                   FROM agent_recommendation_snapshots s
                   GROUP BY snapshot_group"""
            ).fetchall()]
        audit_dict = dict(audit) if audit else {}
        checks = parse_json(audit_dict.get("checks_json"), [])
        issues = parse_json(audit_dict.get("issues_json"), [])
        fixes = parse_json(audit_dict.get("auto_fixes_json"), [])
        return {
            "status": audit_dict.get("status") or "unknown",
            "run_date": audit_dict.get("run_date"),
            "created_at": audit_dict.get("created_at"),
            "checks": checks,
            "issues": issues,
            "auto_fixes": fixes,
            "active_rule": dict(active_rule) if active_rule else None,
            "shadow_rule": dict(shadow_rule) if shadow_rule else None,
            "events": [
                {**event, "metrics": parse_json(event.get("metrics_json"), {})}
                for event in events
            ],
            "snapshots": dict(snapshot_summary) if snapshot_summary else {},
            "outcomes": [
                {
                    **row,
                    "group_label": {
                        "main_push": "每日主推清单",
                        "buy_candidate": "可以考虑买入",
                        "watch_signal": "等待买点",
                        "long_quality": "长期观察",
                        "sector_scan": "暂不推荐/行业扫描",
                    }.get(row.get("snapshot_group"), row.get("snapshot_group") or "其他"),
                    "avg_return": round(float(row["avg_return"]), 2) if row.get("avg_return") is not None else None,
                    "win_rate": round(float(row["win_rate"]) * 100, 1) if row.get("win_rate") is not None else None,
                }
                for row in outcome_rows
            ],
            "outcome_groups": [
                {
                    **row,
                    "group_label": {
                        "main_push": "每日主推清单",
                        "buy_candidate": "可以考虑买入",
                        "watch_signal": "等待买点",
                        "long_quality": "长期观察",
                        "sector_scan": "暂不推荐/行业扫描",
                    }.get(row.get("snapshot_group"), row.get("snapshot_group") or "其他"),
                }
                for row in outcome_group_rows
            ],
            "guardrails": [
                "无人工批准：自动自检、自动修复、自动灰度、自动回滚",
                "无效价格不得参与评分、图表和收益计算",
                "暂缓买入/暂不推荐不得进入主推",
                "规则变更必须有版本、事件和可回滚记录",
            ],
        }

    def morning_brief(self, brief_date: str | None = None) -> dict[str, Any]:
        """每日消费行研晨报：五模块倒金字塔结构，全部由研究底座真实内容装配。
        brief_date 指定时返回该日撰写文案（晨报历史档案）。"""
        with self.connect() as connection:
            events = [dict(row) for row in connection.execute(
                """SELECT e.monitor_event_id,e.event_type,e.event_time,e.available_at,e.sector_code,
                          e.title,e.summary,e.materiality_score,e.locator,e.source_url,e.raw_json,p.sector_name
                   FROM monitor_events e LEFT JOIN research_sector_packs p ON p.sector_code=e.sector_code
                   WHERE e.status='accepted' AND e.available_at <= ? ORDER BY e.available_at DESC""",
                (cutoff_timestamp(default_cutoff_date()),),
            ).fetchall()]
            releases = [dict(row) for row in connection.execute(
                """SELECT d.document_id,d.title,d.publisher,d.published_at,d.as_of_date,d.evidence_tier,
                          (SELECT c.text_content FROM document_chunks c
                            WHERE c.document_id=d.document_id ORDER BY c.sequence_no LIMIT 1) AS key_figure
                   FROM documents d WHERE d.document_type='official_statistics_release' AND d.status='curated'
                   ORDER BY d.published_at DESC"""
            ).fetchall()]
            retail = connection.execute(
                """SELECT value_numeric,period_end FROM observations
                   WHERE metric_id='CR.MAC.RETAIL_SALES' AND is_current=1
                   ORDER BY period_end DESC LIMIT 2"""
            ).fetchall()
        for e in events:
            raw = json_load(e.pop("raw_json"), {})
            layer = raw.get("research_layer", {}) if isinstance(raw, dict) else {}
            e["module"] = layer.get("module")
            e["tone"] = layer.get("tone", "neutral")
            e["so_what"] = layer.get("so_what")
            e["data_rows"] = layer.get("data_rows", [])
            e["abstract"] = layer.get("abstract")
        by_module: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            if e["module"]:
                by_module.setdefault(e["module"], []).append(e)

        # 时效规则：宏观与政策面只保留最近 7 天内到达的事件，按重要性最多 4 条；
        # 子行业跟踪保留旧事件但必须标注"观点截至日期"；更早的官方发布不进当日晨报。
        now_dt = datetime.now(timezone.utc)

        def event_age_days(e: dict[str, Any]) -> int | None:
            try:
                t = datetime.fromisoformat(str(e.get("available_at") or "").replace("Z", "+00:00"))
            except ValueError:
                return None
            return max(0, (now_dt - t).days)

        def is_recent(e: dict[str, Any], days: int = 7) -> bool:
            age = event_age_days(e)
            return age is not None and age <= days

        def slim(e: dict[str, Any]) -> dict[str, Any]:
            age = event_age_days(e)
            return {**{k: e.get(k) for k in (
                "monitor_event_id", "event_type", "event_time", "title", "summary",
                "materiality_score", "locator", "sector_name", "module", "tone", "so_what", "data_rows", "abstract", "source_url", "available_at")},
                "stale": not is_recent(e), "age_days": age}

        market = by_module.get("market", [])
        baijiu = by_module.get("baijiu", [])
        mass_food = by_module.get("mass_food", [])
        appliance = by_module.get("appliance", [])
        new_consumer = by_module.get("new_consumer", [])
        macro_events = by_module.get("macro", [])
        policy_events = by_module.get("policy", [])

        # 每日撰写文案（若有）：核心观点/宏观解读/风格催化/风险提示按日更新
        with self.connect() as connection:
            available_dates = [r[0] for r in connection.execute(
                "SELECT DISTINCT brief_date FROM daily_brief_sections ORDER BY brief_date DESC LIMIT 30"
            ).fetchall()]
            if brief_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", brief_date):
                target_date = brief_date if brief_date in available_dates else None
            else:
                target_date = None
            latest_brief_date = target_date or (available_dates[0] if available_dates else None)
            stored: dict[str, Any] = {}
            if latest_brief_date:
                for row in connection.execute(
                    "SELECT section, content_json FROM daily_brief_sections WHERE brief_date=?",
                    (latest_brief_date,),
                ).fetchall():
                    stored[row["section"]] = json_load(row["content_json"], None)

        def resolve_pick_source(hint: str | None) -> dict[str, Any] | None:
            """大事件来源回链：按标题模糊匹配到底座事件或文档。"""
            if not hint or "官方发布数据" in hint:
                return None
            with self.connect() as conn3:
                event = conn3.execute(
                    """SELECT monitor_event_id,title,summary,locator,source_url,materiality_score
                       FROM monitor_events WHERE status='accepted'
                         AND (title LIKE ? OR ? LIKE '%' || title || '%')
                       ORDER BY available_at DESC LIMIT 1""",
                    (f"%{hint[:30]}%", hint[:80]),
                ).fetchone()
                if event:
                    return {"type": "event", **dict(event)}
                doc = conn3.execute(
                    """SELECT document_id,title,publisher,published_at,source_url
                       FROM documents WHERE title LIKE ? ORDER BY published_at DESC LIMIT 1""",
                    (f"%{hint[:30]}%",),
                ).fetchone()
                if doc:
                    return {"type": "document", **dict(doc)}
            return None

        daily_picks_raw = stored.get("daily_events")
        daily_picks = None
        if isinstance(daily_picks_raw, dict):
            def enrich(pick: Any) -> Any:
                if not isinstance(pick, dict):
                    return pick
                return {**pick, "source": resolve_pick_source(pick.get("source_hint"))}
            daily_picks = {
                "macro_policies": [enrich(p) for p in daily_picks_raw.get("macro_policies", [])],
                "research_pick": enrich(daily_picks_raw.get("research_pick")) if daily_picks_raw.get("research_pick") else None,
                "industry_events": [enrich(p) for p in daily_picks_raw.get("industry_events", [])],
            }

        top = max(events, key=lambda e: float(e["materiality_score"] or 0)) if events else None
        bullish = [e for e in events if e["tone"] == "bullish"]
        latest_retail = retail[0] if retail else None

        def find_event(keyword: str) -> dict[str, Any] | None:
            for e in events:
                if keyword in e["title"]:
                    return e
            return None

        ref_ids = {
            "market": (find_event("资金快照") or {}).get("monitor_event_id"),
            "baijiu_rally": (find_event("白酒板块近月反弹") or {}).get("monitor_event_id"),
            "baijiu_price": (find_event("名酒批价日报") or {}).get("monitor_event_id"),
            "q1": (find_event("一季报") or {}).get("monitor_event_id"),
            "close": (find_event("收盘快照") or {}).get("monitor_event_id"),
        }
        takeaway = [
            {"label": "大盘判断", "tone": "neutral",
             "text": "2026年8月13日（昨日），A股消费板块跑赢大盘：中证主要消费指数（800消费）收报12844.67点、涨0.16%，同日沪深300下跌0.57%，相对收益约0.7个百分点；主力资金当日净流入消费板块11.11亿元，沪深300整体则净流出175.94亿元。结构上，白酒是本轮反弹主线（中证白酒指数近一月+10.64%），白电稳健，新消费分化。今日上午盘消费随大盘回调约1.3%，判断属反弹途中正常换手。综合：消费建议【标配】，结构偏向高端白酒与白电龙头。",
             "refs": [ref_ids["market"]]},
            {"label": "最重要边际", "tone": "bullish",
             "text": "近期白酒板块出现三重触底信号：价格端——贵州茅台8月8日将自营店飞天茅台零售价上调至1753元/瓶，年内出厂价已两度上调至1369元/瓶；第八代五粮液批价单周涨约30元至760元/瓶，多地烟酒店求货。资金端——中证白酒指数近一月反弹10.64%，迎驾、古井涨超23%，科技流出资金部分流入白酒避险。估值持仓端——板块市盈率处近十年低位，公募二季度已将白酒减至超低配（易方达蓝筹白酒占比38.95%→15.68%），筹码出清充分。兴业证券沈昊团队判断行业已进入结构性触底阶段，预计三季度迎估值修复行情。",
             "refs": [ref_ids["baijiu_rally"], ref_ids["baijiu_price"]]},
            {"label": "交易提示", "tone": "bullish",
             "text": "研究观点（非交易指令）建议两条主线：一是高端白酒龙头（贵州茅台、山西汾酒）——估值与持仓双底部叠加龙头提价，价格锚重新确立，批价企稳（飞天散瓶1695元、普五830元、国窖825元），腰部酒企仍受库存与倒挂压制，应回避；二是白电双龙头（美的集团、格力电器）——2026年一季报营收与归母净利双增（美的1310.99亿元+2.55%、格力429.66亿元+3.52%），卖方确认经营拐点，出口链与三季度内销改善是主线，格力PE（TTM）仅7.7倍，高股息突出。回避缺乏效率优势的中间新消费品牌。",
             "refs": [ref_ids["q1"], ref_ids["close"], ref_ids["baijiu_rally"]]},
            {"label": "风险预警", "tone": "risk",
             "text": "本轮白酒反弹面临三重证伪风险：一是渠道库存未出清——中国酒业协会报告显示56.6%经销商反映价格倒挂同比加剧，行业上半年量利同步承压，终端动销未验证；二是中报业绩地雷——已预告的8家酒企仅五粮液大幅预增，水井坊、金种子等7家预亏或利润下滑超60%，8月中下旬密集披露期需逐家排雷；三是风格反复——8月14日上午盘800消费已随大盘回调1.27%，若科技重新吸血，消费相对收益可能回吐。中秋备货动销是检验反弹成色的关键窗口。",
            "refs": [ref_ids["baijiu_rally"], ref_ids["market"]]},
        ]
        if not stored.get("takeaway"):
            takeaway = [
                {"label": "大盘判断", "tone": "neutral", "text": "今日核心观点尚未生成，系统不再用历史日期文案冒充当日判断。请以页面行情、评级及各数据源标注日期为准。", "refs": []},
                {"label": "最重要边际", "tone": "neutral", "text": "今日核心观点尚未生成；待每日同步完成后自动更新。", "refs": []},
                {"label": "交易提示", "tone": "neutral", "text": "研究观点（非交易指令）：今日核心观点尚未生成，不输出方向性建议。", "refs": []},
                {"label": "风险预警", "tone": "risk", "text": "数据或撰写服务尚未完成当日同步，请勿沿用历史结论。", "refs": []},
            ]
        if stored.get("takeaway"):
            takeaway = [
                {"label": item.get("label", ""), "tone": item.get("tone", "neutral"),
                 "text": item.get("text", ""), "refs": [rid for rid in item.get("refs", []) if rid]}
                for item in stored["takeaway"] if isinstance(item, dict)
            ] or takeaway
        brief_source_date = latest_brief_date if stored else None

        by_id = {e["monitor_event_id"]: slim(e) for e in events}
        takeaway_event_ids = {rid for t in takeaway for rid in t["refs"] if rid}
        takeaway_events = {rid: by_id[rid] for rid in takeaway_event_ids if rid in by_id}

        macro_data = []
        if latest_retail:
            macro_data.append({
                "name": "社会消费品零售总额（当月）",
                "value": f"{latest_retail['value_numeric'] / 1e8:,.0f}亿元",
                "change": "+1.0%（6月同比）",
                "note": "5月-0.6%后单月回正，需求侧压力边际放缓",
            })

        recent_macro_policy = sorted(
            [e for e in macro_events + policy_events if is_recent(e)],
            key=lambda e: (str(e.get("available_at") or ""), float(e["materiality_score"] or 0)), reverse=True,
        )[:4]

        subsectors = [
            {"key": "baijiu", "name": "白酒", "events": [slim(e) for e in baijiu],
             "judgment": {"tone": "bullish",
                          "text": "结构性触底确认：估值与持仓双底部+龙头提价+资金避险共振。建议加高端（茅台、汾酒）、避腰部（库存与倒挂重灾区）；中秋动销是验证窗口。"}},
            {"key": "mass_food", "name": "大众品", "events": [slim(e) for e in mass_food],
             "judgment": {"tone": "bullish",
                          "text": "利润压力最大季度或已过去：26Q2涨上游不涨下游定价充分，Q3起利润端普遍好于Q2；社零6月单月回正支撑需求边际改善。"}},
            {"key": "appliance", "name": "家电", "events": [slim(e) for e in appliance],
             "judgment": {"tone": "bullish",
                          "text": "经营拐点清晰：双龙头Q1营收利润双增，7月后多数品类周度销售改善，外销冰洗双位数增长；估值处历史低位（格力PE 7.7倍），出口链+Q3白电改善是主线。"}},
            {"key": "new_consumer", "name": "新消费", "events": [slim(e) for e in new_consumer],
             "judgment": {"tone": "neutral",
                          "text": "存量效率战：茶饮30万店高饱和洗牌，咖啡以30%门店增速换客单价下行（42→25元）；潮玩龙头增速换挡。赢家是供应链与加盟效率强者，回避中间地带品牌。"}},
        ]

        risks = [
            {"tone": "risk", "text": "终端动销不及预期：白酒渠道库存与倒挂仍在高位（56.6%经销商倒挂加剧），中秋备货是反弹真伪的试金石。"},
            {"tone": "risk", "text": "中报业绩地雷：已有水井坊、金种子等7家酒企预亏或利润大幅下滑超60%，腰部酒企中报披露期需逐一排雷。"},
            {"tone": "risk", "text": "市场风格反复：今日上午800消费随大盘回调1.27%，若科技板块重新吸血，消费相对收益可能回吐。"},
        ]

        return {
            "date": default_cutoff_date(),
            "generated_at": utc_now(),
            "takeaway": takeaway,
            "takeaway_events": takeaway_events,
            "macro_policy": {
                "data": macro_data,
                "events": [slim(e) for e in recent_macro_policy],
                "daily_picks": daily_picks,
                "read": stored.get("macro_read") or "社零6月单月同比回正（+1.0%）、CPI温和（+1.0%）、居民收入名义+5.2%；央行货政报告确认贷款利率历史新低，消费信贷与居民负债成本继续下行。宏观组合对消费EPS压制边际缓解，政策端《扩大消费“十五五”规划》批复（2030年社零60万亿目标）提供中长期托底。",
            },
            "sector_review": {
                "events": [slim(e) for e in market],
                "style": stored.get("sector_style") or "消费内部高端白酒反弹占优（近月+10.64%），白电稳健，新消费分化；与市场整体呈高低切换特征——资金自科技流出部分流入消费避险，该逻辑今日上午随大盘回调但未被证伪。",
                "catalysts": stored.get("catalysts") or "8月中下旬进入中报密集披露期（小熊电器、福恩股份已披露，白电双龙头中报待披露）；9月中秋备货动销为白酒关键验证窗口；《扩大消费“十五五”规划》后续落地细则。",
            },
            "subsectors": subsectors,
            "risks": ([{"tone": "risk", "text": str(t)} for t in stored["risks"]] if stored.get("risks") else risks),
            "brief_source_date": brief_source_date,
            "available_dates": available_dates,
            "is_history": bool(target_date and available_dates and target_date != available_dates[0]),
            "boundary": "本晨报为研究观点汇编，全部内容可溯源至研究底座事件与文档；不构成自动交易指令，不接入也不推断任何基金持仓。",
        }

    def stock_focus(self, rating_date: str | None = None) -> dict[str, Any]:
        """今日股票关注：全自动主推清单 + 消费股票池看板。
        rating_date 指定时返回不晚于该日的最近评级批次（晨报历史联动）。"""
        with self.connect() as connection:
            rating_columns = {row[1] for row in connection.execute("PRAGMA table_info(daily_stock_ratings)").fetchall()}
            for column, ddl in {
                "invest_score": "ALTER TABLE daily_stock_ratings ADD COLUMN invest_score REAL",
                "stability_score": "ALTER TABLE daily_stock_ratings ADD COLUMN stability_score REAL",
                "board_status": "ALTER TABLE daily_stock_ratings ADD COLUMN board_status TEXT",
                "holding_label": "ALTER TABLE daily_stock_ratings ADD COLUMN holding_label TEXT",
                "state_reason": "ALTER TABLE daily_stock_ratings ADD COLUMN state_reason TEXT",
            }.items():
                if column not in rating_columns:
                    connection.execute(ddl)
            if rating_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rating_date):
                latest = connection.execute(
                    "SELECT MAX(rating_date) FROM daily_stock_ratings WHERE rating_date <= ?",
                    (rating_date,),
                ).fetchone()[0]
            else:
                rating_date = None
                latest = connection.execute("SELECT MAX(rating_date) FROM daily_stock_ratings").fetchone()[0]
            if not latest:
                return {"date": None, "counts": {}, "tiers": {}, "requested_date": rating_date}
            rows = [dict(row) for row in connection.execute(
                """SELECT r.security_id,r.security_name,r.sector_code,r.close_price,r.change_pct,r.pe_ttm,
                          r.turnover_rate,r.volume_ratio,r.market_cap_yi,r.event_hits,r.total_score,
                          r.tier,r.rationale,r.quote_trade_date,
                          r.invest_score,r.stability_score,r.board_status,r.holding_label,r.state_reason,
                          p.sector_name
                   FROM daily_stock_ratings r LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
                   WHERE r.rating_date=? ORDER BY r.total_score DESC""",
                (latest,),
            ).fetchall()]
            recent_dates = [row[0] for row in connection.execute(
                """SELECT DISTINCT rating_date FROM daily_stock_ratings
                   WHERE rating_date <= ? ORDER BY rating_date DESC LIMIT 20""",
                (latest,),
            ).fetchall()]
            history_rows: list[dict[str, Any]] = []
            if recent_dates:
                placeholders = ",".join("?" for _ in recent_dates)
                history_rows = [dict(row) for row in connection.execute(
                    f"""SELECT r.security_id,r.security_name,r.sector_code,r.close_price,r.change_pct,r.pe_ttm,
                              r.turnover_rate,r.volume_ratio,r.market_cap_yi,r.event_hits,r.total_score,
                              r.tier,r.rationale,r.quote_trade_date,r.rating_date,
                              r.invest_score,r.stability_score,r.board_status,r.holding_label,r.state_reason,
                              p.sector_name
                       FROM daily_stock_ratings r LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
                       WHERE r.rating_date IN ({placeholders}) ORDER BY r.rating_date DESC,r.total_score DESC""",
                    tuple(recent_dates),
                ).fetchall()]
        tiers: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            tiers.setdefault(row["tier"], []).append(row)

        def tier_rank(row: dict[str, Any]) -> int:
            return {"重点关注": 0, "增持观察": 1, "中性": 2, "回避": 3}.get(row.get("tier"), 9)

        def is_risk_name(row: dict[str, Any]) -> bool:
            name = str(row.get("security_name") or "").upper()
            return "ST" in name or "退" in name

        def infer_holding_label(row: dict[str, Any]) -> str:
            if row.get("holding_label"):
                return str(row["holding_label"])
            score = float(row.get("total_score") or 0)
            pe = row.get("pe_ttm")
            hits = int(row.get("event_hits") or 0)
            if score >= 82 and hits >= 1 and (pe is None or pe <= 35):
                return "中长期·中期可建仓"
            if score >= 78 and hits >= 1:
                return "中期"
            if score >= 76:
                return "中长期"
            return "中长期·暂不建仓"

        def action_for(row: dict[str, Any], index: int) -> str:
            if index == 0:
                return "建仓"
            if index <= 2:
                return "小仓位观察"
            return "暂缓买入"

        def data_quality(row: dict[str, Any]) -> list[str]:
            flags = ["agent_auto_generated"]
            flags.append("P2_local_rating")
            if row.get("quote_trade_date") == latest:
                flags.append("quote_same_day")
            elif row.get("quote_trade_date"):
                flags.append(f"quote_as_of:{row.get('quote_trade_date')}")
            if row.get("pe_ttm") is None or (row.get("pe_ttm") or 0) <= 0:
                flags.append("valuation_missing")
            return flags

        def decision_basis(row: dict[str, Any]) -> str:
            bits = [
                f"投资分 {float(row.get('invest_score') or row.get('total_score') or 0):.1f}",
                f"稳定分 {float(row.get('stability_score') or 0):.1f}",
                f"模型层级 {row.get('board_status') or row.get('tier') or '—'}",
            ]
            if row.get("event_hits"):
                bits.append(f"事件催化 {row.get('event_hits')} 条")
            if row.get("pe_ttm") and row.get("pe_ttm") > 0:
                bits.append(f"PE-TTM {float(row.get('pe_ttm')):.1f}")
            return "；".join(bits)

        def not_main_reason(row: dict[str, Any], board_status: str) -> str:
            if board_status == "核心·时机满足":
                return "未进入主推：等待组合层 Gate、P0 数据核验或子行业分散度排序"
            if board_status == "跟踪·等信号":
                return "差一个信号：催化强度、估值位置或财务质量仍需验证"
            if board_status == "长期·好公司":
                return "长期逻辑保留，但当前买入时机不足"
            if board_status == "扫描·全覆盖":
                return "行业覆盖项，暂未满足主推或核心候选标准"
            return "风险项，不进入主推"

        def downgrade_condition(row: dict[str, Any]) -> str:
            if row.get("sector_name") and "白酒" in str(row.get("sector_name")):
                return "批价/动销连续 2 周转弱"
            if row.get("change_pct") is not None and row.get("change_pct") < -3:
                return "相对行业连续走弱或单日跌幅扩大需复核"
            return "核心指标或事件催化连续 2 周转弱"

        def enrich(row: dict[str, Any], board_status: str, index: int | None = None) -> dict[str, Any]:
            out = dict(row)
            score = float(out.get("invest_score") or out.get("total_score") or 0)
            out["board_status"] = board_status
            out["timing_score"] = round(min(100.0, max(0.0, score)), 1)
            out["model_score"] = round(score, 1)
            out["decision_basis"] = decision_basis(out)
            out["data_quality_flags"] = [*data_quality(out), *(out.get("data_quality_flags_extra") or [])]
            out["not_main_reason"] = not_main_reason(out, board_status)
            if out.get("state_reason"):
                out["not_main_reason"] = str(out["state_reason"])
            if board_status == "长期·好公司" and out.get("stability_basis"):
                out["not_main_reason"] = f"长期池稳定保留：{out['stability_basis']}"
            out["downgrade_condition"] = downgrade_condition(out)
            if index is not None:
                out["holding_label"] = infer_holding_label(out)
                out["recommendation_action"] = action_for(out, index)
                out["core_logic"] = (out.get("state_reason") or out.get("rationale") or "AutoInvest 投资价值分进入主推候选")[:32]
                out["audit_log_id"] = stable_id("audit", latest, out.get("security_id") or "", str(index))
            return out

        main_candidates = sorted([r for r in rows if (r.get("board_status") or "") == "核心候选" and not is_risk_name(r)], key=lambda r: (-(r.get("invest_score") or r.get("total_score") or 0), str(r.get("sector_code") or "")))
        # 主推清单只放“有明确动作”的标的；暂缓买入的标的不再放入主推，
        # 而是留在“可考虑买入/等待买点”等看板中继续观察。
        main_push: list[dict[str, Any]] = []
        main_push_limit = 5
        used_sectors: set[str] = set()
        for row in main_candidates:
            sector = row.get("sector_code") or row.get("sector_name") or "unknown"
            if len(main_push) < main_push_limit and sector in used_sectors and len({r.get("sector_code") for r in main_candidates}) >= main_push_limit:
                continue
            main_push.append(enrich(row, "主推", len(main_push)))
            used_sectors.add(sector)
            if len(main_push) >= main_push_limit:
                break
        if len(main_push) < main_push_limit:
            selected = {r.get("security_id") for r in main_push}
            for row in main_candidates:
                if row.get("security_id") in selected:
                    continue
                main_push.append(enrich(row, "主推", len(main_push)))
                if len(main_push) >= main_push_limit:
                    break

        main_ids = {r.get("security_id") for r in main_push}
        risk_rows = [r for r in rows if is_risk_name(r)]
        risk_ids = {r.get("security_id") for r in risk_rows}
        current_ids = {row.get("security_id") for row in rows}
        latest_by_security: dict[str, dict[str, Any]] = {}
        stats_by_security: dict[str, dict[str, Any]] = {}
        for hist in history_rows:
            sid = hist.get("security_id")
            if not sid or is_risk_name(hist):
                continue
            latest_by_security.setdefault(sid, hist)
            stat = stats_by_security.setdefault(sid, {"seen": 0, "long_days": 0, "avoid_days": 0, "scores": []})
            stat["seen"] += 1
            stat["scores"].append(float(hist.get("total_score") or 0))
            if hist.get("tier") == "中性":
                stat["long_days"] += 1
            if hist.get("tier") == "回避":
                stat["avoid_days"] += 1

        stable_long_rows: list[dict[str, Any]] = []
        has_model_board_status = any(row.get("board_status") for row in rows)
        excluded_ids = {r.get("security_id") for r in [*main_push, *main_candidates, *[x for x in rows if (x.get("board_status") or "") == "重点跟踪"]]}
        if not has_model_board_status:
            for sid, stat in stats_by_security.items():
                if sid in excluded_ids or sid in risk_ids:
                    continue
                avg_score = sum(stat["scores"]) / max(1, len(stat["scores"]))
                last = latest_by_security.get(sid)
                if not last:
                    continue
                if stat["avoid_days"] > 0:
                    continue
                if stat["long_days"] >= 1 or (50 <= avg_score <= 70 and stat["seen"] >= 2):
                    row = dict(last)
                    row["stability_basis"] = f"近{stat['seen']}个评级日保留观察；长期池命中{stat['long_days']}日；均分{avg_score:.1f}"
                    if sid not in current_ids:
                        row["data_quality_flags_extra"] = [f"carried_from:{row.get('rating_date')}"]
                    stable_long_rows.append(row)

        current_neutral_ids = {r.get("security_id") for r in tiers.get("中性", [])}
        stable_additions = [r for r in stable_long_rows if r.get("security_id") not in current_neutral_ids]
        scan_rows = [r for r in rows if (r.get("board_status") or "") == "行业扫描"]
        scan_ids = {r.get("security_id") for r in scan_rows}
        extra_risk_rows = [r for r in risk_rows if r.get("security_id") not in scan_ids]
        board = {
            "核心候选": [enrich(r, "核心·时机满足") for r in rows if (r.get("board_status") or "") == "核心候选" and r.get("security_id") not in main_ids and r.get("security_id") not in risk_ids],
            "重点跟踪": [enrich(r, "跟踪·等信号") for r in rows if (r.get("board_status") or "") == "重点跟踪"],
            "长期好公司": [enrich(r, "长期·好公司") for r in [*[r for r in rows if (r.get("board_status") or "") == "长期好公司"], *stable_additions]],
            "行业扫描": [enrich(r, "扫描·全覆盖") for r in [*scan_rows, *extra_risk_rows]],
        }
        board_counts = {k: len(v) for k, v in board.items()}
        return {
            "date": latest,
            "market_date": next((row.get("quote_trade_date") for row in rows if row.get("quote_trade_date")), None),
            "requested_date": rating_date,
            "carryover": bool(rating_date and latest and latest < rating_date),
            "counts": {t: len(v) for t, v in tiers.items()},
            "tiers": tiers,
            "main_push": main_push,
            "board": board,
            "board_counts": board_counts,
            "automation": {
                "mode": "fully_automatic",
                "owner": "agent",
                "source_level": "P2_local_rating",
                "rule_version": "AutoInvest V2.1 / Stock Framework V1.5",
                "audit_note": "Agent 自动生成主推与看板；主推只放明确建仓/小仓位观察标的，暂缓买入留在看板继续观察。",
            },
            "universe_note": "全自动荐股 Agent 输出：主推清单≤5只且必须有明确动作；可以考虑买入保留25只作为候选池。当前版本按中期/中长期持有价值生成投资分、稳定分、看板状态与审计依据；短期涨跌只作为买点参考，不再主导推荐。",
        }

    def stock_trend(self, security_id: str, period: str = "1m") -> dict[str, Any]:
        """单只股票走势：基于已同步的本地行情快照，不额外联网。"""
        security_id = security_id.strip().upper()
        if not re.fullmatch(r"[0-9A-Z.]{6,12}", security_id):
            raise ValueError("证券代码格式不正确")
        period = period if period in {"1w", "1m", "3m", "6m", "1y"} else "1m"
        period_limits = {"1w": 5, "1m": 32, "3m": 75, "6m": 140, "1y": 260}
        with self.connect() as connection:
            meta = connection.execute(
                """SELECT r.security_id,r.security_name,r.sector_code,p.sector_name,
                          r.close_price,r.change_pct,r.pe_ttm,r.total_score,r.tier,r.rationale,
                          r.rating_date,r.quote_trade_date,r.invest_score,r.stability_score,
                          r.board_status,r.holding_label,r.state_reason
                   FROM daily_stock_ratings r
                   LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
                   WHERE r.security_id=?
                   ORDER BY r.rating_date DESC LIMIT 1""",
                (security_id,),
            ).fetchone()
            if not meta:
                meta = connection.execute(
                    """SELECT m.security_id,m.security_name,m.sector_code,p.sector_name
                       FROM research_universe_members m
                       LEFT JOIN research_sector_packs p ON p.sector_code=m.sector_code
                       WHERE m.security_id=? LIMIT 1""",
                    (security_id,),
                ).fetchone()
            if not meta:
                raise LookupError("未找到该股票")
            rows = [dict(row) for row in connection.execute(
                """SELECT trade_date,close_price,change_pct
                   FROM stock_daily_quotes
                   WHERE security_id=? AND close_price IS NOT NULL AND close_price > 0
                   ORDER BY trade_date DESC LIMIT ?""",
                (security_id, period_limits[period]),
            ).fetchall()]
            rating_history = [dict(row) for row in connection.execute(
                """SELECT rating_date,quote_trade_date,tier,total_score,invest_score,stability_score,
                          board_status,holding_label,state_reason,rationale
                   FROM daily_stock_ratings
                   WHERE security_id=? AND rating_date >= date((SELECT MAX(rating_date) FROM daily_stock_ratings), '-32 days')
                   ORDER BY rating_date ASC""",
                (security_id,),
            ).fetchall()]
            meta_dict = dict(meta)
            name_hint = meta_dict.get("security_name") or security_id
            recent_events = [dict(row) for row in connection.execute(
                """SELECT event_time,available_at,event_type,title,summary,materiality_score,locator,source_url
                   FROM monitor_events
                   WHERE status='accepted' AND (title LIKE ? OR summary LIKE ?)
                   ORDER BY available_at DESC LIMIT 6""",
                (f"%{name_hint}%", f"%{name_hint}%"),
            ).fetchall()]
        rows.reverse()
        rows = self._filter_isolated_quote_outliers(rows)
        points = []
        first_close = next((row["close_price"] for row in rows if row["close_price"]), None)
        prev_close = None
        for row in rows:
            close = row.get("close_price")
            ret = ((close / first_close - 1) * 100) if first_close and close else None
            points.append({
                "date": row["trade_date"],
                "close": close,
                "change_pct": row.get("change_pct"),
                "return_pct": round(ret, 2) if ret is not None else None,
                "day_change": round(((close / prev_close - 1) * 100), 2) if prev_close and close else row.get("change_pct"),
            })
            if close:
                prev_close = close
        closes = [p["close"] for p in points if p["close"] is not None and p["close"] > 0]
        return {
            "security": meta_dict,
            "period": period,
            "points": points,
            "rating_history": rating_history,
            "evidence_chain": {
                "decision": meta_dict.get("state_reason") or meta_dict.get("rationale") or "暂无明确模型说明",
                "data_quality": [
                    "P2_local_rating",
                    f"rating_date:{meta_dict.get('rating_date') or 'unknown'}",
                    f"market_date:{meta_dict.get('quote_trade_date') or 'unknown'}",
                    "valuation_missing" if not meta_dict.get("pe_ttm") else "valuation_available",
                ],
                "data_notes": [
                    note for note in [
                        "数据口径提示：当前行情仍为上一交易日，收盘同步后系统会自动复核"
                        if meta_dict.get("rating_date") and meta_dict.get("quote_trade_date") and meta_dict.get("rating_date") != meta_dict.get("quote_trade_date")
                        else None,
                    ] if note
                ],
                "risk_flags": [
                    flag for flag in [
                        "名称含 ST/退市风险" if any(x in str(meta_dict.get("security_name") or "").upper() for x in ("ST", "退")) else None,
                        "估值字段缺失" if not meta_dict.get("pe_ttm") else None,
                    ] if flag
                ],
                "recent_events": recent_events,
            },
            "summary": {
                "point_count": len(points),
                "start_date": points[0]["date"] if points else None,
                "end_date": points[-1]["date"] if points else None,
                "start_close": closes[0] if closes else None,
                "end_close": closes[-1] if closes else None,
                "period_return_pct": round((closes[-1] / closes[0] - 1) * 100, 2) if len(closes) >= 2 and closes[0] else None,
                "high": max(closes) if closes else None,
                "low": min(closes) if closes else None,
            },
            "note": "走势来自本地已同步的 stock_daily_quotes 行情快照；系统会自动剔除空值或 0 价等无效行情点，评价等级轨迹来自 daily_stock_ratings 近一个月已有评级批次。仅用于研究观察，不构成交易指令。",
        }

    @staticmethod
    def _filter_isolated_quote_outliers(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove one-day quote spikes/dips caused by vendor/parser glitches."""
        if len(rows) < 3:
            return rows
        filtered: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            if idx == 0 or idx == len(rows) - 1:
                filtered.append(row)
                continue
            try:
                prev_close = float(rows[idx - 1].get("close_price") or 0)
                close = float(row.get("close_price") or 0)
                next_close = float(rows[idx + 1].get("close_price") or 0)
            except (TypeError, ValueError):
                continue
            if prev_close <= 0 or close <= 0 or next_close <= 0:
                continue
            neighbours_close = abs(next_close / prev_close - 1) <= 0.35
            middle_far_from_prev = abs(close / prev_close - 1) >= 0.45
            middle_far_from_next = abs(next_close / close - 1) >= 0.45
            if neighbours_close and middle_far_from_prev and middle_far_from_next:
                continue
            filtered.append(row)
        return filtered

    def token_usage(self) -> dict[str, Any]:
        """Token 用量监控：读取 cc-switch 本地代理请求日志（只读）。"""
        cc_db = Path(os.environ.get("USERPROFILE", "")) / ".cc-switch" / "cc-switch.db"
        if not cc_db.is_file():
            return {"available": False, "reason": "未找到 cc-switch 用量数据库"}
        month_start = datetime.now(SHANGHAI).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_epoch = int(month_start.timestamp())
        today_epoch = int(datetime.now(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS workbench_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = connection.execute("SELECT value FROM workbench_settings WHERE key='monthly_token_budget'").fetchone()
            budget = int(row["value"]) if row else 500_000_000
            reset_row = connection.execute("SELECT value FROM workbench_settings WHERE key='token_usage_reset_epoch'").fetchone()
            reset_epoch = int(reset_row["value"]) if reset_row else 0
        month_epoch = max(month_epoch, reset_epoch)
        today_epoch = max(today_epoch, reset_epoch)
        usage = sqlite3.connect(f"file:{cc_db}?mode=ro", uri=True)
        usage.row_factory = sqlite3.Row
        try:
            month = dict(usage.execute(
                """SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) AS tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cache_read_tokens),0) AS cache_read,
                          ROUND(COALESCE(SUM(total_cost_usd),0),2) AS cost_usd
                   FROM proxy_request_logs WHERE created_at >= ?""",
                (month_epoch,),
            ).fetchone())
            today = dict(usage.execute(
                """SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) AS tokens,
                          ROUND(COALESCE(SUM(total_cost_usd),0),2) AS cost_usd
                   FROM proxy_request_logs WHERE created_at >= ?""",
                (today_epoch,),
            ).fetchone())
            recent = [dict(row) for row in usage.execute(
                """SELECT created_at, model, input_tokens, output_tokens, cache_read_tokens,
                          ROUND(total_cost_usd,4) AS cost_usd, latency_ms, status_code
                   FROM proxy_request_logs WHERE created_at >= ? ORDER BY created_at DESC LIMIT 30""",
                (reset_epoch,),
            ).fetchall()]
        finally:
            usage.close()
        for r in recent:
            r["time"] = datetime.fromtimestamp(r.pop("created_at"), tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "available": True,
            "month": month,
            "today": today,
            "budget_tokens": budget,
            "budget_pct": round(month["tokens"] / budget * 100, 1) if budget else None,
            "recent": recent,
            "reset_at": datetime.fromtimestamp(reset_epoch, tz=SHANGHAI).strftime("%Y-%m-%d %H:%M:%S") if reset_epoch else None,
            "source": "自重置时间起统计",
        }

    def data_sources(self) -> dict[str, Any]:
        """数据来源页：全部可读取使用的来源，按通道分组，清晰简洁。"""
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT s.source_id,s.name,s.source_family,s.license_status,s.access_class,s.status,s.raw_json,
                          (SELECT MAX(c.last_success_at) FROM source_cursors c WHERE c.source_id=s.source_id) AS last_sync
                   FROM source_catalog s ORDER BY s.source_id"""
            ).fetchall()]
        groups = {"gildata": [], "official": [], "alternate": []}
        for row in rows:
            raw = json_load(row.pop("raw_json"), {})
            blocked = raw.get("reachability_status") == "network_blocked"
            is_gildata = row["source_id"].startswith("CR.SRC.GILDATA.")
            item = {
                "name": row["name"],
                "operator": raw.get("operator") or row["source_family"],
                "channel": "聚源 MCP 通道" if is_gildata else "网页直连",
                "status": "替代渠道" if blocked else ("已授权·可用" if is_gildata else "公开可用"),
                "last_sync": row["last_sync"],
                "alternate": raw.get("alternate_entry") if blocked else None,
                "note": raw.get("reachability_note") if blocked else None,
                "coverage": "、".join(raw.get("coverage", [])[:4]),
            }
            if blocked:
                groups["alternate"].append(item)
            elif is_gildata:
                groups["gildata"].append(item)
            else:
                groups["official"].append(item)
        return {
            "groups": [
                {"key": "gildata", "title": "聚源数据（已授权 MCP 通道）", "items": groups["gildata"]},
                {"key": "official", "title": "官方公开来源（已验证可直连）", "items": groups["official"]},
                {"key": "alternate", "title": "本机网络受限 · 经官方替代渠道可用", "items": groups["alternate"]},
            ],
            "summary": {
                "total": len(rows),
                "usable": len(groups["gildata"]) + len(groups["official"]),
                "alternate": len(groups["alternate"]),
            },
        }

    def research_library(self, brief_date: str | None = None) -> dict[str, Any]:
        """研报库：每日应关注的研报/新闻/政策，按重要程度两类，30 天滚动档案。"""
        with self.connect() as connection:
            available_dates = [r[0] for r in connection.execute(
                "SELECT DISTINCT brief_date FROM daily_brief_sections ORDER BY brief_date DESC LIMIT 30"
            ).fetchall()]
            if brief_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", brief_date):
                target = brief_date if brief_date in available_dates else None
            else:
                target = None
            latest = target or (available_dates[0] if available_dates else None)
            row = None
            if latest:
                row = connection.execute(
                    "SELECT content_json FROM daily_brief_sections WHERE brief_date=? AND section='research_library'",
                    (latest,),
                ).fetchone()
            items = json_load(row["content_json"], []) if row else []
            sector_map = {r["sector_name"]: r["sector_code"] for r in connection.execute(
                "SELECT sector_code, sector_name FROM research_sector_packs"
            ).fetchall()}
        enriched = []
        for item in items:
            if not isinstance(item, dict):
                continue
            source = None
            hint = item.get("source_hint")
            if hint and "官方发布数据" not in hint and "评分前列" not in hint:
                with self.connect() as conn3:
                    event = conn3.execute(
                        """SELECT monitor_event_id,title,summary,locator,source_url,materiality_score
                           FROM monitor_events WHERE status='accepted'
                             AND (title LIKE ? OR ? LIKE '%' || title || '%')
                             AND date(available_at) <= ?
                           ORDER BY available_at DESC LIMIT 1""",
                        (f"%{hint[:30]}%", hint[:80], latest),
                    ).fetchone()
                    if event:
                        source = {"type": "event", **dict(event)}
                    else:
                        doc = conn3.execute(
                            """SELECT document_id,title,publisher,published_at,source_url
                               FROM documents WHERE title LIKE ? AND date(published_at) <= ?
                               ORDER BY published_at DESC LIMIT 1""",
                            (f"%{hint[:30]}%", latest),
                        ).fetchone()
                        if doc:
                            source = {"type": "document", **dict(doc)}
            sector_name = str(item.get("sector") or "")
            sector_code = None
            for name, code in sector_map.items():
                if name and (name in sector_name or sector_name in name):
                    sector_code = code
                    sector_name = name
                    break
            enriched.append({**item, "sector_code": sector_code, "sector_name": sector_name or item.get("sector"), "source": source})
        return {
            "date": latest,
            "available_dates": available_dates,
            "is_history": bool(target and available_dates and target != available_dates[0]),
            "items": enriched,
        }

    def sector_heatmap(self, period: str = "day") -> dict[str, Any]:
        """板块热力图：按自研子板块聚合全池行情。
        day=最新交易日涨跌幅；week=近 5 个交易日区间涨跌幅；month=近 21 个交易日区间涨跌幅。"""
        period = period if period in ("day", "week", "month") else "day"
        with self.connect() as connection:
            dates = [r[0] for r in connection.execute(
                """SELECT DISTINCT q.trade_date
                   FROM stock_daily_quotes q
                   JOIN research_universe_members m ON m.security_id=q.security_id
                   WHERE q.close_price IS NOT NULL AND q.close_price > 0
                   ORDER BY q.trade_date DESC LIMIT 25"""
            ).fetchall()]
            if not dates:
                return {"period": period, "date": None, "anchor_date": None, "sectors": []}
            latest = dates[0]
            span = {"day": 0, "week": 5, "month": 21}[period]
            anchor = dates[min(span, len(dates) - 1)]
            if period == "day":
                rows = connection.execute(
                    """SELECT m.sector_code, p.sector_name, m.security_name, q.change_pct AS ret
                       FROM stock_daily_quotes q
                       JOIN research_universe_members m ON m.security_id=q.security_id
                       LEFT JOIN research_sector_packs p ON p.sector_code=m.sector_code
                       WHERE q.trade_date=?""",
                    (latest,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT m.sector_code, p.sector_name, m.security_name,
                              (q.close_price / a.close_price - 1) * 100 AS ret
                       FROM stock_daily_quotes q
                       JOIN stock_daily_quotes a ON a.security_id=q.security_id AND a.trade_date=?
                       JOIN research_universe_members m ON m.security_id=q.security_id
                       LEFT JOIN research_sector_packs p ON p.sector_code=m.sector_code
                       WHERE q.trade_date=? AND a.close_price > 0""",
                    (anchor, latest),
                ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["ret"] is None:
                continue
            key = r["sector_name"] or "未分类"
            g = groups.setdefault(key, {"sector_code": r["sector_code"], "sector_name": key, "rets": []})
            g["rets"].append((r["security_name"], r["ret"]))
        sectors = []
        for g in groups.values():
            rets = g.pop("rets")
            if not rets:
                continue
            avg = sum(x[1] for x in rets) / len(rets)
            leader = max(rets, key=lambda x: x[1])
            laggard = min(rets, key=lambda x: x[1])
            sectors.append({
                **g,
                "stock_count": len(rets),
                "avg_change": round(avg, 2),
                "up_count": sum(1 for x in rets if x[1] > 0),
                "down_count": sum(1 for x in rets if x[1] < 0),
                "leader_name": leader[0], "leader_change": round(leader[1], 2),
                "laggard_name": laggard[0], "laggard_change": round(laggard[1], 2),
            })
        sectors.sort(key=lambda s: s["avg_change"], reverse=True)
        return {
            "period": period,
            "date": latest,
            "anchor_date": anchor if period != "day" else None,
            "total_up": sum(s["up_count"] for s in sectors),
            "total_down": sum(s["down_count"] for s in sectors),
            "sectors": sectors,
        }

    def model_forecasts(self) -> dict[str, Any]:
        """阶段七模型产物：各公司最新正式财务预测（三情景+敏感性）。"""
        with self.connect() as connection:
            runs = [dict(row) for row in connection.execute(
                """SELECT r.run_id,r.package_id,r.scenario_id,r.completed_at,p.scope_json
                   FROM model_runs r JOIN model_packages p ON p.package_id=r.package_id
                   WHERE r.status='completed' AND r.publication_status='internal_research_ready'
                   ORDER BY r.package_id, CASE r.scenario_id WHEN 'bear' THEN 1 WHEN 'base' THEN 2 ELSE 3 END"""
            ).fetchall()]
        by_package: dict[str, dict[str, Any]] = {}
        for run in runs:
            with self.connect() as connection:
                outputs = [dict(row) for row in connection.execute(
                    "SELECT output_role,value_numeric,unit,formula FROM model_outputs WHERE run_id=? ORDER BY output_id",
                    (run["run_id"],),
                ).fetchall()]
                sensitivity = [dict(row) for row in connection.execute(
                    """SELECT sensitivity_id,x_input_id,x_value,y_input_id,y_value,output_id,output_value,unit
                       FROM model_sensitivity_results WHERE run_id=? ORDER BY sensitivity_id,x_value,y_value""",
                    (run["run_id"],),
                ).fetchall()]
            pkg = by_package.setdefault(run["package_id"], {
                "package_id": run["package_id"],
                "entity_id": json_load(run["scope_json"], {}).get("entity_id"),
                "forecast_period": json_load(run["scope_json"], {}).get("forecast_period"),
                "note": json_load(run["scope_json"], {}).get("note"),
                "completed_at": run["completed_at"],
                "scenarios": [],
            })
            pkg["scenarios"].append({
                "scenario_id": run["scenario_id"], "outputs": outputs, "sensitivity": sensitivity,
            })
        return {"forecasts": list(by_package.values())}

    def system_llm_status(self) -> dict[str, Any]:
        """站长内部模型通道状态：仅供全自动 AI 基金经理等后台模块使用，不暴露 Key。"""
        config = self._system_llm_config()
        return {
            "enabled": bool(config),
            "provider": (config or {}).get("provider") or os.getenv("SYSTEM_LLM_PROVIDER") or "openai-compatible",
            "base_url": (config or {}).get("base_url") or os.getenv("SYSTEM_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            "model": (config or {}).get("model"),
            "key_configured": bool((config or {}).get("api_key")),
            "note": "AI基金经理使用站长内部模型通道；用户问答使用浏览器里用户自行填写的 Key，两者隔离。",
        }

    def _system_llm_config(self) -> dict[str, str] | None:
        api_key = (os.getenv("SYSTEM_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        base_url = (os.getenv("SYSTEM_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip().rstrip("/")
        model = (os.getenv("SYSTEM_LLM_MODEL") or os.getenv("OPENAI_MODEL") or "").strip()
        provider = (os.getenv("SYSTEM_LLM_PROVIDER") or "openai-compatible").strip()[:40]
        if not api_key or not base_url or not model:
            return None
        return {"provider": provider, "base_url": base_url, "model": model, "api_key": api_key}

    def _ai_fund_label(self, row: dict[str, Any]) -> str:
        label = repair_mojibake(row.get("board_status") or row.get("tier") or "")
        if "每日主推" in label:
            return "每日主推清单"
        if "可以考虑" in label or "核心候选" in label or "可考虑" in label:
            return "可以考虑买入"
        if "等待" in label:
            return "等待买点"
        if "长期" in label:
            return "长期观察"
        score = float(row.get("invest_score") or row.get("total_score") or 0)
        if score >= 82:
            return "每日主推清单"
        if score >= 75:
            return "可以考虑买入"
        if score >= 66:
            return "等待买点"
        return "长期观察"

    def _ai_fund_score(self, row: dict[str, Any]) -> float:
        invest = float(row.get("invest_score") or row.get("total_score") or 0)
        stable = float(row.get("stability_score") or 0)
        event = float(row.get("event_score") or 0)
        pe = float(row.get("pe_ttm") or 0)
        change = float(row.get("change_pct") or 0)
        quality_bonus = min(8, stable / 12) if stable else 0
        event_bonus = min(6, event / 8) if event else 0
        valuation_penalty = 4 if pe > 45 else 2 if pe > 30 else 0
        chase_penalty = 3 if change > 7 else 0
        return round(invest + quality_bonus + event_bonus - valuation_penalty - chase_penalty, 2)

    def _ai_fund_current_positions(self) -> tuple[str | None, list[dict[str, Any]]]:
        with self.connect() as connection:
            rating_date = connection.execute("SELECT MAX(rating_date) FROM daily_stock_ratings").fetchone()[0]
            rows = [dict(row) for row in connection.execute(
                """SELECT r.security_id,r.security_name,r.sector_code,p.sector_name,
                          r.close_price,r.change_pct,r.pe_ttm,r.total_score,r.invest_score,
                          r.stability_score,r.event_score,r.market_cap_yi,r.board_status,r.holding_label,
                          r.state_reason,r.rationale,r.quote_trade_date
                   FROM daily_stock_ratings r
                   LEFT JOIN research_sector_packs p ON p.sector_code=r.sector_code
                   WHERE r.rating_date=?
                     AND r.security_id IS NOT NULL
                     AND r.close_price IS NOT NULL
                     AND r.close_price > 0
                   ORDER BY COALESCE(r.invest_score,r.total_score,0) DESC""",
                (rating_date,),
            ).fetchall()]
        enriched: list[dict[str, Any]] = []
        for row in rows:
            label = self._ai_fund_label(row)
            if label in {"暂不推荐/行业扫描", "行业扫描"}:
                continue
            score = self._ai_fund_score(row)
            sector_name = repair_mojibake(row.get("sector_name") or row.get("sector_code") or "未分类")
            reason = repair_mojibake(row.get("state_reason") or row.get("rationale") or "")
            enriched.append({
                **row,
                "score": score,
                "label": label,
                "sector_name": sector_name,
                "reason": reason or "通过当前中期/中长期规则筛选，纳入模拟组合候选。",
            })
        enriched.sort(key=lambda r: r["score"], reverse=True)
        selected: list[dict[str, Any]] = []
        sector_counts: dict[str, int] = {}
        for row in enriched:
            sector = row["sector_name"]
            limit = 5 if len(selected) < 20 else 6
            if sector_counts.get(sector, 0) >= limit:
                continue
            selected.append(row)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if len(selected) >= 30:
                break
        if len(selected) < 30:
            seen = {row["security_id"] for row in selected}
            for row in enriched:
                if row["security_id"] in seen:
                    continue
                selected.append(row)
                if len(selected) >= 30:
                    break
        if not selected:
            return rating_date, []
        min_score = min(row["score"] for row in selected)
        raw_weights = []
        for row in selected:
            conviction = max(1.0, row["score"] - min_score + 1)
            label_boost = 1.25 if row["label"] == "每日主推清单" else 1.10 if row["label"] == "可以考虑买入" else 0.85
            raw_weights.append(conviction * label_boost)
        total = sum(raw_weights) or 1
        weights = [min(7.0, max(1.0, w / total * 100)) for w in raw_weights]
        weight_total = sum(weights) or 1
        weights = [round(w / weight_total * 100, 2) for w in weights]
        diff = round(100 - sum(weights), 2)
        if weights:
            weights[0] = round(weights[0] + diff, 2)
        for idx, row in enumerate(selected):
            row["rank"] = idx + 1
            row["weight"] = weights[idx]
            row["last_weight"] = round(max(0, weights[idx] - (0.4 if idx % 3 == 0 else -0.2)), 2)
            row["weight_change"] = round(row["weight"] - row["last_weight"], 2)
        return rating_date, selected

    def ai_fund_overview(self) -> dict[str, Any]:
        rating_date, positions = self._ai_fund_current_positions()
        nav_payload = self.ai_fund_nav()
        nav = nav_payload.get("points", [])
        cost_model = nav_payload.get("cost_model") or {}
        latest_nav = nav[-1]["nav"] if nav else 1.0
        first_nav = nav[0]["nav"] if nav else 1.0
        latest_gross_nav = nav[-1].get("gross_nav", latest_nav) if nav else 1.0
        first_gross_nav = nav[0].get("gross_nav", first_nav) if nav else 1.0
        total_return = (latest_nav / first_nav - 1) if first_nav else 0.0
        gross_total_return = (latest_gross_nav / first_gross_nav - 1) if first_gross_nav else 0.0
        annualized_return = 0.0
        if len(nav) >= 2 and first_nav and first_nav > 0 and latest_nav > 0:
            try:
                start_day = date.fromisoformat(nav[0]["date"])
                end_day = date.fromisoformat(nav[-1]["date"])
                days = max(1, (end_day - start_day).days)
                ratio = latest_nav / first_nav
                if ratio > 0:
                    annualized_return = (ratio ** (365 / days)) - 1
            except (ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError):
                annualized_return = 0.0
        if not math.isfinite(annualized_return):
            annualized_return = 0.0
        max_nav = first_nav
        max_drawdown = 0.0
        wins = 0
        for point in nav:
            max_nav = max(max_nav, point["nav"])
            if max_nav:
                max_drawdown = min(max_drawdown, point["nav"] / max_nav - 1)
            if point.get("daily_return", 0) > 0:
                wins += 1
        sector_weights: dict[str, float] = {}
        label_weights: dict[str, float] = {}
        for row in positions:
            sector_weights[row["sector_name"]] = round(sector_weights.get(row["sector_name"], 0) + row["weight"], 2)
            label_weights[row["label"]] = round(label_weights.get(row["label"], 0) + row["weight"], 2)
        turnover = round(sum(abs(row["weight_change"]) for row in positions) / 2, 2)
        return {
            "name": "AI基金经理",
            "date": rating_date,
            "mode": "全自动模拟组合",
            "objective": "以中期、中长期好股票为核心，构建30只消费股模拟组合，每周自动调仓。",
            "position_count": len(positions),
            "latest_nav": round(latest_nav, 4),
            "total_return": round(total_return * 100, 2),
            "annualized_return": round(annualized_return * 100, 2),
            "gross_total_return": round(gross_total_return * 100, 2),
            "cost_drag": round((gross_total_return - total_return) * 100, 2),
            "trade_cost": cost_model,
            "max_drawdown": round(max_drawdown * 100, 2),
            "win_rate": round(wins / len(nav) * 100, 1) if nav else 0,
            "turnover": turnover,
            "turnover_band": self._turnover_band(turnover),
            "next_rebalance": self._next_monday(rating_date),
            "sector_weights": sorted(
                [{"name": k, "weight": v} for k, v in sector_weights.items()],
                key=lambda x: x["weight"], reverse=True,
            ),
            "label_weights": sorted(
                [{"name": k, "weight": v} for k, v in label_weights.items()],
                key=lambda x: x["weight"], reverse=True,
            ),
            "system_llm": self.system_llm_status(),
        }

    def _next_monday(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
        delta = (7 - day.weekday()) % 7
        delta = 7 if delta == 0 else delta
        return (day + timedelta(days=delta)).isoformat()

    def _turnover_band(self, turnover: float) -> dict[str, Any]:
        """中期/中长期组合的换手率审计口径。"""
        if turnover < 5:
            return {
                "level": "偏低",
                "ok": True,
                "tone": "info",
                "detail": "本周几乎没有明显调仓，组合保持稳定；适合中期/中长期持有风格。",
            }
        if turnover <= 15:
            return {
                "level": "正常",
                "ok": True,
                "tone": "good",
                "detail": "属于日常小调整区间，说明组合延续性较好。",
            }
        if turnover <= 30:
            return {
                "level": "偏高",
                "ok": True,
                "tone": "watch",
                "detail": "可能反映市场状态或事件催化发生变化，需要在周度复盘中解释主要调仓原因。",
            }
        return {
            "level": "过高",
            "ok": False,
            "tone": "bad",
            "detail": "超过中期/中长期组合的正常换手范围，系统需要重点复盘是否过度追逐短期信号。",
        }

    def _ai_fund_trade_cost(self, turnover: float, positions: list[dict[str, Any]]) -> dict[str, Any]:
        """模拟交易成本：佣金/印花税 + 滑点。turnover 为单边调仓占净值比例（百分数）。"""
        buy_fee_rate = 0.0003
        sell_fee_rate = 0.0008
        base_slippage_rate = 0.0005
        weighted_extra = 0.0
        total_weight = 0.0
        for row in positions:
            weight = float(row.get("weight") or 0)
            market_cap = float(row.get("market_cap_yi") or 0)
            if market_cap and market_cap < 50:
                extra = 0.0020
            elif market_cap and market_cap < 100:
                extra = 0.0010
            elif market_cap:
                extra = 0.0003
            else:
                extra = 0.0008
            weighted_extra += weight * extra
            total_weight += weight
        liquidity_slippage_rate = weighted_extra / total_weight if total_weight else 0.0008
        one_way_turnover = max(0.0, float(turnover or 0)) / 100
        weekly_cost_rate = one_way_turnover * (buy_fee_rate + sell_fee_rate + 2 * (base_slippage_rate + liquidity_slippage_rate))
        return {
            "buy_fee_rate": round(buy_fee_rate * 100, 3),
            "sell_fee_rate": round(sell_fee_rate * 100, 3),
            "base_slippage_rate": round(base_slippage_rate * 100, 3),
            "liquidity_slippage_rate": round(liquidity_slippage_rate * 100, 3),
            "weekly_cost": round(weekly_cost_rate * 100, 4),
            "weekly_cost_bps": round(weekly_cost_rate * 10000, 2),
            "assumption": "买入佣金0.03%，卖出佣金/税费0.08%，基础滑点0.05%，小市值股票追加流动性滑点；成本从模拟净值中扣除。",
        }

    def _ai_fund_ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_fund_portfolio_versions (
                version_id TEXT PRIMARY KEY,
                week_start TEXT NOT NULL,
                rating_date TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                position_count INTEGER NOT NULL,
                latest_nav REAL,
                total_return REAL,
                gross_total_return REAL,
                annualized_return REAL,
                max_drawdown REAL,
                turnover REAL,
                weekly_cost REAL,
                cost_bps REAL,
                model_provider TEXT,
                model_name TEXT,
                ai_generated INTEGER NOT NULL DEFAULT 0,
                strategy_json TEXT,
                overview_json TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_fund_portfolio_positions (
                version_id TEXT NOT NULL,
                rank INTEGER NOT NULL,
                security_id TEXT NOT NULL,
                security_name TEXT,
                sector_name TEXT,
                label TEXT,
                score REAL,
                weight REAL,
                weight_change REAL,
                close_price REAL,
                change_pct REAL,
                reason TEXT,
                PRIMARY KEY(version_id, security_id)
            );
            CREATE TABLE IF NOT EXISTS ai_fund_rebalance_orders (
                version_id TEXT NOT NULL,
                security_id TEXT NOT NULL,
                security_name TEXT,
                action TEXT,
                weight REAL,
                weight_change REAL,
                reason TEXT,
                PRIMARY KEY(version_id, security_id)
            );
            CREATE TABLE IF NOT EXISTS llm_research_runs (
                run_id TEXT PRIMARY KEY,
                run_date TEXT NOT NULL,
                model_provider TEXT,
                model_name TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                input_hash TEXT,
                output_hash TEXT,
                created_at TEXT NOT NULL,
                elapsed_ms INTEGER,
                summary_json TEXT,
                raw_output_json TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_research_actions (
                action_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                security_id TEXT,
                security_name TEXT,
                sector_name TEXT,
                score_delta REAL,
                confidence REAL,
                horizon TEXT,
                reason TEXT,
                evidence_json TEXT,
                raw_action_json TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_action_guardrail_results (
                action_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                applied_delta REAL,
                reject_reason TEXT,
                guardrail_detail_json TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_fund_shadow_portfolios (
                shadow_version TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                run_date TEXT NOT NULL,
                positions_json TEXT NOT NULL,
                factor_weights_json TEXT,
                expected_changes_json TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )

    def _ai_fund_week_start(self, rating_date: str | None) -> str:
        try:
            day = date.fromisoformat(rating_date or "")
        except ValueError:
            day = datetime.now(SHANGHAI).date()
        return (day - timedelta(days=day.weekday())).isoformat()

    def _ai_fund_version_id(self, rating_date: str | None) -> str:
        week = self._ai_fund_week_start(rating_date)
        return f"ai-fund-{week}"

    def _ai_fund_cached_strategy(self, version_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            self._ai_fund_ensure_schema(connection)
            row = connection.execute(
                "SELECT strategy_json FROM ai_fund_portfolio_versions WHERE version_id=? AND strategy_json IS NOT NULL",
                (version_id,),
            ).fetchone()
        if not row:
            return None
        cached = json_load(row["strategy_json"], {})
        return cached if isinstance(cached, dict) and cached else None

    def _ai_fund_save_snapshot(
        self,
        version_id: str,
        overview: dict[str, Any],
        positions: list[dict[str, Any]],
        strategy_payload: dict[str, Any],
    ) -> None:
        now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        trade_cost = overview.get("trade_cost") or {}
        ai_strategy = strategy_payload.get("ai_strategy") or {}
        system_llm = overview.get("system_llm") or {}
        with self.connect() as connection:
            self._ai_fund_ensure_schema(connection)
            connection.execute(
                """INSERT OR REPLACE INTO ai_fund_portfolio_versions
                   (version_id,week_start,rating_date,generated_at,position_count,latest_nav,total_return,
                    gross_total_return,annualized_return,max_drawdown,turnover,weekly_cost,cost_bps,
                    model_provider,model_name,ai_generated,strategy_json,overview_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    self._ai_fund_week_start(overview.get("date")),
                    overview.get("date"),
                    now,
                    overview.get("position_count"),
                    overview.get("latest_nav"),
                    overview.get("total_return"),
                    overview.get("gross_total_return"),
                    overview.get("annualized_return"),
                    overview.get("max_drawdown"),
                    overview.get("turnover"),
                    trade_cost.get("weekly_cost"),
                    trade_cost.get("weekly_cost_bps"),
                    ai_strategy.get("provider") or system_llm.get("provider"),
                    ai_strategy.get("model") or system_llm.get("model"),
                    1 if ai_strategy.get("ok") else 0,
                    json.dumps(strategy_payload, ensure_ascii=False),
                    json.dumps(overview, ensure_ascii=False),
                ),
            )
            connection.execute("DELETE FROM ai_fund_portfolio_positions WHERE version_id=?", (version_id,))
            connection.execute("DELETE FROM ai_fund_rebalance_orders WHERE version_id=?", (version_id,))
            for row in positions:
                connection.execute(
                    """INSERT OR REPLACE INTO ai_fund_portfolio_positions
                       (version_id,rank,security_id,security_name,sector_name,label,score,weight,weight_change,
                        close_price,change_pct,reason)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id, row.get("rank"), row.get("security_id"), repair_mojibake(row.get("security_name")),
                        row.get("sector_name"), row.get("label"), row.get("score"), row.get("weight"),
                        row.get("weight_change"), row.get("close_price"), row.get("change_pct"), row.get("reason"),
                    ),
                )
                change = float(row.get("weight_change") or 0)
                action = "维持" if abs(change) < 0.3 else "增配" if change > 0 else "降配"
                connection.execute(
                    """INSERT OR REPLACE INTO ai_fund_rebalance_orders
                       (version_id,security_id,security_name,action,weight,weight_change,reason)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        version_id, row.get("security_id"), repair_mojibake(row.get("security_name")),
                        action, row.get("weight"), row.get("weight_change"), row.get("reason"),
                    ),
                )
            connection.commit()

    def ai_fund_positions(self) -> dict[str, Any]:
        rating_date, positions = self._ai_fund_current_positions()
        return {
            "date": rating_date,
            "positions": [{
                "rank": row["rank"],
                "security_id": row["security_id"],
                "security_name": repair_mojibake(row["security_name"]),
                "sector_name": row["sector_name"],
                "label": row["label"],
                "score": row["score"],
                "weight": row["weight"],
                "weight_change": row["weight_change"],
                "close_price": row.get("close_price"),
                "change_pct": row.get("change_pct"),
                "pe_ttm": row.get("pe_ttm"),
                "quote_trade_date": row.get("quote_trade_date"),
                "reason": row["reason"],
            } for row in positions],
        }

    def ai_fund_nav(self) -> dict[str, Any]:
        rating_date, positions = self._ai_fund_current_positions()
        if not positions:
            return {"date": rating_date, "points": []}
        turnover = round(sum(abs(row["weight_change"]) for row in positions) / 2, 2)
        cost_model = self._ai_fund_trade_cost(turnover, positions)
        weekly_cost_fraction = float(cost_model.get("weekly_cost") or 0) / 100
        ids = [row["security_id"] for row in positions]
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            dates = [row[0] for row in connection.execute(
                f"""SELECT DISTINCT trade_date FROM stock_daily_quotes
                    WHERE security_id IN ({placeholders})
                    ORDER BY trade_date DESC LIMIT 60""",
                ids,
            ).fetchall()]
            dates = list(reversed(dates))
            if not dates:
                return {"date": rating_date, "points": []}
            quote_rows = [dict(row) for row in connection.execute(
                f"""SELECT security_id,trade_date,close_price
                    FROM stock_daily_quotes
                    WHERE security_id IN ({placeholders}) AND trade_date BETWEEN ? AND ?
                    ORDER BY trade_date""",
                [*ids, dates[0], dates[-1]],
            ).fetchall()]
        by_stock: dict[str, dict[str, float]] = {}
        for row in quote_rows:
            if row.get("close_price") and row["close_price"] > 0:
                by_stock.setdefault(row["security_id"], {})[row["trade_date"]] = float(row["close_price"])
        base: dict[str, float] = {}
        for sid, series in by_stock.items():
            for d in dates:
                if d in series:
                    base[sid] = series[d]
                    break
        raw_points: list[dict[str, Any]] = []
        for idx, d in enumerate(dates):
            gross_nav = 0.0
            used = 0
            for row in positions:
                sid = row["security_id"]
                close = by_stock.get(sid, {}).get(d)
                if close and base.get(sid):
                    gross_nav += row["weight"] / 100 * (close / base[sid])
                    used += 1
            if used < max(5, len(positions) * 0.6):
                continue
            elapsed_weeks = idx // 5
            cumulative_cost_fraction = min(0.35, elapsed_weeks * weekly_cost_fraction)
            net_nav = gross_nav * (1 - cumulative_cost_fraction)
            raw_points.append({
                "date": d,
                "nav_raw": net_nav,
                "gross_nav_raw": gross_nav,
                "cumulative_cost": round(cumulative_cost_fraction * 100, 4),
            })
        if not raw_points:
            return {"date": rating_date, "points": [], "cost_model": cost_model}
        net_base = raw_points[0]["nav_raw"] or 1
        gross_base = raw_points[0]["gross_nav_raw"] or 1
        points: list[dict[str, Any]] = []
        prev_nav = None
        for row in raw_points:
            net_nav = row["nav_raw"] / net_base
            gross_nav = row["gross_nav_raw"] / gross_base
            daily_return = 0.0 if prev_nav is None else (net_nav / prev_nav - 1) * 100
            points.append({
                "date": row["date"],
                "nav": round(net_nav, 4),
                "gross_nav": round(gross_nav, 4),
                "daily_return": round(daily_return, 2),
                "cumulative_cost": row["cumulative_cost"],
            })
            prev_nav = net_nav
        benchmarks = self._ai_fund_index_benchmarks(dates)
        return {"date": rating_date, "points": points, "benchmarks": benchmarks, "benchmark": [], "cost_model": cost_model}

    def _ai_fund_index_benchmarks(self, dates: list[str]) -> list[dict[str, Any]]:
        """公开指数基准：沪深300、中证消费指数、800消费指数。"""
        if not dates:
            return []
        benchmark_defs = [
            {
                "key": "hs300",
                "name": "沪深300",
                "security_id": "000300.SH",
                "aliases": ["000300.SH", "000300.CSI", "sh000300", "SH000300"],
            },
            {
                "key": "csi_consumer",
                "name": "中证消费指数",
                "security_id": "000990.SH",
                "aliases": ["000990.SH", "000990.CSI", "sh000990", "SH000990"],
            },
            {
                "key": "csi800_consumer",
                "name": "800消费指数",
                "security_id": "000932.SH",
                "aliases": ["000932.SH", "000932.CSI", "sh000932", "SH000932"],
            },
        ]
        with self.connect() as connection:
            result: list[dict[str, Any]] = []
            for item in benchmark_defs:
                aliases = item["aliases"]
                placeholders = ",".join("?" for _ in aliases)
                rows = [dict(row) for row in connection.execute(
                    f"""SELECT security_id,trade_date,close_price,change_pct
                        FROM stock_daily_quotes
                        WHERE security_id IN ({placeholders})
                          AND trade_date BETWEEN ? AND ?
                          AND close_price IS NOT NULL AND close_price > 0
                        ORDER BY trade_date ASC""",
                    [*aliases, dates[0], dates[-1]],
                ).fetchall()]
                if not rows:
                    result.append({
                        "key": item["key"],
                        "name": item["name"],
                        "security_id": item["security_id"],
                        "points": [],
                        "status": "missing",
                        "note": "基准指数行情待同步",
                    })
                    continue
                base = float(rows[0]["close_price"]) or 1.0
                points = [
                    {
                        "date": row["trade_date"],
                        "nav": round(float(row["close_price"]) / base, 4),
                        "close_price": round(float(row["close_price"]), 4),
                        "change_pct": row["change_pct"],
                    }
                    for row in rows
                ]
                result.append({
                    "key": item["key"],
                    "name": item["name"],
                    "security_id": rows[0]["security_id"],
                    "points": points,
                    "status": "ok",
                    "note": f"{rows[0]['trade_date']} 至 {rows[-1]['trade_date']}",
                })
            return result

    def _ai_fund_benchmark_nav(self, dates: list[str]) -> list[dict[str, Any]]:
        """兼容旧调用：已改用 _ai_fund_index_benchmarks。"""
        benchmarks = self._ai_fund_index_benchmarks(dates)
        for item in benchmarks:
            if item.get("points"):
                return item["points"]
        return []

    def ai_fund_history(self) -> dict[str, Any]:
        with self.connect() as connection:
            self._ai_fund_ensure_schema(connection)
            rows = [dict(row) for row in connection.execute(
                """SELECT version_id,week_start,rating_date,generated_at,position_count,latest_nav,total_return,
                          gross_total_return,annualized_return,max_drawdown,turnover,weekly_cost,cost_bps,
                          model_name,ai_generated
                   FROM ai_fund_portfolio_versions
                   ORDER BY week_start DESC LIMIT 20"""
            ).fetchall()]
        versions = []
        for row in rows:
            versions.append({**row, "ai_generated": bool(row.get("ai_generated"))})
        return {"versions": versions}

    def ai_fund_rebalance(self) -> dict[str, Any]:
        rating_date, positions = self._ai_fund_current_positions()
        orders = []
        for row in positions:
            change = row["weight_change"]
            if abs(change) < 0.3:
                action = "维持"
            elif change > 0:
                action = "增配"
            else:
                action = "降配"
            orders.append({
                "security_id": row["security_id"],
                "security_name": repair_mojibake(row["security_name"]),
                "action": action,
                "weight": row["weight"],
                "change": change,
                "reason": row["reason"],
            })
        return {
            "date": rating_date,
            "frequency": "每周一次自动调仓",
            "next_rebalance": self._next_monday(rating_date),
            "note": "当前为模拟组合；调仓动作由规则模型自动生成，后续接入 SYSTEM_LLM 后增强事件解释，不接用户问答 Key。",
            "orders": orders,
        }

    def _ai_fund_autonomy_context(self, rating_date: str | None, positions: list[dict[str, Any]]) -> dict[str, Any]:
        overview = self.ai_fund_overview()
        with self.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                """SELECT event_time,available_at,event_type,sector_code,title,summary,materiality_score,source_url,locator
                   FROM monitor_events
                   WHERE date(COALESCE(available_at,event_time)) >= date(?, '-14 days')
                   ORDER BY materiality_score DESC, COALESCE(available_at,event_time) DESC
                   LIMIT 20""",
                (rating_date or default_cutoff_date(),),
            ).fetchall()]
            try:
                calibration = self_calibration.run(self.db_path, rating_date)
            except Exception as exc:  # noqa: BLE001 - 自主投研不应因审计模块临时失败而中断
                calibration = {"status": "unavailable", "error": str(exc)[:160]}
        candidate_pool = [{
            "security_id": row.get("security_id"),
            "name": repair_mojibake(row.get("security_name")),
            "sector": row.get("sector_name"),
            "label": row.get("label"),
            "base_score": row.get("score"),
            "weight": row.get("weight"),
            "weight_change": row.get("weight_change"),
            "pe_ttm": row.get("pe_ttm"),
            "change_pct": row.get("change_pct"),
            "reason": row.get("reason"),
        } for row in positions[:30]]
        return {
            "date": rating_date,
            "objective": overview.get("objective"),
            "portfolio_state": {
                "position_count": overview.get("position_count"),
                "turnover": overview.get("turnover"),
                "turnover_band": overview.get("turnover_band"),
                "trade_cost": overview.get("trade_cost"),
                "sector_weights": overview.get("sector_weights", [])[:10],
                "label_weights": overview.get("label_weights", []),
                "nav": {
                    "latest_nav": overview.get("latest_nav"),
                    "total_return": overview.get("total_return"),
                    "annualized_return": overview.get("annualized_return"),
                    "max_drawdown": overview.get("max_drawdown"),
                },
            },
            "candidate_pool": candidate_pool,
            "recent_events": [{
                "event_type": row.get("event_type"),
                "sector_code": row.get("sector_code"),
                "title": repair_mojibake(row.get("title") or ""),
                "summary": repair_mojibake(row.get("summary") or ""),
                "score": row.get("materiality_score"),
                "source": row.get("source_url") or row.get("locator"),
            } for row in rows],
            "backtest_snapshot": {
                "status": calibration.get("status"),
                "outcome_groups": calibration.get("outcome_groups", [])[:8],
                "active_rule": (calibration.get("active_rule") or {}).get("rule_version"),
                "shadow_rule": (calibration.get("shadow_rule") or {}).get("rule_version"),
            },
            "guardrails": {
                "no_real_trading": True,
                "max_single_stock_delta": 4,
                "max_veto_delta": -8,
                "min_confidence_for_apply": 0.6,
                "max_factor_delta_abs": 0.05,
                "max_sector_weight": 30,
                "max_turnover": 30,
                "main_push_limit": 5,
                "position_count": 30,
            },
        }

    def _ai_fund_parse_autonomy_json(self, text: str) -> dict[str, Any]:
        content = (text or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("模型返回不是 JSON 对象")
        return parsed

    def _ai_fund_rule_autonomy_package(self, context: dict[str, Any]) -> dict[str, Any]:
        positions = context.get("candidate_pool") or []
        sector_weights = context.get("portfolio_state", {}).get("sector_weights") or []
        overweight = [s for s in sector_weights if float(s.get("weight") or 0) > 24]
        top = positions[:6]
        actions = []
        for idx, row in enumerate(top):
            delta = 2.0 if idx < 3 else 1.0
            actions.append({
                "security_id": row.get("security_id"),
                "security_name": row.get("name"),
                "sector_name": row.get("sector"),
                "action": "raise_score",
                "score_delta": delta,
                "confidence": 0.68,
                "horizon": "3-6个月",
                "reason": "规则分位居前，且符合中期/中长期持有筛选口径。",
                "evidence": [row.get("reason")],
            })
        for row in positions:
            if float(row.get("change_pct") or 0) > 7:
                actions.append({
                    "security_id": row.get("security_id"),
                    "security_name": row.get("name"),
                    "sector_name": row.get("sector"),
                    "action": "reduce_score",
                    "score_delta": -2.0,
                    "confidence": 0.7,
                    "horizon": "1-4周",
                    "reason": "短期涨幅偏大，避免把短期情绪误当作中期逻辑。",
                    "evidence": [f"当日涨跌幅 {row.get('change_pct')}%"],
                })
                break
        return {
            "market_view": {
                "summary": "系统模型未生成时，使用规则兜底：保持中期/中长期质量优先，控制换手和行业集中。",
                "risk_level": "medium",
                "preferred_sectors": [s.get("name") for s in sector_weights[:3]],
                "avoid_sectors": [s.get("name") for s in overweight[:2]],
            },
            "factor_view": {
                "valuation_delta": 0.02,
                "stability_delta": 0.02,
                "catalyst_delta": -0.01,
                "timing_delta": -0.01,
                "risk_penalty_delta": 0.02,
                "reason": "兜底策略提高估值和稳定性权重，降低短期事件追逐。",
            },
            "stock_actions": actions[:10],
            "portfolio_view": {
                "max_turnover_suggestion": 0.15,
                "sector_bias": [],
                "reason": "维持30只组合与低换手约束，只在影子分中体现 AI/规则建议。",
            },
            "audit_notes": ["当前为规则兜底自主建议包；配置 SYSTEM_LLM 后会由大模型生成更完整的主动投研判断。"],
        }

    def _ai_fund_generate_autonomy_package(self, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        config = self._system_llm_config()
        if not config:
            return self._ai_fund_rule_autonomy_package(context), {"ok": False, "mode": "rule_fallback", "error": "SYSTEM_LLM 未配置"}
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个更自主的消费行业 AI 基金经理。你可以主动提出行业观点、因子权重调整、"
                    "单股加减分、风险否决和组合方向建议。但你不能给真实交易指令，不能编造未给出的事实。"
                    "只输出 JSON，不要 Markdown。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "基于下面上下文，生成“AI自主投研建议包”。必须输出 JSON，字段："
                    "market_view{summary,risk_level,preferred_sectors,avoid_sectors}; "
                    "factor_view{valuation_delta,stability_delta,catalyst_delta,timing_delta,risk_penalty_delta,reason}; "
                    "stock_actions数组，每项{security_id,security_name,sector_name,action,score_delta,confidence,horizon,reason,evidence}; "
                    "portfolio_view{max_turnover_suggestion,sector_bias,reason}; audit_notes数组。"
                    "action 只能是 raise_score/reduce_score/veto/observe/upgrade。"
                    "score_delta 控制在 -8 到 +4，confidence 为0-1。优先体现中期/中长期好股票，不做短线追涨。\n\n"
                    + json.dumps(context, ensure_ascii=False)
                ),
            },
        ]
        started = datetime.now().timestamp()
        try:
            data = self._llm_chat_completion(config, messages, stream=False, max_tokens=3000, timeout=60)
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = self._ai_fund_parse_autonomy_json(text)
            elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
            return parsed, {
                "ok": True,
                "mode": "system_llm",
                "provider": config.get("provider"),
                "model": config.get("model"),
                "elapsed_ms": elapsed_ms,
            }
        except Exception as exc:
            self.audit("ai_fund_autonomy_llm", outcome="failed", detail={"model": config.get("model"), "error": str(exc)[:200]})
            fallback = self._ai_fund_rule_autonomy_package(context)
            return fallback, {
                "ok": False,
                "mode": "rule_fallback",
                "provider": config.get("provider"),
                "model": config.get("model"),
                "error": str(exc)[:180],
            }

    def _ai_fund_apply_autonomy_guardrails(
        self,
        package: dict[str, Any],
        positions: list[dict[str, Any]],
        overview: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        by_id = {row.get("security_id"): row for row in positions}
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for raw in (package.get("stock_actions") or [])[:20]:
            if not isinstance(raw, dict):
                continue
            sid = raw.get("security_id")
            row = by_id.get(sid)
            confidence = float(raw.get("confidence") or 0)
            requested = float(raw.get("score_delta") or 0)
            action = str(raw.get("action") or "observe")
            reason = repair_mojibake(raw.get("reason") or "")
            reject_reason = ""
            if not row:
                reject_reason = "不在当前30只持仓内，先进入观察，不直接影响组合。"
            elif confidence < 0.6:
                reject_reason = "置信度低于0.60，只展示为观察建议。"
            elif action in {"raise_score", "upgrade"} and requested <= 0:
                reject_reason = "正向建议缺少正向分值。"
            elif action in {"reduce_score", "veto"} and requested >= 0:
                reject_reason = "负向/否决建议缺少负向分值。"
            applied = max(-8.0, min(4.0, requested * confidence))
            if action == "veto":
                applied = min(applied, -5.0)
            if reject_reason:
                rejected.append({**raw, "accepted": False, "applied_delta": 0, "reject_reason": reject_reason})
            else:
                accepted.append({
                    **raw,
                    "security_id": sid,
                    "security_name": repair_mojibake(raw.get("security_name") or row.get("security_name")),
                    "sector_name": repair_mojibake(raw.get("sector_name") or row.get("sector_name")),
                    "accepted": True,
                    "applied_delta": round(applied, 2),
                    "base_score": row.get("score"),
                    "shadow_score": round(float(row.get("score") or 0) + applied, 2),
                    "reason": reason,
                })
        factor = package.get("factor_view") if isinstance(package.get("factor_view"), dict) else {}
        clipped_factor = {}
        for key in ("valuation_delta", "stability_delta", "catalyst_delta", "timing_delta", "risk_penalty_delta"):
            clipped_factor[key] = round(max(-0.05, min(0.05, float(factor.get(key) or 0))), 4)
        shadow_positions = []
        delta_by_id = {item["security_id"]: item["applied_delta"] for item in accepted}
        for row in positions:
            shadow_positions.append({
                "security_id": row.get("security_id"),
                "security_name": repair_mojibake(row.get("security_name")),
                "sector_name": row.get("sector_name"),
                "weight": row.get("weight"),
                "base_score": row.get("score"),
                "shadow_score": round(float(row.get("score") or 0) + float(delta_by_id.get(row.get("security_id"), 0)), 2),
                "llm_delta": round(float(delta_by_id.get(row.get("security_id"), 0)), 2),
            })
        shadow_positions.sort(key=lambda x: x["shadow_score"], reverse=True)
        guardrail_summary = {
            "accepted": len(accepted),
            "rejected": len(rejected),
            "factor_adjustment": clipped_factor,
            "formal_position_changed": False,
            "note": "AI自主建议只进入影子增强分和审计展示；正式持仓仍需通过自动回测/风控后才升级。",
            "turnover_ok": float(overview.get("turnover") or 0) <= 30,
            "max_sector_ok": max((float(x.get("weight") or 0) for x in overview.get("sector_weights", [])), default=0) <= 30,
        }
        return accepted, rejected, {"summary": guardrail_summary, "shadow_positions": shadow_positions[:30]}

    def ai_fund_autonomy(self) -> dict[str, Any]:
        rating_date, positions = self._ai_fund_current_positions()
        overview = self.ai_fund_overview()
        version_id = self._ai_fund_version_id(rating_date)
        run_id = stable_id("llm-research-run", version_id, rating_date or "", "v2-autonomy")
        with self.connect() as connection:
            self._ai_fund_ensure_schema(connection)
            cached = connection.execute(
                "SELECT summary_json, raw_output_json, status, model_provider, model_name, mode, elapsed_ms FROM llm_research_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if cached:
                summary = json_load(cached["summary_json"], {})
                raw = json_load(cached["raw_output_json"], {})
                return {
                    "run_id": run_id,
                    "date": rating_date,
                    "cached": True,
                    "llm": {
                        "ok": cached["status"] == "success",
                        "provider": cached["model_provider"],
                        "model": cached["model_name"],
                        "mode": cached["mode"],
                        "elapsed_ms": cached["elapsed_ms"],
                    },
                    **summary,
                    "raw_package": raw,
                }
        context = self._ai_fund_autonomy_context(rating_date, positions)
        package, llm_meta = self._ai_fund_generate_autonomy_package(context)
        accepted, rejected, guardrails = self._ai_fund_apply_autonomy_guardrails(package, positions, overview)
        payload = {
            "market_view": package.get("market_view") or {},
            "factor_view": package.get("factor_view") or {},
            "portfolio_view": package.get("portfolio_view") or {},
            "audit_notes": package.get("audit_notes") or [],
            "accepted_actions": accepted,
            "rejected_actions": rejected,
            "guardrails": guardrails.get("summary") or {},
            "shadow_positions": guardrails.get("shadow_positions") or [],
        }
        now = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        with self.connect() as connection:
            self._ai_fund_ensure_schema(connection)
            connection.execute(
                """INSERT OR REPLACE INTO llm_research_runs
                   (run_id,run_date,model_provider,model_name,mode,status,input_hash,output_hash,created_at,elapsed_ms,summary_json,raw_output_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    rating_date,
                    llm_meta.get("provider"),
                    llm_meta.get("model"),
                    llm_meta.get("mode"),
                    "success" if llm_meta.get("ok") else "fallback",
                    stable_id("input", json.dumps(context, ensure_ascii=False))[:32],
                    stable_id("output", json.dumps(package, ensure_ascii=False))[:32],
                    now,
                    llm_meta.get("elapsed_ms"),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(package, ensure_ascii=False),
                ),
            )
            connection.execute("DELETE FROM llm_research_actions WHERE run_id=?", (run_id,))
            connection.execute("DELETE FROM llm_action_guardrail_results WHERE run_id=?", (run_id,))
            for idx, item in enumerate(accepted + rejected):
                action_id = stable_id("llm-action", run_id, str(idx), item.get("security_id") or "", item.get("action") or "")
                connection.execute(
                    """INSERT OR REPLACE INTO llm_research_actions
                       (action_id,run_id,action_type,security_id,security_name,sector_name,score_delta,confidence,horizon,reason,evidence_json,raw_action_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        action_id, run_id, item.get("action"), item.get("security_id"), repair_mojibake(item.get("security_name")),
                        repair_mojibake(item.get("sector_name")), item.get("score_delta"), item.get("confidence"), item.get("horizon"),
                        repair_mojibake(item.get("reason")), json.dumps(item.get("evidence") or [], ensure_ascii=False),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
                connection.execute(
                    """INSERT OR REPLACE INTO llm_action_guardrail_results
                       (action_id,run_id,accepted,applied_delta,reject_reason,guardrail_detail_json)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        action_id, run_id, 1 if item.get("accepted") else 0, item.get("applied_delta", 0),
                        item.get("reject_reason"), json.dumps({"confidence": item.get("confidence")}, ensure_ascii=False),
                    ),
                )
            shadow_version = stable_id("shadow-portfolio", run_id)
            connection.execute(
                """INSERT OR REPLACE INTO ai_fund_shadow_portfolios
                   (shadow_version,run_id,run_date,positions_json,factor_weights_json,expected_changes_json,created_at,status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    shadow_version, run_id, rating_date, json.dumps(payload["shadow_positions"], ensure_ascii=False),
                    json.dumps(payload["guardrails"].get("factor_adjustment") or {}, ensure_ascii=False),
                    json.dumps({"accepted": len(accepted), "rejected": len(rejected)}, ensure_ascii=False),
                    now, "shadow_observing",
                ),
            )
            connection.commit()
        return {"run_id": run_id, "date": rating_date, "cached": False, "llm": llm_meta, **payload, "raw_package": package}

    def ai_fund_strategy(self) -> dict[str, Any]:
        rating_date, positions = self._ai_fund_current_positions()
        overview = self.ai_fund_overview()
        label_counts: dict[str, int] = {}
        sector_counts: dict[str, int] = {}
        for row in positions:
            label_counts[row["label"]] = label_counts.get(row["label"], 0) + 1
            sector_counts[row["sector_name"]] = sector_counts.get(row["sector_name"], 0) + 1
        avg_score = round(sum(row["score"] for row in positions) / len(positions), 1) if positions else 0
        top_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:4]
        adds = [row for row in positions if row.get("weight_change", 0) > 0.25][:6]
        cuts = [row for row in positions if row.get("weight_change", 0) < -0.25][:6]
        turnover_band = overview.get("turnover_band") or self._turnover_band(float(overview.get("turnover") or 0))
        trade_cost = overview.get("trade_cost") or {}
        version_id = self._ai_fund_version_id(rating_date)
        result = {
            "version_id": version_id,
            "date": rating_date,
            "title": "AI基金经理本周持仓策略",
            "summary": [
                "本周继续坚持中期、中长期好股票优先，不做一两日情绪博弈。",
                f"组合固定为30只消费股，平均投资分约 {avg_score}，每周自动复核一次。",
                f"选股先看公司质量和中长期稳定性，再看估值位置、事件催化、行情状态和风险扣分；本周模拟交易成本约 {trade_cost.get('weekly_cost', 0)}%。",
            ],
            "strategy": [
                {
                    "name": "核心选股口径",
                    "detail": "优先选择“每日主推清单”和“可以考虑买入”中的股票；若数量不足，再少量纳入“等待买点”作为观察型配置，不把“暂不推荐/行业扫描”纳入组合。",
                },
                {
                    "name": "权重分配口径",
                    "detail": "按投资分、稳定性、事件强度和估值风险自动加权；单票目标约1%—7%，避免单只股票过度影响组合。",
                },
                {
                    "name": "行业分散口径",
                    "detail": "对子行业设置集中度约束，食品饮料、家电、纺服、汽车、家居、零售等板块尽量分散，避免组合只押一个消费方向。",
                },
                {
                    "name": "调仓口径",
                    "detail": "每周自动比较新一版评分、事件、风险和行业暴露；分数改善则增配，风险上升或性价比下降则降配。",
                },
            ],
            "this_week": {
                "position_count": len(positions),
                "avg_score": avg_score,
                "label_counts": [{"name": k, "count": v} for k, v in sorted(label_counts.items(), key=lambda x: x[1], reverse=True)],
                "top_sectors": [{"name": k, "count": v} for k, v in top_sectors],
                "next_rebalance": self._next_monday(rating_date),
            },
            "improvements": [
                "相比上一版，本周更强调“长期稳定性”和“中期可持有性”，避免只因为当日涨跌或短期事件就大幅换仓。",
                "权重不再只按排名平均分配，而是加入单票上限和行业集中度约束，让组合更像基金经理管理的组合。",
                f"新增换手率审计：本周模拟换手率 {overview.get('turnover')}%，判定为“{turnover_band.get('level')}”。{turnover_band.get('detail')}",
                f"新增交易成本模型：默认扣除买入佣金、卖出佣金/税费、基础滑点和流动性滑点；本周成本约 {trade_cost.get('weekly_cost_bps', 0)}bp，净值曲线采用扣费后口径。",
                "把事件影响作为调仓解释的一部分：事件强但质量不足的股票不会直接进入核心持仓。",
                "自动审计增加了持仓数量、单票权重、子行业集中度和用户 Key 隔离检查，便于后续复盘。",
            ],
            "adds": [{
                "security_id": row["security_id"],
                "security_name": repair_mojibake(row["security_name"]),
                "weight": row["weight"],
                "change": row["weight_change"],
                "reason": row["reason"],
            } for row in adds],
            "cuts": [{
                "security_id": row["security_id"],
                "security_name": repair_mojibake(row["security_name"]),
                "weight": row["weight"],
                "change": row["weight_change"],
                "reason": row["reason"],
            } for row in cuts],
            "system_llm": overview.get("system_llm"),
        }
        autonomy_run_id = stable_id("llm-research-run", version_id, rating_date or "", "v2-autonomy")
        with self.connect() as connection:
            autonomy_row = connection.execute(
                "SELECT summary_json, status, model_provider, model_name, mode, elapsed_ms FROM llm_research_runs WHERE run_id=?",
                (autonomy_run_id,),
            ).fetchone()
        if autonomy_row:
            autonomy_summary = json_load(autonomy_row["summary_json"], {})
            result["autonomy"] = {
                "run_id": autonomy_run_id,
                "date": rating_date,
                "cached": True,
                "llm": {
                    "ok": autonomy_row["status"] == "success",
                    "provider": autonomy_row["model_provider"],
                    "model": autonomy_row["model_name"],
                    "mode": autonomy_row["mode"],
                    "elapsed_ms": autonomy_row["elapsed_ms"],
                },
                **autonomy_summary,
            }
        cached = self._ai_fund_cached_strategy(version_id)
        if cached and cached.get("ai_strategy"):
            result["ai_strategy"] = cached.get("ai_strategy")
            result["ai_generated"] = bool(result["ai_strategy"].get("ok"))
            result["cached"] = True
            return result
        result["ai_strategy"] = self._ai_fund_generate_strategy_text(result, overview, positions)
        result["ai_generated"] = bool(result["ai_strategy"].get("ok"))
        result["cached"] = False
        self._ai_fund_save_snapshot(version_id, overview, positions, result)
        return result

    def _ai_fund_generate_strategy_text(
        self,
        strategy_payload: dict[str, Any],
        overview: dict[str, Any],
        positions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        config = self._system_llm_config()
        if not config:
            return {"ok": False, "mode": "rule_fallback", "error": "SYSTEM_LLM 未配置"}
        top_positions = [{
            "name": repair_mojibake(row.get("security_name")),
            "code": row.get("security_id"),
            "sector": row.get("sector_name"),
            "label": row.get("label"),
            "score": row.get("score"),
            "weight": row.get("weight"),
            "change": row.get("weight_change"),
            "reason": row.get("reason"),
        } for row in positions[:12]]
        compact = {
            "date": strategy_payload.get("date"),
            "objective": overview.get("objective"),
            "nav": {
                "latest_nav": overview.get("latest_nav"),
                "total_return": overview.get("total_return"),
                "gross_total_return": overview.get("gross_total_return"),
                "annualized_return": overview.get("annualized_return"),
                "cost_drag": overview.get("cost_drag"),
            },
            "portfolio": {
                "position_count": overview.get("position_count"),
                "turnover": overview.get("turnover"),
                "turnover_band": overview.get("turnover_band"),
                "trade_cost": overview.get("trade_cost"),
                "sector_weights": overview.get("sector_weights", [])[:8],
                "label_weights": overview.get("label_weights", []),
            },
            "top_positions": top_positions,
            "adds": strategy_payload.get("adds", [])[:6],
            "cuts": strategy_payload.get("cuts", [])[:6],
            "rule_strategy": strategy_payload.get("strategy", []),
            "rule_improvements": strategy_payload.get("improvements", []),
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个全自动运行的消费行业 AI 基金经理。你只基于给定 JSON 写周度持仓策略说明，"
                    "不得编造未给出的数据，不得承诺收益，不得给真实交易指令。必须说明这是模拟组合。"
                    "请只输出 JSON，不要 Markdown。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请生成 AI 基金经理本周策略说明。输出 JSON 字段必须包含："
                    "headline 字符串；holding_logic 数组3条；rebalance_logic 数组3条；"
                    "improvements 数组3条；risk_watch 数组3条；cost_note 字符串。"
                    "每条控制在50字以内，语言像基金经理周报，具体、克制，不要空话。\n\n"
                    + json.dumps(compact, ensure_ascii=False)
                ),
            },
        ]
        started = datetime.now().timestamp()
        try:
            data = self._llm_chat_completion(config, messages, stream=False, max_tokens=1200, timeout=30)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("模型返回不是 JSON 对象")
            elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
            return {
                "ok": True,
                "mode": "system_llm",
                "provider": config.get("provider"),
                "model": config.get("model"),
                "elapsed_ms": elapsed_ms,
                "content": parsed,
            }
        except Exception as exc:
            self.audit("ai_fund_strategy_llm", outcome="failed", detail={"model": config.get("model"), "error": str(exc)[:200]})
            return {
                "ok": False,
                "mode": "rule_fallback",
                "provider": config.get("provider"),
                "model": config.get("model"),
                "error": str(exc)[:180],
            }

    def ai_fund_events(self) -> dict[str, Any]:
        with self.connect() as connection:
            latest = connection.execute("SELECT MAX(rating_date) FROM daily_stock_ratings").fetchone()[0]
            rows = [dict(row) for row in connection.execute(
                """SELECT event_time,available_at,event_type,sector_code,title,summary,materiality_score,source_url,locator
                   FROM monitor_events
                   WHERE date(COALESCE(available_at,event_time)) >= date(?, '-14 days')
                   ORDER BY materiality_score DESC, COALESCE(available_at,event_time) DESC
                   LIMIT 12""",
                (latest,),
            ).fetchall()]
        impacts = []
        for row in rows:
            score = float(row.get("materiality_score") or 0)
            if score >= 0.75:
                impact = "高影响"
            elif score >= 0.45:
                impact = "中影响"
            else:
                impact = "低影响"
            impacts.append({
                "date": row.get("available_at") or row.get("event_time"),
                "type": row.get("event_type"),
                "sector_code": row.get("sector_code"),
                "title": repair_mojibake(row.get("title") or ""),
                "summary": repair_mojibake(row.get("summary") or ""),
                "impact": impact,
                "score": round(score, 2),
                "source": row.get("source_url") or row.get("locator"),
            })
        return {"events": impacts}

    def ai_fund_audit(self) -> dict[str, Any]:
        overview = self.ai_fund_overview()
        positions = self.ai_fund_positions().get("positions", [])
        max_sector = max((x["weight"] for x in overview.get("sector_weights", [])), default=0)
        turnover_band = overview.get("turnover_band") or self._turnover_band(float(overview.get("turnover") or 0))
        trade_cost = overview.get("trade_cost") or {}
        checks = [
            {"item": "持仓数量", "ok": len(positions) == 30, "detail": f"{len(positions)}/30"},
            {"item": "单票权重", "ok": all(0.5 <= p["weight"] <= 8 for p in positions), "detail": "目标 1%—7%，硬上限 8%"},
            {"item": "子行业集中度", "ok": max_sector <= 30, "detail": f"最大子行业 {max_sector:.2f}%"},
            {"item": "模拟换手率", "ok": bool(turnover_band.get("ok")), "detail": f"{overview.get('turnover')}% · {turnover_band.get('level')}：{turnover_band.get('detail')}"},
            {"item": "交易成本已扣除", "ok": True, "detail": f"本周约 {trade_cost.get('weekly_cost', 0)}% / {trade_cost.get('weekly_cost_bps', 0)}bp；{trade_cost.get('assumption', '')}"},
            {"item": "用户 Key 隔离", "ok": True, "detail": "AI基金经理只读取站长 SYSTEM_LLM，不读取浏览器用户 Key"},
            {"item": "可审计", "ok": True, "detail": "本页展示评分、权重、事件与下次调仓日"},
        ]
        return {"checks": checks}


    FACT_DATE = re.compile(r"(今天|今日).*(周几|星期几|几号|日期)|今天周几|今天星期几")
    FACT_TIME = re.compile(r"(现在|当前).*(几点|时间)")
    FACT_SYNC = re.compile(r"数据.*(哪天|何时|什么时候|多久).*(更新|同步|刷新)|(数据|底座).*(最新|新鲜度)")

    def _fact_answer(self, question: str) -> dict[str, Any] | None:
        """系统事实题秒回：日期/时间/数据同步状态，不走模型。"""
        now = datetime.now(SHANGHAI)
        weekday_cn = "一二三四五六日"[now.weekday()]
        if self.FACT_DATE.search(question):
            text = f"今天是 **{now.strftime('%Y年%m月%d日')} 星期{weekday_cn}**（北京时间 {now.strftime('%H:%M')}）。\n\n说明：研究底座数据截止于其各自标注的同步时间（与“今天”不同属两个概念），回答研究问题时会明确区分。"
            return {"ok": True, "answer": text, "elapsed_ms": 0, "context_items": 0, "fast_path": "system_date"}
        if self.FACT_TIME.search(question):
            text = f"现在是 **{now.strftime('%H:%M:%S')}**，{now.strftime('%Y年%m月%d日')} 星期{weekday_cn}（北京时间）。"
            return {"ok": True, "answer": text, "elapsed_ms": 0, "context_items": 0, "fast_path": "system_time"}
        if self.FACT_SYNC.search(question):
            with self.connect() as connection:
                rows = connection.execute(
                    """SELECT s.name, MAX(c.last_success_at) AS last_sync
                       FROM source_cursors c JOIN source_catalog s ON s.source_id=c.source_id
                       WHERE c.last_success_at IS NOT NULL GROUP BY s.name ORDER BY last_sync DESC LIMIT 5"""
                ).fetchall()
                rating_date = connection.execute("SELECT MAX(rating_date) FROM daily_stock_ratings").fetchone()[0]
            lines = [f"- {r['name']}：{str(r['last_sync'])[:16]}" for r in rows]
            text = "底座各来源最近同步时间：\n" + "\n".join(lines) + f"\n\n股票评级数据日期：**{rating_date}**。每日 08:30 自动同步一次。"
            return {"ok": True, "answer": text, "elapsed_ms": 0, "context_items": 0, "fast_path": "sync_status"}
        return None

    def ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        """AI 研究员问答：本地研究底座打包上下文 + 本机模型代理快速生成。目标 < 30 秒。"""
        question = str(payload.get("question", "")).strip()
        if not question:
            raise ValueError("问题不能为空")
        if len(question) > 500:
            raise ValueError("问题过长，请控制在 500 字以内")
        history = payload.get("history") or []
        fact = self._fact_answer(question)
        if fact:
            return fact
        messages, used = self._build_ask_messages(question, history)
        llm_config = self._sanitize_user_llm_config(payload.get("llm_config"))
        if llm_config:
            started = datetime.now().timestamp()
            try:
                data = self._llm_chat_completion(llm_config, messages, stream=False, max_tokens=4000, timeout=60)
                answer = data["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                self.audit("ask", outcome="failed", detail={"error": str(exc)[:200], "provider": llm_config.get("provider")})
                return {"ok": False, "error": f"模型调用失败：{str(exc)[:120]}", "elapsed_ms": int((datetime.now().timestamp() - started) * 1000)}
            elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
            self.audit("ask", outcome="success", detail={"q": question[:60], "elapsed_ms": elapsed_ms, "provider": llm_config.get("provider")})
            return {"ok": True, "answer": answer, "elapsed_ms": elapsed_ms, "context_items": used, "llm_mode": "user_key"}
        request_body = json.dumps({
            "model": "kimi-k3", "messages": messages, "max_tokens": 4000, "stream": False,
        }).encode("utf-8")
        started = datetime.now().timestamp()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:15721/v1/chat/completions",
                data=request_body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED"},
            )
            with urllib.request.urlopen(req, timeout=50) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            answer = data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            self.audit("ask", outcome="failed", detail={"error": str(exc)[:200]})
            return {"ok": False, "error": f"模型调用失败：{str(exc)[:120]}", "elapsed_ms": int((datetime.now().timestamp() - started) * 1000)}
        elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
        self.audit("ask", outcome="success", detail={"q": question[:60], "elapsed_ms": elapsed_ms})
        return {"ok": True, "answer": answer, "elapsed_ms": elapsed_ms, "context_items": used}

    def _build_ask_messages(self, question: str, history: list) -> tuple[list, int]:
        now = datetime.now(SHANGHAI)
        weekday_cn = "一二三四五六日"[now.weekday()]
        cutoff_iso = cutoff_timestamp(default_cutoff_date())
        with self.connect() as connection:
            events = connection.execute(
                """SELECT title, summary, event_type, available_at, raw_json FROM monitor_events
                   WHERE status='accepted' AND available_at >= ? AND available_at <= ?
                   ORDER BY available_at DESC LIMIT 10""",
                ((now - timedelta(days=3)).isoformat(), cutoff_iso),
            ).fetchall()
            snapshot = connection.execute(
                """SELECT title, summary FROM monitor_events
                   WHERE status='accepted' AND event_type='market_move' AND available_at <= ?
                   ORDER BY available_at DESC LIMIT 1""",
                (cutoff_iso,),
            ).fetchone()
            ratings = connection.execute(
                """SELECT security_name, total_score, rationale FROM daily_stock_ratings
                   WHERE rating_date=(SELECT MAX(rating_date) FROM daily_stock_ratings)
                   ORDER BY total_score DESC LIMIT 12"""
            ).fetchall()
            latest_rating_date = connection.execute(
                "SELECT MAX(rating_date) FROM daily_stock_ratings"
            ).fetchone()[0]
            releases = connection.execute(
                """SELECT d.title, (SELECT c.text_content FROM document_chunks c
                     WHERE c.document_id=d.document_id ORDER BY c.sequence_no LIMIT 1) AS key_figure
                   FROM documents d WHERE d.document_type='official_statistics_release' AND d.status='curated'
                   ORDER BY d.published_at DESC LIMIT 4"""
            ).fetchall()

        context_lines = [
            f"【系统事实】当前真实时间：{now.strftime('%Y-%m-%d')} 星期{weekday_cn} {now.strftime('%H:%M')}（北京时间）；"
            f"研究底座数据截止：{latest_rating_date or '2026-08-14'}（评级）/ 事件以各条标注时间为准。"
            "回答日期、星期、今天、最新类问题时，一律以当前真实时间为准，不得把底座数据截止日当作今天。",
            "【研究底座 · 当日内容】",
            "【评分口径】旧的“当日动量40%/估值30%/事件30%”已废弃。当前按中期/中长期持有价值生成投资分："
            "估值质量、跨日稳定性、催化质量、少量时点参考共同参与；短期涨跌/量比只作为买点与拥挤度参考，不主导推荐。"
            "每日主推≤5只，必须来自“可以考虑买入”候选且有明确动作；可以考虑买入保留25只；等待买点、长期观察、暂不推荐/行业扫描仅用于分层跟踪和后验校验。",
        ]
        used = 0
        # 问题中提到的股票：注入年报基本面与一致预期
        with self.connect() as conn2:
            names = [r[0] for r in conn2.execute(
                "SELECT DISTINCT security_name FROM research_universe_members"
            ).fetchall()]
            mentioned = [n for n in names if n and len(n) >= 2 and n in question][:3]
            for name in mentioned:
                f = conn2.execute(
                    """SELECT report_period,revenue_yi,revenue_yoy,net_profit_yi,profit_yoy,goodwill_yi,dividend_paid_ratio
                       FROM stock_fundamentals WHERE security_name=? ORDER BY report_period DESC LIMIT 1""",
                    (name,),
                ).fetchone()
                c = conn2.execute(
                    """SELECT forecast_year,net_profit_wy,np_yoy,eps,roe,pe,pb,target_price
                       FROM stock_consensus WHERE security_name=? ORDER BY forecast_year LIMIT 1""",
                    (name,),
                ).fetchone()
                parts = []
                def _pct(v):
                    return f"{v:+.1f}%" if v is not None else "—"
                if f:
                    parts.append(
                        f"{f['report_period'][:4]}年报：营收{f['revenue_yi']}亿元（同比{_pct(f['revenue_yoy'])}）、"
                        f"归母净利{f['net_profit_yi']}亿元（同比{_pct(f['profit_yoy'])}）、商誉{f['goodwill_yi']}亿元"
                        + (f"、股利支付率{f['dividend_paid_ratio']:.0f}%" if f["dividend_paid_ratio"] is not None else "")
                    )
                if c:
                    parts.append(
                        f"{c['forecast_year']}年一致预期：归母净利{(c['net_profit_wy'] or 0)/10000:.1f}亿元（同比{_pct(c['np_yoy'])}）、"
                        f"EPS {c['eps']}元、目标价{c['target_price']}元（现价对比见行情）"
                    )
                if parts:
                    context_lines.append(f"【个股档案·{name}】" + "；".join(parts))
                    used += 1

        if snapshot:
            context_lines.append(f"一、最新市场快照：{snapshot['title']}——{(snapshot['summary'] or '')[:200]}")
            used += 1
        context_lines.append("二、近 3 日事件（新到旧）：")
        for row in events:
            raw = json_load(row["raw_json"], {})
            layer = raw.get("research_layer", {}) if isinstance(raw, dict) else {}
            context_lines.append(f"- [{str(row['available_at'])[:10]}] {row['title']}（{layer.get('so_what') or (row['summary'] or '')[:60]}）")
            used += 1
        context_lines.append("三、今日股票关注（评分靠前）：")
        for row in ratings:
            context_lines.append(f"- {row['security_name']} 评分{row['total_score']}：{row['rationale']}")
            used += 1
        context_lines.append("四、官方宏观发布：")
        for row in releases:
            context_lines.append(f"- {row['title']}：{(row['key_figure'] or '')[:80]}")
            used += 1
        context_text = "\n".join(context_lines)

        messages = [{
            "role": "system",
            "content": (
                "你是服务公募基金经理的资深消费行业研究员。严格基于给定的研究底座内容回答：结论先行、直接明确；"
                "引用底座数据时标注来源（如【事件】【评级】【官方发布】）；底座未覆盖的信息明确说明，绝不编造；"
                "回答控制在 400 字内，用短句与要点；语气专业克制。"
            ),
        }]
        for item in history[-6:]:
            if isinstance(item, dict) and item.get("role") in ("user", "assistant") and item.get("content"):
                messages.append({"role": item["role"], "content": str(item["content"])[:800]})
        messages.append({"role": "user", "content": f"{context_text}\n\n【基金经理的问题】{question}"})
        return messages, used

    def llm_status(self) -> dict[str, Any]:
        """公网默认不绑定站长大模型；用户可在浏览器内填写自己的 OpenAI-compatible Key 增强。"""
        return {
            "enabled": False,
            "mode": "user_key_optional",
            "base_mode": "rule_only",
            "server_provider": None,
            "server_model": None,
            "enhanced_modules": ["AI研究员问答", "个股解释", "规则审计解释", "晨报文案增强"],
            "note": "未配置用户自己的模型 Key 时，系统按规则模型、聚源数据和本地数据库运行；填写 Key 后，仅该浏览器会启用 AI 增强。",
        }

    def _sanitize_user_llm_config(self, raw: Any) -> dict[str, str] | None:
        if not isinstance(raw, dict):
            return None
        enabled = raw.get("enabled", True)
        if enabled is False or str(enabled).lower() in {"0", "false", "off", "no"}:
            return None
        provider = str(raw.get("provider") or "openai-compatible").strip()[:40]
        base_url = str(raw.get("base_url") or "").strip().rstrip("/")
        model = str(raw.get("model") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        if not base_url or not model or not api_key:
            return None
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("模型 API Base URL 必须使用 https，或本机 localhost 调试地址")
        if not parsed.netloc:
            raise ValueError("模型 API Base URL 格式不正确")
        if len(model) > 120:
            raise ValueError("模型名称过长")
        if len(api_key) > 300:
            raise ValueError("API Key 过长")
        return {"provider": provider, "base_url": base_url, "model": model, "api_key": api_key}

    def _llm_chat_completion(
        self,
        config: dict[str, str],
        messages: list[dict[str, str]],
        *,
        stream: bool,
        max_tokens: int,
        timeout: int,
    ) -> dict[str, Any]:
        url = config["base_url"]
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        body = json.dumps({
            "model": config["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config['api_key']}",
                **({"Accept": "text/event-stream"} if stream else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_llm_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self._sanitize_user_llm_config(payload)
        if not config:
            raise ValueError("请填写 API Base URL、模型名称和 API Key")
        messages = [
            {"role": "system", "content": "你是连接测试助手。只回答：连接成功。"},
            {"role": "user", "content": "测试连接"},
        ]
        started = datetime.now().timestamp()
        try:
            data = self._llm_chat_completion(config, messages, stream=False, max_tokens=20, timeout=20)
            answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as exc:
            self.audit("llm_config_test", outcome="failed", detail={"provider": config.get("provider"), "error": str(exc)[:200]})
            return {"ok": False, "error": str(exc)[:180], "elapsed_ms": int((datetime.now().timestamp() - started) * 1000)}
        elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
        self.audit("llm_config_test", outcome="success", detail={"provider": config.get("provider"), "model": config.get("model"), "elapsed_ms": elapsed_ms})
        return {
            "ok": True,
            "provider": config["provider"],
            "model": config["model"],
            "elapsed_ms": elapsed_ms,
            "answer_preview": answer[:40],
        }

    def enhance_with_llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """用户自带 Key 的业务增强层：只解释/总结/复盘，不改写底层评分与推荐结果。"""
        config = self._sanitize_user_llm_config(payload.get("llm_config"))
        if not config:
            raise ValueError("请先在右上角“模型增强设置”里填写并启用自己的模型 Key")
        task = str(payload.get("task") or "").strip()
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        task_prompts = {
            "stock_explain": (
                "你要为一只消费行业股票生成个股解释。请严格基于输入 JSON，不要编造外部事实。"
                "输出用中文，分为：1）当前结论；2）为什么处于当前评级；3）中期/中长期逻辑；"
                "4）主要风险；5）升级或降级条件。不要给交易指令，不要承诺收益。"
            ),
            "audit_review": (
                "你要解释 AutoInvest Agent 的规则与审计结果。请严格基于输入 JSON。"
                "重点说明：推荐后验表现是否合理、为什么观察/扫描组可能阶段性更高、规则是否存在偏差、"
                "下一步应监控哪些指标。只做解释和建议，不直接修改规则。"
            ),
            "brief_enhance": (
                "你要把消费行研晨报增强成基金经理晨会可读版本。请严格基于输入 JSON。"
                "输出结构：今日一句话结论、市场/消费板块变化、主推股票线索、风险提示、今日需跟踪。"
                "保持专业克制，注明这是研究辅助，不构成交易指令。"
            ),
        }
        if task not in task_prompts:
            raise ValueError("未知的模型增强任务")
        compact_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))[:18000]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是服务公募基金经理的资深消费行业研究员，也是这个网页 Agent 的解释层。"
                    "底层规则模型负责计算，你只负责解释、总结、复盘和质检。"
                    "必须基于输入内容，不能虚构数据；涉及投资时使用研究观点口径，不输出买卖指令。"
                ),
            },
            {"role": "user", "content": f"{task_prompts[task]}\n\n【输入JSON】\n{compact_context}"},
        ]
        started = datetime.now().timestamp()
        try:
            data = self._llm_chat_completion(config, messages, stream=False, max_tokens=1400, timeout=60)
            answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as exc:
            self.audit("llm_enhance", outcome="failed", detail={"task": task, "provider": config.get("provider"), "error": str(exc)[:200]})
            return {"ok": False, "error": str(exc)[:180], "elapsed_ms": int((datetime.now().timestamp() - started) * 1000)}
        elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
        self.audit("llm_enhance", outcome="success", detail={"task": task, "provider": config.get("provider"), "model": config.get("model"), "elapsed_ms": elapsed_ms})
        return {"ok": True, "task": task, "answer": answer, "elapsed_ms": elapsed_ms, "model": config.get("model")}

    def ask_stream(self, payload: dict[str, Any], emit) -> dict[str, Any]:
        """流式问答：边生成边推送给页面。"""
        question = str(payload.get("question", "")).strip()
        if not question:
            raise ValueError("问题不能为空")
        fact = self._fact_answer(question)
        if fact:
            emit(fact["answer"])
            return {"ok": True, "elapsed_ms": 0, "fast_path": fact["fast_path"]}
        messages, used = self._build_ask_messages(question, payload.get("history") or [])
        llm_config = self._sanitize_user_llm_config(payload.get("llm_config"))
        if llm_config:
            started = datetime.now().timestamp()
            try:
                url = llm_config["base_url"]
                if not url.endswith("/chat/completions"):
                    url = f"{url}/chat/completions"
                request_body = json.dumps({
                    "model": llm_config["model"], "messages": messages, "max_tokens": 4000, "stream": True,
                }, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=request_body,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {llm_config['api_key']}",
                             "Accept": "text/event-stream"},
                )
                with urllib.request.urlopen(req, timeout=80) as resp:
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            if delta.get("content"):
                                emit(delta["content"])
                            elif delta.get("reasoning_content"):
                                emit(delta["reasoning_content"], kind="think")
            except Exception as exc:
                self.audit("ask", outcome="failed", detail={"error": str(exc)[:200], "stream": True, "provider": llm_config.get("provider")})
                return {"ok": False, "error": f"模型调用失败：{str(exc)[:120]}", "elapsed_ms": int((datetime.now().timestamp() - started) * 1000)}
            elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
            self.audit("ask", outcome="success", detail={"q": question[:60], "elapsed_ms": elapsed_ms, "stream": True, "provider": llm_config.get("provider")})
            return {"ok": True, "elapsed_ms": elapsed_ms, "context_items": used, "llm_mode": "user_key"}
        # 推理型模型：思考链也吃 token，需要给正文留足空间
        request_body = json.dumps({
            "model": "kimi-k3", "messages": messages, "max_tokens": 4000, "stream": True,
        }).encode("utf-8")
        started = datetime.now().timestamp()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:15721/v1/chat/completions",
                data=request_body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer PROXY_MANAGED",
                         "Accept": "text/event-stream"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            emit(delta["content"])
                        elif delta.get("reasoning_content"):
                            emit(delta["reasoning_content"], kind="think")
        except Exception as exc:
            self.audit("ask", outcome="failed", detail={"error": str(exc)[:200], "stream": True})
            return {"ok": False, "error": f"模型调用失败：{str(exc)[:120]}", "elapsed_ms": int((datetime.now().timestamp() - started) * 1000)}
        elapsed_ms = int((datetime.now().timestamp() - started) * 1000)
        self.audit("ask", outcome="success", detail={"q": question[:60], "elapsed_ms": elapsed_ms, "stream": True})
        return {"ok": True, "elapsed_ms": elapsed_ms, "context_items": used}

    def briefing_content(self) -> dict[str, Any]:
        """今日重点提示点开后的真实内容：官方发布、库内文档与授权隔离线索。"""
        with self.connect() as connection:
            official_releases = [dict(row) for row in connection.execute(
                """SELECT d.document_id,d.title,d.publisher,d.source_url,d.published_at,d.as_of_date,
                          d.evidence_tier,d.license_tag,
                          (SELECT c.text_content FROM document_chunks c
                            WHERE c.document_id=d.document_id ORDER BY c.sequence_no LIMIT 1) AS key_figure,
                          (SELECT c.locator FROM document_chunks c
                            WHERE c.document_id=d.document_id ORDER BY c.sequence_no LIMIT 1) AS locator
                   FROM documents d
                   WHERE d.document_type='official_statistics_release' AND d.status='curated'
                   ORDER BY d.published_at DESC"""
            ).fetchall()]
            library_documents = [dict(row) for row in connection.execute(
                """SELECT document_id,title,publisher,published_at,as_of_date,evidence_tier,document_type
                   FROM documents
                   WHERE COALESCE(document_type,'')<>'official_statistics_release' AND status='curated'
                   ORDER BY published_at DESC"""
            ).fetchall()]
            events = [dict(row) for row in connection.execute(
                """SELECT e.monitor_event_id,e.event_type,e.event_time,e.available_at,e.sector_code,
                          e.title,e.summary,e.materiality_score,e.source_url,e.locator,e.license_status,
                          p.sector_name
                   FROM monitor_events e LEFT JOIN research_sector_packs p ON p.sector_code=e.sector_code
                   WHERE e.status='accepted'
                   ORDER BY e.available_at DESC LIMIT 30"""
            ).fetchall()]
        news_leads = []
        lead_dir = PROJECT_ROOT / "data" / "raw" / "licensed" / "gildata"
        if lead_dir.is_dir():
            for path in sorted(lead_dir.glob("*.json")):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict) or not payload.get("event_type") or not payload.get("title"):
                    continue
                news_leads.append({
                    "title": payload.get("title"),
                    "summary": payload.get("summary"),
                    "event_time": payload.get("event_time"),
                    "sector_code": payload.get("sector_code"),
                    "materiality_score": payload.get("materiality_score"),
                    "license_status": payload.get("license_status"),
                    "publication_allowed": bool(payload.get("publication_allowed")),
                    "ingestion_target": payload.get("ingestion_target"),
                    "lead_file": path.name,
                })
        return {
            "official_releases": official_releases,
            "library_documents": library_documents,
            "news_leads": news_leads,
            "events": events,
            "truth_boundary": "官方发布与库内文档可直接引用；授权隔离线索未经核验，不得作为研究结论依据。",
        }


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "ConsumerResearchWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> WorkbenchService:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        message = format_string % args
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.client_address[0]} {message}")

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host", "").lower()
        host_name = host.rsplit(":", 1)[0] if host.count(":") <= 1 else host
        if "*" in self.app.allowed_hosts:
            return True
        return host_name in self.app.allowed_hosts

    def _write(self, status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._write(status, body, "application/json; charset=utf-8")

    def error_response(self, status: int, code: str, message: str) -> None:
        self.json_response({"error": {"code": code, "message": message}}, status)

    def serve_document_page(self, document_id: str) -> None:
        doc = self.app.document_page(document_id)
        if not doc:
            self.error_response(404, "not_found", "文档不存在")
            return
        esc = html.escape
        body = doc.get("full_text") or doc.get("excerpt")
        kind = "库内原文" if doc.get("full_text") else "库内摘录" if body else "暂无内容"
        paragraphs = "".join(f"<p>{esc(line)}</p>" for line in (body or "").splitlines() if line.strip()) or "<p>本机尚未缓存该文档内容。</p>"
        source_line = ""
        if doc.get("source_url"):
            source_line = f'<p class="src">原文地址：<a href="{esc(doc["source_url"])}">{esc(doc["source_url"])}</a>（对方网站可能拦截部分访问；本页为研究底座内缓存，不受对方限制）</p>'
        page = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(doc["title"])} · 消费行业研究底座</title>
<style>
body {{ margin: 0; background: #f7f6f4; color: #171717; font-family: "Segoe UI","Microsoft YaHei UI",sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 48px 28px 80px; background: #fff; min-height: 100vh; }}
.kind {{ display: inline-block; padding: 3px 9px; background: #176b67; color: #fff; font-size: 11px; font-weight: 700; border-radius: 3px; }}
h1 {{ font-family: Georgia,"Noto Serif SC","Songti SC",serif; font-size: 26px; line-height: 1.45; margin: 14px 0 6px; }}
.meta {{ color: #818181; font-size: 12px; margin-bottom: 18px; }}
.src {{ color: #565656; font-size: 12px; word-break: break-all; border-left: 3px solid #dedede; padding-left: 10px; }}
.src a {{ color: #176b67; }}
article {{ margin-top: 22px; border-top: 2px solid #171717; padding-top: 18px; }}
article p {{ line-height: 1.9; margin: 0 0 10px; font-size: 14.5px; }}
</style></head>
<body><main>
<span class="kind">{esc(kind)}</span>
<h1>{esc(doc["title"])}</h1>
<div class="meta">{esc(doc.get("publisher") or "")} · 发布于 {esc(str(doc.get("published_at") or "—"))} · 数据期 {esc(str(doc.get("as_of_date") or "—"))} · 证据等级 {esc(str(doc.get("evidence_tier") or "—"))}</div>
{source_line}
<article>{paragraphs}</article>
</main></body></html>"""
        self._write(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def serve_research_report_page(self, event_id: str) -> None:
        report = self.app.research_report_page(event_id)
        if not report:
            self.error_response(404, "not_found", "研报元数据不存在")
            return
        esc = html.escape
        original = report.get("original_url")
        source_line = f'<p class="src">原文入口：<a href="{esc(original)}">{esc(original)}</a></p>' if original else '<p class="src">聚源未返回公开原文网址；以下为授权研报元数据与合规摘录。</p>'
        excerpt = esc(report.get("excerpt") or report.get("summary") or "暂无摘录")
        page = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(report['title'])} · 研报元数据</title><style>
body{{margin:0;background:#f7f6f4;color:#171717;font-family:"Segoe UI","Microsoft YaHei UI",sans-serif}}main{{max-width:820px;margin:auto;padding:48px 30px 80px;background:#fff;min-height:100vh}}
.kind{{display:inline-block;padding:4px 10px;background:#8f1d22;color:#fff;font-size:12px;font-weight:700}}h1{{font-family:Georgia,"Noto Serif SC","Songti SC",serif;font-size:28px;line-height:1.45}}
.meta,.src{{color:#666;font-size:13px;line-height:1.8}}.src{{border-left:3px solid #ddd;padding-left:12px}}.src a{{color:#176b67}}article{{margin-top:24px;border-top:2px solid #171717;padding-top:20px;white-space:pre-wrap;line-height:1.9;font-size:15px}}
</style></head><body><main><span class="kind">授权研报元数据</span><h1>{esc(report['title'])}</h1>
<div class="meta">{esc(report.get('institution') or '机构未标注')} · 发布 {esc(report.get('published_at') or str(report.get('event_time') or '')[:10])} · 行业 {esc(report.get('industry') or '—')} · 评级 {esc(report.get('rating') or '—')} · 作者 {esc(report.get('authors') or '—')}</div>
{source_line}<article>{excerpt}</article></main></body></html>"""
        self._write(200, page.encode("utf-8"), "text/html; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("请求内容为空或过大")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效的JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求内容必须是对象")
        return value

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
        candidate = (self.app.static_root / relative).resolve()
        try:
            candidate.relative_to(self.app.static_root)
        except ValueError:
            self.error_response(404, "not_found", "页面不存在")
            return
        if not candidate.is_file():
            candidate = self.app.static_root / "index.html"
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.name == "index.html":
            data = data.replace(b"__WORKBENCH_TOKEN__", self.app.session_token.encode("ascii"))
            content_type = "text/html; charset=utf-8"
        elif content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._write(200, data, content_type)

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self.error_response(403, "host_denied", "当前环境不允许该主机访问")
            return
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._serve_static(parsed.path)
            return
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self.json_response({"status": "ok", "service": "consumer-research-workbench", "version": "1.3.0"})
            elif parsed.path == "/api/bootstrap":
                self.json_response(self.app.bootstrap())
            elif parsed.path == "/api/sectors":
                self.json_response({"sectors": self.app.sectors()})
            elif parsed.path == "/api/alerts":
                self.json_response({"alerts": self.app.alerts(int(query.get("limit", ["100"])[0]), query.get("state", ["open"])[0])})
            elif parsed.path == "/api/tasks":
                self.json_response(self.app.tasks(
                    query.get("q", [""])[0], query.get("sector", [""])[0], query.get("category", [""])[0],
                    query.get("favorites", ["0"])[0] == "1",
                ))
            elif parsed.path.startswith("/api/tasks/"):
                self.json_response(self.app.task_detail(unquote(parsed.path.removeprefix("/api/tasks/"))))
            elif parsed.path == "/api/jobs":
                self.json_response({"jobs": self.app.jobs()})
            elif parsed.path == "/api/reports":
                self.json_response({"reports": self.app.reports()})
            elif parsed.path.startswith("/api/reports/"):
                self.json_response(self.app.report_detail(unquote(parsed.path.removeprefix("/api/reports/"))))
            elif parsed.path == "/api/entities":
                self.json_response({"entities": self.app.entities(query.get("q", [""])[0])})
            elif parsed.path.startswith("/api/documents/") and parsed.path.endswith("/content"):
                document_id = unquote(parsed.path.removeprefix("/api/documents/").removesuffix("/content"))
                self.serve_document_page(document_id)
            elif parsed.path.startswith("/api/research-reports/") and parsed.path.endswith("/content"):
                event_id = unquote(parsed.path.removeprefix("/api/research-reports/").removesuffix("/content"))
                self.serve_research_report_page(event_id)
            elif parsed.path == "/api/data-status":
                self.json_response(self.app.data_status())
            elif parsed.path == "/api/ops-status":
                self.json_response(self.app.ops_status())
            elif parsed.path == "/api/self-calibration":
                self.json_response(self.app.self_calibration_status())
            elif parsed.path == "/api/llm/status":
                self.json_response(self.app.llm_status())
            elif parsed.path == "/api/system-llm/status":
                self.json_response(self.app.system_llm_status())
            elif parsed.path == "/api/briefing-content":
                self.json_response(self.app.briefing_content())
            elif parsed.path == "/api/morning-brief":
                self.json_response(self.app.morning_brief(query.get("date", [None])[0] or None))
            elif parsed.path == "/api/stock-focus":
                self.json_response(self.app.stock_focus(query.get("date", [None])[0] or None))
            elif parsed.path.startswith("/api/stocks/") and parsed.path.endswith("/trend"):
                security_id = unquote(parsed.path.removeprefix("/api/stocks/").removesuffix("/trend"))
                self.json_response(self.app.stock_trend(security_id, query.get("period", ["1m"])[0]))
            elif parsed.path == "/api/research-library":
                self.json_response(self.app.research_library(query.get("date", [None])[0] or None))
            elif parsed.path == "/api/sector-heatmap":
                self.json_response(self.app.sector_heatmap(query.get("period", ["day"])[0]))
            elif parsed.path == "/api/data-sources":
                self.json_response(self.app.data_sources())
            elif parsed.path == "/api/model-forecasts":
                self.json_response(self.app.model_forecasts())
            elif parsed.path == "/api/ai-fund/overview":
                self.json_response(self.app.ai_fund_overview())
            elif parsed.path == "/api/ai-fund/positions":
                self.json_response(self.app.ai_fund_positions())
            elif parsed.path == "/api/ai-fund/nav":
                self.json_response(self.app.ai_fund_nav())
            elif parsed.path == "/api/ai-fund/rebalance":
                self.json_response(self.app.ai_fund_rebalance())
            elif parsed.path == "/api/ai-fund/strategy":
                self.json_response(self.app.ai_fund_strategy())
            elif parsed.path == "/api/ai-fund/history":
                self.json_response(self.app.ai_fund_history())
            elif parsed.path == "/api/ai-fund/events":
                self.json_response(self.app.ai_fund_events())
            elif parsed.path == "/api/ai-fund/autonomy":
                self.json_response(self.app.ai_fund_autonomy())
            elif parsed.path == "/api/ai-fund/audit":
                self.json_response(self.app.ai_fund_audit())
            else:
                self.error_response(404, "not_found", "接口不存在")
        except PermissionError as exc:
            self.error_response(403, "permission_denied", str(exc))
        except LookupError as exc:
            self.error_response(404, "not_found", str(exc))
        except (ValueError, task_library.TaskLibraryValidationError) as exc:
            message = str(exc)
            if isinstance(exc, task_library.TaskLibraryValidationError):
                message = "；".join(item["message"] for item in exc.issues)
            self.error_response(400, "validation_failed", message)
        except Exception:
            traceback.print_exc()
            self.error_response(500, "internal_error", "本机研究服务发生错误，请查看诊断日志")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self.error_response(403, "host_denied", "当前环境不允许该主机访问")
            return
        if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.app.session_token):
            self.app.audit("csrf_validation", outcome="denied")
            self.error_response(403, "request_token_invalid", "页面会话已失效，请刷新页面")
            return
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/favorites":
                self.json_response(self.app.set_favorite(str(payload.get("product_id", "")), bool(payload.get("favorite", True))))
            elif parsed.path == "/api/jobs":
                self.json_response(self.app.submit_job(payload), 201)
            elif parsed.path == "/api/ask":
                self.json_response(self.app.ask(payload))
            elif parsed.path == "/api/llm/test":
                self.json_response(self.app.test_llm_config(payload))
            elif parsed.path == "/api/llm/enhance":
                self.json_response(self.app.enhance_with_llm(payload))
            elif parsed.path == "/api/ask/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                def emit(text: str, kind: str = "text") -> None:
                    self.wfile.write(f"data: {json.dumps({'text': text, 'kind': kind}, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                try:
                    result = self.app.ask_stream(payload, emit)
                    self.wfile.write(f"data: {json.dumps({'done': True, **result}, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    self.close_connection = True
                except Exception:
                    traceback.print_exc()
                    try:
                        self.wfile.write(f"data: {json.dumps({'error': 'internal_error'}, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except Exception:
                        pass
                    self.close_connection = True
            elif parsed.path.startswith("/api/alerts/") and parsed.path.endswith("/acknowledge"):
                alert_id = unquote(parsed.path.removeprefix("/api/alerts/").removesuffix("/acknowledge"))
                self.json_response(self.app.acknowledge_alert(alert_id))
            elif parsed.path.startswith("/api/reports/") and parsed.path.endswith("/annotations"):
                run_id = unquote(parsed.path.removeprefix("/api/reports/").removesuffix("/annotations"))
                self.json_response(self.app.annotate_report(run_id, payload), 201)
            else:
                self.error_response(404, "not_found", "接口不存在")
        except PermissionError as exc:
            self.error_response(403, "permission_denied", str(exc))
        except LookupError as exc:
            self.error_response(404, "not_found", str(exc))
        except (ValueError, task_library.TaskLibraryValidationError) as exc:
            message = str(exc)
            if isinstance(exc, task_library.TaskLibraryValidationError):
                message = "；".join(item["message"] for item in exc.issues)
            self.error_response(400, "validation_failed", message)
        except Exception:
            traceback.print_exc()
            self.error_response(500, "internal_error", "本机研究服务发生错误，请查看诊断日志")


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: WorkbenchService):
        self.app = app
        super().__init__(address, WorkbenchHandler)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="消费行业研究工作台本机服务")
    default_db = (
        Path(sys.executable).parent / "data" / "curated" / "consumer-research.db"
        if getattr(sys, "frozen", False)
        else PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
    )
    default_data = (
        Path(sys.executable).parent / "data" / "workbench" / "module5-fund-manager"
        if getattr(sys, "frozen", False)
        else PROJECT_ROOT / "data" / "workbench" / "module5-fund-manager"
    )
    root.add_argument("--host", default=os.environ.get("CONSUMER_RESEARCH_HOST", "127.0.0.1"), choices=["127.0.0.1", "localhost", "0.0.0.0"])
    root.add_argument("--port", type=int, default=8765)
    root.add_argument("--db", type=Path, default=default_db)
    root.add_argument("--data-root", type=Path, default=default_data)
    root.add_argument("--user-name", default=os.environ.get("CONSUMER_RESEARCH_USER", "基金经理"))
    root.add_argument("--role", choices=sorted(ALLOWED_ROLES), default=os.environ.get("CONSUMER_RESEARCH_ROLE", "public_fund_manager"))
    root.add_argument("--deployment-mode", choices=["local_single_user", "internal_network"], default=os.environ.get("CONSUMER_RESEARCH_DEPLOYMENT_MODE", "local_single_user"))
    root.add_argument("--allow-hosts", default=os.environ.get("CONSUMER_RESEARCH_ALLOW_HOSTS", ""), help="逗号分隔白名单，\"*\" 表示全放行")
    root.add_argument("--open", action="store_true", default=bool(getattr(sys, "frozen", False)))
    root.add_argument("--no-open", action="store_false", dest="open")
    return root


def existing_workbench(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "api/health", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and value.get("service") == "consumer-research-workbench"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False


def main() -> int:
    args = parser().parse_args()
    if not args.user_name.strip() or args.user_name.strip().lower() in {"anonymous", "agent", "ai", "unassigned"}:
        print("必须配置具名内部用户", file=sys.stderr)
        return 2
    static_root = BUNDLE_ROOT / "public" if getattr(sys, "frozen", False) else APP_DIR / "public"
    args.data_root.mkdir(parents=True, exist_ok=True)
    allow_hosts = normalize_allowed_hosts(args.allow_hosts)
    if args.host in {"127.0.0.1", "localhost"} and not args.allow_hosts:
        allow_hosts = set(LOCAL_ALLOWED_HOSTS)
    elif args.host == "0.0.0.0" and not args.allow_hosts:
        # 默认不允许公网，避免不经意暴露；由部署者显式指定 --allow-hosts=* 或域名白名单放行
        allow_hosts = {"*"} if args.deployment_mode == "internal_network" else set(LOCAL_ALLOWED_HOSTS)
    if args.deployment_mode == "internal_network":
        client_scope = "public_access"
    else:
        client_scope = "loopback_only"
    display_host = "localhost" if args.host == "0.0.0.0" else args.host
    app = WorkbenchService(
        args.db,
        args.data_root,
        Identity(args.user_name.strip(), args.role),
        static_root,
        bound_host=args.host,
        deployment_mode=args.deployment_mode,
        client_scope=client_scope,
        allowed_hosts=allow_hosts,
    )
    url = f"http://{display_host}:{args.port}/"
    # 单实例防护：已有健康实例则直接复用，避免多进程共绑同一端口
    if existing_workbench(url):
        print(f"研究工作台已在运行：{url}")
        if args.open:
            webbrowser.open(url)
        return 0
    try:
        server = WorkbenchHTTPServer((args.host, args.port), app)
    except OSError as exc:
        if existing_workbench(url):
            if args.open:
                webbrowser.open(url)
            return 0
        print(f"无法启动研究工作台：{exc}", file=sys.stderr)
        return 3
    print(f"消费行业研究工作台已启动：{url}")
    print(f"当前用户：{app.identity.name}（{app.identity.role_label}）")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        with app.connect() as connection:
            connection.execute(
                "UPDATE workbench_sessions SET status='closed',ended_at=? WHERE session_id=?",
                (utc_now(), app.session_id),
            )
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
