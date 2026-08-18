PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS model_definitions (
  model_id TEXT PRIMARY KEY,
  model_type TEXT NOT NULL,
  model_version TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  required_output_roles_json TEXT NOT NULL,
  specification_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_packages (
  package_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL,
  package_hash TEXT NOT NULL,
  as_of_timestamp TEXT NOT NULL,
  environment TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  content_label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(model_id) REFERENCES model_definitions(model_id)
);

CREATE TABLE IF NOT EXISTS model_runs (
  run_id TEXT PRIMARY KEY,
  package_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  run_signature TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL,
  publication_status TEXT NOT NULL,
  human_review_required INTEGER NOT NULL CHECK(human_review_required IN (0,1)),
  error_summary TEXT,
  UNIQUE(package_id, scenario_id, run_signature),
  FOREIGN KEY(package_id) REFERENCES model_packages(package_id)
);

CREATE INDEX IF NOT EXISTS idx_model_runs_package
  ON model_runs(package_id, scenario_id, completed_at);

CREATE TABLE IF NOT EXISTS model_inputs (
  input_record_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  input_id TEXT NOT NULL,
  input_kind TEXT NOT NULL,
  value_numeric REAL NOT NULL,
  unit TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  observation_id TEXT,
  evidence_id TEXT,
  available_at TEXT,
  content_label TEXT NOT NULL,
  rationale TEXT,
  confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  source_json TEXT NOT NULL,
  UNIQUE(run_id, input_id),
  FOREIGN KEY(run_id) REFERENCES model_runs(run_id),
  FOREIGN KEY(observation_id) REFERENCES observations(observation_id),
  FOREIGN KEY(evidence_id) REFERENCES evidence(evidence_id)
);

CREATE TABLE IF NOT EXISTS model_outputs (
  output_record_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  output_id TEXT NOT NULL,
  output_role TEXT NOT NULL,
  value_numeric REAL NOT NULL,
  unit TEXT NOT NULL,
  formula TEXT NOT NULL,
  content_label TEXT NOT NULL,
  quality_status TEXT NOT NULL,
  lineage_json TEXT NOT NULL,
  UNIQUE(run_id, output_id),
  FOREIGN KEY(run_id) REFERENCES model_runs(run_id)
);

CREATE TABLE IF NOT EXISTS model_sensitivity_results (
  sensitivity_record_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  sensitivity_id TEXT NOT NULL,
  x_input_id TEXT NOT NULL,
  x_value REAL NOT NULL,
  y_input_id TEXT,
  y_value REAL,
  output_id TEXT NOT NULL,
  output_value REAL NOT NULL,
  unit TEXT NOT NULL,
  UNIQUE(run_id, sensitivity_id, x_value, y_value),
  FOREIGN KEY(run_id) REFERENCES model_runs(run_id)
);

CREATE TABLE IF NOT EXISTS model_validation_events (
  event_id TEXT PRIMARY KEY,
  package_id TEXT,
  run_id TEXT,
  severity TEXT NOT NULL,
  event_code TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  FOREIGN KEY(package_id) REFERENCES model_packages(package_id),
  FOREIGN KEY(run_id) REFERENCES model_runs(run_id)
);

CREATE VIEW IF NOT EXISTS v_model_reproducibility_trace AS
SELECT r.run_id, r.package_id, r.scenario_id, r.status, p.model_id,
       p.as_of_timestamp, i.input_id, i.input_kind, i.value_numeric AS input_value,
       i.unit AS input_unit, i.observation_id, i.evidence_id, i.content_label AS input_label,
       o.output_id, o.output_role, o.value_numeric AS output_value,
       o.unit AS output_unit, o.formula, o.lineage_json
FROM model_runs r
JOIN model_packages p ON p.package_id = r.package_id
LEFT JOIN model_inputs i ON i.run_id = r.run_id
LEFT JOIN model_outputs o ON o.run_id = r.run_id;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('002_consumer_research_model_engine', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
