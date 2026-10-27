"""Load the participant CSVs into raw tables, unchanged."""

import csv
import sqlite3
from pathlib import Path

# (csv file, table, columns); requests come from two files and get a split label.
CSV_TABLES = [
    ("user_identities.csv", "identities", ("user_id", "name", "email", "phone", "email_hash", "updated_at")),
    ("user_audit_log.csv", "identity_audit", ("audit_id", "user_id", "event_time", "field_name", "old_value", "new_value")),
    ("system_activity.csv", "activity", ("activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id", "reported_result", "details")),
    ("deletion_rules.csv", "rules", ("rule_id", "data_category", "condition", "required_action")),
    ("evidence_files.csv", "evidence_files", ("file_id", "file_path", "origin", "captured_at")),
    ("sample_output.csv", "sample_output", ("case_id", "deletion_status", "steps_json")),
]
REQUEST_FILES = [("deletion_requests.csv", "eval"), ("sample_deletion_requests.csv", "sample")]
REQUEST_COLUMNS = ("case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _insert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], rows: list[tuple]) -> None:
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", rows)


def load_tables(conn: sqlite3.Connection, dataset_dir: Path) -> None:
    for filename, table, columns in CSV_TABLES:
        rows = read_csv(dataset_dir / filename)
        _insert(conn, table, columns, [tuple(row[c] for c in columns) for row in rows])
    for filename, split in REQUEST_FILES:
        rows = read_csv(dataset_dir / filename)
        _insert(
            conn,
            "requests",
            ("split", *REQUEST_COLUMNS),
            [(split, *(row[c] for c in REQUEST_COLUMNS)) for row in rows],
        )
