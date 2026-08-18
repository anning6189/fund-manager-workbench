from __future__ import annotations

import http.client
import importlib.util
import json
import shutil
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "apps" / "fund-manager-workbench" / "server.py"
REPORT_PATH = PROJECT_ROOT / "tests" / "module-5-fund-manager-workbench-acceptance.v1.json"


def load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location("fund_manager_workbench_server", SERVER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError("Cannot load workbench server")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Acceptance:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def check(self, check_id: str, condition: bool, detail: Any = None) -> None:
        self.results.append({"check_id": check_id, "status": "passed" if condition else "failed", "detail": detail})

    def finish(self) -> dict[str, Any]:
        passed = sum(item["status"] == "passed" for item in self.results)
        return {
            "module": "5. 基金经理使用界面",
            "version": "1.0.0",
            "status": "passed" if passed == len(self.results) else "failed",
            "passed": passed,
            "total": len(self.results),
            "checks": self.results,
        }


def request(base: str, path: str, method: str = "GET", token: str | None = None,
            payload: dict[str, Any] | None = None) -> tuple[int, Any, dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["X-Workbench-Token"] = token
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            value = json.loads(raw.decode("utf-8")) if "json" in content_type else raw.decode("utf-8")
            return response.status, value, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        value = json.loads(raw.decode("utf-8")) if raw else {}
        return exc.code, value, dict(exc.headers.items())


def main() -> int:
    module = load_server_module()
    acceptance = Acceptance()
    with tempfile.TemporaryDirectory(prefix="consumer-module5-") as directory:
        temp_root = Path(directory)
        temp_db = temp_root / "consumer-research.db"
        shutil.copy2(PROJECT_ROOT / "data" / "curated" / "consumer-research.db", temp_db)
        identity = module.Identity("模块五验收基金经理", "public_fund_manager")
        service = module.WorkbenchService(
            temp_db,
            temp_root / "workbench-data",
            identity,
            PROJECT_ROOT / "apps" / "fund-manager-workbench" / "public",
        )
        server = module.WorkbenchHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        token = service.session_token
        try:
            status, health, headers = request(base, "/api/health")
            acceptance.check("M5-001-health-endpoint", status == 200 and health["status"] == "ok", health)
            acceptance.check("M5-002-security-headers", headers.get("X-Frame-Options") == "DENY" and "default-src 'self'" in headers.get("Content-Security-Policy", ""), headers)

            status, html, headers = request(base, "/")
            acceptance.check("M5-003-web-interface", status == 200 and "消费行业晨报" in html, status)
            acceptance.check("M5-004-token-injected", "__WORKBENCH_TOKEN__" not in html and token in html, None)
            acceptance.check("M5-005-no-store", headers.get("Cache-Control") == "no-store", headers.get("Cache-Control"))

            _, bootstrap, _ = request(base, "/api/bootstrap")
            acceptance.check("M5-006-named-user", bootstrap["identity"]["name"] == identity.name, bootstrap["identity"])
            acceptance.check("M5-007-fund-manager-role", bootstrap["identity"]["role"] == "public_fund_manager", bootstrap["identity"])
            acceptance.check("M5-008-default-yesterday-cutoff", bootstrap["cutoff"]["date"] == module.default_cutoff_date(), bootstrap["cutoff"])
            acceptance.check("M5-009-eleven-sectors-summary", bootstrap["counts"]["sectors"] == 11, bootstrap["counts"])
            acceptance.check("M5-010-110-task-products-summary", bootstrap["counts"]["products"] == 110, bootstrap["counts"])
            acceptance.check("M5-011-truth-boundary", "缺失" in bootstrap["truth_boundary"], bootstrap["truth_boundary"])
            acceptance.check("M5-012-no-holdings-boundary", any("持仓" in item for item in bootstrap["prohibitions"]), bootstrap["prohibitions"])
            acceptance.check("M5-013-no-auto-trading-boundary", any("交易" in item for item in bootstrap["prohibitions"]), bootstrap["prohibitions"])
            acceptance.check("M5-014-no-auto-publication-boundary", any("发布" in item for item in bootstrap["prohibitions"]), bootstrap["prohibitions"])

            _, sectors, _ = request(base, "/api/sectors")
            acceptance.check("M5-015-eleven-sector-map", len(sectors["sectors"]) == 11, len(sectors["sectors"]))
            acceptance.check("M5-016-sector-research-thesis", all(item["research_thesis"] for item in sectors["sectors"]), None)
            acceptance.check("M5-017-sector-market-coverage", all("a_share_count" in item and "hk_share_count" in item for item in sectors["sectors"]), None)
            acceptance.check("M5-018-sector-data-stream-state", all("populated_streams" in item and "required_streams" in item for item in sectors["sectors"]), None)

            _, tasks, _ = request(base, "/api/tasks")
            acceptance.check("M5-019-110-task-products", tasks["result_count"] == 110, tasks["result_count"])
            _, industry_tasks, _ = request(base, "/api/tasks?category=industry")
            acceptance.check("M5-020-category-filter", industry_tasks["result_count"] == 22, industry_tasks["result_count"])
            _, sector_tasks, _ = request(base, "/api/tasks?sector=CR.S.FB")
            acceptance.check("M5-021-sector-filter", sector_tasks["result_count"] == 10, sector_tasks["result_count"])
            _, search_tasks, _ = request(base, "/api/tasks?q=" + urllib.parse.quote("景气"))
            acceptance.check("M5-022-search", search_tasks["result_count"] > 0, search_tasks["result_count"])

            product = sector_tasks["results"][0]
            _, detail, _ = request(base, "/api/tasks/" + urllib.parse.quote(product["product_id"], safe=""))
            acceptance.check("M5-023-product-detail", detail["product"]["product_id"] == product["product_id"], detail["product"]["product_id"])
            acceptance.check("M5-024-parameter-schema", "cutoff_timestamp" in detail["product"]["parameter_schema"]["required"], detail["product"]["parameter_schema"])
            acceptance.check("M5-025-data-readiness-visible", detail["product"]["data_readiness"] == "ready_with_data_gaps", detail["product"]["data_readiness"])

            status, error, _ = request(base, "/api/favorites", "POST", None, {"product_id": product["product_id"], "favorite": True})
            acceptance.check("M5-026-write-token-required", status == 403 and error["error"]["code"] == "request_token_invalid", error)
            status, favorite, _ = request(base, "/api/favorites", "POST", token, {"product_id": product["product_id"], "favorite": True})
            acceptance.check("M5-027-favorite", status == 200 and favorite["favorite"], favorite)
            _, favorites, _ = request(base, "/api/tasks?favorites=1")
            acceptance.check("M5-028-favorite-persisted", favorites["result_count"] == 1, favorites["result_count"])
            status, unfavorite, _ = request(base, "/api/favorites", "POST", token, {"product_id": product["product_id"], "favorite": False})
            acceptance.check("M5-029-unfavorite", status == 200 and not unfavorite["favorite"], unfavorite)

            job_payload = {
                "product_id": product["product_id"],
                "research_question": "截至昨天，该行业景气方向、驱动和风险发生了哪些变化？",
                "cutoff_date": module.default_cutoff_date(),
                "markets": ["A_SHARE"],
                "priority": "normal",
                "execute_now": False,
            }
            status, submitted, _ = request(base, "/api/jobs", "POST", token, job_payload)
            acceptance.check("M5-030-submit-job", status == 201 and submitted["job"]["status"] == "queued", submitted)
            acceptance.check("M5-031-named-submitter", submitted["job"]["submitted_by"] == identity.name, submitted["job"])
            acceptance.check("M5-032-cutoff-normalized", submitted["job"]["cutoff_timestamp"].endswith("Z"), submitted["job"]["cutoff_timestamp"])
            _, jobs, _ = request(base, "/api/jobs")
            acceptance.check("M5-033-own-job-visible", any(item["job_id"] == submitted["job"]["job_id"] for item in jobs["jobs"]), len(jobs["jobs"]))

            future_payload = dict(job_payload, cutoff_date=(module.datetime.now(module.SHANGHAI).date()).isoformat())
            status, future_error, _ = request(base, "/api/jobs", "POST", token, future_payload)
            acceptance.check("M5-034-future-cutoff-rejected", status == 400 and "昨天" in future_error["error"]["message"], future_error)
            holdings_payload = dict(job_payload, fund_holdings=[{"security": "TEST", "weight": 1.0}])
            status, holdings_error, _ = request(base, "/api/jobs", "POST", token, holdings_payload)
            acceptance.check("M5-035-holdings-rejected", status == 400 and "持仓" in holdings_error["error"]["message"], holdings_error)

            _, alerts, _ = request(base, "/api/alerts?limit=10")
            acceptance.check("M5-036-alerts-visible", len(alerts["alerts"]) > 0, len(alerts["alerts"]))
            acceptance.check("M5-036A-alert-source-detail", all("source" in item and "event" in item for item in alerts["alerts"]), None)
            acceptance.check("M5-036B-public-source-link", any(item.get("source") and item["source"].get("source_url") for item in alerts["alerts"]), None)
            alert_id = alerts["alerts"][0]["alert_id"]
            status, acknowledged, _ = request(base, "/api/alerts/" + urllib.parse.quote(alert_id, safe="") + "/acknowledge", "POST", token, {})
            acceptance.check("M5-037-alert-acknowledgement", status == 200 and acknowledged["acknowledged_by"] == identity.name, acknowledged)

            _, reports, _ = request(base, "/api/reports")
            acceptance.check("M5-038-report-list", len(reports["reports"]) >= 3, len(reports["reports"]))
            run_id = reports["reports"][0]["run_id"]
            _, report, _ = request(base, "/api/reports/" + urllib.parse.quote(run_id, safe=""))
            acceptance.check("M5-039-report-body", bool(report["report_markdown"]), len(report["report_markdown"] or ""))
            acceptance.check("M5-040-claim-graph", len(report["claims"]) > 0, len(report["claims"]))
            acceptance.check("M5-041-claim-evidence-trace", any(claim["evidence"] for claim in report["claims"]), None)
            acceptance.check("M5-042-counter-evidence", any(ev["relation_type"] == "counter" for claim in report["claims"] for ev in claim["evidence"]), None)
            status, annotation, _ = request(base, "/api/reports/" + urllib.parse.quote(run_id, safe="") + "/annotations", "POST", token, {"note": "验收批注：需要复核证据定位。"})
            acceptance.check("M5-043-named-annotation", status == 201 and annotation["author"] == identity.name, annotation)
            _, annotated, _ = request(base, "/api/reports/" + urllib.parse.quote(run_id, safe=""))
            acceptance.check("M5-044-annotation-persisted", any(item["annotation_id"] == annotation["annotation_id"] for item in annotated["annotations"]), len(annotated["annotations"]))
            status, approval_error, _ = request(base, "/api/reports/" + urllib.parse.quote(run_id, safe="") + "/approve", "POST", token, {"notes": "removed"})
            acceptance.check("M5-045-human-review-endpoint-removed", status == 404, approval_error)

            _, data_status, _ = request(base, "/api/data-status")
            acceptance.check("M5-046-22-coverage-cells", len(data_status["coverage"]) == 22, len(data_status["coverage"]))
            acceptance.check("M5-047-license-gates-visible", any((item["decision"] or "pending") == "pending" for item in data_status["licenses"]), None)
            acceptance.check("M5-048-freshness-visible", isinstance(data_status["freshness"], list), len(data_status["freshness"]))
            acceptance.check("M5-049-snapshot-status-visible", isinstance(data_status["snapshot_counts"], dict), data_status["snapshot_counts"])

            connection = sqlite3.connect(temp_db)
            audit_count = connection.execute("SELECT COUNT(*) FROM workbench_audit_events WHERE actor=?", (identity.name,)).fetchone()[0]
            session = connection.execute("SELECT client_scope,status FROM workbench_sessions WHERE session_id=?", (service.session_id,)).fetchone()
            indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='index'").fetchall()}
            connection.close()
            acceptance.check("M5-050-audit-log", audit_count >= 5, audit_count)
            acceptance.check("M5-051-loopback-session", session == ("loopback_only", "active"), session)
            acceptance.check("M5-052-sqlite-query-indexes", {"idx_workbench_audit_actor_time", "idx_workbench_annotations_run_status", "idx_task_library_jobs_submitter_status_time"}.issubset(indexes), sorted(indexes))

            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            connection.request("GET", "/api/health", headers={"Host": "example.com"})
            denied = connection.getresponse()
            denied_body = json.loads(denied.read().decode("utf-8"))
            connection.close()
            acceptance.check("M5-053-host-header-defense", denied.status == 403 and denied_body["error"]["code"] == "host_denied", denied_body)

            connection = sqlite3.connect(temp_db)
            review_count = connection.execute("SELECT COUNT(*) FROM workflow_reviews WHERE run_id=?", (run_id,)).fetchone()[0]
            direct_ready = connection.execute("SELECT COUNT(*) FROM workflow_runs WHERE run_id=? AND status='completed' AND publication_status='internal_research_ready' AND human_review_required=0", (run_id,)).fetchone()[0]
            connection.close()
            acceptance.check("M5-054-no-human-review-records", review_count == 0, review_count)
            acceptance.check("M5-055-report-directly-visible", direct_ready == 1, direct_ready)
            script = (PROJECT_ROOT / "apps" / "fund-manager-workbench" / "public" / "app.js").read_text(encoding="utf-8")
            acceptance.check("M5-056-homepage-morning-brief", "今日消费行业简讯" in script and "国内宏观" in script and "消费行业" in script, None)
            acceptance.check("M5-057-six-to-eight-priority-items", "output.slice(0, 8)" in script and "今日重点提示" in script, None)
            acceptance.check("M5-058-daily-report-deeplink", "open-daily-report" in script and "今日消费行业突发与重点事件行研报告" in script, None)
            acceptance.check(
                "M5-059-sector-and-task-pages-removed",
                '["sectors", "行业地图"]' not in script
                and '["tasks", "研究任务"]' not in script
                and 'state.view === "sectors"' not in script
                and 'state.view === "tasks"' not in script,
                None,
            )
            acceptance.check(
                "M5-060-brief-detail-interaction",
                'data-brief-index=' in script
                and 'openBriefDetail' in script
                and '涉及的数据源' in script
                and '打开来源主页' in script,
                None,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    report = acceptance.finish()
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "passed": report["passed"], "total": report["total"], "report": str(REPORT_PATH)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
