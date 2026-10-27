"""Hand-built evaluation cases, beyond the 32 public samples.

Each audit case builds a small, isolated world — a fresh database with the real
deletion rules, one fictional subject, and a few hand-placed pieces of
evidence — runs the production LangGraph workflow on it, and compares the
answer with an expected answer written by hand from the challenge rules. They
target behaviours the samples barely exercise: listings vs explicit sightings,
evidence captured after audit_at, replica drift, repeat requests by the same
person, tombstones and retention, and the ordering of recreation-risk fixes.

The classifier cases send tricky request wordings through the real (cached)
model call and check the deletion decision.
"""

import csv
from dataclasses import dataclass, field
from typing import Callable

from audit.db import create_fresh
from audit.graph import RunContext, build_graph
from audit.ingest.common import Observation, insert_coverage, insert_observation
from audit.ingest.identities import build_identifiers
from audit.intent import RequestClassification, classify_requests
from audit.normalize import email_hash
from audit.paths import CACHE_DIR, DATASET_DIR
from audit.policy import load_rules

DB_PATH = CACHE_DIR / "custom_cases.db"
EMAIL = "ana.testa@example.com"
REQUEST = {
    "case_id": "custom_1", "split": "custom", "email": EMAIL,
    "requested_at": "2026-09-01T10:00:00Z", "reported_status": "COMPLETE",
    "reported_at": "2026-09-02T10:00:00Z", "audit_at": "2026-09-10T12:00:00Z",
    "request_text": "Please delete my account and everything linked to it.",
}


class World:
    """A one-subject audit database built evidence by evidence."""

    def __init__(self) -> None:
        self.conn = create_fresh(DB_PATH)
        with (DATASET_DIR / "deletion_rules.csv").open(newline="", encoding="utf-8") as handle:
            self.conn.executemany(
                "INSERT INTO rules VALUES (:rule_id, :data_category, :condition, :required_action)",
                list(csv.DictReader(handle)),
            )
        self.conn.execute(
            "INSERT INTO identities VALUES (?, ?, ?, ?, ?, ?)",
            ("user_9001", "Ana Testa", EMAIL, "+1 555 010 0100", email_hash(EMAIL), "2026-01-01T00:00:00Z"),
        )
        self.add_request(REQUEST)

    def add_request(self, request: dict) -> None:
        self.conn.execute(
            "INSERT INTO requests VALUES (:case_id, :split, :email, :requested_at, :reported_status, "
            ":reported_at, :audit_at, :request_text)", request,
        )

    def _file(self, evidence_id: str, captured_at: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO evidence_files VALUES (?, ?, 'custom', ?)",
                          (evidence_id, f"custom/{evidence_id}", captured_at))

    def sight(self, evidence_id: str, captured_at: str, resource_id: str, system_id: str, state: str,
              observed_at: str | None = None, copy_role: str | None = None, store: str = "export",
              record_type: str | None = None, **fields) -> None:
        self._file(evidence_id, captured_at)
        obs = Observation(
            evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at,
            observed_at=observed_at or captured_at, system_id=system_id, resource_id=resource_id,
            store=store, copy_role=copy_role, record_type=record_type, state=state,
        )
        insert_observation(self.conn, obs.fill_from_record(dict(fields)))

    def listing(self, evidence_id: str, captured_at: str, system_id: str, store: str, copy_role: str = "primary") -> None:
        """A snapshot that lists every row of one table at captured_at."""
        self._file(evidence_id, captured_at)
        insert_coverage(self.conn, evidence_id, captured_at, system_id, store, copy_role)

    def audit(self, case_id: str = "custom_1", authorizes_deletion: bool = True) -> tuple[str, list[tuple]]:
        build_identifiers(self.conn)
        request = dict(self.conn.execute("SELECT * FROM requests WHERE case_id = ?", (case_id,)).fetchone())
        intent = RequestClassification(
            request_id="", intent="DELETION" if authorizes_deletion else "ACCESS",
            authorizes_deletion=authorizes_deletion, rationale="fixture",
        )
        ctx = RunContext(conn=self.conn, rules=load_rules(self.conn), intents={request["request_text"]: intent})
        state = build_graph(ctx).invoke({"request": request, "trace": []})
        steps = [(s["operation"], s["resource_id"], s["evidence_id"]) for s in state["steps"]]
        return state["deletion_status"], steps


@dataclass
class AuditCase:
    name: str
    checks: str
    build: Callable[[], World]
    expected_status: str
    expected_steps: list[tuple]
    case_id: str = "custom_1"
    authorizes_deletion: bool = True


def _stale_sighting_then_listing() -> World:
    w = World()
    w.sight("ev_old_snapshot", "2026-08-01T00:00:00Z", "account_9001", "account_service", "PRESENT",
            store="customer_profiles", copy_role="primary", email=EMAIL)
    w.sight("ev_listing", "2026-09-05T00:00:00Z", "account_7777", "account_service", "PRESENT",
            store="customer_profiles", copy_role="primary", email="someone.else@example.com")
    w.listing("ev_listing", "2026-09-05T00:00:00Z", "account_service", "customer_profiles")
    return w


def _fresh_sighting_beats_listing() -> World:
    w = _stale_sighting_then_listing()
    w.sight("ev_lookup", "2026-09-04T00:00:00Z", "account_9001", "account_service", "PRESENT",
            store="profile_lookups", email=EMAIL)
    return w


def _replica_drift() -> World:
    w = World()
    w.sight("ev_failover", "2026-09-06T00:00:00Z", "account_9001", "account_service", "PRESENT",
            store="customer_profiles_failover_replica", copy_role="failover_replica", email=EMAIL)
    w.listing("ev_failover", "2026-09-06T00:00:00Z", "account_service", "customer_profiles_primary")
    return w


def _late_evidence_ignored() -> World:
    w = World()
    w.sight("ev_before_audit", "2026-09-09T00:00:00Z", "search_9001", "search_service", "ABSENT",
            store="index_queries", total_hits=0, query=EMAIL)
    w.sight("ev_after_audit", "2026-09-12T00:00:00Z", "search_9001", "search_service", "PRESENT",
            store="index_queries", total_hits=1, query=EMAIL)
    return w


def _tombstone_retention_backup() -> World:
    w = World()
    w.sight("ev_refs", "2026-09-08T00:00:00Z", "tombstone_9001", "identity_service", "PRESENT",
            store="deleted_customer_references", account_status="DELETED", subject_reference="user_9001",
            name=None, email=None, phone=None, email_hash=None)
    w.sight("ev_refs", "2026-09-08T00:00:00Z", "audit_record_9001", "security_service", "PRESENT",
            store="audit_records", subject_reference="user_9001", retention_until="2034-01-01T00:00:00Z")
    w.sight("ev_refs", "2026-09-08T00:00:00Z", "backup_9001", "backup_service", "BLOCKED",
            store="restore_entries", customer_email=EMAIL, restore_allowed=False)
    return w


def _recreation_risks() -> World:
    w = World()
    w.sight("ev_export", "2026-09-08T00:00:00Z", "search_9001", "search_service", "PRESENT",
            store="index_queries", email=EMAIL)
    w.sight("ev_export", "2026-09-08T00:00:00Z", "analytics_9001", "analytics_platform", "PRESENT",
            store="profiles", email=EMAIL)
    w.sight("ev_export", "2026-09-08T00:00:00Z", "data_flow_9001", "integration_service", "ACTIVE",
            store="connectors", customer_email=EMAIL, enabled=True)
    w.sight("ev_export", "2026-09-08T00:00:00Z", "backup_9001", "backup_service", "ACTIVE",
            store="restore_entries", customer_email=EMAIL, restore_allowed=True)
    return w


def _repeat_request() -> World:
    w = World()
    w.add_request({**REQUEST, "case_id": "custom_0", "requested_at": "2026-02-01T10:00:00Z",
                   "reported_at": "2026-02-02T10:00:00Z", "audit_at": "2026-03-01T12:00:00Z"})
    # The first episode's account, gone since February; only the new account matters now.
    w.sight("ev_feb_lookup", "2026-02-20T00:00:00Z", "account_1111", "account_service", "ABSENT",
            store="profile_lookups", status_code=404, lookup_key=EMAIL)
    w.sight("ev_sep_lookup", "2026-09-05T00:00:00Z", "account_2222", "account_service", "PRESENT",
            store="profile_lookups", email=EMAIL)
    return w


AUDIT_CASES = [
    AuditCase(
        "stale sighting, newer listing", "an absence proven by a later full listing beats a pre-request sighting",
        _stale_sighting_then_listing, "COMPLETE",
        [("VERIFY_RECORD_ABSENT", "account_9001", "ev_listing")],
    ),
    AuditCase(
        "fresh sighting, newer listing", "a sighting made after the request outranks a listing that omits the record",
        _fresh_sighting_beats_listing, "INCOMPLETE",
        [("REPORT_REMAINING_RECORD", "account_9001", "ev_lookup"), ("DELETE_RECORD", "account_9001", ""),
         ("VERIFY_RECORD_ABSENT", "account_9001", "")],
    ),
    AuditCase(
        "replica drift", "a failover-replica row is a remaining copy even when the primary table lacks it",
        _replica_drift, "INCOMPLETE",
        [("REPORT_REMAINING_RECORD", "account_9001", "ev_failover"), ("DELETE_RECORD", "account_9001", ""),
         ("VERIFY_RECORD_ABSENT", "account_9001", "")],
    ),
    AuditCase(
        "evidence after audit_at", "a file captured after audit_at can neither change nor support the answer",
        _late_evidence_ignored, "COMPLETE",
        [("VERIFY_RECORD_ABSENT", "search_9001", "ev_before_audit")],
    ),
    AuditCase(
        "tombstone, retention, blocked backup", "a user_id-only tombstone is redacted; retained and blocked records verify",
        _tombstone_retention_backup, "COMPLETE",
        [("VERIFY_RECORD_PRESENT", "audit_record_9001", "ev_refs"),
         ("VERIFY_RECORD_REDACTED", "tombstone_9001", "ev_refs"),
         ("VERIFY_RESTORE_BLOCKED", "backup_9001", "ev_refs")],
    ),
    AuditCase(
        "recreation risks first", "flows are disabled and restores blocked before records are changed",
        _recreation_risks, "INCOMPLETE",
        [("REPORT_REMAINING_RECORD", "search_9001", "ev_export"),
         ("REPORT_EXPOSED_PII", "analytics_9001", "ev_export"),
         ("REPORT_ACTIVE_DATA_FLOW", "data_flow_9001", "ev_export"),
         ("REPORT_RESTORE_RISK", "backup_9001", "ev_export"),
         ("DISABLE_DATA_FLOW", "data_flow_9001", ""), ("BLOCK_RESTORE", "backup_9001", ""),
         ("DELETE_RECORD", "search_9001", ""), ("ANONYMIZE_RECORD", "analytics_9001", ""),
         ("VERIFY_RECORD_ABSENT", "search_9001", ""), ("VERIFY_RECORD_ANONYMIZED", "analytics_9001", ""),
         ("VERIFY_DATA_FLOW_DISABLED", "data_flow_9001", ""), ("VERIFY_RESTORE_BLOCKED", "backup_9001", "")],
    ),
    AuditCase(
        "repeat request by the same person", "evidence from an earlier request's episode is out of scope",
        _repeat_request, "INCOMPLETE",
        [("REPORT_REMAINING_RECORD", "account_2222", "ev_sep_lookup"), ("DELETE_RECORD", "account_2222", ""),
         ("VERIFY_RECORD_ABSENT", "account_2222", "")],
    ),
    AuditCase(
        "not a deletion request", "a non-deletion request gets NOT_APPLICABLE and no steps, whatever the evidence",
        _recreation_risks, "NOT_APPLICABLE", [], authorizes_deletion=False,
    ),
]

# (request text, should it authorize deletion?)
CLASSIFIER_CASES = [
    ("Please unsubscribe me from your newsletters. I am not asking you to delete my account.", False),
    ("Löschen Sie bitte mein Konto und alle damit verbundenen personenbezogenen Daten.", True),
    ("Last month I asked you to delete my account. Can you check whether that really happened and finish it if not?", True),
    ("Mi apellido está mal escrito en la cuenta. ¿Pueden corregirlo, por favor?", False),
    ("Close my account. You may keep the invoices you are legally required to retain.", True),
    ("Please send me a copy of my data first; I have not decided yet whether to close the account.", False),
]


@dataclass
class CaseResult:
    name: str
    checks: str
    passed: bool
    details: list[str] = field(default_factory=list)


def run_audit_cases() -> list[CaseResult]:
    results = []
    for case in AUDIT_CASES:
        world = case.build()
        status, steps = world.audit(case.case_id, case.authorizes_deletion)
        world.conn.close()
        details = []
        if status != case.expected_status:
            details.append(f"status {status}, expected {case.expected_status}")
        if steps != case.expected_steps:
            details.append(f"steps {steps}, expected {case.expected_steps}")
        results.append(CaseResult(case.name, case.checks, not details, details))
    DB_PATH.unlink(missing_ok=True)
    return results


def run_classifier_cases(allow_model: bool = True) -> list[CaseResult]:
    answers = classify_requests([text for text, _ in CLASSIFIER_CASES], allow_model=allow_model)
    results = []
    for text, expected in CLASSIFIER_CASES:
        answer = answers[text]
        passed = answer.authorizes_deletion == expected
        details = [] if passed else [f"got {answer.intent}/{answer.authorizes_deletion}: {answer.rationale}"]
        results.append(CaseResult(text, f"expected authorizes_deletion={expected} ({answer.intent})", passed, details))
    return results
