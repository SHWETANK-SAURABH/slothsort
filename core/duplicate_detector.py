"""Exact duplicate detection with streamed SHA-256 hashes (never the LLM)."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from core.scanner import FileRecord

_CHUNK = 1024 * 1024
# (path, size, mtime_ns) -> digest. Unchanged files are not re-hashed.
_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def hash_record(record: FileRecord) -> str:
    key = (str(record.path), record.size, record.mtime_ns)
    cached = _HASH_CACHE.get(key)
    if cached is None:
        cached = sha256_file(record.path)
        _HASH_CACHE[key] = cached
    record.sha256 = cached
    return cached


@dataclass
class DuplicateGroup:
    group_id: int
    sha256: str
    files: list[FileRecord]

    @property
    def hash_prefix(self) -> str:
        return self.sha256[:12]


def find_duplicates(records: list[FileRecord]) -> list[DuplicateGroup]:
    """Group files with identical content. Sets ``duplicate_group`` on members."""
    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for r in records:
        if r.size > 0:
            by_size[r.size].append(r)

    by_hash: dict[str, list[FileRecord]] = defaultdict(list)
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue  # a unique size cannot be an exact duplicate
        for r in same_size:
            try:
                by_hash[hash_record(r)].append(r)
            except OSError:
                continue

    groups: list[DuplicateGroup] = []
    for digest, members in sorted(by_hash.items(), key=lambda kv: kv[1][0].rel_path):
        if len(members) > 1:
            gid = len(groups) + 1
            for m in members:
                m.duplicate_group = gid
            groups.append(DuplicateGroup(gid, digest, sorted(members, key=lambda m: m.rel_path)))
    return groups
