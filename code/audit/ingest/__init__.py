"""One-time ingestion: normalize every participant file into the audit database."""

import sqlite3
from pathlib import Path

from audit.db import create_fresh
from audit.ingest.activity import load_activity
from audit.ingest.documents import load_documents
from audit.ingest.identities import build_identifiers
from audit.ingest.json_records import load_json_records
from audit.ingest.logs import load_logs
from audit.ingest.sql_snapshots import load_sql_snapshots
from audit.ingest.tables import load_tables
from audit.ingest.text_records import load_text_records


def run_ingest(dataset_dir: Path, db_path: Path, allow_model: bool = True) -> tuple[sqlite3.Connection, list[str]]:
    """Rebuild the database; returns it plus any PDF/PNG file_ids skipped for lack of an extraction."""
    conn = create_fresh(db_path)
    with conn:
        load_tables(conn, dataset_dir)
        build_identifiers(conn)
        load_sql_snapshots(conn, dataset_dir)
        load_json_records(conn, dataset_dir)
        load_logs(conn, dataset_dir)
        load_text_records(conn, dataset_dir)
        skipped = load_documents(conn, dataset_dir, allow_model=allow_model)
        load_activity(conn)
    return conn, skipped
