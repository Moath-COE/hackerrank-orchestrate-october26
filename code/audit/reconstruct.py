"""Point-in-time state reconstruction, separately for every copy of a resource.

For one resource and one cutoff (the case's audit_at) this gathers every
sighting that existed by then:
  * observations from evidence files and from the few activity templates that
    report an observed condition (claims carry no state and are kept apart);
  * absences proven by coverage: a snapshot that lists a whole table or
    collection proves that a resource missing from it did not exist there.

Sightings are grouped by copy (the primary view vs each named replica) and the
latest sighting per copy wins. A later lookup against the live service updates
the primary view but says nothing about a replica, so a stale replica row stays
visible until a later snapshot of that replica shows otherwise.

Listings are weaker than explicit sightings: an assurance export or inventory
may list only the records it was asked about, so a resource missing from it is
absent only by inference. Within a copy, an explicit sighting made during the
request's lifetime (at or after requested_at) therefore outranks a listing
absence; a listing absence only replaces an explicit sighting that predates
the request and that the listing comes after.

This module records facts only: the state and which of the subject's
identifiers are still visible. Whether that satisfies policy is decided in
audit.policy.
"""

import json
import sqlite3
from dataclasses import dataclass, field

from audit.identity import Subject
from audit.ingest.activity import CATALOG_EVENT
from audit.ingest.common import clean
from audit.systems import system_for_resource

# Sightings at the same instant prefer direct file evidence over activity rows.
KIND_PRIORITY = {"activity": 0, "file": 1}
DIRECT_FIELDS = ("name", "email", "phone", "email_hash")
NON_VALUES = {"", "none", "null", "[]", "false", "0"}


@dataclass(frozen=True)
class Sighting:
    evidence_id: str
    evidence_kind: str      # file | activity
    captured_at: str
    observed_at: str
    copy: str               # primary | <replica name>
    state: str              # ABSENT | PRESENT | REDACTED | ANONYMIZED | ACTIVE | DISABLED | BLOCKED
    exposed: frozenset[str] = frozenset()  # identifier kinds of the subject still visible
    via: str = "observation"               # observation | coverage | catalog

    def sort_key(self) -> tuple:
        return (self.observed_at, KIND_PRIORITY.get(self.evidence_kind, 0), self.captured_at, self.evidence_id)


@dataclass
class ResourceEvidence:
    resource_id: str
    system_id: str | None
    observations: list[sqlite3.Row] = field(default_factory=list)  # everything citable by audit_at
    latest: dict[str, Sighting] = field(default_factory=dict)       # copy -> latest sighting
    claims: list[str] = field(default_factory=list)                 # activity ids that only claim

    def attrs(self) -> list[dict]:
        return [json.loads(row["attrs_json"]) for row in self.observations]


def copy_of(copy_role: str | None, store: str | None) -> str:
    """Name of the physical copy a sighting describes."""
    if copy_role and copy_role not in ("primary", "snapshot"):
        return copy_role
    if store and "replica" in store:
        return store
    return "primary"


def exposed_kinds(row: sqlite3.Row, attrs: dict, identifiers: set[tuple[str, str]], subject: Subject) -> frozenset[str]:
    """Identifier kinds of the subject that this observation still shows."""
    kinds = {kind for kind, value in identifiers if (kind, value) in subject.identifiers}
    # Populated direct-identifier fields count even when spelled differently from the index.
    kinds |= {name for name in DIRECT_FIELDS if clean(row[name])}
    if clean(row["user_ref"]):
        kinds.add("user_id")
    if str(clean(attrs.get("result")) or "").upper() == "IDENTIFIABLE":
        kinds.add("identifiable")
    for key in ("matched_value", "direct_identifiers"):
        if str(clean(attrs.get(key)) or "").strip().lower() not in NON_VALUES:
            kinds.add("identifiable")
    return frozenset(kinds)


def _store_prefixes(conn: sqlite3.Connection) -> dict[tuple[str, str], set[str]]:
    """Resource-id prefixes seen in each (evidence, store), to keep coverage on the right kind of record."""
    prefixes: dict[tuple[str, str], set[str]] = {}
    for row in conn.execute(
        "SELECT DISTINCT evidence_id, store, resource_id FROM observations WHERE evidence_kind = 'file'"
    ):
        if row["resource_id"]:
            prefixes.setdefault((row["evidence_id"], row["store"]), set()).add(row["resource_id"].rsplit("_", 1)[0])
    return prefixes


class Reconstructor:
    """Reconstructs resource state for many cases over one database."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.prefixes = _store_prefixes(conn)
        self.captured = {
            row["file_id"]: row["captured_at"]
            for row in conn.execute("SELECT file_id, captured_at FROM evidence_files")
        }

    def _identifiers(self, obs_id: int) -> set[tuple[str, str]]:
        return {
            (row["kind"], row["value"])
            for row in self.conn.execute("SELECT kind, value FROM obs_identifiers WHERE obs_id = ?", (obs_id,))
        }

    def resource(self, resource_id: str, subject: Subject, requested_at: str, audit_at: str) -> ResourceEvidence:
        system_id = system_for_resource(resource_id)
        evidence = ResourceEvidence(resource_id=resource_id, system_id=system_id)
        sightings: list[Sighting] = []
        catalog: list[Sighting] = []
        seen_stores: set[tuple[str, str]] = set()

        rows = self.conn.execute(
            "SELECT * FROM observations WHERE resource_id = ? AND captured_at <= ? AND observed_at <= ? "
            "ORDER BY observed_at, obs_id",
            (resource_id, audit_at, audit_at),
        ).fetchall()
        for row in rows:
            evidence.observations.append(row)
            evidence.system_id = evidence.system_id or row["system_id"]
            seen_stores.add((row["evidence_id"], row["store"]))
            if row["state"] is None:
                evidence.claims.append(row["evidence_id"])
                continue
            attrs = json.loads(row["attrs_json"])
            if attrs.get("event_type") == CATALOG_EVENT:
                # A catalog entry proves the record existed, not which fields it still holds,
                # so it only stands in when no source shows the record's actual state.
                catalog.append(Sighting(
                    evidence_id=row["evidence_id"], evidence_kind=row["evidence_kind"],
                    captured_at=row["captured_at"], observed_at=row["observed_at"],
                    copy="primary", state=row["state"], via="catalog",
                ))
                continue
            sightings.append(Sighting(
                evidence_id=row["evidence_id"], evidence_kind=row["evidence_kind"],
                captured_at=row["captured_at"], observed_at=row["observed_at"],
                copy=copy_of(row["copy_role"], row["store"]), state=row["state"],
                exposed=exposed_kinds(row, attrs, self._identifiers(row["obs_id"]), subject),
            ))

        for sighting in sorted(sightings, key=Sighting.sort_key):
            evidence.latest[sighting.copy] = sighting

        if evidence.system_id:
            listed: dict[str, Sighting] = {}
            for absence in sorted(self._coverage_absences(resource_id, evidence.system_id, audit_at, seen_stores),
                                  key=Sighting.sort_key):
                listed[absence.copy] = absence
            for copy, absence in listed.items():
                explicit = evidence.latest.get(copy)
                if explicit is None or (explicit.observed_at < requested_at and absence.sort_key() > explicit.sort_key()):
                    evidence.latest[copy] = absence

        if not evidence.latest and catalog:
            evidence.latest["primary"] = max(catalog, key=Sighting.sort_key)
        return evidence

    def _coverage_absences(
        self, resource_id: str, system_id: str, audit_at: str, seen_stores: set[tuple[str, str]]
    ) -> list[Sighting]:
        prefix = resource_id.rsplit("_", 1)[0]
        absences = []
        for cov in self.conn.execute(
            "SELECT * FROM coverage WHERE system_id = ? AND observed_at <= ?", (system_id, audit_at)
        ):
            captured_at = self.captured.get(cov["evidence_id"], "")
            if not captured_at or captured_at > audit_at or (cov["evidence_id"], cov["store"]) in seen_stores:
                continue
            store_prefixes = self.prefixes.get((cov["evidence_id"], cov["store"]))
            if store_prefixes and prefix not in store_prefixes:
                continue  # the listing is of a different kind of record in the same system
            absences.append(Sighting(
                evidence_id=cov["evidence_id"], evidence_kind="file", captured_at=captured_at,
                observed_at=cov["observed_at"], copy=copy_of(cov["copy_role"], cov["store"]),
                state="ABSENT", via="coverage",
            ))
        return absences
