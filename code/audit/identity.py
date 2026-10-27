"""Resolve a request to its subject: the stable user_id plus every identifier they ever had.

The request email may be current or historical, so it is looked up in the
identifiers index (current snapshot + audit-log history + derived hashes).

A person can file more than one deletion request over the year. Evidence from
an earlier request's episode (up to that request's audit_at) belongs to that
episode: the account deleted in March is not the account deleted in August.
The subject therefore carries an evidence window start.
"""

import sqlite3
from dataclasses import dataclass, field

from audit.normalize import norm_email

# Kinds that link evidence to a person. Names collide across people and are
# only used to judge whether a linked record still exposes the person.
LINK_KINDS = ("user_id", "email", "email_hash", "phone")


@dataclass
class Subject:
    user_id: str
    identifiers: set[tuple[str, str]] = field(default_factory=set)  # (kind, normalized value)
    window_start: str | None = None  # previous episode's audit_at; earlier evidence is out of scope

    @property
    def link_identifiers(self) -> set[tuple[str, str]]:
        return {(kind, value) for kind, value in self.identifiers if kind in LINK_KINDS}


def resolve_subject(conn: sqlite3.Connection, request: sqlite3.Row) -> Subject | None:
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM identifiers WHERE kind = 'email' AND value = ?",
        (norm_email(request["email"]),),
    ).fetchall()
    if len(rows) != 1:
        return None  # unknown or ambiguous email: nothing can be attributed safely
    user_id = rows[0]["user_id"]

    identifiers = {
        (row["kind"], row["value"])
        for row in conn.execute("SELECT kind, value FROM identifiers WHERE user_id = ?", (user_id,))
    }

    # Earlier requests by the same person, across both request files.
    previous = conn.execute(
        """SELECT MAX(r.audit_at) FROM requests r
           JOIN identifiers i ON i.kind = 'email' AND i.value = lower(r.email)
           WHERE i.user_id = ? AND r.case_id != ? AND r.audit_at < ?""",
        (user_id, request["case_id"], request["requested_at"]),
    ).fetchone()[0]

    return Subject(user_id=user_id, identifiers=identifiers, window_start=previous)
