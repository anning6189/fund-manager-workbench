#!/usr/bin/env python3
"""Stage 7 consumer-research model engine.

The engine deliberately uses a small arithmetic expression language instead of
Python eval. Facts are resolved from the Stage 6 point-in-time warehouse;
scenario values must be explicitly labelled as assumptions. Every output keeps
its formula and direct input lineage in SQLite.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import consumer_knowledge_store as knowledge  # noqa: E402


DEFAULT_DB = PROJECT_ROOT / "data" / "curated" / "consumer-research.db"
MODEL_SCHEMA_PATH = PROJECT_ROOT / "sql" / "002_consumer_research_model_engine.sql"
MODEL_SPEC_PATH = PROJECT_ROOT / "specs" / "models" / "consumer-research-model-engine.v1.json"

MODEL_TYPES_REQUIRING_THREE_SCENARIOS = {
    "market_size_dual",
    "financial_forecast",
    "valuation_expectations",
}
FORBIDDEN_MODEL_KEYS = {
    "buy",
    "sell",
    "trade_instruction",
    "order_instruction",
    "portfolio_action",
    "fund_holdings",
    "fund_holding",
    "portfolio_holdings",
    "portfolio_exposure",
    "portfolio_position",
    "fund_position",
    "position_inference",
}
SAFE_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}


class ModelValidationError(Exception):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__("; ".join(str(item["message"]) for item in issues))


def issue(code: str, message: str, path: str = "$") -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{hash_text('|'.join(parts))[:24]}"


def scan_forbidden(value: Any, path: str = "$") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_MODEL_KEYS:
                matches.append(f"{path}.{key}")
            matches.extend(scan_forbidden(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(scan_forbidden(child, f"{path}[{index}]"))
    return matches


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


class SafeFormula:
    """Parse and evaluate a deliberately small expression language."""

    BIN_OPS = {
        ast.Add: lambda a, b: a + b,
        ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b,
        ast.Pow: lambda a, b: a**b,
    }
    UNARY_OPS = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}

    def __init__(self, expression: str):
        self.expression = expression
        try:
            self.tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"invalid formula syntax: {exc.msg}") from exc
        self._validate(self.tree)

    def _validate(self, node: ast.AST) -> None:
        allowed = (
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Constant,
            ast.Name,
            ast.Load,
            ast.Call,
            *self.BIN_OPS.keys(),
            *self.UNARY_OPS.keys(),
        )
        for child in ast.walk(node):
            if not isinstance(child, allowed):
                raise ValueError(f"forbidden formula syntax: {type(child).__name__}")
            if isinstance(child, ast.Constant) and not finite_number(child.value):
                raise ValueError("formula constants must be finite numbers")
            if isinstance(child, ast.Call):
                if not isinstance(child.func, ast.Name) or child.func.id not in SAFE_FUNCTIONS:
                    raise ValueError("formula function is not in the whitelist")
                if child.keywords:
                    raise ValueError("formula keyword arguments are forbidden")
            if isinstance(child, ast.Name) and child.id.startswith("_"):
                raise ValueError("private names are forbidden")

    @property
    def names(self) -> set[str]:
        return {
            node.id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Name) and node.id not in SAFE_FUNCTIONS
        }

    def evaluate(self, values: dict[str, float]) -> float:
        result = self._eval(self.tree.body, values)
        if not finite_number(result):
            raise ValueError("formula result is not finite")
        return float(result)

    def _eval(self, node: ast.AST, values: dict[str, float]) -> float:
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise KeyError(node.id)
            return float(values[node.id])
        if isinstance(node, ast.BinOp):
            function = self.BIN_OPS.get(type(node.op))
            if function is None:
                raise ValueError("operator is not allowed")
            return float(function(self._eval(node.left, values), self._eval(node.right, values)))
        if isinstance(node, ast.UnaryOp):
            function = self.UNARY_OPS.get(type(node.op))
            if function is None:
                raise ValueError("unary operator is not allowed")
            return float(function(self._eval(node.operand, values)))
        if isinstance(node, ast.Call):
            function = SAFE_FUNCTIONS[node.func.id]  # validated above
            return float(function(*(self._eval(arg, values) for arg in node.args)))
        raise ValueError(f"unsupported formula node: {type(node).__name__}")


def init_engine(db_path: Path) -> dict[str, Any]:
    stage6 = knowledge.init_store(db_path)
    specification = read_json(MODEL_SPEC_PATH)
    schema = MODEL_SCHEMA_PATH.read_text(encoding="utf-8")
    now = knowledge.utc_now()
    with knowledge.connect(db_path) as connection:
        connection.executescript(schema)
        for model in specification["model_types"]:
            connection.execute(
                """INSERT INTO model_definitions(
                       model_id,model_type,model_version,name,status,
                       required_output_roles_json,specification_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(model_id) DO UPDATE SET
                     model_type=excluded.model_type,model_version=excluded.model_version,
                     name=excluded.name,status=excluded.status,
                     required_output_roles_json=excluded.required_output_roles_json,
                     specification_json=excluded.specification_json,updated_at=excluded.updated_at""",
                (
                    model["model_id"],
                    model["model_type"],
                    specification["version"],
                    model["name"],
                    "active",
                    knowledge.canonical_json(model["required_output_roles"]),
                    knowledge.canonical_json(model),
                    now,
                    now,
                ),
            )
    return {
        "status": "initialized",
        "database": str(db_path),
        "stage6": stage6,
        "models_loaded": len(specification["model_types"]),
        "model_spec_id": specification["spec_id"],
    }


def model_catalog(db_path: Path) -> dict[str, dict[str, Any]]:
    with knowledge.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT model_id,model_type,required_output_roles_json FROM model_definitions WHERE status='active'"
        ).fetchall()
    return {
        row["model_id"]: {
            "model_type": row["model_type"],
            "required_output_roles": json.loads(row["required_output_roles_json"]),
        }
        for row in rows
    }


def validate_package(db_path: Path, package: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    required = [
        "package_id",
        "model_id",
        "model_type",
        "as_of_timestamp",
        "environment",
        "scope",
        "fact_inputs",
        "scenarios",
        "calculations",
    ]
    for field in required:
        if field not in package:
            issues.append(issue("required_field_missing", f"Missing required field: {field}", f"$.{field}"))
    if issues:
        return issues

    forbidden = scan_forbidden(package)
    if forbidden:
        issues.append(issue("research_boundary_violation", f"Forbidden decision/holdings fields: {forbidden}"))

    try:
        cutoff = knowledge.normalize_timestamp(package["as_of_timestamp"])
    except (TypeError, ValueError) as exc:
        issues.append(issue("invalid_as_of_timestamp", str(exc), "$.as_of_timestamp"))
        cutoff = None

    catalog = model_catalog(db_path)
    definition = catalog.get(package["model_id"])
    if definition is None:
        issues.append(issue("unknown_model_id", f"Unknown model_id: {package['model_id']}", "$.model_id"))
    elif definition["model_type"] != package["model_type"]:
        issues.append(issue("model_type_mismatch", "model_type does not match model_id", "$.model_type"))

    scope = package.get("scope")
    if not isinstance(scope, dict) or not scope.get("scope_key"):
        issues.append(issue("invalid_scope", "scope.scope_key is required", "$.scope.scope_key"))
    package_scope_key = scope.get("scope_key") if isinstance(scope, dict) else None

    facts = package.get("fact_inputs")
    if not isinstance(facts, list):
        issues.append(issue("invalid_fact_inputs", "fact_inputs must be a list", "$.fact_inputs"))
        facts = []
    facts_ids: list[str] = []
    required_fact_fields = {"input_id", "entity_id", "metric_id", "period_end", "unit", "scope_key", "content_label"}
    for index, item in enumerate(facts):
        path = f"$.fact_inputs[{index}]"
        missing = required_fact_fields - set(item)
        if missing:
            issues.append(issue("fact_field_missing", f"Missing fact fields: {sorted(missing)}", path))
            continue
        facts_ids.append(item["input_id"])
        if item["content_label"] != "FACT_OBSERVATION":
            issues.append(issue("fact_label_invalid", "Warehouse facts must use FACT_OBSERVATION", f"{path}.content_label"))
        if package["model_type"] in {"market_size_dual", "competition_concentration"} and item["scope_key"] != package_scope_key:
            issues.append(issue("scope_mismatch", "Market model inputs must share scope.scope_key", f"{path}.scope_key"))

    scenarios = package.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        issues.append(issue("scenarios_missing", "At least one scenario is required", "$.scenarios"))
        scenarios = []
    scenario_ids: list[str] = []
    scenario_input_sets: list[set[str]] = []
    for s_index, scenario in enumerate(scenarios):
        s_path = f"$.scenarios[{s_index}]"
        scenario_id = scenario.get("scenario_id")
        if not scenario_id:
            issues.append(issue("scenario_id_missing", "scenario_id is required", s_path))
            continue
        scenario_ids.append(scenario_id)
        assumptions = scenario.get("assumptions")
        if not isinstance(assumptions, list):
            issues.append(issue("invalid_assumptions", "assumptions must be a list", f"{s_path}.assumptions"))
            continue
        assumption_ids: set[str] = set()
        for a_index, assumption in enumerate(assumptions):
            path = f"{s_path}.assumptions[{a_index}]"
            required_assumption_fields = {
                "input_id", "value", "unit", "scope_key", "content_label",
                "available_at", "rationale", "confidence",
            }
            missing = required_assumption_fields - set(assumption)
            if missing:
                issues.append(issue("assumption_field_missing", f"Missing assumption fields: {sorted(missing)}", path))
                continue
            assumption_ids.add(assumption["input_id"])
            if assumption["content_label"] != "SCENARIO_ASSUMPTION":
                issues.append(issue("assumption_label_invalid", "Scenario values must use SCENARIO_ASSUMPTION", f"{path}.content_label"))
            if not finite_number(assumption["value"]):
                issues.append(issue("assumption_value_invalid", "Assumption must be a finite number", f"{path}.value"))
            if not finite_number(assumption["confidence"]) or not 0 <= float(assumption["confidence"]) <= 1:
                issues.append(issue("assumption_confidence_invalid", "confidence must be between 0 and 1", f"{path}.confidence"))
            if not str(assumption["rationale"]).strip():
                issues.append(issue("assumption_rationale_missing", "Assumption rationale cannot be blank", f"{path}.rationale"))
            try:
                available = knowledge.parse_timestamp(assumption["available_at"])
                if cutoff and available > knowledge.parse_timestamp(cutoff):
                    issues.append(issue("future_assumption_leakage", "Assumption was not available at the model cutoff", f"{path}.available_at"))
            except (TypeError, ValueError) as exc:
                issues.append(issue("invalid_assumption_timestamp", str(exc), f"{path}.available_at"))
            if package["model_type"] in {"market_size_dual", "competition_concentration"} and assumption["scope_key"] != package_scope_key:
                issues.append(issue("scope_mismatch", "Market model inputs must share scope.scope_key", f"{path}.scope_key"))
        scenario_input_sets.append(assumption_ids)

    if len(set(scenario_ids)) != len(scenario_ids):
        issues.append(issue("duplicate_scenario_id", "scenario_id values must be unique", "$.scenarios"))
    if package["model_type"] in MODEL_TYPES_REQUIRING_THREE_SCENARIOS and set(scenario_ids) != {"bear", "base", "bull"}:
        issues.append(issue("three_scenarios_required", "Model requires exactly bear/base/bull scenarios", "$.scenarios"))
    if scenario_input_sets and any(item != scenario_input_sets[0] for item in scenario_input_sets[1:]):
        issues.append(issue("scenario_input_mismatch", "All scenarios must define the same assumption input_ids", "$.scenarios"))

    calculations = package.get("calculations")
    if not isinstance(calculations, list) or not calculations:
        issues.append(issue("calculations_missing", "calculations must be a non-empty list", "$.calculations"))
        calculations = []
    output_ids: list[str] = []
    output_roles: set[str] = set()
    formula_names: dict[str, set[str]] = {}
    for index, calculation in enumerate(calculations):
        path = f"$.calculations[{index}]"
        required_calculation_fields = {"output_id", "output_role", "formula", "unit", "content_label"}
        missing = required_calculation_fields - set(calculation)
        if missing:
            issues.append(issue("calculation_field_missing", f"Missing calculation fields: {sorted(missing)}", path))
            continue
        output_ids.append(calculation["output_id"])
        output_roles.add(calculation["output_role"])
        if calculation["content_label"] != "AGENT_CALCULATION":
            issues.append(issue("calculation_label_invalid", "Outputs must use AGENT_CALCULATION", f"{path}.content_label"))
        try:
            formula_names[calculation["output_id"]] = SafeFormula(calculation["formula"]).names
        except ValueError as exc:
            issues.append(issue("unsafe_formula", str(exc), f"{path}.formula"))
    if len(set(output_ids)) != len(output_ids):
        issues.append(issue("duplicate_output_id", "output_id values must be unique", "$.calculations"))
    if len(set(facts_ids)) != len(facts_ids):
        issues.append(issue("duplicate_fact_input_id", "Fact input_id values must be unique", "$.fact_inputs"))
    all_input_ids = set(facts_ids) | (scenario_input_sets[0] if scenario_input_sets else set())
    if all_input_ids & set(output_ids):
        issues.append(issue("input_output_id_collision", "Input and output identifiers must be distinct"))
    known_symbols = all_input_ids | set(output_ids)
    unknown_symbols = sorted({name for names in formula_names.values() for name in names if name not in known_symbols})
    if unknown_symbols:
        issues.append(issue("formula_symbol_unknown", f"Unknown formula symbols: {unknown_symbols}", "$.calculations"))
    if definition:
        missing_roles = set(definition["required_output_roles"]) - output_roles
        if missing_roles:
            issues.append(issue("required_output_role_missing", f"Missing output roles: {sorted(missing_roles)}", "$.calculations"))

    if package["model_type"] == "valuation_expectations":
        controls = package.get("controls", {})
        valuation_time = controls.get("valuation_input_timestamp")
        forecast_time = controls.get("forecast_snapshot_timestamp")
        try:
            valuation_norm = knowledge.normalize_timestamp(valuation_time)
            forecast_norm = knowledge.normalize_timestamp(forecast_time)
            if valuation_norm != forecast_norm:
                issues.append(issue("valuation_timestamp_mismatch", "Valuation and forecast snapshot timestamps must match", "$.controls"))
            if cutoff and knowledge.parse_timestamp(valuation_norm) > knowledge.parse_timestamp(cutoff):
                issues.append(issue("valuation_future_leakage", "Valuation snapshot cannot be after cutoff", "$.controls"))
        except (TypeError, ValueError) as exc:
            issues.append(issue("valuation_timestamp_invalid", str(exc), "$.controls"))

    sensitivities = package.get("sensitivities", [])
    if not isinstance(sensitivities, list):
        issues.append(issue("invalid_sensitivities", "sensitivities must be a list", "$.sensitivities"))
        sensitivities = []
    scenario_assumptions = scenario_input_sets[0] if scenario_input_sets else set()
    for index, sensitivity in enumerate(sensitivities):
        path = f"$.sensitivities[{index}]"
        required_sensitivity = {"sensitivity_id", "scenario_id", "x_input_id", "x_values", "output_id"}
        missing = required_sensitivity - set(sensitivity)
        if missing:
            issues.append(issue("sensitivity_field_missing", f"Missing sensitivity fields: {sorted(missing)}", path))
            continue
        if sensitivity["scenario_id"] not in scenario_ids:
            issues.append(issue("sensitivity_scenario_unknown", "Sensitivity scenario_id is unknown", path))
        if sensitivity["x_input_id"] not in scenario_assumptions:
            issues.append(issue("sensitivity_input_invalid", "x_input_id must be a scenario assumption", path))
        if sensitivity.get("y_input_id") and sensitivity["y_input_id"] not in scenario_assumptions:
            issues.append(issue("sensitivity_input_invalid", "y_input_id must be a scenario assumption", path))
        if sensitivity["output_id"] not in output_ids:
            issues.append(issue("sensitivity_output_unknown", "Sensitivity output_id is unknown", path))
        for key in ("x_values", "y_values"):
            values = sensitivity.get(key)
            if values is not None and (not isinstance(values, list) or not values or not all(finite_number(value) for value in values)):
                issues.append(issue("sensitivity_values_invalid", f"{key} must contain finite values", f"{path}.{key}"))

    if package["model_type"] in {"market_size_dual", "financial_forecast", "valuation_expectations"} and not sensitivities:
        issues.append(issue("sensitivity_required", "This model type requires a sensitivity definition", "$.sensitivities"))
    return issues


def resolve_fact_inputs(db_path: Path, package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    cutoff = knowledge.normalize_timestamp(package["as_of_timestamp"])
    for index, fact in enumerate(package["fact_inputs"]):
        result = knowledge.query_metric(
            db_path,
            fact["entity_id"],
            fact["metric_id"],
            cutoff,
            fact["period_end"],
        )
        candidates = result["observations"]
        if fact.get("source_id"):
            candidates = [row for row in candidates if row.get("source_id") == fact["source_id"]]
        path = f"$.fact_inputs[{index}]"
        if not candidates:
            errors.append(issue("fact_unavailable_at_cutoff", f"No eligible fact for {fact['input_id']} at {cutoff}", path))
            continue
        if len(candidates) != 1:
            errors.append(issue("fact_ambiguous", f"Expected one fact for {fact['input_id']}, found {len(candidates)}", path))
            continue
        row = candidates[0]
        if row["value_numeric"] is None or not finite_number(row["value_numeric"]):
            errors.append(issue("fact_not_numeric", f"Fact {fact['input_id']} is not numeric", path))
            continue
        if row["unit"] != fact["unit"]:
            errors.append(issue("fact_unit_mismatch", f"Expected {fact['unit']}, warehouse returned {row['unit']}", path))
            continue
        if not row.get("evidence_id") or not row.get("observation_id"):
            errors.append(issue("fact_evidence_missing", f"Fact {fact['input_id']} lacks observation/evidence lineage", path))
            continue
        resolved[fact["input_id"]] = {
            "input_id": fact["input_id"],
            "input_kind": "FACT",
            "value": float(row["value_numeric"]),
            "unit": row["unit"],
            "scope_key": fact["scope_key"],
            "observation_id": row["observation_id"],
            "evidence_id": row["evidence_id"],
            "available_at": row["available_at"],
            "content_label": "FACT_OBSERVATION",
            "rationale": None,
            "confidence": None,
            "source": row,
        }
    if errors:
        raise ModelValidationError(errors)
    return resolved


def scenario_inputs(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["input_id"]: {
            "input_id": item["input_id"],
            "input_kind": "ASSUMPTION",
            "value": float(item["value"]),
            "unit": item["unit"],
            "scope_key": item["scope_key"],
            "observation_id": None,
            "evidence_id": None,
            "available_at": knowledge.normalize_timestamp(item["available_at"]),
            "content_label": "SCENARIO_ASSUMPTION",
            "rationale": item["rationale"],
            "confidence": float(item["confidence"]),
            "source": item,
        }
        for item in scenario["assumptions"]
    }


def calculate_outputs(calculations: list[dict[str, Any]], inputs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    values = {input_id: float(item["value"]) for input_id, item in inputs.items()}
    pending = {item["output_id"]: item for item in calculations}
    outputs: list[dict[str, Any]] = []
    while pending:
        progress = False
        for output_id, calculation in list(pending.items()):
            formula = SafeFormula(calculation["formula"])
            if not formula.names.issubset(values):
                continue
            try:
                result = formula.evaluate(values)
            except ZeroDivisionError as exc:
                raise ModelValidationError([issue("division_by_zero", f"Division by zero in {output_id}", f"$.calculations.{output_id}")]) from exc
            except (ValueError, OverflowError) as exc:
                raise ModelValidationError([issue("calculation_failed", f"{output_id}: {exc}", f"$.calculations.{output_id}")]) from exc
            values[output_id] = result
            outputs.append(
                {
                    "output_id": output_id,
                    "output_role": calculation["output_role"],
                    "value": result,
                    "unit": calculation["unit"],
                    "formula": calculation["formula"],
                    "content_label": "AGENT_CALCULATION",
                    "dependencies": sorted(formula.names),
                }
            )
            del pending[output_id]
            progress = True
        if not progress:
            unresolved = {
                output_id: sorted(SafeFormula(item["formula"]).names - set(values))
                for output_id, item in pending.items()
            }
            raise ModelValidationError([issue("calculation_dependency_cycle", f"Unresolved formula dependencies: {unresolved}")])
    return outputs


def output_by_role(outputs: list[dict[str, Any]]) -> dict[str, float]:
    return {item["output_role"]: float(item["value"]) for item in outputs}


def validate_model_relationships(model_type: str, outputs: list[dict[str, Any]]) -> None:
    roles = output_by_role(outputs)
    errors: list[dict[str, Any]] = []
    tolerance = 1e-8
    if model_type == "market_size_dual":
        if roles["market_size_top_down"] <= 0 or roles["market_size_bottom_up"] <= 0 or roles["market_size_midpoint"] <= 0:
            errors.append(issue("market_size_nonpositive", "Market size outputs must be positive"))
        if not 0 <= roles["cross_check_gap"] <= 1:
            errors.append(issue("cross_check_gap_invalid", "Cross-method gap must be between 0 and 1"))
    elif model_type == "competition_concentration":
        for key in ("market_share_leader", "cr3", "cr5", "long_tail_share"):
            if not -tolerance <= roles[key] <= 1 + tolerance:
                errors.append(issue("share_out_of_bounds", f"{key} must be between 0 and 1"))
        if roles["cr3"] > roles["cr5"] + tolerance:
            errors.append(issue("concentration_order_invalid", "CR3 cannot exceed CR5"))
        if abs(roles["cr5"] + roles["long_tail_share"] - 1) > tolerance:
            errors.append(issue("market_share_not_reconciled", "CR5 plus long-tail share must equal 1"))
    elif model_type == "company_operating_bridge":
        if abs(roles["bridge_residual"]) > tolerance:
            errors.append(issue("operating_bridge_not_reconciled", "Volume-price bridge residual is not zero"))
    elif model_type == "financial_forecast":
        if roles["forecast_revenue"] <= 0:
            errors.append(issue("forecast_revenue_nonpositive", "Forecast revenue must be positive"))
        if roles["forecast_gross_profit"] > roles["forecast_revenue"] + tolerance:
            errors.append(issue("gross_profit_invalid", "Gross profit cannot exceed revenue"))
        if roles["forecast_fcf"] > roles["forecast_cfo"] + tolerance:
            errors.append(issue("fcf_reconciliation_invalid", "FCF cannot exceed CFO when capex is non-negative"))
    elif model_type == "valuation_expectations":
        for key in ("forward_pe", "implied_market_cap", "reverse_implied_earnings"):
            if roles[key] <= 0:
                errors.append(issue("valuation_nonpositive", f"{key} must be positive"))
    if errors:
        raise ModelValidationError(errors)


def sensitivity_results(
    package: dict[str, Any],
    scenario: dict[str, Any],
    combined_inputs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    output_units = {item["output_id"]: item["unit"] for item in package["calculations"]}
    assumption_ids = {item["input_id"] for item in scenario["assumptions"]}
    for sensitivity in package.get("sensitivities", []):
        if sensitivity["scenario_id"] != scenario["scenario_id"]:
            continue
        if sensitivity["x_input_id"] not in assumption_ids or (
            sensitivity.get("y_input_id") and sensitivity["y_input_id"] not in assumption_ids
        ):
            raise ModelValidationError([issue("sensitivity_input_invalid", "Sensitivity may only override scenario assumptions")])
        y_values = sensitivity.get("y_values", [None])
        for x_value in sensitivity["x_values"]:
            for y_value in y_values:
                altered = {key: dict(value) for key, value in combined_inputs.items()}
                altered[sensitivity["x_input_id"]]["value"] = float(x_value)
                if sensitivity.get("y_input_id"):
                    altered[sensitivity["y_input_id"]]["value"] = float(y_value)
                outputs = calculate_outputs(package["calculations"], altered)
                value = next(item["value"] for item in outputs if item["output_id"] == sensitivity["output_id"])
                results.append(
                    {
                        "sensitivity_id": sensitivity["sensitivity_id"],
                        "x_input_id": sensitivity["x_input_id"],
                        "x_value": float(x_value),
                        "y_input_id": sensitivity.get("y_input_id"),
                        "y_value": float(y_value) if y_value is not None else None,
                        "output_id": sensitivity["output_id"],
                        "output_value": float(value),
                        "unit": output_units[sensitivity["output_id"]],
                    }
                )
    return results


def publication_status(package: dict[str, Any]) -> str:
    if package["environment"] == "demonstration":
        return "demonstration_only"
    return "internal_research_ready"


def load_completed_run(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM model_runs WHERE run_id=?", (run_id,)).fetchone()
    outputs = connection.execute(
        "SELECT output_id,output_role,value_numeric,unit,formula,content_label,quality_status,lineage_json FROM model_outputs WHERE run_id=? ORDER BY output_id",
        (run_id,),
    ).fetchall()
    sensitivity = connection.execute(
        "SELECT sensitivity_id,x_input_id,x_value,y_input_id,y_value,output_id,output_value,unit FROM model_sensitivity_results WHERE run_id=? ORDER BY sensitivity_id,x_value,y_value",
        (run_id,),
    ).fetchall()
    return {
        "run_id": run_id,
        "scenario_id": row["scenario_id"],
        "status": row["status"],
        "publication_status": row["publication_status"],
        "outputs": [dict(item) for item in outputs],
        "sensitivity_results": [dict(item) for item in sensitivity],
    }


def run_package(db_path: Path, package_path: Path) -> dict[str, Any]:
    init_engine(db_path)
    package = read_json(package_path)
    issues = validate_package(db_path, package)
    if issues:
        raise ModelValidationError(issues)
    facts = resolve_fact_inputs(db_path, package)
    package_hash = knowledge.sha256_json(package)
    cutoff = knowledge.normalize_timestamp(package["as_of_timestamp"])
    results: list[dict[str, Any]] = []

    with knowledge.connect(db_path) as connection:
        existing = connection.execute(
            "SELECT package_hash FROM model_packages WHERE package_id=?", (package["package_id"],)
        ).fetchone()
        if existing and existing["package_hash"] != package_hash:
            raise ModelValidationError([issue("package_id_content_conflict", "package_id already exists with different content")])
        connection.execute(
            """INSERT OR IGNORE INTO model_packages(
                   package_id,model_id,package_hash,as_of_timestamp,environment,scope_json,content_label,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                package["package_id"], package["model_id"], package_hash, cutoff,
                package["environment"], knowledge.canonical_json(package["scope"]),
                "MODEL_INPUT_PACKAGE", knowledge.utc_now(),
            ),
        )
        for scenario in package["scenarios"]:
            signature = knowledge.sha256_json(
                {"package_hash": package_hash, "scenario_id": scenario["scenario_id"], "engine": "1.0.0"}
            )
            run_id = stable_id("mr", package["package_id"], scenario["scenario_id"], signature)
            existing_run = connection.execute(
                "SELECT status FROM model_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing_run and existing_run["status"] == "completed":
                results.append(load_completed_run(connection, run_id))
                continue
            now = knowledge.utc_now()
            connection.execute(
                """INSERT OR REPLACE INTO model_runs(
                       run_id,package_id,scenario_id,run_signature,started_at,completed_at,status,
                       publication_status,human_review_required,error_summary
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, package["package_id"], scenario["scenario_id"], signature,
                    now, None, "running", publication_status(package),
                    0, None,
                ),
            )
            assumptions = scenario_inputs(scenario)
            combined = {**facts, **assumptions}
            for item in combined.values():
                input_record_id = stable_id("mi", run_id, item["input_id"])
                connection.execute(
                    """INSERT INTO model_inputs(
                           input_record_id,run_id,input_id,input_kind,value_numeric,unit,scope_key,
                           observation_id,evidence_id,available_at,content_label,rationale,confidence,source_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        input_record_id, run_id, item["input_id"], item["input_kind"], item["value"],
                        item["unit"], item["scope_key"], item["observation_id"], item["evidence_id"],
                        item["available_at"], item["content_label"], item["rationale"], item["confidence"],
                        knowledge.canonical_json(item["source"]),
                    ),
                )
            outputs = calculate_outputs(package["calculations"], combined)
            validate_model_relationships(package["model_type"], outputs)
            for item in outputs:
                output_record_id = stable_id("mo", run_id, item["output_id"])
                connection.execute(
                    """INSERT INTO model_outputs(
                           output_record_id,run_id,output_id,output_role,value_numeric,unit,
                           formula,content_label,quality_status,lineage_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        output_record_id, run_id, item["output_id"], item["output_role"], item["value"],
                        item["unit"], item["formula"], item["content_label"], "validated",
                        knowledge.canonical_json({"direct_dependencies": item["dependencies"]}),
                    ),
                )
            sensitivities = sensitivity_results(package, scenario, combined)
            for item in sensitivities:
                sensitivity_record_id = stable_id(
                    "ms", run_id, item["sensitivity_id"], str(item["x_value"]), str(item["y_value"])
                )
                connection.execute(
                    """INSERT INTO model_sensitivity_results(
                           sensitivity_record_id,run_id,sensitivity_id,x_input_id,x_value,y_input_id,
                           y_value,output_id,output_value,unit
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sensitivity_record_id, run_id, item["sensitivity_id"], item["x_input_id"],
                        item["x_value"], item["y_input_id"], item["y_value"], item["output_id"],
                        item["output_value"], item["unit"],
                    ),
                )
            connection.execute(
                "UPDATE model_runs SET completed_at=?,status='completed' WHERE run_id=?",
                (knowledge.utc_now(), run_id),
            )
            results.append(load_completed_run(connection, run_id))

        if package["model_type"] in MODEL_TYPES_REQUIRING_THREE_SCENARIOS:
            ordering_role = {
                "market_size_dual": "market_size_midpoint",
                "financial_forecast": "forecast_net_profit",
                "valuation_expectations": "implied_market_cap",
            }[package["model_type"]]
            values: dict[str, float] = {}
            for result in results:
                matching = next(item for item in result["outputs"] if item["output_role"] == ordering_role)
                values[result["scenario_id"]] = float(matching.get("value_numeric", matching.get("value")))
            if not values["bear"] <= values["base"] <= values["bull"]:
                raise ModelValidationError([issue("scenario_order_invalid", f"Expected bear <= base <= bull for {ordering_role}: {values}")])

    return {
        "status": "completed",
        "package_id": package["package_id"],
        "package_hash": package_hash,
        "model_id": package["model_id"],
        "model_type": package["model_type"],
        "as_of_timestamp": cutoff,
        "environment": package["environment"],
        "publication_status": publication_status(package),
        "runs": results,
    }


def engine_status(db_path: Path) -> dict[str, Any]:
    init_engine(db_path)
    tables = [
        "model_definitions", "model_packages", "model_runs", "model_inputs",
        "model_outputs", "model_sensitivity_results", "model_validation_events",
    ]
    with knowledge.connect(db_path) as connection:
        counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
        by_type = [
            dict(row)
            for row in connection.execute(
                """SELECT d.model_type,COUNT(DISTINCT p.package_id) AS packages,COUNT(r.run_id) AS runs
                   FROM model_definitions d
                   LEFT JOIN model_packages p ON p.model_id=d.model_id
                   LEFT JOIN model_runs r ON r.package_id=p.package_id
                   GROUP BY d.model_type ORDER BY d.model_type"""
            ).fetchall()
        ]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
    return {
        "database": str(db_path),
        "counts": counts,
        "model_types": by_type,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="消费行业研究阶段七模型与计算引擎")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="初始化模型表和五类模型定义")
    run_parser = subparsers.add_parser("run", help="运行模型包")
    run_parser.add_argument("--package", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate", help="只验证模型包")
    validate_parser.add_argument("--package", type=Path, required=True)
    subparsers.add_parser("status", help="显示模型仓状态")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            emit(init_engine(args.db))
        elif args.command == "run":
            emit(run_package(args.db, args.package))
        elif args.command == "validate":
            init_engine(args.db)
            package = read_json(args.package)
            issues = validate_package(args.db, package)
            emit({"valid": not issues, "issues": issues})
            return 0 if not issues else 2
        elif args.command == "status":
            emit(engine_status(args.db))
        return 0
    except ModelValidationError as exc:
        emit({"status": "blocked", "issues": exc.issues})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
