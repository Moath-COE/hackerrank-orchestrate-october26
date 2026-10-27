"""Turn system_activity rows into observations.

Most activity rows are claims ("DELETE_RECORD ... SUCCESS") and get state NULL:
they point at a resource but never prove its state. A few templates report an
observed condition and carry a state:
  * CHECK_DATA_FLOW      "... data flow X remains enabled"          -> ACTIVE
  * CHECK_RESTORE_ACCESS "... archive X can still be restored"      -> ACTIVE
  * SYNC/RESTORE         "... repopulated downstream resource X"    -> PRESENT
LINK_RESOURCE_IDENTITY rows name the subject's email and user id in the details
text; those identifiers are indexed like any other observation's. A catalog
entry also shows the resource existed when it was catalogued, so it is a
PRESENT sighting; it says nothing about which fields the record still holds,
which audit.reconstruct accounts for.
"""

import sqlite3

from audit.ingest.common import Observation, insert_observation

STATE_PHRASES = (
    ("remains enabled", "ACTIVE"),
    ("can still be restored", "ACTIVE"),
    ("repopulated downstream resource", "PRESENT"),
)


CATALOG_EVENT = "LINK_RESOURCE_IDENTITY"


def activity_state(event_type: str, details: str) -> str | None:
    if event_type == CATALOG_EVENT:
        return "PRESENT"
    for phrase, state in STATE_PHRASES:
        if phrase in details:
            return state
    return None


def load_activity(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT * FROM activity ORDER BY event_time, activity_id").fetchall()
    for row in rows:
        obs = Observation(
            evidence_id=row["activity_id"], evidence_kind="activity",
            captured_at=row["event_time"], observed_at=row["event_time"],
            system_id=row["target_system"], resource_id=row["resource_id"], store=row["target_system"],
            state=activity_state(row["event_type"], row["details"]), locator=row["activity_id"],
            attrs={
                "event_type": row["event_type"],
                "reported_result": row["reported_result"],
                "source_system": row["source_system"],
                "target_system": row["target_system"],
                "details": row["details"],
            },
        )
        insert_observation(conn, obs)
    return len(rows)
