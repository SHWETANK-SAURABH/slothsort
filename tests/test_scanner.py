from pathlib import Path

import pytest

from conftest import can_symlink, snapshot
from core.duplicate_detector import find_duplicates
from core.extractor import extract
from core.scanner import InvalidRootError, scan_directory, validate_root


def _make(root: Path) -> None:
    (root / "notes.txt").write_text("Meeting notes for the Q3 planning session", encoding="utf-8")
    (root / "copy_of_notes.txt").write_text("Meeting notes for the Q3 planning session", encoding="utf-8")
    (root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (root / "song.mp3").write_bytes(b"ID3\x00fake")
    (root / "empty.md").write_text("", encoding="utf-8")
    (root / ".hidden.txt").write_text("secret", encoding="utf-8")
    (root / "~$lock.docx").write_bytes(b"x")
    sub = root / "sub"
    sub.mkdir()
    (sub / "deep.md").write_text("# Deep file", encoding="utf-8")
    (root / ".slothsort").mkdir()
    (root / ".slothsort" / "journal.sqlite3").write_bytes(b"db")


def test_scan_temp_directory(root, settings):
    _make(root)
    result = scan_directory(root, settings)
    names = {f.rel_path for f in result.files}
    assert names == {"notes.txt", "copy_of_notes.txt", "data.csv", "song.mp3", "empty.md", "sub/deep.md"}
    skipped = {s.rel_path for s in result.skipped}
    assert ".hidden.txt" in skipped and "~$lock.docx" in skipped
    assert not any(".slothsort" in n for n in names | skipped)


def test_unsupported_files_reported_not_crashing(root, settings):
    _make(root)
    result = scan_directory(root, settings)
    mp3 = next(f for f in result.files if f.name == "song.mp3")
    assert mp3.supported is False
    assert mp3.extraction_status == "unsupported type"
    extract(mp3, settings)  # must be a no-op, not an error
    assert mp3.extraction_status == "unsupported type"


def test_extraction_text_empty_and_corrupt(root, settings):
    _make(root)
    (root / "broken.pdf").write_bytes(b"%PDF-1.4 not really a pdf")
    (root / "broken.docx").write_bytes(b"not a zip")
    result = scan_directory(root, settings)
    by_name = {f.name: f for f in result.files}
    for f in result.files:
        extract(f, settings)
    assert by_name["notes.txt"].extraction_status == "text extracted"
    assert "Meeting notes" in by_name["notes.txt"].excerpt
    assert by_name["empty.md"].extraction_status == "empty"
    assert by_name["broken.pdf"].extraction_status.startswith("could not read")
    assert by_name["broken.docx"].extraction_status == "corrupt or invalid DOCX"


def test_exact_duplicates_by_hash(root, settings):
    _make(root)
    (root / "similar_name_notes.txt").write_text("Different content entirely!!!!!!!!!", encoding="utf-8")
    result = scan_directory(root, settings)
    groups = find_duplicates(result.files)
    assert len(groups) == 1
    assert {f.name for f in groups[0].files} == {"notes.txt", "copy_of_notes.txt"}
    assert len(groups[0].hash_prefix) == 12


def test_scan_does_not_modify_files(root, settings):
    _make(root)
    before = snapshot(root)
    result = scan_directory(root, settings)
    for f in result.files:
        extract(f, settings)
    find_duplicates(result.files)
    assert snapshot(root) == before


def test_symlinks_are_skipped(root, settings, tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("symlinks not permitted on this system")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside root", encoding="utf-8")
    (root / "link_dir").symlink_to(outside, target_is_directory=True)
    (root / "link_file.txt").symlink_to(outside / "private.txt")
    (root / "real.txt").write_text("inside", encoding="utf-8")
    result = scan_directory(root, settings)
    assert [f.name for f in result.files] == ["real.txt"]
    reasons = {s.rel_path: s.reason for s in result.skipped}
    assert "symbolic" in reasons["link_file.txt"]
    assert "link_dir/" in reasons


def test_file_limit(root, settings):
    for i in range(5):
        (root / f"f{i}.txt").write_text(str(i), encoding="utf-8")
    from dataclasses import replace

    result = scan_directory(root, replace(settings, max_files=3))
    assert len(result.files) == 3 and result.truncated


def test_validate_root_rejections(tmp_path):
    with pytest.raises(InvalidRootError):
        validate_root("")
    with pytest.raises(InvalidRootError):
        validate_root("relative/path")
    with pytest.raises(InvalidRootError):
        validate_root(tmp_path / "missing")
    with pytest.raises(InvalidRootError):
        validate_root(Path(tmp_path.anchor))
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(InvalidRootError):
        validate_root(f)


def test_windows_junctions_are_skipped(root, settings, tmp_path):
    _winapi = pytest.importorskip("_winapi")
    if not hasattr(_winapi, "CreateJunction"):
        pytest.skip("junctions unavailable")
    outside = tmp_path / "outside_j"
    outside.mkdir()
    (outside / "private.txt").write_text("outside root", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    (root / "real.txt").write_text("inside", encoding="utf-8")
    result = scan_directory(root, settings)
    assert [f.name for f in result.files] == ["real.txt"]
    assert any(s.rel_path == "junction/" for s in result.skipped)
    with pytest.raises(InvalidRootError):
        validate_root(root / "junction")
