"""Apply deletion_rules.csv to reconstructed resources.

Each resource gets a data category (stated by the evidence when a source gives
a record_type, otherwise inferred from its id prefix and the fields it carries),
the rule for that category, and a verdict on its state at audit_at:
  COMPLIANT  — every copy has the required treatment; cite the proving sighting;
  VIOLATION  — some copy still breaks the rule; cite the violating sighting;
  UNKNOWN    — only claims exist, so the state cannot be proven either way.

Active data flows and restorable backups are not data categories in the rules
file, but they are the recreation risks the rules exist to prevent: a flow must
be disabled, and a retained backup (rule BACKUP_ARCHIVE) must not be restorable.
"""

import sqlite3
from dataclasses import dataclass

from audit.reconstruct import ResourceEvidence, Sighting

# Default category per resource-id prefix, when no source states one.
PREFIX_CATEGORIES = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "cache": "CACHE_ENTRY",
    "export": "USER_EXPORT",
    "marketing": "MARKETING_PROFILE",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    "audit_record": "SECURITY_AUDIT_LOG",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "backup": "BACKUP_ARCHIVE",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "data_flow": "DATA_FLOW",
    "analytics": "ANALYTICS_PROFILE",
    "support": "SUPPORT_ATTACHMENT",
    "object": "USER_EXPORT",
}

# Field and event cues that distinguish categories sharing a prefix.
ATTACHMENT_KEYS = {"file_name", "uploaded_by", "storage_class"}
ATTACHMENT_EVENTS = {"attachment_pii_scan"}
MESSAGE_KEYS = {"message_text", "message_excerpt", "requester_email", "requester_name"}
MESSAGE_EVENTS = {"message_pii_scan", "message_read", "support_ticket_read_completed"}
MESSAGE_WORDS = ("conversation", "ticket", "message")  # in document descriptions of the record
CONTACT_EVENTS = {"contact_lookup_completed"}
# When the record's own fields do not tell, the operation the deletion workflow
# attempted on it shows how the platform itself classified the record.
WORKER_OPERATION_CATEGORIES = {
    ("support", "REDACT_RECORD"): "SUPPORT_MESSAGE",
    ("support", "DELETE_RECORD"): "SUPPORT_ATTACHMENT",
    ("analytics", "DELETE_RECORD"): "SECONDARY_IDENTIFIER",
    ("analytics", "ANONYMIZE_RECORD"): "ANALYTICS_PROFILE",
}

# Identifier kinds a retained or redacted record may keep. A tombstone keeps the
# stable user_id by definition; a redacted support conversation keeps its
# customer reference so the shared context stays usable. An anonymized record
# keeps nothing that links it to the person.
PERMITTED_KINDS = {
    "ACCOUNT_TOMBSTONE": {"user_id"},
    "SUPPORT_MESSAGE": {"user_id"},
    "EMAIL_DELIVERY_LOG": {"user_id"},
}

# Treatments: what the final state of a compliant resource is.
FINAL_STATE = {
    "DELETE": "ABSENT",
    "REDACT": "REDACTED",
    "ANONYMIZE": "ANONYMIZED",
    "RETAIN": "PRESENT",
    "BLOCK_RESTORE": "BLOCKED",
    "DISABLE_FLOW": "DISABLED",
}


@dataclass(frozen=True)
class Treatment:
    category: str
    action: str             # DELETE | REDACT | ANONYMIZE | RETAIN | BLOCK_RESTORE | DISABLE_FLOW
    rule_id: str | None


@dataclass
class Assessment:
    resource_id: str
    system_id: str
    treatment: Treatment
    verdict: str            # COMPLIANT | VIOLATION | UNKNOWN
    final_state: str        # state the resource must end in
    evidence: Sighting | None


def load_rules(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """data_category -> (rule_id, required_action)."""
    return {row["data_category"]: (row["rule_id"], row["required_action"]) for row in conn.execute("SELECT * FROM rules")}


def categorize(resource: ResourceEvidence) -> str:
    stated = [row["record_type"] for row in resource.observations if row["record_type"]]
    if stated:
        return stated[-1]
    prefix = resource.resource_id.rsplit("_", 1)[0]
    attrs = resource.attrs()
    keys = {key for record in attrs for key in record}
    events = {str(record.get("event")) for record in attrs}
    text = " ".join(
        str(record.get(key) or "") for record in attrs for key in ("record_kind", "notes", "details")
    ).lower()

    worker_hints = [
        WORKER_OPERATION_CATEGORIES[(prefix, record["event_type"])]
        for record in attrs
        if record.get("source_system") == "deletion_worker" and (prefix, record.get("event_type")) in WORKER_OPERATION_CATEGORIES
    ]

    if prefix == "support":
        if keys & ATTACHMENT_KEYS or events & ATTACHMENT_EVENTS or "attachment" in text:
            return "SUPPORT_ATTACHMENT"
        if keys & MESSAGE_KEYS or events & MESSAGE_EVENTS or any(word in text for word in MESSAGE_WORDS):
            return "SUPPORT_MESSAGE"
        if worker_hints:
            return worker_hints[-1]
        # No shared conversation context is shown, so the record belongs only to the subject.
        return "SUPPORT_ATTACHMENT"
    if prefix == "analytics":
        if events & CONTACT_EVENTS:
            return "SECONDARY_IDENTIFIER"  # a contact lookup keyed by the person's identifier, not a profile
        if worker_hints:
            return worker_hints[-1]
    if prefix == "object" and "attachment" in text:
        return "SUPPORT_ATTACHMENT"
    return PREFIX_CATEGORIES.get(prefix, "USER_PROFILE")


def _retention_active(resource: ResourceEvidence, audit_at: str) -> bool:
    """A retained record stays retained unless a stated retention end has passed."""
    ends = [
        str(record.get(key))
        for record in resource.attrs()
        for key in ("retention_until", "retained_until", "retain_until")
        if record.get(key) not in (None, "", "null")
    ]
    return not ends or max(ends) >= audit_at


def treatment_for(resource: ResourceEvidence, rules: dict[str, tuple[str, str]], audit_at: str) -> Treatment:
    category = categorize(resource)
    if category == "DATA_FLOW":
        return Treatment(category, "DISABLE_FLOW", None)
    rule_id, action = rules.get(category, (None, "DELETE"))
    if category == "BACKUP_ARCHIVE":
        action = "BLOCK_RESTORE"  # retained, provided restore access is blocked
    elif action == "RETAIN" and not _retention_active(resource, audit_at):
        action = "DELETE"
    return Treatment(category, action, rule_id)


def complies(sighting: Sighting, treatment: Treatment) -> bool | None:
    """Whether one copy's latest sighting satisfies the treatment; None if it cannot tell."""
    state, action = sighting.state, treatment.action
    if sighting.via == "catalog":
        # Existence alone breaks a deletion but says nothing about redaction or restore state.
        return False if action == "DELETE" else None
    if action == "DELETE":
        return state == "ABSENT"
    if action in ("REDACT", "ANONYMIZE"):
        if state in ("ABSENT", "REDACTED", "ANONYMIZED"):
            return True
        if state != "PRESENT":
            return None
        return not (sighting.exposed - PERMITTED_KINDS.get(treatment.category, set()))
    if action == "RETAIN":
        # A missing retained record cannot be fixed by any allowed operation; it proves nothing.
        return True if state != "ABSENT" else None
    if action == "BLOCK_RESTORE":
        return {"BLOCKED": True, "ABSENT": True, "ACTIVE": False}.get(state)
    if action == "DISABLE_FLOW":
        return {"DISABLED": True, "ABSENT": True, "ACTIVE": False}.get(state)
    return None


def assess(resource: ResourceEvidence, rules: dict[str, tuple[str, str]], audit_at: str) -> Assessment:
    treatment = treatment_for(resource, rules, audit_at)
    verdicts = [(complies(sighting, treatment), sighting) for sighting in resource.latest.values()]
    violations = [s for ok, s in verdicts if ok is False]
    proofs = [s for ok, s in verdicts if ok is True]

    if violations:
        verdict, evidence = "VIOLATION", max(violations, key=Sighting.sort_key)
    elif proofs:
        verdict, evidence = "COMPLIANT", max(proofs, key=Sighting.sort_key)
    else:
        verdict, evidence = "UNKNOWN", None

    final_state = FINAL_STATE[treatment.action]
    if verdict == "COMPLIANT" and evidence.state == "ABSENT" and treatment.action in ("REDACT", "ANONYMIZE", "RETAIN"):
        final_state = "ABSENT"  # the record is already gone; that is what can be verified
    return Assessment(
        resource_id=resource.resource_id,
        system_id=resource.system_id or "",
        treatment=treatment,
        verdict=verdict,
        final_state=final_state,
        evidence=evidence,
    )


def case_status(assessments: list[Assessment]) -> str:
    return "INCOMPLETE" if any(a.verdict == "VIOLATION" for a in assessments) else "COMPLETE"


def describe(assessment: Assessment) -> dict:
    """JSON-friendly summary for traces and debugging."""
    evidence = assessment.evidence
    return {
        "resource_id": assessment.resource_id,
        "category": assessment.treatment.category,
        "action": assessment.treatment.action,
        "rule_id": assessment.treatment.rule_id,
        "verdict": assessment.verdict,
        "evidence": None if evidence is None else {
            "id": evidence.evidence_id, "copy": evidence.copy, "state": evidence.state,
            "observed_at": evidence.observed_at, "exposed": sorted(evidence.exposed), "via": evidence.via,
        },
    }
