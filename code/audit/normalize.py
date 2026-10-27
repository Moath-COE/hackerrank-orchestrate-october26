"""Canonical forms for personal identifiers, plus extraction from arbitrary text.

Evidence spells the same identifier many ways (upper-cased emails, phones with
dashes or spaces, user ids embedded in sentences), so every comparison happens
on the normalized value produced here.
"""

import hashlib
import re
import unicodedata

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")
HASH_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
USER_REF_RE = re.compile(r"\buser_\d+\b")

# Identifier kinds, ordered from strongest to weakest link to a person.
KINDS = ("user_id", "email", "email_hash", "phone", "name")


def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def norm_hash(value: str) -> str:
    return value.strip().lower()


def norm_user_ref(value: str) -> str:
    return value.strip().lower()


def norm_name(value: str) -> str:
    """Accent-, case- and punctuation-insensitive name form ("Mateo Ríos" == "MATEO RIOS")."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-z]+", " ", ascii_only.casefold()).split())


def email_hash(email: str) -> str:
    """The dataset's email_hash is sha256 of the email as stored (verified on user_identities)."""
    return hashlib.sha256(email.strip().encode()).hexdigest()


NORMALIZERS = {
    "user_id": norm_user_ref,
    "email": norm_email,
    "email_hash": norm_hash,
    "phone": norm_phone,
    "name": norm_name,
}


def normalize(kind: str, value: str) -> str:
    return NORMALIZERS[kind](value)


def extract_identifiers(text: str) -> set[tuple[str, str]]:
    """Find every machine-recognizable identifier in free text.

    Names are deliberately not extracted: they collide across users and only
    count when they appear in an explicit name field.
    """
    found: set[tuple[str, str]] = set()
    for match in EMAIL_RE.findall(text):
        found.add(("email", norm_email(match)))
    for match in PHONE_RE.findall(text):
        digits = norm_phone(match)
        if len(digits) >= 8:
            found.add(("phone", digits))
    for match in HASH_RE.findall(text):
        found.add(("email_hash", norm_hash(match)))
    for match in USER_REF_RE.findall(text):
        found.add(("user_id", norm_user_ref(match)))
    return found
