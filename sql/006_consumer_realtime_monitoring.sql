PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS monitor_rules (
    rule_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    condition_json TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info','watch','important','critical')),
    cooldown_minutes INTEGER NOT NULL,
    task_template_json TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    subscription_name TEXT NOT NULL,
    subscriber_type TEXT NOT NULL,
    subscriber_id TEXT NOT NULL,
    sector_code TEXT REFERENCES taxonomy_nodes(node_code),
    event_types_json TEXT NOT NULL,
    minimum_severity TEXT NOT NULL,
    delivery_channels_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_runs (
    monitor_run_id TEXT PRIMARY KEY,
    cutoff_timestamp TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('scheduled','manual','event')),
    status TEXT NOT NULL CHECK(status IN ('running','completed','partially_complete','blocked','failed')),
    rules_evaluated INTEGER NOT NULL DEFAULT 0,
    signals_evaluated INTEGER NOT NULL DEFAULT 0,
    alerts_created INTEGER NOT NULL DEFAULT 0,
    alerts_deduplicated INTEGER NOT NULL DEFAULT 0,
    triggered_tasks INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS monitor_events (
    monitor_event_id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES source_catalog(source_id),
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    available_at TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    sector_code TEXT REFERENCES taxonomy_nodes(node_code),
    entity_id TEXT REFERENCES entities(entity_id),
    security_id TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    materiality_score REAL NOT NULL CHECK(materiality_score >= 0 AND materiality_score <= 1),
    source_url TEXT,
    locator TEXT,
    content_hash TEXT NOT NULL,
    license_status TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('accepted','license_gated','quarantined')),
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_alerts (
    alert_id TEXT PRIMARY KEY,
    deduplication_key TEXT NOT NULL UNIQUE,
    rule_code TEXT NOT NULL REFERENCES monitor_rules(rule_code),
    monitor_run_id TEXT NOT NULL REFERENCES monitor_runs(monitor_run_id),
    monitor_event_id TEXT REFERENCES monitor_events(monitor_event_id),
    sector_code TEXT REFERENCES taxonomy_nodes(node_code),
    entity_id TEXT REFERENCES entities(entity_id),
    severity TEXT NOT NULL CHECK(severity IN ('info','watch','important','critical')),
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL CHECK(state IN ('open','acknowledged','resolved','suppressed')),
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    resolution_reason TEXT,
    publication_status TEXT NOT NULL DEFAULT 'internal_only'
);

CREATE TABLE IF NOT EXISTS monitor_alert_events (
    alert_event_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES monitor_alerts(alert_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS monitor_task_triggers (
    trigger_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES monitor_alerts(alert_id),
    research_task_package_id TEXT REFERENCES research_task_packages(task_package_id),
    sector_code TEXT REFERENCES taxonomy_nodes(node_code),
    template_id TEXT,
    trigger_reason TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('low','normal','high','urgent')),
    status TEXT NOT NULL CHECK(status IN ('suggested','queued','dismissed','started','completed')),
    automatic_execution INTEGER NOT NULL DEFAULT 0 CHECK(automatic_execution IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_delivery_outbox (
    delivery_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES monitor_alerts(alert_id),
    subscription_id TEXT NOT NULL REFERENCES monitor_subscriptions(subscription_id),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed','suppressed')),
    external_delivery INTEGER NOT NULL DEFAULT 0 CHECK(external_delivery IN (0,1)),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS monitor_briefs (
    brief_id TEXT PRIMARY KEY,
    brief_date TEXT NOT NULL,
    cutoff_timestamp TEXT NOT NULL,
    monitor_run_id TEXT NOT NULL REFERENCES monitor_runs(monitor_run_id),
    status TEXT NOT NULL,
    alert_count INTEGER NOT NULL,
    task_suggestion_count INTEGER NOT NULL,
    artifact_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitor_alert_state ON monitor_alerts(state,severity,last_detected_at);
CREATE INDEX IF NOT EXISTS idx_monitor_event_time ON monitor_events(event_type,available_at);
CREATE INDEX IF NOT EXISTS idx_monitor_task_status ON monitor_task_triggers(status,priority,created_at);

CREATE VIEW IF NOT EXISTS v_realtime_research_alerts AS
SELECT a.alert_id,a.rule_code,a.sector_code,a.entity_id,a.severity,a.title,a.state,
       a.first_detected_at,a.last_detected_at,a.occurrence_count,a.publication_status,
       t.template_id,t.priority,t.status AS task_trigger_status
FROM monitor_alerts a
LEFT JOIN monitor_task_triggers t ON t.alert_id=a.alert_id;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('006_consumer_realtime_monitoring', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
