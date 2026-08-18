PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS workbench_installations (
    installation_id TEXT PRIMARY KEY,
    installation_name TEXT NOT NULL,
    deployment_mode TEXT NOT NULL CHECK(deployment_mode IN ('local_single_user','internal_network')),
    bound_host TEXT NOT NULL,
    data_root TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workbench_sessions (
    session_id TEXT PRIMARY KEY,
    installation_id TEXT NOT NULL REFERENCES workbench_installations(installation_id),
    actor TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    client_scope TEXT NOT NULL DEFAULT 'loopback_only',
    status TEXT NOT NULL CHECK(status IN ('active','closed','failed'))
);

CREATE TABLE IF NOT EXISTS workbench_audit_events (
    audit_event_id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES workbench_sessions(session_id),
    actor TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failed')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workbench_report_annotations (
    annotation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    claim_id TEXT,
    section_name TEXT,
    author TEXT NOT NULL,
    author_role TEXT NOT NULL,
    note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    FOREIGN KEY(run_id,claim_id) REFERENCES workflow_claims(run_id,claim_id)
);

CREATE TABLE IF NOT EXISTS workbench_user_preferences (
    user_id TEXT NOT NULL,
    preference_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id,preference_key)
);

CREATE INDEX IF NOT EXISTS idx_workbench_audit_actor_time
ON workbench_audit_events(actor,occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_workbench_annotations_run_status
ON workbench_report_annotations(run_id,status,created_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_library_jobs_submitter_status_time
ON task_library_jobs(submitted_by,status,created_at DESC);

CREATE INDEX IF NOT EXISTS idx_monitor_alerts_state_severity_time
ON monitor_alerts(state,severity,last_detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_time
ON workflow_runs(status,started_at DESC);

PRAGMA optimize;
