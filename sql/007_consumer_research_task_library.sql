PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS task_library_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('industry','monitoring','company','event','model')),
    description TEXT NOT NULL,
    entity_requirement TEXT NOT NULL,
    parameter_schema_json TEXT NOT NULL,
    output_contract_json TEXT NOT NULL,
    default_priority TEXT NOT NULL,
    expected_minutes INTEGER NOT NULL,
    tags_json TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('internal_active','draft','deprecated')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_library_products (
    product_id TEXT PRIMARY KEY,
    task_package_id TEXT NOT NULL UNIQUE REFERENCES research_task_packages(task_package_id),
    template_id TEXT NOT NULL REFERENCES task_library_templates(template_id),
    sector_code TEXT NOT NULL REFERENCES taxonomy_nodes(node_code),
    title TEXT NOT NULL,
    short_description TEXT NOT NULL,
    research_question_template TEXT NOT NULL,
    metric_ids_json TEXT NOT NULL,
    source_routes_json TEXT NOT NULL,
    quality_gates_json TEXT NOT NULL,
    parameter_schema_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    search_text TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('internal_active','draft','deprecated')),
    visibility TEXT NOT NULL CHECK(visibility IN ('internal','restricted')),
    data_readiness TEXT NOT NULL,
    owner_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_library_product_versions (
    product_version_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES task_library_products(product_id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    release_status TEXT NOT NULL CHECK(release_status IN ('internal_active','draft','deprecated')),
    released_by TEXT NOT NULL,
    released_at TEXT NOT NULL,
    release_notes TEXT,
    UNIQUE(product_id,version)
);

CREATE TABLE IF NOT EXISTS task_library_role_permissions (
    role_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY(role_id,capability)
);

CREATE TABLE IF NOT EXISTS task_library_saved_views (
    view_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    filter_json TEXT NOT NULL,
    is_system INTEGER NOT NULL CHECK(is_system IN (0,1)),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_library_favorites (
    user_id TEXT NOT NULL,
    product_id TEXT NOT NULL REFERENCES task_library_products(product_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id,product_id)
);

CREATE TABLE IF NOT EXISTS task_library_jobs (
    job_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES task_library_products(product_id),
    submitted_by TEXT NOT NULL,
    submitter_role TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    cutoff_timestamp TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('low','normal','high','urgent')),
    status TEXT NOT NULL CHECK(status IN ('queued','validating','running','pending_human_review','completed','blocked','failed','cancelled')),
    data_readiness TEXT NOT NULL,
    workflow_request_path TEXT,
    workflow_run_id TEXT,
    result_artifact_path TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(product_id,submitted_by,request_hash)
);

CREATE TABLE IF NOT EXISTS task_library_job_events (
    job_event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES task_library_jobs(job_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_library_reviews (
    review_id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES task_library_products(product_id),
    product_version_id TEXT REFERENCES task_library_product_versions(product_version_id),
    review_type TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('pending','approved','changes_requested','rejected')),
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_library_usage_events (
    usage_event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    user_role TEXT NOT NULL,
    event_type TEXT NOT NULL,
    product_id TEXT REFERENCES task_library_products(product_id),
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS task_library_products_fts USING fts5(
    product_id UNINDEXED,
    title,
    short_description,
    search_text,
    tags,
    tokenize='unicode61'
);

CREATE INDEX IF NOT EXISTS idx_task_products_sector_template ON task_library_products(sector_code,template_id,status);
CREATE INDEX IF NOT EXISTS idx_task_jobs_status_priority ON task_library_jobs(status,priority,created_at);
CREATE INDEX IF NOT EXISTS idx_task_job_submitter ON task_library_jobs(submitted_by,created_at);

CREATE VIEW IF NOT EXISTS v_research_task_library AS
SELECT p.product_id,p.title,p.sector_code,p.template_id,t.name AS template_name,t.category,
       p.status,p.visibility,p.data_readiness,t.expected_minutes,p.updated_at
FROM task_library_products p
JOIN task_library_templates t ON t.template_id=p.template_id;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('007_consumer_research_task_library', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
