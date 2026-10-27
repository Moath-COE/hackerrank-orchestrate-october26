"""Build the identifiers index from the current snapshot plus the identity change history.

A request email may be historical, and old systems may still hold an old phone
or hash, so every value a user ever had is indexed with its validity window.
"""

import sqlite3

from audit.normalize import email_hash, normalize


def build_identifiers(conn: sqlite3.Connection) -> None:
    rows: list[tuple] = []

    for user in conn.execute("SELECT * FROM identities"):
        for kind, column in (("user_id", "user_id"), ("name", "name"), ("email", "email"),
                             ("phone", "phone"), ("email_hash", "email_hash")):
            raw = user[column]
            if raw:
                rows.append((user["user_id"], kind, normalize(kind, raw), raw, "current", None, None))

    for change in conn.execute("SELECT * FROM identity_audit ORDER BY event_time, audit_id"):
        kind = change["field_name"]
        if kind not in ("email", "phone", "email_hash", "name"):
            continue
        if change["old_value"]:
            rows.append((change["user_id"], kind, normalize(kind, change["old_value"]),
                         change["old_value"], change["audit_id"], None, change["event_time"]))
        if change["new_value"]:
            rows.append((change["user_id"], kind, normalize(kind, change["new_value"]),
                         change["new_value"], change["audit_id"], change["event_time"], None))

    # Old systems may index a historical email only by its hash; derive any that are missing.
    known_hashes = {(r[0], r[2]) for r in rows if r[1] == "email_hash"}
    for user_id, kind, _value, raw, _source, valid_from, valid_to in list(rows):
        if kind == "email":
            digest = email_hash(raw)
            if (user_id, digest) not in known_hashes:
                known_hashes.add((user_id, digest))
                rows.append((user_id, "email_hash", digest, digest, "derived", valid_from, valid_to))

    conn.executemany(
        """INSERT INTO identifiers (user_id, kind, value, raw_value, source, valid_from, valid_to)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
