#!/usr/bin/env python3
"""Full-consumer research coverage and task-package engine.

The engine makes a strict distinction between research definitions, a staged
licensed security universe, and populated production metrics.  It never turns
definitions or license-gated raw data into a false "data populated" claim.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import consumer_data_production as production
import consumer_knowledge_store as knowledge


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
SPEC_PATH = PROJECT_ROOT / "specs" / "coverage" / "full-consumer-research-coverage.v1.json"
METRIC_PATH = PROJECT_ROOT / "specs" / "consumer-metric-dictionary.v1.json"
DOMAIN_PATH = PROJECT_ROOT / "specs" / "consumer-domain-model.v1.json"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "005_full_consumer_research_coverage.sql"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "coverage" / "module2-full-consumer"
ENGINE_VERSION = "1.0.0"

MARKET_BY_MIC = {"XSHG": "A_SHARE", "XSHE": "A_SHARE", "XBSE": "A_SHARE", "XHKG": "HK_SHARE"}
QUESTION_BY_TEMPLATE = {
    "P0_01_INDUSTRY_PANORAMA": "{sector}当前产业链、规模、格局、景气与估值处于什么位置？",
    "P0_02_PROSPERITY_TRACKING": "{sector}截至研究截止时点的景气方向、拐点信号与证据是什么？",
    "P0_03_COMPANY_DEEP_DIVE": "{sector}代表公司的增长驱动、竞争壁垒、盈利质量和风险是什么？",
    "P0_04_COMPANY_COMPARISON": "{sector}可比公司的经营、财务、估值和预期差如何横向比较？",
    "P0_05_EARNINGS_REVIEW": "{sector}最新财报反映了哪些超预期、低预期和趋势变化？",
    "P0_06_EVENT_POLICY_IMPACT": "最新事件或政策如何沿产业链传导并影响{sector}？",
    "P0_07_COMPETITION_LANDSCAPE": "{sector}竞争格局、份额、进入壁垒和竞争行为如何变化？",
    "P0_08_MARKET_SIZE": "{sector}市场规模、量价拆分、渗透率和中期空间是多少？",
    "P0_09_VALUATION_EXPECTATIONS": "{sector}当前估值隐含了什么增长和盈利预期？",
    "P0_10_RISK_CATALYST_MONITOR": "{sector}未来需要监测的催化剂、风险和证伪信号是什么？",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def spec() -> dict[str, Any]:
    return read_json(SPEC_PATH)


def metric_contract() -> tuple[list[str], dict[str, list[str]]]:
    metrics = read_json(METRIC_PATH)
    common = [item["metric_id"] for item in metrics["common_metrics"]]
    sectors = {
        pack["sector_code"]: [item["metric_id"] for item in pack["metrics"]]
        for pack in metrics["sector_metric_packs"]
    }
    return common, sectors


def domain_index() -> tuple[dict[str, str], dict[str, str], list[str]]:
    model = read_json(DOMAIN_PATH)
    names: dict[str, str] = {}
    parents: dict[str, str] = {}
    leaves: list[str] = []

    def visit(node: dict[str, Any], parent: str | None = None) -> None:
        names[node["code"]] = node["name"]
        if parent:
            parents[node["code"]] = parent
        children = node.get("children", [])
        if not children:
            leaves.append(node["code"])
        for child in children:
            visit(child, node["code"])

    for root in model["taxonomy"]:
        visit(root)
    return names, parents, leaves


def init_coverage(db_path: Path) -> dict[str, Any]:
    production.init_production(db_path)
    coverage_spec = spec()
    names, parents, leaves = domain_index()
    common_metrics, sector_metrics = metric_contract()
    now = knowledge.utc_now()
    mappings = 0
    with knowledge.connect(db_path) as connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for sector_code, contract in coverage_spec["sector_research_contracts"].items():
            connection.execute(
                """INSERT OR REPLACE INTO research_sector_packs(
                       sector_code,sector_name,parent_domain,research_thesis,cycle_drivers_json,
                       value_chain_json,metric_ids_json,required_streams_json,template_ids_json,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,'ready',?)""",
                (
                    sector_code, names[sector_code], parents[sector_code], contract["thesis"],
                    canonical(contract["cycle_drivers"]), canonical(contract["value_chain"]),
                    canonical(common_metrics + sector_metrics[sector_code]),
                    canonical(coverage_spec["required_streams"]),
                    canonical(coverage_spec["required_templates"]), now,
                ),
            )
            for position, rule in enumerate(contract["vendor_rules"]):
                mapping_id = production.stable_id("map", sector_code, str(position), canonical(rule))
                specificity = len(set(rule) & {"l1", "l2", "l3", "l2_in", "l3_in"})
                confidence = 0.70 if "security_name_regex" in rule else min(0.99, 0.84 + specificity * 0.05)
                connection.execute(
                    """INSERT OR REPLACE INTO taxonomy_vendor_mappings(
                           mapping_id,vendor_scheme,vendor_l1,vendor_l2,vendor_l3,sector_code,
                           mapping_type,confidence,effective_from,review_status,rule_json
                       ) VALUES(?,?,?,?,?,?,'rule',?,?,'active',?)""",
                    (
                        mapping_id, "GILDATA_SW", rule.get("l1"), rule.get("l2"), rule.get("l3"),
                        sector_code, confidence, "2026-08-12", canonical(rule),
                    ),
                )
                mappings += 1
    return {
        "status": "ready", "engine_version": ENGINE_VERSION, "database": str(db_path),
        "sector_packs": len(coverage_spec["sector_research_contracts"]),
        "leaf_nodes": len(leaves), "common_metrics": len(common_metrics),
        "sector_metrics": sum(map(len, sector_metrics.values())), "metric_definitions": len(common_metrics) + sum(map(len, sector_metrics.values())),
        "vendor_mapping_rules": mappings, "research_templates": len(coverage_spec["required_templates"]),
    }


def rule_matches(security: dict[str, Any], rule: dict[str, Any]) -> bool:
    fields = {
        "l1": security.get("vendor_industry_l1"),
        "l2": security.get("vendor_industry_l2"),
        "l3": security.get("vendor_industry_l3"),
    }
    for key in ("l1", "l2", "l3"):
        if key in rule and fields[key] != rule[key]:
            return False
    for key in ("l2_in", "l3_in"):
        if key in rule and fields[key[:2]] not in rule[key]:
            return False
    pattern = rule.get("security_name_regex")
    if pattern and not re.search(pattern, security.get("security_name", ""), flags=re.IGNORECASE):
        return False
    return True


def security_mappings(security: dict[str, Any], coverage_spec: dict[str, Any]) -> list[tuple[str, float]]:
    matches: dict[str, float] = {}
    for sector_code, contract in coverage_spec["sector_research_contracts"].items():
        for rule in contract["vendor_rules"]:
            if rule_matches(security, rule):
                specificity = len(set(rule) & {"l1", "l2", "l3", "l2_in", "l3_in"})
                confidence = 0.70 if "security_name_regex" in rule else min(0.99, 0.84 + specificity * 0.05)
                matches[sector_code] = max(matches.get(sector_code, 0), confidence)
    if not matches:
        return []

    # 研究池采用“一个证券、一个主板块”。名称只用于解决宽口径行业规则的冲突，
    # 不再让同一证券同时进入多个板块并在热力图中重复计数。
    name = security.get("security_name", "")
    overrides = (
        (r"酒店|宾馆|旅馆|旅游|景区|中免", "CR.V.TL"),
        (r"餐饮|咖啡|茶饮|酒家|饭店", "CR.V.FS"),
        (r"宠物|孩子王|爱婴|泡泡玛特|布鲁可|创源", "CR.S.PT"),
        (r"食品|饮料|酒业|乳业|调味", "CR.S.FB"),
    )
    for pattern, sector_code in overrides:
        if sector_code in matches and re.search(pattern, name, flags=re.IGNORECASE):
            return [(sector_code, max(matches[sector_code], 0.95))]

    # 更具体的供应商行业规则具有更高置信度；置信度相同时使用固定顺序保证幂等。
    priority = {code: index for index, code in enumerate((
        "CR.S.PT", "CR.S.PH", "CR.S.FB", "CR.D.AP", "CR.D.AU", "CR.D.HL",
        "CR.D.AF", "CR.V.TL", "CR.V.FS", "CR.V.RT", "CR.V.CE",
    ))}
    chosen = min(matches.items(), key=lambda item: (-item[1], priority.get(item[0], 999), item[0]))
    return [chosen]


def build_universe(db_path: Path, snapshot_paths: list[Path], as_of_date: str,
                   output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    init_coverage(db_path)
    coverage_spec = spec()
    if not snapshot_paths:
        raise ValueError("At least one source snapshot is required")
    snapshots: list[dict[str, Any]] = []
    merged: dict[str, dict[str, Any]] = {}
    source_record_count = 0
    for path in snapshot_paths:
        snapshot = read_json(path)
        problems = production.validate_snapshot(db_path, snapshot)
        if problems:
            raise production.ProductionValidationError(problems)
        if snapshot["stream_name"] != "master_data":
            raise ValueError(f"Universe source must be master_data: {path}")
        if snapshot["provider_as_of_date"] > as_of_date:
            raise ValueError(f"Source snapshot is later than universe as_of_date: {path}")
        snapshots.append(snapshot)
        source_record_count += snapshot["projected_count"]
        for security in snapshot["securities"]:
            merged[security["security_id"]] = dict(security)

    source_ids = sorted(item["snapshot_id"] for item in snapshots)
    content = {
        "source_snapshot_ids": source_ids, "as_of_date": as_of_date,
        "securities": [merged[key] for key in sorted(merged)], "engine": ENGINE_VERSION,
    }
    content_hash = knowledge.sha256_json(content)
    universe_id = production.stable_id("universe", as_of_date, content_hash)
    aggregate_snapshot = {
        "snapshot_id": f"CR.COVERAGE.GILDATA.A_SHARE.FULL_CONSUMER.{as_of_date.replace('-', '')}",
        "source_id": snapshots[0]["source_id"],
        "stream_name": "master_data",
        "market": "A_SHARE",
        "provider_as_of_date": as_of_date,
        "retrieved_at": max(item["retrieved_at"] for item in snapshots),
        "license_status": "contract_and_redistribution_terms_pending_verification",
        "ingestion_target": "raw_license_gate",
        "publication_allowed": False,
        "requested_fields": sorted({key for security in merged.values() for key in security}),
        "source_reported_count": source_record_count,
        "projected_count": len(merged),
        "over_retrieval_fields_discarded": sorted({
            field for item in snapshots for field in item.get("over_retrieval_fields_discarded", [])
        }),
        "no_fund_holdings_or_positions": True,
        "provenance_source_snapshot_ids": source_ids,
        "securities": [merged[key] for key in sorted(merged)],
    }
    aggregate_path = output_root / universe_id.replace(":", "-") / "full-consumer-production-snapshot.json"
    write_json(aggregate_path, aggregate_snapshot)
    production_registration = production.register_snapshot(db_path, aggregate_path)
    pending_license = any(item["ingestion_target"] == "raw_license_gate" for item in snapshots)
    universe_status = "staged_license_gate" if pending_license else "ready"
    sector_security_ids: dict[str, set[str]] = defaultdict(set)
    unmapped_breakdown: Counter[str] = Counter()
    mapped_security_ids: set[str] = set()
    membership_rows: list[tuple[Any, ...]] = []
    multi_mapped_count = 0
    for security_id in sorted(merged):
        security = merged[security_id]
        mappings = security_mappings(security, coverage_spec)
        if len(mappings) > 1:
            multi_mapped_count += 1
        if mappings:
            mapped_security_ids.add(security_id)
            for sector_code, confidence in mappings:
                sector_security_ids[sector_code].add(security_id)
                membership_rows.append((
                    production.stable_id("member", universe_id, security_id, sector_code), universe_id,
                    security_id, security["security_code"], security["security_name"], security["market_mic"],
                    security["trading_status"], security.get("vendor_industry_l1"), security.get("vendor_industry_l2"),
                    security.get("vendor_industry_l3"), sector_code, "mapped", confidence,
                ))
        else:
            unmapped_breakdown[f"{security.get('vendor_industry_l1')} / {security.get('vendor_industry_l2')}"] += 1
            membership_rows.append((
                production.stable_id("member", universe_id, security_id, "UNMAPPED"), universe_id,
                security_id, security["security_code"], security["security_name"], security["market_mic"],
                security["trading_status"], security.get("vendor_industry_l1"), security.get("vendor_industry_l2"),
                security.get("vendor_industry_l3"), None, "review_required", 0.0,
            ))

    now = knowledge.utc_now()
    review_count = len(merged) - len(mapped_security_ids)
    with knowledge.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO research_universe_snapshots(
                   universe_snapshot_id,source_snapshot_ids_json,as_of_date,market,security_count,
                   mapped_security_count,review_required_count,content_hash,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(universe_snapshot_id) DO UPDATE SET
                   source_snapshot_ids_json=excluded.source_snapshot_ids_json,
                   security_count=excluded.security_count,mapped_security_count=excluded.mapped_security_count,
                   review_required_count=excluded.review_required_count,status=excluded.status""",
            (
                universe_id, canonical(source_ids), as_of_date, "MULTI", len(merged), len(mapped_security_ids),
                review_count, content_hash, universe_status, now,
            ),
        )
        connection.execute("DELETE FROM research_universe_members WHERE universe_snapshot_id=?", (universe_id,))
        connection.executemany(
            """INSERT INTO research_universe_members(
                   membership_id,universe_snapshot_id,security_id,security_code,security_name,market_mic,
                   trading_status,vendor_industry_l1,vendor_industry_l2,vendor_industry_l3,
                   sector_code,mapping_status,mapping_confidence
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            membership_rows,
        )

    result = {
        "universe_snapshot_id": universe_id, "as_of_date": as_of_date,
        "source_snapshot_ids": source_ids, "source_record_count": source_record_count,
        "unique_security_count": len(merged), "mapped_security_count": len(mapped_security_ids),
        "review_required_count": review_count, "membership_count": len(membership_rows),
        "multi_mapped_security_count": multi_mapped_count, "status": universe_status,
        "production_snapshot_id": production_registration["snapshot_id"],
        "production_snapshot_path": str(aggregate_path),
        "sector_security_counts": {code: len(ids) for code, ids in sorted(sector_security_ids.items())},
        "unmapped_breakdown": dict(unmapped_breakdown.most_common()),
        "truth_boundary": "The universe is research-mapped but remains non-publishable while source licence review is pending.",
    }
    path = output_root / universe_id.replace(":", "-") / "research-universe-summary.json"
    write_json(path, result)
    result["artifact_path"] = str(path)
    return result


def populated_streams(connection: Any, sector_code: str, market: str, required: list[str]) -> list[str]:
    populated: list[str] = []
    for stream in required:
        row = connection.execute(
            """SELECT COALESCE(SUM(record_count),0) AS records
               FROM production_coverage_watermarks
               WHERE stream_name=? AND market IN (?, 'ALL') AND sector_code IN (?, 'ALL')
                 AND completeness_status IN ('complete','completed','full')""",
            (stream, market, sector_code),
        ).fetchone()
        if row["records"] > 0:
            populated.append(stream)
    return populated


def build_coverage_matrix(db_path: Path, universe_snapshot_id: str, as_of_date: str,
                          output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    initialized = init_coverage(db_path)
    coverage_spec = spec()
    common_metrics, sector_metrics = metric_contract()
    names, _, _ = domain_index()
    required = coverage_spec["required_streams"]
    matrix: list[dict[str, Any]] = []
    with knowledge.connect(db_path) as connection:
        universe = connection.execute(
            "SELECT * FROM research_universe_snapshots WHERE universe_snapshot_id=?", (universe_snapshot_id,)
        ).fetchone()
        if not universe:
            raise ValueError(f"Unknown universe snapshot: {universe_snapshot_id}")
        for sector_code in coverage_spec["sector_research_contracts"]:
            for market in ("A_SHARE", "HK_SHARE"):
                mics = [mic for mic, mapped in MARKET_BY_MIC.items() if mapped == market]
                placeholders = ",".join("?" for _ in mics)
                security_count = connection.execute(
                    f"""SELECT COUNT(DISTINCT security_id) AS count FROM research_universe_members
                         WHERE universe_snapshot_id=? AND sector_code=? AND market_mic IN ({placeholders})""",
                    (universe_snapshot_id, sector_code, *mics),
                ).fetchone()["count"]
                populated = populated_streams(connection, sector_code, market, required)
                missing = [stream for stream in required if stream not in populated]
                blockers: list[dict[str, Any]] = []
                if security_count == 0:
                    blockers.append({"code": "security_universe_not_populated", "market": market})
                if universe["status"] == "staged_license_gate" and security_count:
                    blockers.append({"code": "source_license_pending", "scope": "security_universe"})
                if missing:
                    blockers.append({"code": "required_streams_not_populated", "streams": missing})
                metric_status = "populated" if len(populated) == len(required) else ("partially_populated" if populated else "definitions_only")
                universe_status = universe["status"] if security_count else "not_populated"
                record = {
                    "sector_code": sector_code, "sector_name": names[sector_code], "market": market,
                    "security_count": security_count,
                    "metric_definition_count": len(common_metrics) + len(sector_metrics[sector_code]),
                    "required_stream_count": len(required), "populated_stream_count": len(populated),
                    "populated_streams": populated, "missing_streams": missing,
                    "metric_population_status": metric_status, "universe_status": universe_status,
                    "research_pack_status": "ready", "blockers": blockers, "as_of_date": as_of_date,
                }
                matrix.append(record)
                connection.execute(
                    """INSERT OR REPLACE INTO research_coverage_status(
                           sector_code,market,security_count,metric_definition_count,required_stream_count,
                           populated_stream_count,metric_population_status,universe_status,research_pack_status,
                           blockers_json,as_of_date,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sector_code, market, security_count, record["metric_definition_count"], len(required),
                        len(populated), metric_status, universe_status, "ready", canonical(blockers),
                        as_of_date, knowledge.utc_now(),
                    ),
                )
    result = {
        "spec_id": coverage_spec["spec_id"], "universe_snapshot_id": universe_snapshot_id,
        "as_of_date": as_of_date, "sector_pack_count": initialized["sector_packs"],
        "market_sector_cells": len(matrix), "matrix": matrix,
        "truth_boundary": "A ready research pack is not a populated dataset. Every missing stream and market universe is explicit.",
    }
    path = output_root / universe_snapshot_id.replace(":", "-") / "full-consumer-coverage-matrix.json"
    write_json(path, result)
    result["artifact_path"] = str(path)
    return result


def generate_task_packages(db_path: Path, cutoff_timestamp: str,
                           output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    init_coverage(db_path)
    knowledge.parse_timestamp(cutoff_timestamp)
    coverage_spec = spec()
    common_metrics, sector_metrics = metric_contract()
    names, _, _ = domain_index()
    packages: list[dict[str, Any]] = []
    with knowledge.connect(db_path) as connection:
        source_counts = {
            row["stream_name"]: row["count"] for row in connection.execute(
                """SELECT p.stream_name,COUNT(DISTINCT r.source_id) AS count
                   FROM production_dataset_contracts p
                   LEFT JOIN source_catalog r ON 1=0
                   GROUP BY p.stream_name"""
            ).fetchall()
        }
        route_counts = Counter(item["stream_name"] for item in production.source_streams(db_path))
        for sector_code in coverage_spec["sector_research_contracts"]:
            metric_ids = common_metrics + sector_metrics[sector_code]
            routes = [
                {"stream_name": stream, "registered_source_routes": route_counts.get(stream, source_counts.get(stream, 0))}
                for stream in coverage_spec["required_streams"]
            ]
            for template_id in coverage_spec["required_templates"]:
                question = QUESTION_BY_TEMPLATE[template_id].format(sector=names[sector_code])
                payload = {
                    "sector_code": sector_code, "template_id": template_id, "cutoff_timestamp": cutoff_timestamp,
                    "research_question": question, "metric_queries": metric_ids, "source_routes": routes,
                    "quality_gates": coverage_spec["quality_gates"],
                    "portfolio_boundary": "no_fund_holdings_or_position_inference",
                }
                package_hash = knowledge.sha256_json(payload)
                package_id = production.stable_id("task", sector_code, template_id, cutoff_timestamp, package_hash)
                package = {"task_package_id": package_id, **payload, "status": "ready_with_data_gaps", "package_hash": package_hash}
                packages.append(package)
                connection.execute(
                    """INSERT OR REPLACE INTO research_task_packages(
                           task_package_id,sector_code,template_id,cutoff_timestamp,research_question,
                           metric_queries_json,source_routes_json,quality_gates_json,status,package_hash,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        package_id, sector_code, template_id, cutoff_timestamp, question, canonical(metric_ids),
                        canonical(routes), canonical(coverage_spec["quality_gates"]), "ready_with_data_gaps",
                        package_hash, knowledge.utc_now(),
                    ),
                )
    result = {
        "cutoff_timestamp": cutoff_timestamp, "sector_count": len(coverage_spec["sector_research_contracts"]),
        "template_count": len(coverage_spec["required_templates"]), "task_package_count": len(packages),
        "packages": packages,
        "truth_boundary": "Task packages are executable research contracts; ready_with_data_gaps does not mean source data is populated.",
    }
    path = output_root / "task-packages" / (cutoff_timestamp[:10] + ".json")
    write_json(path, result)
    result["artifact_path"] = str(path)
    return result


def coverage_status(db_path: Path) -> dict[str, Any]:
    initialized = init_coverage(db_path)
    with knowledge.connect(db_path) as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in (
                "research_sector_packs", "taxonomy_vendor_mappings", "research_universe_snapshots",
                "research_universe_members", "research_coverage_status", "research_task_packages",
            )
        }
        population = {
            row["metric_population_status"]: row["count"] for row in connection.execute(
                "SELECT metric_population_status,COUNT(*) AS count FROM research_coverage_status GROUP BY metric_population_status"
            ).fetchall()
        }
    return {**initialized, "table_counts": counts, "metric_population_cells": population}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    universe = sub.add_parser("build-universe")
    universe.add_argument("--snapshot", action="append", type=Path, required=True)
    universe.add_argument("--as-of-date", required=True)
    matrix = sub.add_parser("coverage-matrix")
    matrix.add_argument("--universe-id", required=True)
    matrix.add_argument("--as-of-date", required=True)
    tasks = sub.add_parser("generate-tasks")
    tasks.add_argument("--cutoff", required=True)
    sub.add_parser("status")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init":
            result = init_coverage(args.db)
        elif args.command == "build-universe":
            result = build_universe(args.db, args.snapshot, args.as_of_date)
        elif args.command == "coverage-matrix":
            result = build_coverage_matrix(args.db, args.universe_id, args.as_of_date)
        elif args.command == "generate-tasks":
            result = generate_task_packages(args.db, args.cutoff)
        else:
            result = coverage_status(args.db)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, production.ProductionValidationError) as exc:
        payload = {"status": "blocked", "error": str(exc)}
        if isinstance(exc, production.ProductionValidationError):
            payload["issues"] = exc.issues
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
