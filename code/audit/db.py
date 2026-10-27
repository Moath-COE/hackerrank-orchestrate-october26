"""SQLite schema and connection helpers.

Three layers:
  1. raw tables  — participant CSVs loaded verbatim;
  2. identifiers — every current and historical identifier per user, normalized;
  3. evidence    — observations (one sighting of a resource at a time), the
                   identifiers found in each, and coverage (which sources list
                   an entire store, so a missing row proves absence).
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE requests (
    case_id TEXT PRIMARY KEY,
    split TEXT NOT NULL,              -- eval | sample
    email TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    reported_status TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    audit_at TEXT NOT NULL,
    request_text TEXT NOT NULL
);
CREATE TABLE sample_output (
    case_id TEXT PRIMARY KEY,
    deletion_status TEXT NOT NULL,
    steps_json TEXT NOT NULL
);
CREATE TABLE identities (
    user_id TEXT PRIMARY KEY,
    name TEXT, email TEXT, phone TEXT, email_hash TEXT,
    updated_at TEXT
);
CREATE TABLE identity_audit (
    audit_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT, new_value TEXT
);
CREATE TABLE activity (
    activity_id TEXT PRIMARY KEY,
    event_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source_system TEXT, target_system TEXT,
    resource_id TEXT,
    reported_result TEXT,
    details TEXT
);
CREATE TABLE rules (
    rule_id TEXT PRIMARY KEY,
    data_category TEXT NOT NULL,
    condition TEXT NOT NULL,
    required_action TEXT NOT NULL
);
CREATE TABLE evidence_files (
    file_id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    origin TEXT,
    captured_at TEXT NOT NULL
);

CREATE TABLE identifiers (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,               -- user_id | email | email_hash | phone | name
    value TEXT NOT NULL,              -- normalized
    raw_value TEXT NOT NULL,
    source TEXT NOT NULL,             -- current | derived | <audit_id>
    valid_from TEXT,                  -- NULL = unknown / since forever
    valid_to TEXT                     -- NULL = still current
);
CREATE INDEX identifiers_lookup ON identifiers (kind, value);
CREATE INDEX identifiers_user ON identifiers (user_id);

CREATE TABLE observations (
    obs_id INTEGER PRIMARY KEY,
    evidence_id TEXT NOT NULL,        -- file_id | activity_id | audit_id
    evidence_kind TEXT NOT NULL,      -- file | activity | audit
    locator TEXT,                     -- table / line / JSON path, for debugging
    captured_at TEXT NOT NULL,        -- when the evidence item existed (citable at or after this)
    observed_at TEXT NOT NULL,        -- when the observed fact held
    system_id TEXT,                   -- owning system, from the resource-id prefix when known
    resource_id TEXT,
    store TEXT,                       -- physical table, log, or export section
    collection TEXT,                  -- logical collection name when stated
    copy_role TEXT,                   -- primary | read_replica | replica_eu_west | ...
    record_type TEXT,                 -- data category when the source states it
    state TEXT,                       -- PRESENT | ABSENT | REDACTED | ANONYMIZED | ACTIVE |
                                      -- DISABLED | BLOCKED | NULL (claim only)
    name TEXT, email TEXT, phone TEXT, email_hash TEXT, user_ref TEXT,
    attrs_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX observations_resource ON observations (resource_id, observed_at);
CREATE INDEX observations_evidence ON observations (evidence_id);

CREATE TABLE obs_identifiers (
    obs_id INTEGER NOT NULL REFERENCES observations (obs_id),
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (obs_id, kind, value)
);
CREATE INDEX obs_identifiers_lookup ON obs_identifiers (kind, value);

CREATE TABLE coverage (
    evidence_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    system_id TEXT NOT NULL,
    store TEXT NOT NULL,
    copy_role TEXT
);
CREATE INDEX coverage_system ON coverage (system_id, observed_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def create_fresh(path: Path) -> sqlite3.Connection:
    """Delete any previous database and create an empty one with the schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn
