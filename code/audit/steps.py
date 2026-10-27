"""Turn policy assessments into the ordered steps_json array, and check the contract.

Ordering follows the problem statement: phase (FINDING, REMEDIATION,
VERIFICATION), then the operation order of the allowed-operations table, then
system_id, then resource_id. That operation order already puts DISABLE_DATA_FLOW
and BLOCK_RESTORE ahead of record changes, so nothing can be recreated while
it is being cleaned up.
"""

import json

from audit.policy import Assessment

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# Allowed operations, in table order, with the state each one carries.
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OPERATION_ORDER = {operation: index for index, operation in enumerate(OPERATIONS)}

FINDING_FOR_ACTION = {
    "DELETE": "REPORT_REMAINING_RECORD",
    "REDACT": "REPORT_EXPOSED_PII",
    "ANONYMIZE": "REPORT_EXPOSED_PII",
    "DISABLE_FLOW": "REPORT_ACTIVE_DATA_FLOW",
    "BLOCK_RESTORE": "REPORT_RESTORE_RISK",
}
REMEDIATION_FOR_ACTION = {
    "DELETE": "DELETE_RECORD",
    "REDACT": "REDACT_RECORD",
    "ANONYMIZE": "ANONYMIZE_RECORD",
    "DISABLE_FLOW": "DISABLE_DATA_FLOW",
    "BLOCK_RESTORE": "BLOCK_RESTORE",
}
VERIFICATION_FOR_STATE = {
    "ABSENT": "VERIFY_RECORD_ABSENT",
    "PRESENT": "VERIFY_RECORD_PRESENT",
    "REDACTED": "VERIFY_RECORD_REDACTED",
    "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED",
    "DISABLED": "VERIFY_DATA_FLOW_DISABLED",
    "BLOCKED": "VERIFY_RESTORE_BLOCKED",
}


def _step(operation: str, assessment: Assessment, evidence_id: str) -> dict:
    phase, state = OPERATIONS[operation]
    return {
        "step_no": 0,
        "phase": phase,
        "system_id": assessment.system_id,
        "resource_id": assessment.resource_id,
        "operation": operation,
        "state": state,
        "evidence_id": evidence_id,
    }


def plan_steps(assessments: list[Assessment], status: str) -> list[dict]:
    """Findings + remediation + verification for INCOMPLETE; evidence-backed verification for COMPLETE."""
    steps = []
    for a in assessments:
        if a.verdict == "VIOLATION":
            steps.append(_step(FINDING_FOR_ACTION[a.treatment.action], a, a.evidence.evidence_id))
            steps.append(_step(REMEDIATION_FOR_ACTION[a.treatment.action], a, ""))
        if status == "INCOMPLETE":
            # Every relevant resource must be checked after remediation, proven or not yet.
            steps.append(_step(VERIFICATION_FOR_STATE[a.final_state], a, ""))
        elif a.verdict == "COMPLIANT":
            steps.append(_step(VERIFICATION_FOR_STATE[a.final_state], a, a.evidence.evidence_id))

    steps.sort(key=lambda s: (PHASES.index(s["phase"]), OPERATION_ORDER[s["operation"]], s["system_id"], s["resource_id"]))
    for number, step in enumerate(steps, start=1):
        step["step_no"] = number
    return steps


def validate_steps(status: str, steps: list[dict]) -> list[str]:
    """Contract problems in one case's answer; an empty list means it is valid."""
    problems = []
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        problems.append(f"invalid status {status}")
    if status == "NOT_APPLICABLE" and steps:
        problems.append("NOT_APPLICABLE must have no steps")
    previous = (-1, -1)
    for number, step in enumerate(steps, start=1):
        if step["step_no"] != number:
            problems.append(f"step {number} numbered {step['step_no']}")
        expected = OPERATIONS.get(step["operation"])
        if expected is None or expected != (step["phase"], step["state"]):
            problems.append(f"step {number}: {step['operation']} with {step['phase']}/{step['state']}")
            continue
        position = (PHASES.index(step["phase"]), OPERATION_ORDER[step["operation"]])
        if position < previous:
            problems.append(f"step {number} out of phase/operation order")
        previous = position
        cites = bool(step["evidence_id"])
        if step["phase"] == "FINDING" and not cites:
            problems.append(f"step {number}: finding without evidence")
        if status == "COMPLETE" and not cites:
            problems.append(f"step {number}: COMPLETE verification without evidence")
        if status == "INCOMPLETE" and step["phase"] != "FINDING" and cites:
            problems.append(f"step {number}: future step cites evidence")
    if status == "COMPLETE" and any(step["phase"] != "VERIFICATION" for step in steps):
        problems.append("COMPLETE may only contain verification steps")
    return problems


STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def serialize(steps: list[dict]) -> str:
    """Deterministic compact JSON in the same form as sample_output.csv (sorted keys)."""
    return json.dumps(
        [{key: step[key] for key in STEP_FIELDS} for step in steps],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
