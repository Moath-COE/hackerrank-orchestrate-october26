# Evaluation Report

All numbers are reproducible with:

```bash
uv run --project code python code/main.py evaluate
```

This command runs three things:

1. the full pipeline on the 32 public sample requests, scored against `dataset/sample_output.csv`;
2. 8 hand-built audit cases;
3. 6 hand-built classifier cases.

Metric definitions live in the docstring of `code/audit/scoring.py`.

## 1. Results on the 32 sample requests

### Case-level status accuracy

| Expected \ Predicted | COMPLETE | INCOMPLETE | NOT_APPLICABLE |
| --- | ---: | ---: | ---: |
| COMPLETE | **10** | 0 | 0 |
| INCOMPLETE | 0 | **22** | 0 |

**Status accuracy: 1.000 (32/32).**

The samples contain no non-deletion request. That decision is evaluated separately (§2.2), and on the evaluation set the model's decisions are cross-checked against the activity log at run time (§3).

### Step-level accuracy

A step is compared as the whole tuple (phase, system_id, resource_id, operation, state, evidence_id). Matches are counted per case, as multisets.

| Metric | Value |
| --- | ---: |
| Expected steps | 193 |
| Step precision | **1.000** |
| Step recall | **0.974** |
| Step F1 | **0.987** |
| Resource recall (expected resource ids found at all) | 0.955 |

Every step the agent emits on the samples is correct. All 5 missed steps are for resources the agent did not find; see §5.

### Field-level accuracy

Expected steps are aligned to predicted ones by (phase, resource_id, operation). A field is scored correct when the aligned prediction has the same value. A missing step counts as wrong on every field.

| Field | Accuracy |
| --- | ---: |
| `system_id` | 0.974 |
| `state` | 0.974 |
| `evidence_id` | 0.974 |
| `step_no` | 0.922 |

On every aligned step, `system_id`, `state` and `evidence_id` match. `step_no` scores lower only because a missing step shifts the numbering of every later step in its case.

### Sequence accuracy

| Metric | Value |
| --- | ---: |
| Exact full-sequence match (status + every step, in order, with step numbers) | **0.844 (27/32)** |
| Mean ordered-sequence similarity (LCS / longer sequence) | 0.963 |

Ordering never causes a mismatch. The 5 inexact cases each miss one step and are otherwise in the expected order.

## 2. Custom evaluation cases

### 2.1 Audit cases (8/8 pass)

Each case is a small, isolated world: a fresh database with the real `deletion_rules.csv`, one fictional subject, and hand-placed evidence. The case runs through the production LangGraph workflow, and its result is compared with an answer written by hand from the challenge rules. Source: `code/evaluation/custom_cases.py`.

| # | Case | What it checks | Expected answer | Result |
| --- | --- | --- | --- | --- |
| 1 | Stale sighting, newer listing | A full snapshot listing taken after the request proves absence over a profile row seen before the request | COMPLETE, `VERIFY_RECORD_ABSENT` citing the listing | PASS |
| 2 | Fresh sighting, newer listing | A lookup that finds the profile after the request outranks a later listing that omits it (pattern of sample case_24) | INCOMPLETE, finding cites the lookup | PASS |
| 3 | Replica drift | A failover-replica row is a remaining copy even when the primary table lacks it | INCOMPLETE, `REPORT_REMAINING_RECORD` → `DELETE_RECORD` → `VERIFY_RECORD_ABSENT` | PASS |
| 4 | Evidence after `audit_at` | A file captured after `audit_at` that shows the record again can neither change nor support the answer | COMPLETE, citing the pre-audit evidence | PASS |
| 5 | Tombstone, retention, blocked backup | A tombstone keeping only `user_id` is correctly redacted; a security record within retention and a blocked backup are verified | COMPLETE: PRESENT, REDACTED and BLOCKED verifications in operation order | PASS |
| 6 | Recreation risks first | Search entry, identifiable analytics, an active connector and a restorable backup | INCOMPLETE: 4 findings, then `DISABLE_DATA_FLOW` and `BLOCK_RESTORE` before `DELETE`/`ANONYMIZE`, then 4 verifications | PASS |
| 7 | Repeat request by the same person | An account found in an earlier request's episode is out of scope; only the new account is audited | INCOMPLETE on the new account only | PASS |
| 8 | Not a deletion request | Same evidence as case 6, but intent is ACCESS | NOT_APPLICABLE, `[]` | PASS |

To check that the cases have teeth, two behaviours were switched off in-process:

- disabling listing-based absence makes case 1 fail;
- disabling the previous-episode window makes case 7 fail.

Each case catches the regression it targets.

### 2.2 Classifier cases (6/6 pass)

Real (cached) model calls with the production prompt `code/prompts/request_classification.md`:

| Request text | Expected | Model intent | Result |
| --- | --- | --- | --- |
| "Please unsubscribe me from your newsletters. I am not asking you to delete my account." | no deletion | MARKETING_OPT_OUT | PASS |
| "Löschen Sie bitte mein Konto und alle damit verbundenen personenbezogenen Daten." | deletion | DELETION | PASS |
| "Last month I asked you to delete my account. Can you check whether that really happened and finish it if not?" | deletion | DELETION | PASS |
| "Mi apellido está mal escrito en la cuenta. ¿Pueden corregirlo, por favor?" | no deletion | CORRECTION | PASS |
| "Close my account. You may keep the invoices you are legally required to retain." | deletion | DELETION | PASS |
| "Please send me a copy of my data first; I have not decided yet whether to close the account." | no deletion | ACCESS | PASS |

## 3. Evaluation-set checks (no labels available)

| Check | Result |
| --- | --- |
| Rows | 518, one per request, sorted by case number, header `case_id,deletion_status,steps_json` |
| Contract validator (numbering, phase/operation order, allowed operation/state pairs, evidence rules) | 0 problems |
| Status distribution | COMPLETE 155, INCOMPLETE 353, NOT_APPLICABLE 10 |
| Non-deletion requests vs the activity log's privacy-intake classifications | 10 vs 10, 0 disagreements |
| Determinism | a cold run (all model caches deleted) and a cached run produce byte-identical `output.csv` |
| Deletion cases with no account step | 37 of 508; about 10 belong to an early family of cases whose account appears nowhere in the data |

## 4. Failures found and improvements made

Each row is one iteration, measured on the samples with the same scorer.

| # | Change | Failure it addressed | Status acc. | Precision | Recall | F1 | Exact seq. |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | Baseline: identifier linking; every snapshot listing treated as complete | — | 0.969 | 0.993 | 0.767 | 0.865 | 0.281 |
| 2 | Listing precedence: an explicit sighting after the request outranks a listing absence. Documents observed at their capture or as-of time instead of the row's own update time | Partial export bundles "proved" present records absent (case_24 profile, case_21 audit record). A PDF's 4-month-old row date let an older listing override it (case_07) | 1.000 | 1.000 | 0.819 | 0.900 | 0.281 |
| 3 | Link on when a fact happened (`observed_at`), cite only files captured by `audit_at` | Audit records seen in logs written before `audit_at` but exported later were dropped (case_09) | 1.000 | 1.000 | 0.855 | 0.922 | 0.344 |
| 4 | Cross-case workflow correlation: anonymous deletion-worker rows attributed by time window, status agreement and elimination | Deleted accounts and anonymized analytics records carry no identifier at all | 1.000 | 0.978 | 0.927 | 0.952 | 0.594 |
| 5 | Per-template time windows; resources identified to anyone are excluded; no forcing of profile rows onto cases | Step 4 gave case_04 another case's account (case_04 has no profile row), and gave case_12 a search entry that belongs to another user | 1.000 | 1.000 | 0.933 | 0.965 | 0.656 |
| 6 | Workflow cadence, learned per run from already-certain attributions, used only to rule candidates out | Several cases share one `reported_at`, so windows alone left 93 of 118 account gaps ambiguous | 1.000 | 1.000 | **0.974** | **0.987** | **0.844** |
| 7 | Catalog entries prove existence only when no other state evidence exists. Support records described as tickets or conversations count as messages, and the worker's attempted operation is the last-resort category hint | case_61 answered COMPLETE with an empty step list. A support ticket with shared agent/order context (case_53) was deleted instead of redacted | 1.000 | 1.000 | 0.974 | 0.987 | 0.844 |

Effect on the evaluation set:

- Step 6 cut deletion cases without an account step from 118 to 37.
- Step 7 changed exactly two evaluation answers (case_53, case_61) and none of the samples.

One attempt was rolled back. Treating every catalog entry as a presence sighting (first version of step 7) let catalog rows logged minutes after a screenshot or snapshot displace that evidence. That changed citations in 6 evaluation cases and flipped 2 statuses. The rule was narrowed to "only when nothing else shows the record's state" before anything shipped.

## 5. Known remaining failures

| Missed step (samples) | Cause | Fixable? |
| --- | --- | --- |
| case_04 `account_004`; case_18 `audit_record_6645`; case_20 `audit_record_5569` | These ids occur nowhere in the participant data except the answer key | No |
| case_21 `analytics_5335`; case_27 `account_3866` | Anonymous worker rows still shared by more than one candidate case after elimination and cadence checks; deliberately left unassigned rather than guessed | Only by guessing, which risks false records in two cases |

Heuristics worth knowing:

- The listing-precedence rule (step 2) and the category cues (step 7) were inferred from a handful of sample cases.
- They are covered by custom cases 1–2 and by the evaluation-set diff checks above, but on new data they are the most likely source of error.
- 277 compliant verdicts in the evaluation set rest on listing-based absence.
