"""Ingest plain-text handover records.

Check lines look like:
  - 2026-10-01T12:00:00Z | account_service | account_146 | USER_PROFILE | NOT_FOUND | e.lindberg18@yahoo.com
"""

import sqlite3
from pathlib import Path

from audit.ingest.common import Observation, evidence_files, infer_state, insert_observation


def load_text_file(conn: sqlite3.Connection, evidence_id: str, path: Path, captured_at: str) -> None:
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.startswith("- "):
            continue
        parts = [part.strip() for part in line[2:].split("|")]
        if len(parts) != 6:
            continue
        observed_at, system_id, resource_id, record_type, result, subject = parts
        obs = Observation(
            evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at, observed_at=observed_at,
            system_id=system_id, resource_id=resource_id, store="handover", record_type=record_type,
            state=infer_state({"result": result}), locator=f"{path.name}:{line_no}",
        )
        insert_observation(conn, obs.fill_from_record({"result": result, "subject_reference": subject}))


def load_text_records(conn: sqlite3.Connection, dataset_dir: Path) -> int:
    files = evidence_files(conn, ".txt")
    for file in files:
        load_text_file(conn, file["file_id"], dataset_dir / file["file_path"], file["captured_at"])
    return len(files)
