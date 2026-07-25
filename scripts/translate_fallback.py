"""Detect missing hi/ml translation on scraped scheme fields and fill gaps via Sarvam Mayura.

myscheme.gov.in's own translation coverage is inconsistent per-scheme: some schemes have
genuinely translated body content, others only translate the section header labels and
leave body text in English. Any field found in English gets machine-translated here and
flagged - per the project's DATASET NOTE, machine-translated fields must be marked clearly
for manual review before use, never treated as final silently.
"""

import os
import time

from sarvamai import SarvamAI

TARGET_LANG_CODE = {"hi": "hi-IN", "ml": "ml-IN"}
SCRIPT_RANGE = {
    "hi": ("ऀ", "ॿ"),  # Devanagari
    "ml": ("ഀ", "ൿ"),  # Malayalam
}
TRANSLATABLE_FIELDS = [
    "name",
    "ministry",
    "details",
    "benefits",
    "eligibility",
    "application_process",
    "documents_required",
]
MAX_CHARS = 900  # stay under mayura:v1's 1000-char limit with margin


def _has_target_script(text: str, lang: str) -> bool:
    if not text:
        return True  # empty field - nothing to translate, don't flag
    lo, hi = SCRIPT_RANGE[lang]
    return any(lo <= ch <= hi for ch in text)


def _chunk(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        split_at = remaining.rfind(". ", 0, max_chars)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, max_chars)
        if split_at == -1:
            split_at = max_chars
        else:
            split_at += 1
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    return chunks


def _translate(client: SarvamAI, text: str, lang: str) -> str:
    pieces = []
    for chunk in _chunk(text, MAX_CHARS):
        resp = client.text.translate(
            input=chunk,
            source_language_code="en-IN",
            target_language_code=TARGET_LANG_CODE[lang],
            model="mayura:v1",
            mode="formal",
        )
        pieces.append(resp.translated_text)
        time.sleep(0.2)  # be polite to the API
    return " ".join(pieces)


def fill_translation_gaps(record: dict) -> dict:
    """Mutates record in place: for hi/ml, machine-translates any field still in English
    (source text taken from the 'en' record) and records which fields were touched in
    record['hi']['mt_fields'] / record['ml']['mt_fields']."""
    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY not set - needed for Mayura translation fallback")
    client = SarvamAI(api_subscription_key=api_key)

    for lang in ("hi", "ml"):
        mt_fields = []
        for field in TRANSLATABLE_FIELDS:
            value = record[lang].get(field, "")
            if _has_target_script(value, lang):
                continue
            source_text = record["en"].get(field, "")
            if not source_text:
                continue
            record[lang][field] = _translate(client, source_text, lang)
            mt_fields.append(field)
        record[lang]["mt_fields"] = mt_fields
        record[lang]["translated"] = bool(mt_fields)

    return record
