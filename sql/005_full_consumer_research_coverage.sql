PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS research_sector_packs (
    sector_code TEXT PRIMARY KEY REFERENCES taxonomy_nodes(node_code),
    sector_name TEXT NOT NULL,
    parent_domain TEXT NOT NULL,
    research_thesis TEXT NOT NULL,
    cycle_drivers_json TEXT NOT NULL,
    value_chain_json TEXT NOT NULL,
    metric_ids_json TEXT NOT NULL,
    required_streams_json TEXT NOT NULL,
    template_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taxonomy_vendor_mappings (
    mapping_id TEXT PRIMARY KEY,
    vendor_scheme TEXT NOT NULL,
    vendor_l1 TEXT,
    vendor_l2 TEXT,
    vendor_l3 TEXT,
    sector_code TEXT NOT NULL REFERENCES taxonomy_nodes(node_code),
    mapping_type TEXT NOT NULL CHECK(mapping_type IN ('exact','rule','review_required')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    review_status TEXT NOT NULL,
    rule_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS research_universe_snapshots (
    universe_snapshot_id TEXT PRIMARY KEY,
    source_snapshot_ids_json TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    market TEXT NOT NULL,
    security_count INTEGER NOT NULL,
    mapped_security_count INTEGER NOT NULL,
    review_required_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_universe_members (
    membership_id TEXT PRIMARY KEY,
    universe_snapshot_id TEXT NOT NULL REFERENCES research_universe_snapshots(universe_snapshot_id) ON DELETE CASCADE,
    security_id TEXT NOT NULL,
    security_code TEXT NOT NULL,
    security_name TEXT NOT NULL,
    market_mic TEXT NOT NULL,
    trading_status TEXT NOT NULL,
    vendor_industry_l1 TEXT,
    vendor_industry_l2 TEXT,
    vendor_industry_l3 TEXT,
    sector_code TEXT REFERENCES taxonomy_nodes(node_code),
    mapping_status TEXT NOT NULL,
    mapping_confidence REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_research_universe_security
ON research_universe_members(universe_snapshot_id, security_id);

CREATE TABLE IF NOT EXISTS research_coverage_status (
    sector_code TEXT NOT NULL REFERENCES taxonomy_nodes(node_code),
    market TEXT NOT NULL,
    security_count INTEGER NOT NULL DEFAULT 0,
    metric_definition_count INTEGER NOT NULL DEFAULT 0,
    required_stream_count INTEGER NOT NULL DEFAULT 0,
    populated_stream_count INTEGER NOT NULL DEFAULT 0,
    metric_population_status TEXT NOT NULL,
    universe_status TEXT NOT NULL,
    research_pack_status TEXT NOT NULL,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    as_of_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(sector_code,market)
);

CREATE TABLE IF NOT EXISTS research_task_packages (
    task_package_id TEXT PRIMARY KEY,
    sector_code TEXT NOT NULL REFERENCES taxonomy_nodes(node_code),
    template_id TEXT NOT NULL,
    cutoff_timestamp TEXT NOT NULL,
    research_question TEXT NOT NULL,
    metric_queries_json TEXT NOT NULL,
    source_routes_json TEXT NOT NULL,
    quality_gates_json TEXT NOT NULL,
    status TEXT NOT NULL,
    package_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS v_full_consumer_coverage AS
SELECT p.sector_code,p.sector_name,p.parent_domain,p.status AS pack_status,
       c.market,c.security_count,c.metric_definition_count,
       c.required_stream_count,c.populated_stream_count,c.metric_population_status,
       c.universe_status,c.research_pack_status,c.blockers_json,c.as_of_date
FROM research_sector_packs p
LEFT JOIN research_coverage_status c ON c.sector_code=p.sector_code;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('005_full_consumer_research_coverage', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
