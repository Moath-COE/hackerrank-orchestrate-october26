"""Ingest JSON evidence records.

Four shapes occur:
  * snapshot bundles — {"snapshots": [{captured_at, data_store, collection, records}]};
    each section lists a whole collection at its capture time, so it is coverage
    (an empty customer_profiles section proves the profile is gone), except
    operational_record_history sections, which are lookup ledgers;
  * service exports — {"service_exports": {system: {section: [records]}}}, point
    lookups each with their own observed_at;
  * lookup ledgers — {"records": [{recorded_at, system, resource_id, record_type,
    result, subject_reference}]};
  * single collections — {captured_at, collection, records}.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from audit.ingest.common import Observation, evidence_files, infer_state, insert_coverage, insert_observation

LEDGER_COLLECTIONS = {"operational_record_history"}


def _ledger_observation(evidence_id: str, captured_at: str, record: dict[str, Any], locator: str) -> Observation:
    """A {recorded_at, system, resource_id, record_type, result, <identifier>} lookup entry."""
    record = dict(record)
    obs = Observation(
        evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at,
        observed_at=record.pop("recorded_at"), system_id=record.pop("system"),
        resource_id=record.pop("resource_id"), store="lookup_ledger",
        record_type=record.pop("record_type"), state=infer_state(record), locator=locator,
    )
    return obs.fill_from_record(record)


def _load_bundle(conn: sqlite3.Connection, evidence_id: str, captured_at: str, data: dict) -> None:
    for index, section in enumerate(data["snapshots"]):
        collection = section["collection"]
        store = f"{section['data_store']}.{collection}"
        if collection not in LEDGER_COLLECTIONS:
            insert_coverage(conn, evidence_id, section["captured_at"], section["data_store"], store, "snapshot")
        for position, record in enumerate(section["records"]):
            locator = f"snapshots[{index}].records[{position}]"
            if collection in LEDGER_COLLECTIONS:
                insert_observation(conn, _ledger_observation(evidence_id, captured_at, record, locator))
                continue
            record = dict(record)
            obs = Observation(
                evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at,
                observed_at=section["captured_at"], system_id=section["data_store"],
                resource_id=record.pop("resource_id"), store=store, collection=collection,
                copy_role="snapshot", state=infer_state(record), locator=locator,
            )
            insert_observation(conn, obs.fill_from_record(record))


def _load_service_export(conn: sqlite3.Connection, evidence_id: str, captured_at: str, data: dict) -> None:
    for system_id, sections in data["service_exports"].items():
        for section, records in sections.items():
            for position, record in enumerate(records):
                record = dict(record)
                hits = record.pop("hits", None)
                if hits is not None:
                    record["total_hits"] = hits.get("total")
                    record["documents"] = hits.get("documents", [])
                obs = Observation(
                    evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at,
                    observed_at=record.pop("observed_at"), system_id=system_id,
                    resource_id=record.pop("resource_id"), store=f"{system_id}.{section}",
                    collection=section, state=infer_state(record),
                    locator=f"service_exports.{system_id}.{section}[{position}]",
                )
                insert_observation(conn, obs.fill_from_record(record))


def _load_records(conn: sqlite3.Connection, evidence_id: str, captured_at: str, data: dict) -> None:
    for position, record in enumerate(data["records"]):
        locator = f"records[{position}]"
        if "result" in record and "system" in record:
            insert_observation(conn, _ledger_observation(evidence_id, captured_at, record, locator))
            continue
        record = dict(record)
        obs = Observation(
            evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at,
            observed_at=data.get("captured_at", captured_at), system_id=data.get("source"),
            resource_id=record.pop("resource_id"), store=data.get("source"),
            collection=data.get("collection"), state=infer_state(record), locator=locator,
        )
        insert_observation(conn, obs.fill_from_record(record))


def load_json_file(conn: sqlite3.Connection, evidence_id: str, path: Path, captured_at: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "snapshots" in data:
        _load_bundle(conn, evidence_id, captured_at, data)
    elif "service_exports" in data:
        _load_service_export(conn, evidence_id, captured_at, data)
    elif "records" in data:
        _load_records(conn, evidence_id, captured_at, data)
    else:
        raise ValueError(f"unrecognized JSON evidence shape in {path.name}: {sorted(data)}")


def load_json_records(conn: sqlite3.Connection, dataset_dir: Path) -> int:
    files = evidence_files(conn, ".json")
    for file in files:
        load_json_file(conn, file["file_id"], dataset_dir / file["file_path"], file["captured_at"])
    return len(files)
