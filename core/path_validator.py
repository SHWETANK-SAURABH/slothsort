"""Deterministic validation of every path the model suggests.

Model output is untrusted. Nothing it produces is used as a path until it has
passed through these functions.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from config import ALLOWED_CATEGORIES, SUPPORTED_EXTENSIONS

INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
MAX_RAW_LEN = 200


class PathValidationError(ValueError):
    """A suggested name or destination is unsafe or invalid."""


def normalize_category(category: str) -> str:
    """Map a category case-insensitively onto the allowed list or raise."""
    if not isinstance(category, str):
        raise PathValidationError("Category must be text.")
    wanted = category.strip().lower()
    for allowed in ALLOWED_CATEGORIES:
        if allowed.lower() == wanted:
            return allowed
    raise PathValidationError(f"Category '{category[:40]}' is not one of the allowed categories.")


def validate_filename(raw: str, original_ext: str, max_len: int = 120) -> str:
    """Return a safe filename that keeps ``original_ext``, or raise.

    Rejects absolute paths, traversal, path separators, empty and overly long
    names. Invalid characters are replaced, reserved Windows names are avoided.
    """
    if not isinstance(raw, str):
        raise PathValidationError("Filename must be text.")
    name = raw.strip()
    if not name:
        raise PathValidationError("Filename is empty.")
    if len(name) > MAX_RAW_LEN:
        raise PathValidationError("Filename is too long.")
    if PureWindowsPath(name).is_absolute() or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive:
        raise PathValidationError("Absolute paths are not allowed.")
    if ".." in name.replace("\\", "/").split("/") or name in (".", ".."):
        raise PathValidationError("Parent-directory traversal is not allowed.")
    if "/" in name or "\\" in name:
        raise PathValidationError("Path separators are not allowed in a filename.")

    # Drop whatever extension the model gave if it is a known one; the original wins.
    stem = name
    suffix = Path(name).suffix
    if suffix and (suffix.lower() == original_ext.lower() or suffix.lower() in SUPPORTED_EXTENSIONS):
        stem = name[: -len(suffix)]

    stem = INVALID_CHARS.sub("_", stem)
    stem = re.sub(r"\s+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip(" ._")
    if not stem:
        raise PathValidationError("Filename has no usable characters.")
    if stem.upper().split(".")[0] in WINDOWS_RESERVED:
        stem = f"{stem}_file"
    stem = stem[: max(1, max_len - len(original_ext))].rstrip(" ._")
    return f"{stem}{original_ext}"


def is_within_root(path: Path, root: Path) -> bool:
    """True if ``path`` (resolved, without requiring existence) lies inside ``root``."""
    try:
        resolved = Path(path).resolve(strict=False)
        resolved.relative_to(Path(root).resolve(strict=False))
        return resolved != Path(root).resolve(strict=False)
    except (ValueError, OSError):
        return False


def build_destination(root: Path, category: str, filename: str) -> Path:
    """Build ``root/<category>/<filename>`` and verify it stays inside root."""
    category = normalize_category(category)
    if Path(filename).name != filename:
        raise PathValidationError("Filename must not contain directories.")
    dest = Path(root) / category / filename
    if not is_within_root(dest, root):
        raise PathValidationError("Destination escapes the selected folder.")
    if dest.parent.resolve(strict=False) != (Path(root) / category).resolve(strict=False):
        raise PathValidationError("Destination folder resolves somewhere unexpected.")
    return dest


def unique_destination(dest: Path, reserved: set[str]) -> Path:
    """Return ``dest`` or ``stem_2.ext``, ``stem_3.ext``... avoiding existing and reserved paths."""
    candidate = dest
    n = 2
    while candidate.exists() or str(candidate).lower() in reserved:
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        n += 1
        if n > 999:
            raise PathValidationError("Could not find a free destination name.")
    return candidate
