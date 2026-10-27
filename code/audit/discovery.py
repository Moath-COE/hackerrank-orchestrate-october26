"""Discover which resources belong to a case's subject.

Identifier links: any observation (evidence file row, log lookup, catalog
activity) that carries one of the subject's identifiers — current or historical
email, phone, email hash, or user_id — links its resource to the subject. Only
facts observed within the case's window count: after the subject's previous
request episode, and no later than audit_at. The window is judged on when the
fact happened (observed_at), not when its file was exported: a log line written
before audit_at shows the link existed then, even if the log was collected
later. Such late files are never cited; reconstruction only cites evidence
captured by audit_at.
"""

import sqlite3

from audit.identity import Subject


def identifier_links(conn: sqlite3.Connection, subject: Subject, audit_at: str) -> dict[str, set[str]]:
    """resource_id -> evidence ids that tie it to the subject by identifier."""
    links: dict[str, set[str]] = {}
    for kind, value in sorted(subject.link_identifiers):
        rows = conn.execute(
            """SELECT o.resource_id, o.evidence_id, o.observed_at FROM obs_identifiers oi
               JOIN observations o USING (obs_id)
               WHERE oi.kind = ? AND oi.value = ? AND o.resource_id IS NOT NULL AND o.observed_at <= ?""",
            (kind, value, audit_at),
        )
        for row in rows:
            if subject.window_start and row["observed_at"] <= subject.window_start:
                continue
            links.setdefault(row["resource_id"], set()).add(row["evidence_id"])
    return links
