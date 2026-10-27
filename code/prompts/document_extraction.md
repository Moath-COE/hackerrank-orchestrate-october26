You are extracting facts from one business artifact of an online shop (Northstar Shop) for a data-deletion audit. The artifact is either a screenshot of an internal admin console or the text layer of a PDF document. Downstream code matches your output against other records, so precision matters more than coverage of decorative content.

Extract one record per distinct data entity the artifact shows: each table row, each detail panel, each listed document or reference. If a detail panel describes the same entity as a table row, merge them into a single record. Ignore navigation menus, aggregate metrics (counts, rates, sizes), and chart widgets unless they describe a specific entity.

For every record:
- Copy identifiers exactly as shown, character for character: resource ids such as `account_5762`, `support_001`, `backup_9287`, `data_flow_1121`, `marketing_001`, `audit_record_003`; user ids such as `user_17`; emails; phone numbers; hashes. Never construct, complete, or normalize an identifier that is not visibly present.
- `resource_id` is the entity's own system id in `<prefix>_<number>` form. Use null when the entity has no such id on screen (an order number or ticket number alone is not a resource id; put those in `other_references`).
- `user_ref` is a `user_<number>` reference attached to the entity, or null.
- `name`, `email`, `phone`, `email_hash` are the personal identifiers of a person (customer) displayed for that entity, or null. `name` is only ever a person's name: never put a connector, product, team, file, or system name there (describe those in `record_kind` or `notes`). Do not copy one row's identifiers into another row.
- `other_references` lists other identifying strings tied to the entity: order ids, ticket ids, statement numbers, file names, archive paths, object keys.
- `status_text` is the verbatim status label(s) shown for the entity (for example "Live", "Healthy", "Retained reference", "Continuous", "Archive", "Delivered"), or null.
- `state` is your reading of the entity's data state, using only explicit cues:
  - ACTIVE: a running sync, connector, or data flow (enabled, healthy, continuous, live delivery), or a backup shown as restorable.
  - DISABLED: a connector or data flow shown as disabled, paused, or stopped.
  - BLOCKED: a backup or archive whose restore is shown as blocked, denied, or disabled.
  - REDACTED: a record shown with its personal fields masked or marked redacted while the record remains.
  - ANONYMIZED: a record shown as anonymized or no longer linked to a person.
  - ABSENT: the artifact states the record was not found or no longer exists.
  - PRESENT: the record is shown as existing and none of the above applies.
- `observed_at` is the timestamp the artifact attaches to this entity's current state (ISO 8601, UTC, e.g. `2026-09-12T07:30:00Z`), or null. Only convert from the artifact's own wording; if no year is shown, use the artifact-level date.
- `retain_until` is a stated retention end for the entity (ISO 8601), or null.
- `notes` is one short factual sentence of anything else relevant to deletion (restore allowed, selector, destination, retention reason), or null.

At the document level, give the artifact's title, the system or product area it belongs to if evident, and the artifact-level capture or "as of" timestamp if one is shown (ISO 8601), else null.
