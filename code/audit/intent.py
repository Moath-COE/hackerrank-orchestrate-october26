"""Request-intent classification: does a request actually authorize deletion?

This is the only model call made per request. Distinct request texts are sent
in batches, and each answer is cached on disk keyed by the text, the prompt and
the model, so a rerun makes no calls and returns identical results.
"""

import hashlib
import json
import sqlite3
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from audit.llm import LLMUnavailable, model_name, structured_call
from audit.paths import CACHE_DIR, CODE_DIR

PROMPT_PATH = CODE_DIR / "prompts" / "request_classification.md"
CACHE_PATH = CACHE_DIR / "request_intents.json"
BATCH_SIZE = 20

Intent = Literal["DELETION", "ACCESS", "PORTABILITY_EXPORT", "CORRECTION", "MARKETING_OPT_OUT", "OTHER"]


class RequestClassification(BaseModel):
    request_id: str = Field(description="The id of the request, copied exactly")
    intent: Intent
    authorizes_deletion: bool
    rationale: str = Field(description="One short English sentence")


class ClassificationBatch(BaseModel):
    classifications: list[RequestClassification]


def _cache_key(text: str) -> str:
    prompt_sha = hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()
    return hashlib.sha256(f"{prompt_sha}\n{model_name()}\n{text}".encode()).hexdigest()


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def _classify_batch(texts: list[str]) -> dict[str, RequestClassification]:
    """Classify up to BATCH_SIZE texts in one call; returns text -> classification."""
    ids = {f"r{index}": text for index, text in enumerate(texts, start=1)}
    body = "\n\n".join(f'<request id="{rid}">\n{text}\n</request>' for rid, text in ids.items())
    messages = [SystemMessage(PROMPT_PATH.read_text(encoding="utf-8")), HumanMessage(body)]
    result = structured_call(f"classify:{len(texts)}", ClassificationBatch, messages)
    by_id = {item.request_id: item for item in result.classifications}
    return {text: by_id[rid] for rid, text in ids.items() if rid in by_id}


def cross_check(conn: sqlite3.Connection, requests: list[sqlite3.Row],
                intents: dict[str, RequestClassification]) -> list[str]:
    """Compare the model's decisions with the privacy-intake rows of the activity log.

    Intake logs "classified request for account_X as non-deletion" while a request
    is open. Those rows are claims and never decide a case, but a disagreement is
    worth a human look. Returns one warning line per disagreement.
    """
    owners = {}
    for row in conn.execute(
        """SELECT DISTINCT a.activity_id, a.event_time, i.user_id FROM activity a
           JOIN observations o ON o.resource_id = a.resource_id
           JOIN obs_identifiers oi ON oi.obs_id = o.obs_id
           JOIN identifiers i ON i.kind = oi.kind AND i.value = oi.value
           WHERE a.event_type = 'CLASSIFY_REQUEST' AND i.kind IN ('user_id', 'email', 'email_hash', 'phone')"""
    ):
        owners.setdefault((row["activity_id"], row["event_time"]), set()).add(row["user_id"])

    users = {
        row["case_id"]: row["user_id"]
        for row in conn.execute(
            "SELECT DISTINCT r.case_id, i.user_id FROM requests r "
            "JOIN identifiers i ON i.kind = 'email' AND i.value = lower(r.email)"
        )
    }
    flagged_non_deletion = set()
    for (activity_id, event_time), user_ids in owners.items():
        for request in requests:
            if users.get(request["case_id"]) in user_ids and request["requested_at"] <= event_time <= request["reported_at"]:
                flagged_non_deletion.add(request["case_id"])

    warnings = []
    for request in requests:
        model_says = intents[request["request_text"]].authorizes_deletion
        intake_says = request["case_id"] not in flagged_non_deletion
        if model_says != intake_says:
            warnings.append(
                f"{request['case_id']}: model says authorizes_deletion={model_says}, privacy intake log says {intake_says}"
            )
    return warnings


def classify_requests(texts: list[str], allow_model: bool = True) -> dict[str, RequestClassification]:
    """Return a classification for every distinct text, calling the model only for cache misses."""
    cache = _load_cache()
    keys = {text: _cache_key(text) for text in dict.fromkeys(texts)}
    missing = [text for text, key in keys.items() if key not in cache]
    if missing and not allow_model:
        raise LLMUnavailable(f"{len(missing)} request texts have no cached classification and --no-llm was given")

    for start in range(0, len(missing), BATCH_SIZE):
        batch = missing[start:start + BATCH_SIZE]
        answers = _classify_batch(batch)
        # A batch answer that skipped an item is retried on its own.
        for text in batch:
            answer = answers.get(text) or _classify_batch([text]).get(text)
            if answer is None:
                raise RuntimeError(f"model returned no classification for request text: {text[:80]!r}")
            cache[keys[text]] = {**answer.model_dump(exclude={"request_id"}), "text": text}
        _save_cache(cache)

    return {
        text: RequestClassification(request_id="", **{k: v for k, v in cache[key].items() if k != "text"})
        for text, key in keys.items()
    }
