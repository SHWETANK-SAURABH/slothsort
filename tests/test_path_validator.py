from pathlib import Path

import pytest

from core.path_validator import (
    PathValidationError,
    build_destination,
    is_within_root,
    normalize_category,
    unique_destination,
    validate_filename,
)


@pytest.mark.parametrize(
    "raw",
    ["/etc/passwd", "C:\\Windows\\evil.pdf", "C:evil.pdf", "\\\\server\\share\\x.pdf"],
)
def test_absolute_paths_rejected(raw):
    with pytest.raises(PathValidationError):
        validate_filename(raw, ".pdf")


@pytest.mark.parametrize("raw", ["../escape.pdf", "..\\escape.pdf", "..", "a/../../b.pdf"])
def test_traversal_rejected(raw):
    with pytest.raises(PathValidationError):
        validate_filename(raw, ".pdf")


@pytest.mark.parametrize("raw", ["Invoices/bill.pdf", "sub\\bill.pdf"])
def test_separators_rejected(raw):
    with pytest.raises(PathValidationError):
        validate_filename(raw, ".pdf")


def test_empty_and_long_rejected():
    for raw in ["", "   ", "***", "x" * 500]:
        with pytest.raises(PathValidationError):
            validate_filename(raw, ".pdf")


def test_invalid_characters_sanitized():
    assert validate_filename('Bill: Oct <2024> "final"?.pdf', ".pdf") == "Bill_Oct_2024_final.pdf"
    assert validate_filename("tab\there", ".txt") == "tab_here.txt"


def test_extension_preserved():
    assert validate_filename("Electricity_Bill", ".pdf") == "Electricity_Bill.pdf"
    assert validate_filename("Electricity_Bill.PDF", ".pdf") == "Electricity_Bill.pdf"
    assert validate_filename("Report.txt", ".pdf") == "Report.pdf"
    assert validate_filename("Photo.jpg", ".JPG") == "Photo.JPG"


def test_reserved_windows_names():
    assert validate_filename("CON", ".txt") == "CON_file.txt"


def test_truncates_to_max_len():
    name = validate_filename("A" * 150, ".pdf", max_len=40)
    assert len(name) <= 40 and name.endswith(".pdf")


def test_categories():
    assert normalize_category("invoices") == "Invoices"
    for bad in ["Secrets", "../Invoices", "", "Invoices/sub"]:
        with pytest.raises(PathValidationError):
            normalize_category(bad)


def test_build_destination_inside_root(tmp_path):
    root = tmp_path.resolve()
    dest = build_destination(root, "Notes", "x.txt")
    assert dest == root / "Notes" / "x.txt"
    assert is_within_root(dest, root)
    assert not is_within_root(root / ".." / "x.txt", root)
    assert not is_within_root(root, root)
    with pytest.raises(PathValidationError):
        build_destination(root, "Notes", "../x.txt")


def test_unique_destination(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert unique_destination(tmp_path / "a.txt", set()).name == "a_2.txt"
    reserved = {str(tmp_path / "a_2.txt").lower()}
    assert unique_destination(tmp_path / "a.txt", reserved).name == "a_3.txt"
    assert unique_destination(tmp_path / "b.txt", set()) == tmp_path / "b.txt"
