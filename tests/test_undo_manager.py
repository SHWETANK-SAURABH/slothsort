from pathlib import Path

from core.audit_log import Journal
from core.duplicate_detector import sha256_file
from core.file_executor import ApprovedOperation, approve_operations, execute_plan
from core.undo_manager import undo_action, undo_batch


def _move(root: Path, journal: Journal, name: str = "doc.txt", text: str = "content"):
    src = root / name
    src.write_text(text, encoding="utf-8")
    st = src.stat()
    dest = root / "Notes" / f"Renamed_{name}"
    op = ApprovedOperation(src, dest, sha256_file(src), st.st_size, st.st_mtime_ns)
    report = execute_plan(approve_operations(root, [op]), journal)
    assert report.moved
    return src, dest, report


def test_undo_restores_file(root, journal):
    src, dest, report = _move(root, journal)
    res = undo_action(journal, report.moved[0].action_id)
    assert res.ok, res.message
    assert src.read_text(encoding="utf-8") == "content"
    assert not dest.exists()
    assert not (root / "Notes").exists()  # empty category folder cleaned up
    assert journal.get(report.moved[0].action_id).undone


def test_undo_twice_refused(root, journal):
    _, _, report = _move(root, journal)
    aid = report.moved[0].action_id
    assert undo_action(journal, aid).ok
    assert not undo_action(journal, aid).ok


def test_undo_collision_never_overwrites(root, journal):
    src, dest, report = _move(root, journal)
    src.write_text("a new file took the old name", encoding="utf-8")
    res = undo_action(journal, report.moved[0].action_id)
    assert not res.ok and "Nothing was overwritten" in res.message
    assert src.read_text(encoding="utf-8") == "a new file took the old name"
    assert dest.exists()
    rec = journal.get(report.moved[0].action_id)
    assert not rec.undone and rec.undo_error


def test_undo_refuses_modified_file(root, journal):
    src, dest, report = _move(root, journal)
    dest.write_text("edited after organizing", encoding="utf-8")
    res = undo_action(journal, report.moved[0].action_id)
    assert not res.ok and "modified" in res.message
    assert dest.exists() and not src.exists()


def test_undo_when_file_missing(root, journal):
    _, dest, report = _move(root, journal)
    dest.unlink()
    res = undo_action(journal, report.moved[0].action_id)
    assert not res.ok and "no longer" in res.message


def test_undo_failed_action_refused(root, journal):
    rid = journal.record(
        batch_id="b", root=root, action_type="move", original_path=root / "x", final_path=root / "y", status="failed"
    )
    assert not undo_action(journal, rid).ok


def test_undo_batch(root, journal):
    files = []
    ops = []
    for n in ("a.txt", "b.txt"):
        p = root / n
        p.write_text(n, encoding="utf-8")
        st = p.stat()
        ops.append(ApprovedOperation(p, root / "Notes" / n, sha256_file(p), st.st_size, st.st_mtime_ns))
        files.append(p)
    report = execute_plan(approve_operations(root, ops), journal)
    results = undo_batch(journal, report.batch_id)
    assert all(r.ok for r in results) and len(results) == 2
    assert all(f.exists() for f in files)


def test_journal_persists_after_reopen(root, settings):
    j1 = Journal(settings.db_path)
    src, dest, report = _move(root, j1)
    del j1
    j2 = Journal(settings.db_path)
    records = j2.list_actions()
    assert len(records) == 1 and records[0].final_path == str(dest)
    assert undo_action(j2, records[0].id).ok
    assert Journal(settings.db_path).get(records[0].id).undone
