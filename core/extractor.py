"""Bounded, read-only content extraction.

Only a small excerpt is ever read: text files are read up to a byte cap, PDFs
up to a page cap, DOCX via a size-capped read of ``word/document.xml``.
Images yield metadata only (dimensions/format) - no OCR or vision is claimed.
"""

from __future__ import annotations

import re
import zipfile
from html import unescape

from config import Settings
from core.scanner import FileRecord

_WS = re.compile(r"\s+")
_DOCX_PARA = re.compile(r"</w:p>")
_XML_TAG = re.compile(r"<[^>]+>")
SCREENSHOT_HINTS = ("screenshot", "screen shot", "screen_shot", "capture", "snip")


def _clean(text: str, limit: int) -> str:
    text = text.replace("\x00", " ")
    text = _WS.sub(" ", text).strip()
    return text[:limit]


def _read_text(record: FileRecord, settings: Settings) -> tuple[str, str]:
    # Read a few times the excerpt size so multi-byte text is not cut short.
    with open(record.path, "rb") as fh:
        raw = fh.read(settings.excerpt_chars * 4)
    if not raw.strip():
        return "empty", ""
    if b"\x00" in raw[:1024]:
        return "binary content (not text)", ""
    text = raw.decode("utf-8", errors="replace")
    return "text extracted", _clean(text, settings.excerpt_chars)


def _read_pdf(record: FileRecord, settings: Settings) -> tuple[str, str, dict]:
    import pymupdf as fitz

    meta: dict = {}
    with fitz.open(record.path) as doc:
        if doc.needs_pass:
            return "encrypted PDF (skipped)", "", meta
        meta["pages"] = doc.page_count
        title = (doc.metadata or {}).get("title") or ""
        if title:
            meta["pdf_title"] = title[:120]
        parts: list[str] = []
        for i in range(min(doc.page_count, settings.max_pdf_pages)):
            parts.append(doc.load_page(i).get_text("text"))
            if sum(len(p) for p in parts) >= settings.excerpt_chars:
                break
    text = _clean(" ".join(parts), settings.excerpt_chars)
    if not text:
        return "no text layer (possibly scanned)", "", meta
    return "text extracted", text, meta


def _read_docx(record: FileRecord, settings: Settings) -> tuple[str, str]:
    cap = 2 * 1024 * 1024  # never decompress more than 2 MB of XML
    with zipfile.ZipFile(record.path) as zf:
        with zf.open("word/document.xml") as fh:
            xml = fh.read(cap).decode("utf-8", errors="replace")
    xml = _DOCX_PARA.sub(" \n", xml)
    text = unescape(_XML_TAG.sub("", xml))
    text = _clean(text, settings.excerpt_chars)
    return ("text extracted", text) if text else ("empty", "")


def _read_image(record: FileRecord) -> tuple[str, dict]:
    from PIL import Image

    with Image.open(record.path) as img:  # reads the header only
        meta = {"width": img.width, "height": img.height, "format": img.format}
    lower = record.name.lower()
    meta["screenshot_name_hint"] = any(h in lower for h in SCREENSHOT_HINTS)
    return "image metadata only", meta


def extract(record: FileRecord, settings: Settings) -> None:
    """Populate ``record.extraction_status``, ``excerpt`` and ``metadata`` in place.

    Never raises: any problem is recorded as a readable status.
    """
    if not record.supported or record.extraction_status != "pending":
        return
    if record.size == 0:
        record.extraction_status = "empty"
        return
    try:
        if record.kind in ("text", "spreadsheet"):
            record.extraction_status, record.excerpt = _read_text(record, settings)
        elif record.kind == "pdf":
            record.extraction_status, record.excerpt, meta = _read_pdf(record, settings)
            record.metadata.update(meta)
        elif record.kind == "docx":
            record.extraction_status, record.excerpt = _read_docx(record, settings)
        elif record.kind == "image":
            record.extraction_status, meta = _read_image(record)
            record.metadata.update(meta)
        else:
            record.extraction_status = "unsupported type"
    except PermissionError:
        record.extraction_status = "unreadable (permission denied)"
    except (zipfile.BadZipFile, KeyError):
        record.extraction_status = "corrupt or invalid DOCX"
    except Exception as exc:  # corrupt PDFs/images raise library-specific errors
        record.extraction_status = f"could not read ({type(exc).__name__})"
