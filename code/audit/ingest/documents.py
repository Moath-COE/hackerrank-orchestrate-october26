"""Ingest PDF and screenshot evidence through the cached model extraction.

PDF extractions are checked against the PDF's own text layer: an identifier the
model returned that does not literally occur in the document is dropped and
noted, so a hallucinated value can never link evidence to a person.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from audit.extraction import ExtractedRecord, extract_document, pdf_text
from audit.ingest.common import Observation, evidence_files, insert_observation
from audit.systems import system_for_resource

IDENTIFIER_FIELDS = ("resource_id", "name", "email", "phone", "email_hash", "user_ref")
# ACTIVE / DISABLED / BLOCKED describe processes and restore paths, not stored records.
PROCESS_STATES = {"ACTIVE", "DISABLED", "BLOCKED"}
PROCESS_SYSTEMS = {"integration_service", "backup_service"}


def _record_state(record: ExtractedRecord) -> str:
    state = record.state or "PRESENT"
    if state in PROCESS_STATES and system_for_resource(record.resource_id) not in PROCESS_SYSTEMS:
        return "PRESENT"
    return state


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _occurs(value: str, squashed_text: str, digits_text: str) -> bool:
    """Whitespace/case-insensitive containment; phones also match on digits alone."""
    if _squash(value) in squashed_text:
        return True
    digits = re.sub(r"\D", "", value)
    return len(digits) >= 8 and digits in digits_text


def _iso(value: str | None) -> str | None:
    """Normalize a model-supplied timestamp to the dataset's UTC 'Z' form; None if unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _verify_against_text(record: ExtractedRecord, text: str) -> list[str]:
    """Null out identifier fields not present in the source text; return the dropped field names."""
    squashed = _squash(text)
    digits = re.sub(r"\D", "", text)
    dropped = []
    for field in IDENTIFIER_FIELDS:
        value = getattr(record, field)
        if value and not _occurs(value, squashed, digits):
            setattr(record, field, None)
            dropped.append(field)
    return dropped


def load_document_file(
    conn: sqlite3.Connection, evidence_id: str, path: Path, captured_at: str, origin: str, allow_model: bool
) -> bool:
    document = extract_document(evidence_id, path, allow_model=allow_model)
    if document is None:
        return False
    source_text = pdf_text(path) if path.suffix.lower() == ".pdf" else None
    # An artifact shows each entity as it stood when the artifact was produced (its
    # "as of" time, else its capture time). A row's own timestamp is when that row
    # last changed, which says nothing about whether it still exists later.
    observed_at = min(_iso(document.shown_timestamp) or captured_at, captured_at)

    for position, record in enumerate(document.records):
        dropped = _verify_against_text(record, source_text) if source_text is not None else []
        attrs = {
            "record_time": _iso(record.observed_at),
            "document_title": document.title,
            "record_kind": record.record_kind,
            "status_text": record.status_text,
            "other_references": record.other_references,
            "retain_until": _iso(record.retain_until) or record.retain_until,
            "notes": record.notes,
            "extraction": "pdf_text" if source_text is not None else "vision",
        }
        if dropped:
            attrs["unverified_fields_dropped"] = dropped
        obs = Observation(
            evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at, observed_at=observed_at,
            system_id=record.system_hint or document.system_hint or origin, resource_id=record.resource_id,
            store=origin, record_type=None, state=_record_state(record),
            name=record.name, email=record.email, phone=record.phone, email_hash=record.email_hash,
            user_ref=record.user_ref, attrs=attrs, locator=f"{path.name}:records[{position}]",
        )
        insert_observation(conn, obs)
    return True


def load_documents(conn: sqlite3.Connection, dataset_dir: Path, allow_model: bool = True) -> list[str]:
    """Load every PDF/PNG extraction; return the file_ids skipped for lack of a cached extraction."""
    skipped = []
    rows = conn.execute("SELECT file_id, origin FROM evidence_files").fetchall()
    origins = {row["file_id"]: row["origin"] for row in rows}
    for file in evidence_files(conn, ".pdf", ".png"):
        loaded = load_document_file(
            conn, file["file_id"], dataset_dir / file["file_path"], file["captured_at"],
            origins[file["file_id"]], allow_model,
        )
        if not loaded:
            skipped.append(file["file_id"])
    return skipped
