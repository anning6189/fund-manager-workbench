PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS production_dataset_contracts (
    stream_name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    history_start TEXT,
    partition_strategy TEXT NOT NULL,
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    quality_gates_json TEXT NOT NULL DEFAULT '[]',
    service_level_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_license_decisions (
    source_id TEXT PRIMARY KEY REFERENCES source_catalog(source_id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','public_official','pending','rejected')),
    allowed_targets_json TEXT NOT NULL,
    redistribution_allowed INTEGER NOT NULL DEFAULT 0,
    cache_policy TEXT,
    decided_by TEXT,
    decided_at TEXT,
    notes TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
    stream_name TEXT NOT NULL REFERENCES production_dataset_contracts(stream_name),
    market TEXT,
    provider_as_of_date TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_reported_count INTEGER NOT NULL,
    projected_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    license_status TEXT NOT NULL,
    ingestion_target TEXT NOT NULL CHECK(ingestion_target IN ('raw_license_gate','curated','quarantine')),
    publication_allowed INTEGER NOT NULL DEFAULT 0,
    schema_status TEXT NOT NULL,
    quality_status TEXT NOT NULL,
    field_projection_json TEXT NOT NULL,
    discarded_fields_json TEXT NOT NULL DEFAULT '[]',
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backfill_runs (
    backfill_run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    cutoff_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','running','partially_complete','complete_with_external_gates','completed','blocked','failed')),
    mode TEXT NOT NULL CHECK(mode IN ('full','incremental','reconciliation')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS backfill_partitions (
    backfill_run_id TEXT NOT NULL REFERENCES backfill_runs(backfill_run_id) ON DELETE CASCADE,
    partition_id TEXT NOT NULL,
    stream_name TEXT NOT NULL REFERENCES production_dataset_contracts(stream_name),
    source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
    market TEXT,
    sector_code TEXT,
    period_start TEXT,
    period_end TEXT,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    target_layer TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'planned','ready','running','staged_raw_license_gate','completed','external_gate','blocked','failed','not_applicable'
    )),
    checkpoint TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    snapshot_id TEXT REFERENCES production_snapshots(snapshot_id),
    started_at TEXT,
    completed_at TEXT,
    error_json TEXT,
    PRIMARY KEY(backfill_run_id, partition_id)
);

CREATE TABLE IF NOT EXISTS production_quality_results (
    quality_result_id TEXT PRIMARY KEY,
    backfill_run_id TEXT REFERENCES backfill_runs(backfill_run_id) ON DELETE CASCADE,
    partition_id TEXT,
    snapshot_id TEXT REFERENCES production_snapshots(snapshot_id),
    gate_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('passed','warning','failed','blocked')),
    observed_value TEXT,
    expected_value TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_coverage_watermarks (
    stream_name TEXT NOT NULL REFERENCES production_dataset_contracts(stream_name),
    source_id TEXT NOT NULL REFERENCES source_catalog(source_id),
    market TEXT NOT NULL DEFAULT 'ALL',
    sector_code TEXT NOT NULL DEFAULT 'ALL',
    earliest_period TEXT,
    latest_period TEXT,
    record_count INTEGER NOT NULL DEFAULT 0,
    completeness_status TEXT NOT NULL,
    license_gate TEXT,
    last_snapshot_id TEXT REFERENCES production_snapshots(snapshot_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(stream_name,source_id,market,sector_code)
);

CREATE TABLE IF NOT EXISTS snapshot_promotion_events (
    promotion_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES production_snapshots(snapshot_id),
    from_layer TEXT NOT NULL,
    to_layer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved','blocked','rejected')),
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_backfill_partition_status
ON backfill_partitions(backfill_run_id,status,stream_name);

CREATE INDEX IF NOT EXISTS idx_snapshot_stream_date
ON production_snapshots(stream_name,provider_as_of_date,source_id);

CREATE VIEW IF NOT EXISTS v_production_backfill_audit AS
SELECT r.backfill_run_id,r.cutoff_date,r.status AS run_status,
       p.partition_id,p.stream_name,p.source_id,p.market,p.sector_code,
       p.period_start,p.period_end,p.target_layer,p.status AS partition_status,
       p.record_count,p.checkpoint,p.snapshot_id
FROM backfill_runs r
JOIN backfill_partitions p ON p.backfill_run_id=r.backfill_run_id;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('004_consumer_data_production', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
