"""Provider-agnostic chat-model access, with token-usage accounting.

The model is chosen by the MODEL environment variable in LangChain's
"provider:model" form (e.g. "anthropic:claude-sonnet-5-5"); the matching
provider package and API key variable must be present. Every call appends one
line to the usage log so the usage report can be built from real numbers.
"""

import json
import os
import time
from functools import cache
from typing import Any

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from audit.paths import CACHE_DIR, REPO_ROOT

USAGE_LOG = CACHE_DIR / "llm_usage.jsonl"

# Provider-specific request settings. Extraction and classification are simple,
# well-specified tasks, so Anthropic models run at low effort.
PROVIDER_KWARGS: dict[str, dict[str, Any]] = {
    "anthropic": {"effort": "low"},
}

# USD per million tokens (input, output), for the usage report.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class LLMUnavailable(RuntimeError):
    """Raised when a model call is needed but no model is configured."""


def model_name() -> str:
    load_dotenv(REPO_ROOT / ".env")
    name = os.environ.get("MODEL", "").strip()
    if not name:
        raise LLMUnavailable("MODEL is not set; add MODEL=<provider>:<model> to .env")
    return name


@cache
def chat_model() -> BaseChatModel:
    name = model_name()
    provider = name.split(":", 1)[0] if ":" in name else ""
    return init_chat_model(name, max_tokens=16000, **PROVIDER_KWARGS.get(provider, {}))


def structured_call(purpose: str, schema: type, messages: list[Any]) -> Any:
    """Call the configured model with native structured output and log token usage.

    Uses the provider's JSON-schema mode rather than forced tool calling, which
    current Claude models reject.
    """
    runnable = chat_model().with_structured_output(schema, method="json_schema", include_raw=True)
    started = time.monotonic()
    result = runnable.invoke(messages)
    raw = result["raw"]
    stop_reason = (raw.response_metadata or {}).get("stop_reason")
    usage = raw.usage_metadata or {}
    record_usage(purpose, usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                 time.monotonic() - started, stop_reason)
    if stop_reason == "refusal":
        raise RuntimeError(f"model refused the {purpose} request")
    if result.get("parsing_error") or result.get("parsed") is None:
        raise RuntimeError(f"{purpose}: could not parse structured output: {result.get('parsing_error')}")
    return result["parsed"]


def record_usage(purpose: str, input_tokens: int, output_tokens: int, seconds: float, stop_reason: str | None) -> None:
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model_name(),
        "purpose": purpose,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "seconds": round(seconds, 2),
        "stop_reason": stop_reason,
    }
    with USAGE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
