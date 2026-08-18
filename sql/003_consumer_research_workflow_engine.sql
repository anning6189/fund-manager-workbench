PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workflow_definitions (
    workflow_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    role_contracts_json TEXT NOT NULL,
    task_graph_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_packages (
    package_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflow_definitions(workflow_id),
    package_hash TEXT NOT NULL,
    cutoff_timestamp TEXT NOT NULL,
    template_id TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL REFERENCES workflow_packages(package_id),
    status TEXT NOT NULL CHECK(status IN (
        'planned','running','pending_human_review','completed','blocked','failed'
    )),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    resumed_count INTEGER NOT NULL DEFAULT 0,
    publication_status TEXT NOT NULL,
    human_review_required INTEGER NOT NULL DEFAULT 0,
    output_directory TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workflow_tasks (
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    wave_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending','running','completed','degraded','blocked','failed','skipped'
    )),
    required INTEGER NOT NULL DEFAULT 1,
    dependencies_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    output_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT,
    PRIMARY KEY(run_id, task_id)
);

CREATE TABLE IF NOT EXISTS workflow_claims (
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    claim_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    text TEXT NOT NULL,
    content_label TEXT NOT NULL,
    importance TEXT NOT NULL,
    confidence REAL NOT NULL,
    as_of_date TEXT NOT NULL,
    formula TEXT,
    input_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    PRIMARY KEY(run_id, claim_id)
);

CREATE TABLE IF NOT EXISTS workflow_claim_evidence (
    run_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('supporting','counter')),
    PRIMARY KEY(run_id, claim_id, evidence_id, relation_type),
    FOREIGN KEY(run_id, claim_id) REFERENCES workflow_claims(run_id, claim_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    artifact_type TEXT NOT NULL,
    path TEXT,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS workflow_reviews (
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    review_type TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('pending','approved','changes_requested','rejected')),
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    task_id TEXT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIEW IF NOT EXISTS v_workflow_run_audit AS
SELECT
    r.run_id,
    r.package_id,
    r.status AS run_status,
    r.publication_status,
    r.human_review_required,
    t.task_id,
    t.role_id,
    t.lane,
    t.wave_no,
    t.status AS task_status,
    t.attempt_count,
    t.started_at,
    t.completed_at
FROM workflow_runs r
JOIN workflow_tasks t ON t.run_id = r.run_id;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('003_consumer_research_workflow_engine', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
