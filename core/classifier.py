"""Deterministic, metadata/keyword classifier.

Used as (a) the fallback when the local model is unavailable or fails, and
(b) the metadata-only baseline in the evaluation. It is intentionally simple
and always marks its output as needing review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.scanner import FileRecord

KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("Invoices", ("invoice", "inv_", "bill", "amount due", "due date", "electricity")),
    ("Receipts", ("receipt", "rcpt", "thank you for your purchase", "order total", "paid with")),
    ("Assignments", ("assignment", "homework", "hw_", "coursework", "submitted by", "due:", "essay")),
    ("Notes", ("notes", "meeting", "minutes", "todo", "agenda", "journal")),
    ("Screenshots", ("screenshot", "screen shot", "screen_shot", "snip", "capture")),
]


@dataclass
class HeuristicSuggestion:
    category: str
    suggested_filename: str
    reason: str
    confidence: float


def _stem_words(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    words = [w for w in re.split(r"[^A-Za-z0-9]+", stem) if w]
    return "_".join(w.capitalize() for w in words) or "File"


def classify_heuristic(record: FileRecord, use_content: bool = True) -> HeuristicSuggestion:
    haystack = record.name.lower()
    if use_content:
        haystack += " " + record.excerpt[:800].lower()

    for category, words in KEYWORDS:
        hit = next((w for w in words if w in haystack), None)
        if hit:
            where = "filename" if hit in record.name.lower() else "extracted text"
            return HeuristicSuggestion(category, _stem_words(record.name), f"Keyword '{hit.strip()}' in {where}.", 0.5)

    by_kind = {"image": "Images", "spreadsheet": "Spreadsheets", "pdf": "Documents", "docx": "Documents", "text": "Documents"}
    category = by_kind.get(record.kind, "Other")
    return HeuristicSuggestion(category, _stem_words(record.name), f"Based on file type ({record.ext or 'none'}).", 0.3)
