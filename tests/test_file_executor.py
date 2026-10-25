import dataclasses
import os
from pathlib import Path

import pytest

from core.duplicate_detector import sha256_file
from core.file_executor import (
    ApprovedOperation,
    ApprovedPlan,
    UnapprovedPlanError,
    approve_operations,
    execute_plan,
)


def _op(src: Path, dest: Path) -> ApprovedOperation:
    st = src.stat()
    return ApprovedOperation(src, dest, sha256_file(src), st.st_size, st.st_mtime_ns)


def _file(root: Path, name: str, text: str = "hello") -> Path:
    p = root / name
    p.write_text(text, encoding="utf-8")
    return p


def test_successful_move_and_rename(root, journal):
    src = _file(root, "doc_001.txt", "invoice text")
    dest = root / "Invoices" / "Electricity_Bill.txt"
    report = execute_plan(approve_operations(root, [_op(src, dest)]), journal)
    assert len(report.moved) == 1 and not report.failed
    assert not src.exists()
    assert dest.read_text(encoding="utf-8") == "invoice text"
    rec = journal.get(report.moved[0].action_id)
    assert rec.status == "success" and rec.final_path == str(dest) and rec.original_path == str(src)


def test_never_overwrites_existing_destination(root, journal):
    src = _file(root, "a.txt", "new")
    dest = root / "Notes" / "a.txt"
    op = _op(src, dest)
    dest.parent.mkdir()
    dest.write_text("existing", encoding="utf-8")  # appears after planning
    report = execute_plan(approve_operations(root, [op]), journal)
    assert report.failed and "Nothing was overwritten" in report.failed[0].message
    assert dest.read_text(encoding="utf-8") == "existing"
    assert src.read_text(encoding="utf-8") == "new"
    assert journal.get(report.failed[0].action_id).status == "failed"


def test_destination_collision_within_plan_rejected(root):
    a, b = _file(root, "a.txt", "1"), _file(root, "b.txt", "2")
    dest = root / "Notes" / "same.txt"
    with pytest.raises(UnapprovedPlanError):
        approve_operations(root, [_op(a, dest), _op(b, dest)])


def test_source_equals_destination_fails(root, journal):
    src = _file(root, "Notes.txt")
    report = execute_plan(approve_operations(root, [_op(src, src)]), journal)
    assert report.failed and "same file" in report.failed[0].message


def test_outside_root_rejected(root, tmp_path):
    src = _file(root, "a.txt")
    with pytest.raises(UnapprovedPlanError):
        approve_operations(root, [_op(src, tmp_path / "escaped.txt")])
    with pytest.raises(UnapprovedPlanError):
        approve_operations(root, [_op(src, root / ".." / "escaped.txt")])


def test_unapproved_plan_rejected(root, journal):
    src = _file(root, "a.txt")
    op = _op(src, root / "Notes" / "a.txt")
    with pytest.raises(UnapprovedPlanError):
        execute_plan([op], journal)  # raw operations are not a plan
    forged = ApprovedPlan(root=root, operations=(op,), fingerprint="0" * 64)
    with pytest.raises(UnapprovedPlanError):
        execute_plan(forged, journal)
    assert src.exists()


def test_tampered_plan_rejected(root, journal, tmp_path):
    src = _file(root, "a.txt")
    plan = approve_operations(root, [_op(src, root / "Notes" / "a.txt")])
    evil = dataclasses.replace(plan.operations[0], destination=tmp_path / "evil.txt")
    tampered = dataclasses.replace(plan, operations=(evil,))
    with pytest.raises(UnapprovedPlanError):
        execute_plan(tampered, journal)
    assert src.exists() and not (tmp_path / "evil.txt").exists()


def test_changed_source_is_not_moved(root, journal):
    src = _file(root, "a.txt", "original")
    op = _op(src, root / "Notes" / "a.txt")
    src.write_text("changed after review!", encoding="utf-8")
    report = execute_plan(approve_operations(root, [op]), journal)
    assert report.failed and "changed" in report.failed[0].message
    assert src.exists()


def test_missing_source_reported(root, journal):
    src = _file(root, "a.txt")
    op = _op(src, root / "Notes" / "a.txt")
    src.unlink()
    report = execute_plan(approve_operations(root, [op]), journal)
    assert report.failed and "no longer exists" in report.failed[0].message


def test_failed_os_operation_is_reported(root, journal, monkeypatch):
    src = _file(root, "a.txt")
    op = _op(src, root / "Notes" / "a.txt")

    def boom(*_a, **_k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "rename", boom)
    monkeypatch.setattr(os, "link", boom, raising=False)
    report = execute_plan(approve_operations(root, [op]), journal)
    assert report.failed and "PermissionError" in report.failed[0].message
    assert src.exists()
    assert journal.get(report.failed[0].action_id).error


def test_partial_failures_do_not_stop_batch(root, journal):
    good = _file(root, "good.txt", "g")
    bad = _file(root, "bad.txt", "b")
    ops = [_op(bad, root / "Notes" / "bad.txt"), _op(good, root / "Notes" / "good.txt")]
    bad.unlink()
    report = execute_plan(approve_operations(root, ops), journal)
    assert len(report.moved) == 1 and len(report.failed) == 1
    assert (root / "Notes" / "good.txt").exists()
