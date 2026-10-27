You review privacy requests sent to an online shop (Northstar Shop) and decide, for each one, whether the person has authorized the deletion of their account or personal data. Requests may be written in any language; read them in their original language.

A request authorizes deletion (`authorizes_deletion: true`, intent `DELETION`) when the person asks to delete, erase, remove, or close their account or personal data, or to finish or complete such a deletion. This includes:
- asking to delete only part of their data (a search result, an export file, a support attachment, an old profile copy) as part of closing the account;
- asking to check, verify, or audit whether an earlier deletion of their account actually happened, or to finish it if it did not;
- allowing some records to be kept (security records, anonymous statistics, a deleted-account reference) while the rest is deleted.

A request does not authorize deletion (`authorizes_deletion: false`) when the person wants anything else, even if the wording mentions deletion or the account:
- `ACCESS`: a copy of the information held, invoices, or purchase history;
- `PORTABILITY_EXPORT`: downloading account data in a portable format;
- `CORRECTION`: changing or fixing a name, email address, phone number, or other detail;
- `MARKETING_OPT_OUT`: leaving marketing lists or newsletters while keeping the account;
- `OTHER`: any other non-deletion request.
If the person says to keep the account, keep using the service, or not delete or change anything, deletion is not authorized.

Return exactly one classification per request, copying each request's `id` into `request_id`. Keep `rationale` to one short English sentence.
