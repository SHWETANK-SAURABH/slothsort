"""Central configuration for SlothSort.

Every value can be overridden with an environment variable so the app can be
tuned without code changes. Documents being scanned can never change these
values: configuration is read once from the process environment only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


ALLOWED_CATEGORIES: tuple[str, ...] = (
    "Documents",
    "Invoices",
    "Receipts",
    "Assignments",
    "Notes",
    "Screenshots",
    "Images",
    "Spreadsheets",
    "Other",
)

TEXT_EXTENSIONS = {".txt", ".md", ".csv"}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | PDF_EXTENSIONS | DOCX_EXTENSIONS | IMAGE_EXTENSIONS

# Directory SlothSort writes its own state into. Always skipped by the scanner.
APP_DIR_NAME = ".slothsort"


@dataclass(frozen=True)
class Settings:
    ollama_url: str = field(default_factory=lambda: os.environ.get("SLOTHSORT_OLLAMA_URL", "http://localhost:11434"))
    model: str = field(default_factory=lambda: os.environ.get("SLOTHSORT_MODEL", "qwen2.5:3b"))
    request_timeout_s: float = field(default_factory=lambda: _env_float("SLOTHSORT_TIMEOUT", 120.0))
    max_retries: int = field(default_factory=lambda: _env_int("SLOTHSORT_MAX_RETRIES", 2))
    batch_size: int = field(default_factory=lambda: _env_int("SLOTHSORT_BATCH_SIZE", 4))
    excerpt_chars: int = field(default_factory=lambda: _env_int("SLOTHSORT_EXCERPT_CHARS", 1200))
    max_file_mb: int = field(default_factory=lambda: _env_int("SLOTHSORT_MAX_FILE_MB", 50))
    max_files: int = field(default_factory=lambda: _env_int("SLOTHSORT_MAX_FILES", 500))
    max_pdf_pages: int = field(default_factory=lambda: _env_int("SLOTHSORT_MAX_PDF_PAGES", 3))
    review_threshold: float = field(default_factory=lambda: _env_float("SLOTHSORT_REVIEW_THRESHOLD", 0.6))
    max_filename_len: int = 120
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("SLOTHSORT_DB", Path(__file__).resolve().parent / APP_DIR_NAME / "journal.sqlite3")
        )
    )

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


def get_settings() -> Settings:
    return Settings()
