"""Read-only directory scanner.

The scanner never modifies, opens-as-program, or executes anything. It walks
only the user-selected root, skips hidden/temporary files, symlinks and
junctions, and SlothSort's own state directory.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config import (
    APP_DIR_NAME,
    DOCX_EXTENSIONS,
    IMAGE_EXTENSIONS,
    PDF_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    Settings,
)

TEMP_PREFIXES = ("~$", ".~", "~")
TEMP_SUFFIXES = (".tmp", ".temp", ".part", ".crdownload", ".swp", ".bak", "~")
SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_HIDDEN_ATTR = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)


@dataclass
class FileRecord:
    """Everything SlothSort knows about one file. Paths are absolute and resolved."""

    path: Path
    rel_path: str
    name: str
    ext: str
    size: int
    mtime_ns: int
    kind: str
    supported: bool
    sha256: str = ""
    extraction_status: str = "pending"
    excerpt: str = ""
    metadata: dict = field(default_factory=dict)
    duplicate_group: int | None = None

    @property
    def id(self) -> str:
        return self.rel_path


@dataclass
class SkippedEntry:
    rel_path: str
    reason: str


@dataclass
class ScanResult:
    root: Path
    files: list[FileRecord] = field(default_factory=list)
    skipped: list[SkippedEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def supported_files(self) -> list[FileRecord]:
        return [f for f in self.files if f.supported]


class InvalidRootError(ValueError):
    """Raised when the chosen folder cannot be scanned safely."""


def detect_kind(ext: str) -> str:
    ext = ext.lower()
    if ext in TEXT_EXTENSIONS:
        return "spreadsheet" if ext == ".csv" else "text"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in DOCX_EXTENSIONS:
        return "docx"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "unsupported"


def is_link_or_junction(path: Path) -> bool:
    """True for symlinks and Windows junctions/reparse points."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _REPARSE_POINT)


def is_hidden(path: Path) -> bool:
    if path.name.startswith("."):
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _HIDDEN_ATTR)


def is_temporary(name: str) -> bool:
    lower = name.lower()
    return lower in SYSTEM_FILES or lower.startswith(TEMP_PREFIXES) or lower.endswith(TEMP_SUFFIXES)


def validate_root(root: str | os.PathLike) -> Path:
    """Validate a user-supplied folder and return its resolved absolute path."""
    if root is None or not str(root).strip():
        raise InvalidRootError("Please enter a folder path.")
    raw = Path(str(root).strip().strip('"')).expanduser()
    if not raw.is_absolute():
        raise InvalidRootError("Please enter an absolute folder path.")
    if not raw.exists():
        raise InvalidRootError(f"Folder does not exist: {raw}")
    if not raw.is_dir():
        raise InvalidRootError(f"Not a folder: {raw}")
    if is_link_or_junction(raw):
        raise InvalidRootError("The selected folder is a symbolic link or junction. Choose the real folder instead.")
    resolved = raw.resolve()
    if resolved == Path(resolved.anchor):
        raise InvalidRootError("Refusing to scan an entire drive. Choose a specific folder.")
    if resolved == Path.home().resolve():
        raise InvalidRootError("Refusing to scan your whole home folder. Choose a specific sub-folder.")
    return resolved


def scan_directory(
    root: str | os.PathLike,
    settings: Settings,
    progress: Callable[[int, str], None] | None = None,
) -> ScanResult:
    """Walk ``root`` read-only and collect metadata for every regular file."""
    root_path = validate_root(root)
    result = ScanResult(root=root_path)

    for dirpath, dirnames, filenames in os.walk(root_path, topdown=True, followlinks=False):
        current = Path(dirpath)
        kept_dirs = []
        for d in sorted(dirnames):
            child = current / d
            rel = child.relative_to(root_path).as_posix()
            if d == APP_DIR_NAME or d == "__pycache__":
                continue
            if is_hidden(child):
                result.skipped.append(SkippedEntry(rel + "/", "hidden folder"))
            elif is_link_or_junction(child):
                result.skipped.append(SkippedEntry(rel + "/", "symbolic link / junction (not followed)"))
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            full = current / name
            rel = full.relative_to(root_path).as_posix()
            if is_link_or_junction(full):
                result.skipped.append(SkippedEntry(rel, "symbolic link (not followed)"))
                continue
            if is_hidden(full):
                result.skipped.append(SkippedEntry(rel, "hidden file"))
                continue
            if is_temporary(name):
                result.skipped.append(SkippedEntry(rel, "temporary or system file"))
                continue
            if len(result.files) >= settings.max_files:
                result.truncated = True
                result.skipped.append(SkippedEntry(rel, f"file limit reached ({settings.max_files})"))
                continue
            try:
                st = os.lstat(full)
            except OSError as exc:
                result.errors.append(f"{rel}: cannot read metadata ({exc.strerror or exc})")
                continue
            if not stat.S_ISREG(st.st_mode):
                result.skipped.append(SkippedEntry(rel, "not a regular file"))
                continue
            ext = full.suffix
            kind = detect_kind(ext)
            record = FileRecord(
                path=full,
                rel_path=rel,
                name=name,
                ext=ext,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                kind=kind,
                supported=ext.lower() in SUPPORTED_EXTENSIONS,
            )
            if not record.supported:
                record.extraction_status = "unsupported type"
            elif st.st_size > settings.max_file_bytes:
                record.extraction_status = f"too large (> {settings.max_file_mb} MB)"
            result.files.append(record)
            if progress:
                progress(len(result.files), rel)
    return result
