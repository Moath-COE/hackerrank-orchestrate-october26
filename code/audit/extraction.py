"""One-time structured extraction of PDF and screenshot evidence.

PDFs carry a text layer, which is extracted locally with pypdf and sent as
text; screenshots are sent as images to a vision-capable model. Both return the
same ExtractedDocument schema. Results are cached on disk keyed by the file's
content hash, the prompt, and the model, so ingestion only calls the model when
one of those changes.
"""

import base64
import hashlib
import json
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field
from pypdf import PdfReader

from audit.llm import model_name, structured_call
from audit.paths import CACHE_DIR, CODE_DIR

PROMPT_PATH = CODE_DIR / "prompts" / "document_extraction.md"
EXTRACTION_DIR = CACHE_DIR / "extractions"

State = Literal["PRESENT", "ABSENT", "REDACTED", "ANONYMIZED", "ACTIVE", "DISABLED", "BLOCKED"]


class ExtractedRecord(BaseModel):
    resource_id: str | None = Field(description="The entity's own <prefix>_<number> id exactly as shown, or null")
    system_hint: str | None = Field(description="Owning system or product area if evident, or null")
    record_kind: str | None = Field(description="Short description of what the entity is, e.g. 'backup archive'")
    status_text: str | None = Field(description="Verbatim status label(s) shown for the entity, or null")
    state: State | None = Field(description="Data state read from explicit cues only, or null")
    name: str | None
    email: str | None
    phone: str | None
    email_hash: str | None
    user_ref: str | None = Field(description="A user_<number> reference attached to the entity, or null")
    other_references: list[str] = Field(description="Order ids, ticket ids, file names, paths tied to the entity")
    observed_at: str | None = Field(description="ISO 8601 UTC timestamp of the entity's shown state, or null")
    retain_until: str | None = Field(description="ISO 8601 retention end shown for the entity, or null")
    notes: str | None = Field(description="One short factual sentence relevant to deletion, or null")


class ExtractedDocument(BaseModel):
    title: str
    system_hint: str | None
    shown_timestamp: str | None = Field(description="Artifact-level capture/as-of timestamp, ISO 8601, or null")
    records: list[ExtractedRecord]


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cache_key(path: Path) -> dict[str, str]:
    return {
        "file_sha256": _sha256(path.read_bytes()),
        "prompt_sha256": _sha256(PROMPT_PATH.read_bytes()),
        "model": model_name(),
    }


def _messages(path: Path) -> list:
    system = SystemMessage(PROMPT_PATH.read_text(encoding="utf-8"))
    if path.suffix.lower() == ".pdf":
        content = [{"type": "text", "text": f"PDF document `{path.name}`, text layer:\n\n{pdf_text(path)}"}]
    else:
        content = [
            {"type": "text", "text": f"Screenshot `{path.name}`."},
            {"type": "image", "base64": base64.b64encode(path.read_bytes()).decode(), "mime_type": "image/png"},
        ]
    return [system, HumanMessage(content=content)]


def extract_document(file_id: str, path: Path, allow_model: bool = True) -> ExtractedDocument | None:
    """Return the cached extraction for a file, calling the model only on a cache miss.

    Returns None on a cache miss when allow_model is False.
    """
    cache_path = EXTRACTION_DIR / f"{file_id}.json"
    key = _cache_key(path)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("key") == key:
            return ExtractedDocument.model_validate(cached["document"])
    if not allow_model:
        return None
    document = structured_call(f"extract:{file_id}", ExtractedDocument, _messages(path))
    EXTRACTION_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"key": key, "document": document.model_dump()}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return document
