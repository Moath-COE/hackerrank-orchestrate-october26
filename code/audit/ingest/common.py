"""Shared observation model used by every evidence loader."""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from audit.normalize import EMAIL_RE, HASH_RE, USER_REF_RE, extract_identifiers, norm_name
from audit.systems import system_for_resource

# Field names that carry each canonical identifier, across all evidence formats.
NAME_KEYS = ("full_name", "display_name", "requester_name", "customer_name", "name")
EMAIL_KEYS = ("email", "requester_email", "owner_email", "customer_email")
PHONE_KEYS = ("phone", "requester_phone", "owner_phone", "customer_phone")
HASH_KEYS = ("email_hash",)
# Reference fields may hold a user id, an email, or a hash depending on the source.
REFERENCE_KEYS = ("user_id", "customer_reference", "subject_reference", "subject_selector", "subject_ref")
BOOL_KEYS = ("enabled", "restore_allowed", "download_available", "available", "archive_present",
             "eligible", "deletion_block", "scheduler_registered", "marker_found")

NULL_STRINGS = {"", "null", "none"}

# Explicit result vocabularies used by lookup ledgers, handovers and diagnostics.
RESULT_STATES = {
    "FOUND": "PRESENT",
    "NOT_FOUND": "ABSENT",
    "IDENTIFIABLE": "PRESENT",
    "ANONYMIZED": "ANONYMIZED",
    "REDACTED": "REDACTED",
    "ACTIVE": "ACTIVE",
    "DISABLED": "DISABLED",
    "BLOCKED": "BLOCKED",
    "HIT": "PRESENT",
    "MISS": "ABSENT",
}


def clean(value: Any) -> Any:
    """Map the several spellings of "no value" to None."""
    if isinstance(value, str) and value.strip().lower() in NULL_STRINGS:
        return None
    return value


def to_bool(value: Any) -> bool | None:
    value = clean(value)
    if value is None or isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any) -> int | None:
    try:
        return int(clean(value))
    except (TypeError, ValueError):
        return None


def infer_state(fields: dict[str, Any]) -> str:
    """State a source reports for one resource, from whichever status fields it carries.

    Checks run from most to least specific so that, for example, a restore policy
    read on a missing archive is ABSENT rather than BLOCKED.
    """
    result = clean(fields.get("result"))
    if isinstance(result, str) and result.upper() in RESULT_STATES:
        return RESULT_STATES[result.upper()]
    for key in ("status_code", "http_status"):
        if _as_int(fields.get(key)) == 404:
            return "ABSENT"
    for key in ("rows", "total_hits", "total"):
        count = _as_int(fields.get(key))
        if count is not None:
            return "ABSENT" if count == 0 else "PRESENT"
    for key in ("available", "archive_present"):
        if to_bool(fields.get(key)) is False:
            return "ABSENT"
    if clean(fields.get("enabled")) is not None:
        return "ACTIVE" if to_bool(fields["enabled"]) else "DISABLED"
    if clean(fields.get("restore_allowed")) is not None:
        return "ACTIVE" if to_bool(fields["restore_allowed"]) else "BLOCKED"
    if clean(fields.get("eligible")) is not None:
        restorable = to_bool(fields["eligible"]) and not to_bool(fields.get("deletion_block"))
        return "ACTIVE" if restorable else "BLOCKED"
    decision = clean(fields.get("decision"))
    if isinstance(decision, str):
        return "ACTIVE" if decision.lower() in {"allow", "allowed", "approve"} else "BLOCKED"
    return "PRESENT"


@dataclass
class Observation:
    evidence_id: str
    evidence_kind: str
    captured_at: str
    observed_at: str
    system_id: str | None
    resource_id: str | None
    store: str | None = None
    collection: str | None = None
    copy_role: str | None = None
    record_type: str | None = None
    state: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    email_hash: str | None = None
    user_ref: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    locator: str | None = None

    def fill_from_record(self, record: dict[str, Any]) -> "Observation":
        """Populate canonical identifier fields from a (possibly nested) record."""
        flat = _flatten(record)
        self.name = self.name or _first(flat, NAME_KEYS)
        self.email = self.email or _first(flat, EMAIL_KEYS)
        self.phone = self.phone or _first(flat, PHONE_KEYS)
        self.email_hash = self.email_hash or _first(flat, HASH_KEYS)
        for key in REFERENCE_KEYS:
            value = clean(flat.get(key))
            if value is None:
                continue
            value = str(value)
            if USER_REF_RE.fullmatch(value.strip()):
                self.user_ref = self.user_ref or value
            elif EMAIL_RE.fullmatch(value.strip()):
                self.email = self.email or value
            elif HASH_RE.fullmatch(value.strip()):
                self.email_hash = self.email_hash or value
        for key in BOOL_KEYS:
            if key in record:
                record[key] = to_bool(record[key])
        self.attrs = {**record, **self.attrs}
        return self

    def identifiers(self) -> set[tuple[str, str]]:
        """Every identifier visible anywhere in this observation (fields and free text)."""
        found: set[tuple[str, str]] = set()
        for value in (self.email, self.phone, self.email_hash, self.user_ref):
            if value:
                found |= extract_identifiers(str(value))
        for value in _strings(self.attrs):
            found |= extract_identifiers(value)
        if self.name:
            found.add(("name", norm_name(self.name)))
        return found


def _flatten(record: dict[str, Any]) -> dict[str, Any]:
    """Merge nested dicts into one level; outer keys win on conflict."""
    flat: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, dict):
            for inner_key, inner_value in _flatten(value).items():
                flat.setdefault(inner_key, inner_value)
    for key, value in record.items():
        if not isinstance(value, dict):
            flat[key] = value
    return flat


def _first(flat: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = clean(flat.get(key))
        if value is not None:
            return str(value)
    return None


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _strings(inner)


def insert_observation(conn: sqlite3.Connection, obs: Observation) -> int:
    owner = system_for_resource(obs.resource_id)
    if owner and owner != obs.system_id:
        obs.attrs.setdefault("reported_system", obs.system_id)
        obs.system_id = owner
    cursor = conn.execute(
        """INSERT INTO observations (evidence_id, evidence_kind, locator, captured_at, observed_at,
               system_id, resource_id, store, collection, copy_role, record_type, state,
               name, email, phone, email_hash, user_ref, attrs_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obs.evidence_id, obs.evidence_kind, obs.locator, obs.captured_at, obs.observed_at,
            obs.system_id, obs.resource_id, obs.store, obs.collection, obs.copy_role,
            obs.record_type, obs.state,
            obs.name, obs.email, obs.phone, obs.email_hash, obs.user_ref,
            json.dumps(obs.attrs, sort_keys=True, ensure_ascii=False, default=str),
        ),
    )
    obs_id = cursor.lastrowid
    conn.executemany(
        "INSERT OR IGNORE INTO obs_identifiers (obs_id, kind, value) VALUES (?, ?, ?)",
        [(obs_id, kind, value) for kind, value in sorted(obs.identifiers())],
    )
    return obs_id


def insert_coverage(
    conn: sqlite3.Connection,
    evidence_id: str,
    observed_at: str,
    system_id: str,
    store: str,
    copy_role: str | None,
) -> None:
    conn.execute(
        "INSERT INTO coverage (evidence_id, observed_at, system_id, store, copy_role) VALUES (?, ?, ?, ?, ?)",
        (evidence_id, observed_at, system_id, store, copy_role),
    )


def evidence_files(conn: sqlite3.Connection, *suffixes: str) -> list[sqlite3.Row]:
    """Manifest rows whose path ends with one of the given extensions, in file_id order."""
    rows = conn.execute("SELECT file_id, file_path, captured_at FROM evidence_files ORDER BY file_id").fetchall()
    return [row for row in rows if row["file_path"].lower().endswith(suffixes)]
