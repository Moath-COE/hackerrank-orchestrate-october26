"""Attribute anonymous deletion-workflow activity to cases.

Once a profile is deleted or an analytics record anonymized, no identifier is
left to find it by: the only trace is a deletion_worker row in the global
activity log ("Deletion worker submitted primary profile account_6440 ...").
Those rows are not tied to a person, and several cases' workflows run in the
same hours, so they are attributed by elimination across all cases at once:

  * each workflow step runs at a characteristic time relative to the case's
    reported_at (profile submission about an hour before, policy actions in
    the half hour before, final results and status checks in the two hours
    after), so a row can only belong to an authorized case whose window for
    that kind of row contains it;
  * a resource that any evidence links to a known person by identifier is not
    anonymous, and is left to identifier linking;
  * the primary-profile row's reported result is the workflow's own summary,
    so it must agree with the case's reported_status;
  * a case holds at most one resource of each type (one primary profile, one
    analytics profile, ...), and a type it already has by identifier is closed;
  * rows of one workflow run keep a fixed cadence: measured from the run's
    primary-profile row, each later row falls on one of a few second-of-minute
    offsets. The offsets are not assumed — they are learned on every run from
    cases whose rows are already attributed with certainty (by identifier or
    by elimination), and then used only to rule candidates out.

A resource is assigned only when exactly one candidate case is left, repeating
until nothing changes. Ambiguous resources stay unassigned: a missed link costs
one verification step, a wrong one adds a false record to two cases.
"""

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from audit.identity import LINK_KINDS

# Detail-text marker -> (earliest, latest) hours relative to reported_at.
TEMPLATE_WINDOWS = (
    ("submitted primary profile", (-1.5, -0.5)),
    ("Policy action completed", (-0.75, 0.0)),
    ("received the final result", (0.25, 2.25)),
    ("Connector status check confirmed", (0.25, 2.25)),
    ("Restore-access check confirmed", (0.25, 2.25)),
)
DEFAULT_WINDOW = (-0.5, 0.5)  # single-step workflow summaries reported at reported_at
# The primary-profile row reports the same outcome the workflow reported for the case.
PROFILE_RESULTS = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
PROFILE_TEMPLATE = "submitted primary profile"
# A cadence offset is trusted when it covers this share of the calibration rows,
# and a row class is only checked once it has this many calibration rows.
OFFSET_MIN_SHARE = 0.02
CALIBRATION_MIN_ROWS = 30


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resource_type(resource_id: str) -> str:
    return resource_id.rsplit("_", 1)[0]


def _window(details: str) -> tuple[timedelta, timedelta]:
    earliest, latest = next((w for marker, w in TEMPLATE_WINDOWS if marker in details), DEFAULT_WINDOW)
    return timedelta(hours=earliest), timedelta(hours=latest)


def _row_class(row: sqlite3.Row) -> str:
    return "policy" if "Policy action completed" in row["details"] else "result"


def _offset(row: sqlite3.Row, anchor: datetime) -> int:
    return int((_time(row["event_time"]) - anchor).total_seconds()) % 60


def identified_resources(conn: sqlite3.Connection) -> set[str]:
    """Resources that some observation ties to a known person by identifier, at any time."""
    placeholders = ", ".join("?" for _ in LINK_KINDS)
    rows = conn.execute(
        f"""SELECT DISTINCT o.resource_id FROM obs_identifiers oi
            JOIN observations o USING (obs_id)
            JOIN identifiers i ON i.kind = oi.kind AND i.value = oi.value
            WHERE oi.kind IN ({placeholders}) AND o.resource_id IS NOT NULL""",
        LINK_KINDS,
    )
    return {row["resource_id"] for row in rows}


def _fits(row: sqlite3.Row, request: sqlite3.Row) -> bool:
    earliest, latest = _window(row["details"])
    reported = _time(request["reported_at"])
    if not reported + earliest <= _time(row["event_time"]) <= reported + latest:
        return False
    if PROFILE_TEMPLATE in row["details"]:
        return PROFILE_RESULTS.get(request["reported_status"]) == row["reported_result"]
    return True


class _Attribution:
    """Candidate sets and forced assignments for anonymous worker resources."""

    def __init__(self, rows_by_resource, candidates, case_resources, worker_rows):
        self.rows = rows_by_resource                 # anonymous resource -> worker rows
        self.candidates = candidates                 # anonymous resource -> candidate cases
        self.case_resources = case_resources         # case -> resources known to be its own
        self.worker_rows = worker_rows               # any resource -> worker rows
        self.assigned: dict[str, str] = {}
        self.offsets: dict[str, set[int]] = {}

    def assign(self, resource_id: str, case_id: str) -> None:
        self.assigned[resource_id] = case_id
        self.case_resources[case_id].add(resource_id)
        kind = resource_type(resource_id)
        for other, options in self.candidates.items():
            if other not in self.assigned and resource_type(other) == kind:
                options.discard(case_id)

    def propagate(self) -> bool:
        changed = False
        progress = True
        while progress:
            progress = False
            for resource_id, options in sorted(self.candidates.items()):
                if resource_id not in self.assigned and len(options) == 1:
                    self.assign(resource_id, next(iter(options)))
                    progress = changed = True
        return changed

    def anchor(self, case_id: str) -> datetime | None:
        """Time of the case's primary-profile row, if exactly one is known."""
        times = {
            _time(row["event_time"])
            for resource_id in self.case_resources[case_id] if resource_type(resource_id) == "account"
            for row in self.worker_rows.get(resource_id, ()) if PROFILE_TEMPLATE in row["details"]
        }
        return next(iter(times)) if len(times) == 1 else None

    def calibrate(self) -> None:
        """Learn the cadence offsets from cases whose rows are attributed with certainty."""
        observed: dict[str, Counter] = defaultdict(Counter)
        for case_id, resources in self.case_resources.items():
            anchor = self.anchor(case_id)
            if anchor is None:
                continue
            for resource_id in resources:
                if resource_type(resource_id) == "account":
                    continue
                for row in self.worker_rows.get(resource_id, ()):
                    observed[_row_class(row)][_offset(row, anchor)] += 1
        self.offsets = {
            row_class: {offset for offset, n in counts.items() if n >= OFFSET_MIN_SHARE * sum(counts.values())}
            for row_class, counts in observed.items() if sum(counts.values()) >= CALIBRATION_MIN_ROWS
        }

    def _on_cadence(self, rows, anchor: datetime) -> bool:
        return all(
            _offset(row, anchor) in self.offsets[_row_class(row)]
            for row in rows if _row_class(row) in self.offsets and PROFILE_TEMPLATE not in row["details"]
        )

    def prune(self) -> bool:
        """Drop candidate cases whose known workflow cadence contradicts the resource's rows."""
        changed = False
        for resource_id, options in self.candidates.items():
            if resource_id in self.assigned:
                continue
            rows = self.rows[resource_id]
            for case_id in sorted(options):
                if resource_type(resource_id) == "account":
                    profile = [r for r in rows if PROFILE_TEMPLATE in r["details"]]
                    if len(profile) != 1:
                        continue
                    anchor = _time(profile[0]["event_time"])
                    others = [row for other in self.case_resources[case_id] if resource_type(other) != "account"
                              for row in self.worker_rows.get(other, ())]
                    fits = self._on_cadence(others, anchor)
                else:
                    anchor = self.anchor(case_id)
                    fits = anchor is None or self._on_cadence(rows, anchor)
                if not fits:
                    options.discard(case_id)
                    changed = True
        return changed


def correlate_workflows(
    conn: sqlite3.Connection,
    requests: list[sqlite3.Row],
    authorized: set[str],
    identifier_links: dict[str, set[str]],
) -> dict[str, dict[str, set[str]]]:
    """case_id -> {resource_id: activity ids} for anonymous resources attributed to the case.

    identifier_links maps each authorized case to the resources linked to it by identifier.
    """
    cases = {r["case_id"]: r for r in requests if r["case_id"] in authorized}
    identified = identified_resources(conn)
    closed_types = {cid: {resource_type(r) for r in identifier_links.get(cid, ())} for cid in cases}

    worker_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        "SELECT * FROM activity WHERE source_system = 'deletion_worker' AND resource_id != '' ORDER BY event_time"
    ):
        worker_rows[row["resource_id"]].append(row)
    rows_by_resource = {rid: rows for rid, rows in worker_rows.items() if rid not in identified}

    candidates: dict[str, set[str]] = {}
    for resource_id, rows in rows_by_resource.items():
        kind = resource_type(resource_id)
        options = {
            case_id for case_id, request in cases.items()
            if kind not in closed_types[case_id] and all(_fits(row, request) for row in rows)
        }
        if options:
            candidates[resource_id] = options

    attribution = _Attribution(
        rows_by_resource, candidates,
        {cid: set(identifier_links.get(cid, ())) for cid in cases}, worker_rows,
    )
    attribution.propagate()
    attribution.calibrate()
    while attribution.prune() and attribution.propagate():
        pass

    links: dict[str, dict[str, set[str]]] = defaultdict(dict)
    for resource_id, case_id in attribution.assigned.items():
        links[case_id][resource_id] = {row["activity_id"] for row in rows_by_resource[resource_id]}
    return dict(links)
