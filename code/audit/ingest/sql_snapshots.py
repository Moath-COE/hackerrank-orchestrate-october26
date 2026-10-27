"""Ingest SQLite-compatible snapshot exports.

Each file is executed into a scratch in-memory database and introspected. Three
table families exist:
  * typed inventory tables (customer_profiles, support_records, ...) scoped by a
    snapshot_scope table that names each covered system and its capture time;
  * generic customer_data_<copy> tables holding one JSON record per row, where
    the primary and replica copies of the same store can drift apart;
  * typed primary/replica pairs (customer_profiles_primary / _failover_replica).
Every table in a snapshot lists all of its rows, so each one is recorded as
coverage: a resource missing from a covered table was absent at capture time.
"""

import json
import re
import sqlite3
from pathlib import Path

from audit.ingest.common import (
    Observation, clean, evidence_files, infer_state, insert_coverage, insert_observation,
)

# Typed table -> (system_id, collection). Copy role comes from the table suffix.
TYPED_TABLES = {
    "customer_profiles": ("account_service", "customer_profiles"),
    "customer_search_documents": ("search_service", "customer_search_documents"),
    "customer_analytics_profiles": ("analytics_platform", "customer_analytics_profiles"),
    "support_records": ("support_platform", "support_records"),
    "customer_exports": ("export_service", "customer_exports"),
    "connector_subscriptions": ("integration_service", "connector_subscriptions"),
    "backup_restore_entries": ("backup_service", "backup_restore_entries"),
    "security_audit_records": ("security_service", "security_audit_records"),
    "search_documents": ("search_service", "customer_search_documents"),
}
GENERIC_PREFIX = "customer_data_"
CAPTURED_RE = re.compile(r"^--\s*(?:Captured at:|snapshot_completed_at=)\s*(\S+)", re.MULTILINE)


def _split_table(table: str) -> tuple[str, str]:
    """'customer_profiles_failover_replica' -> ('customer_profiles', 'failover_replica')."""
    for base in sorted(TYPED_TABLES, key=len, reverse=True):
        if table == base:
            return base, "primary"
        if table.startswith(base + "_"):
            return base, table[len(base) + 1:]
    if table.startswith(GENERIC_PREFIX):
        return GENERIC_PREFIX.rstrip("_"), table[len(GENERIC_PREFIX):]
    raise ValueError(f"unrecognized snapshot table {table!r}")


def load_sql_file(conn: sqlite3.Connection, evidence_id: str, path: Path, manifest_captured_at: str) -> None:
    text = path.read_text(encoding="utf-8")
    header_match = CAPTURED_RE.search(text)
    captured_at = header_match.group(1) if header_match else manifest_captured_at

    scratch = sqlite3.connect(":memory:")
    scratch.row_factory = sqlite3.Row
    scratch.executescript(text)
    tables = [r[0] for r in scratch.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    scope: dict[str, str] = {}
    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: r["captured_at"] for r in scratch.execute("SELECT * FROM snapshot_scope")}
        tables.remove("snapshot_scope")

    generic_systems: set[str] = set()
    for table in tables:
        if table.startswith(GENERIC_PREFIX):
            generic_systems |= {r[0] for r in scratch.execute(f'SELECT DISTINCT origin_system FROM "{table}"')}

    for table in tables:
        base, copy_role = _split_table(table)
        rows = scratch.execute(f'SELECT * FROM "{table}" ORDER BY resource_id').fetchall()

        if table.startswith(GENERIC_PREFIX):
            for system_id in sorted(generic_systems):
                insert_coverage(conn, evidence_id, captured_at, system_id, table, copy_role)
            for row in rows:
                record = json.loads(row["record_json"])
                obs = Observation(
                    evidence_id=evidence_id, evidence_kind="file", captured_at=manifest_captured_at, observed_at=captured_at,
                    system_id=row["origin_system"], resource_id=row["resource_id"], store=table,
                    collection=row["collection_name"], copy_role=copy_role, state=infer_state(record),
                    attrs={"updated_at": row["updated_at"]}, locator=f"{table}:{row['resource_id']}",
                )
                insert_observation(conn, obs.fill_from_record(record))
            continue

        system_id, collection = TYPED_TABLES[base]
        observed_at = scope.get(system_id, captured_at)
        if scope and system_id not in scope:
            continue  # inventory table for a system the snapshot does not claim to cover
        insert_coverage(conn, evidence_id, observed_at, system_id, table, copy_role)
        for row in rows:
            record = {key: clean(row[key]) for key in row.keys()}
            obs = Observation(
                evidence_id=evidence_id, evidence_kind="file", captured_at=manifest_captured_at, observed_at=observed_at,
                system_id=system_id, resource_id=record.pop("resource_id"), store=table,
                collection=collection, copy_role=copy_role, state=infer_state(record),
                locator=f"{table}:{row['resource_id']}",
            )
            insert_observation(conn, obs.fill_from_record(record))

    # Inventory systems in scope with no table rows at all still prove absence for that system.
    covered = {r[0] for r in conn.execute("SELECT system_id FROM coverage WHERE evidence_id = ?", (evidence_id,))}
    for system_id, observed_at in sorted(scope.items()):
        if system_id not in covered:
            insert_coverage(conn, evidence_id, observed_at, system_id, "snapshot_scope", "primary")
    scratch.close()


def load_sql_snapshots(conn: sqlite3.Connection, dataset_dir: Path) -> int:
    files = evidence_files(conn, ".sql")
    for file in files:
        load_sql_file(conn, file["file_id"], dataset_dir / file["file_path"], file["captured_at"])
    return len(files)
