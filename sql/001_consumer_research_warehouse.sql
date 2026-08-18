PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_catalog (
  source_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source_family TEXT NOT NULL,
  evidence_tier TEXT NOT NULL,
  license_status TEXT NOT NULL,
  access_class TEXT NOT NULL,
  status TEXT NOT NULL,
  point_in_time_support TEXT,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_packages (
  package_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  license_tag TEXT NOT NULL,
  gate_status TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  stored_at TEXT NOT NULL,
  FOREIGN KEY(source_id) REFERENCES source_catalog(source_id)
);

CREATE TABLE IF NOT EXISTS metric_definitions (
  metric_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  definition TEXT NOT NULL,
  unit TEXT NOT NULL,
  frequency TEXT NOT NULL,
  grain TEXT NOT NULL,
  time_semantics TEXT NOT NULL,
  preferred_source_tier TEXT NOT NULL,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS taxonomy_nodes (
  node_code TEXT PRIMARY KEY,
  parent_code TEXT,
  name TEXT NOT NULL,
  level INTEGER NOT NULL,
  status TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  FOREIGN KEY(parent_code) REFERENCES taxonomy_nodes(node_code)
);

CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  jurisdiction TEXT,
  status TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  attributes_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(entity_type, canonical_name);

CREATE TABLE IF NOT EXISTS entity_aliases (
  entity_id TEXT NOT NULL,
  alias TEXT NOT NULL COLLATE NOCASE,
  language TEXT NOT NULL DEFAULT 'zh-CN',
  alias_type TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  source_id TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
  review_status TEXT NOT NULL,
  PRIMARY KEY(entity_id, alias, source_id),
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_alias_lookup ON entity_aliases(alias);

CREATE TABLE IF NOT EXISTS external_identifiers (
  entity_id TEXT NOT NULL,
  id_type TEXT NOT NULL,
  issuer TEXT NOT NULL,
  value TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
  PRIMARY KEY(id_type, issuer, value),
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
);

CREATE TABLE IF NOT EXISTS relationships (
  relationship_id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_id TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  observed_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  review_status TEXT NOT NULL,
  attributes_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(subject_id) REFERENCES entities(entity_id),
  FOREIGN KEY(object_id) REFERENCES entities(entity_id)
);

CREATE INDEX IF NOT EXISTS idx_relationship_subject ON relationships(subject_id, predicate);
CREATE INDEX IF NOT EXISTS idx_relationship_object ON relationships(object_id, predicate);

CREATE TABLE IF NOT EXISTS entity_classifications (
  assignment_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  node_code TEXT NOT NULL,
  assignment_type TEXT NOT NULL,
  exposure_ratio REAL,
  valid_from TEXT,
  valid_to TEXT,
  observed_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  review_status TEXT NOT NULL,
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id),
  FOREIGN KEY(node_code) REFERENCES taxonomy_nodes(node_code)
);

CREATE INDEX IF NOT EXISTS idx_classification_node ON entity_classifications(node_code, assignment_type);

CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  source_record_id TEXT,
  document_type TEXT NOT NULL,
  title TEXT NOT NULL,
  publisher TEXT NOT NULL,
  source_url TEXT,
  local_object_path TEXT,
  published_at TEXT NOT NULL,
  available_at TEXT NOT NULL,
  as_of_date TEXT,
  retrieved_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  mime_type TEXT,
  language TEXT NOT NULL DEFAULT 'zh-CN',
  document_version TEXT NOT NULL,
  license_tag TEXT NOT NULL,
  access_class TEXT NOT NULL,
  evidence_tier TEXT NOT NULL,
  status TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_id, source_record_id, document_version),
  FOREIGN KEY(source_id) REFERENCES source_catalog(source_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_available ON documents(available_at, document_type);

CREATE TABLE IF NOT EXISTS document_entities (
  document_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  review_status TEXT NOT NULL,
  PRIMARY KEY(document_id, entity_id, relation_type),
  FOREIGN KEY(document_id) REFERENCES documents(document_id),
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id)
);

CREATE TABLE IF NOT EXISTS document_chunks (
  chunk_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  sequence_no INTEGER NOT NULL,
  page_start INTEGER,
  page_end INTEGER,
  section_path TEXT,
  chunk_type TEXT NOT NULL,
  locator TEXT NOT NULL,
  text_content TEXT NOT NULL,
  table_json TEXT,
  token_count INTEGER,
  content_hash TEXT NOT NULL,
  available_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(document_id, sequence_no),
  FOREIGN KEY(document_id) REFERENCES documents(document_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id, sequence_no);

CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
  chunk_id UNINDEXED,
  document_id UNINDEXED,
  title,
  section_path,
  text_content,
  tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS document_chunks_ai AFTER INSERT ON document_chunks BEGIN
  INSERT INTO document_chunks_fts(chunk_id, document_id, title, section_path, text_content)
  SELECT new.chunk_id, new.document_id, d.title, COALESCE(new.section_path, ''), new.text_content
  FROM documents d WHERE d.document_id = new.document_id;
END;

CREATE TRIGGER IF NOT EXISTS document_chunks_ad AFTER DELETE ON document_chunks BEGIN
  DELETE FROM document_chunks_fts WHERE chunk_id = old.chunk_id;
END;

CREATE TRIGGER IF NOT EXISTS document_chunks_au AFTER UPDATE ON document_chunks BEGIN
  DELETE FROM document_chunks_fts WHERE chunk_id = old.chunk_id;
  INSERT INTO document_chunks_fts(chunk_id, document_id, title, section_path, text_content)
  SELECT new.chunk_id, new.document_id, d.title, COALESCE(new.section_path, ''), new.text_content
  FROM documents d WHERE d.document_id = new.document_id;
END;

CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  chunk_id TEXT,
  source_id TEXT NOT NULL,
  locator TEXT NOT NULL,
  support_type TEXT NOT NULL,
  evidence_tier TEXT NOT NULL,
  published_at TEXT NOT NULL,
  available_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  license_tag TEXT NOT NULL,
  access_class TEXT NOT NULL,
  FOREIGN KEY(document_id) REFERENCES documents(document_id),
  FOREIGN KEY(chunk_id) REFERENCES document_chunks(chunk_id),
  FOREIGN KEY(source_id) REFERENCES source_catalog(source_id)
);

CREATE TABLE IF NOT EXISTS observations (
  observation_id TEXT PRIMARY KEY,
  metric_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  security_id TEXT,
  value_numeric REAL,
  value_text TEXT,
  unit TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  as_of_date TEXT NOT NULL,
  observed_at TEXT,
  published_at TEXT NOT NULL,
  available_at TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL,
  value_status TEXT NOT NULL,
  statement_scope TEXT,
  consolidation_scope TEXT,
  accounting_standard TEXT,
  currency TEXT,
  scale REAL,
  restatement_status TEXT NOT NULL,
  fiscal_period_type TEXT,
  version_no INTEGER NOT NULL,
  is_current INTEGER NOT NULL CHECK(is_current IN (0,1)),
  supersedes_observation_id TEXT,
  quality_status TEXT NOT NULL,
  attributes_json TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(metric_id) REFERENCES metric_definitions(metric_id),
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id),
  FOREIGN KEY(evidence_id) REFERENCES evidence(evidence_id),
  FOREIGN KEY(supersedes_observation_id) REFERENCES observations(observation_id)
);

CREATE INDEX IF NOT EXISTS idx_observation_query ON observations(entity_id, metric_id, period_end, available_at, is_current);
CREATE INDEX IF NOT EXISTS idx_observation_cutoff ON observations(available_at, as_of_date);

CREATE TABLE IF NOT EXISTS ingestion_runs (
  run_id TEXT PRIMARY KEY,
  package_id TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  status TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  inserted_records INTEGER NOT NULL DEFAULT 0,
  updated_records INTEGER NOT NULL DEFAULT 0,
  rejected_records INTEGER NOT NULL DEFAULT 0,
  error_summary TEXT,
  FOREIGN KEY(source_id) REFERENCES source_catalog(source_id)
);

CREATE TABLE IF NOT EXISTS ingestion_errors (
  error_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  package_id TEXT NOT NULL,
  record_type TEXT,
  record_id TEXT,
  error_code TEXT NOT NULL,
  message TEXT NOT NULL,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES ingestion_runs(run_id)
);

CREATE TABLE IF NOT EXISTS source_cursors (
  source_id TEXT NOT NULL,
  stream_name TEXT NOT NULL,
  cursor_value TEXT,
  watermark_available_at TEXT,
  last_success_at TEXT,
  next_due_at TEXT,
  status TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(source_id, stream_name),
  FOREIGN KEY(source_id) REFERENCES source_catalog(source_id)
);

CREATE TABLE IF NOT EXISTS freshness_state (
  source_id TEXT NOT NULL,
  stream_name TEXT NOT NULL,
  expected_max_lag_hours REAL NOT NULL,
  latest_available_at TEXT,
  checked_at TEXT NOT NULL,
  lag_hours REAL,
  status TEXT NOT NULL,
  PRIMARY KEY(source_id, stream_name),
  FOREIGN KEY(source_id) REFERENCES source_catalog(source_id)
);

CREATE TABLE IF NOT EXISTS quality_events (
  event_id TEXT PRIMARY KEY,
  severity TEXT NOT NULL,
  event_type TEXT NOT NULL,
  entity_id TEXT,
  metric_id TEXT,
  document_id TEXT,
  detected_at TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL,
  FOREIGN KEY(entity_id) REFERENCES entities(entity_id),
  FOREIGN KEY(metric_id) REFERENCES metric_definitions(metric_id),
  FOREIGN KEY(document_id) REFERENCES documents(document_id)
);

CREATE VIEW IF NOT EXISTS v_point_in_time_observations AS
SELECT o.*
FROM observations o
WHERE o.quality_status = 'curated';

CREATE VIEW IF NOT EXISTS v_evidence_trace AS
SELECT e.evidence_id, e.locator, e.evidence_tier, e.support_type,
       d.document_id, d.title, d.publisher, d.source_url, d.published_at,
       c.chunk_id, c.section_path, c.text_content
FROM evidence e
JOIN documents d ON d.document_id = e.document_id
LEFT JOIN document_chunks c ON c.chunk_id = e.chunk_id;

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('001_consumer_research_warehouse', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
