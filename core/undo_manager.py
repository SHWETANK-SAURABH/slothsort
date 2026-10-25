"""Safe undo of moves recorded in the journal. Never overwrites anything."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from config import ALLOWED_CATEGORIES
from core.audit_log import Journal
from core.duplicate_detector import sha256_file
from core.file_executor import move_no_overwrite
from core.path_validator import is_within_root
from core.scanner import is_link_or_junction


@dataclass
class UndoResult:
    action_id: str
    ok: bool
    message: str


def undo_action(journal: Journal, action_id: str) -> UndoResult:
    record = journal.get(action_id)
    if record is None:
        return UndoResult(action_id, False, "No such action in the journal.")
    if record.action_type != "move" or record.status != "success":
        return UndoResult(action_id, False, "Only successful moves made by SlothSort can be undone.")
    if record.undone:
        return UndoResult(action_id, False, "This action was already undone.")

    root = Path(record.root)
    current = Path(record.final_path or "")
    original = Path(record.original_path)

    def fail(msg: str) -> UndoResult:
        journal.record_undo_failure(action_id, msg)
        return UndoResult(action_id, False, msg)

    if not is_within_root(current, root) or not is_within_root(original, root):
        return fail("Paths are outside the original folder; refusing to undo.")
    if not current.exists():
        return fail(f"The file is no longer at {current.name}. It may have been moved or deleted.")
    if is_link_or_junction(current) or not current.is_file():
        return fail("The organized path is no longer a regular file.")
    if record.sha256 and sha256_file(current) != record.sha256:
        return fail("The file was modified after organizing; refusing to undo automatically.")
    if os.path.lexists(original):
        return fail(f"Something already exists at the original location ({original.name}). Nothing was overwritten.")

    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        if not is_within_root(original.parent.resolve(), root) and original.parent.resolve() != root.resolve():
            return fail("Original folder now resolves outside the selected folder.")
        move_no_overwrite(current, original)
    except FileExistsError:
        return fail("Something already exists at the original location. Nothing was overwritten.")
    except OSError as exc:
        return fail(f"Undo failed: {exc.strerror or exc}")

    journal.mark_undone(action_id)
    _remove_empty_category_dir(current.parent, root)
    return UndoResult(action_id, True, f"Restored {original.name}")


def undo_batch(journal: Journal, batch_id: str) -> list[UndoResult]:
    """Undo every not-yet-undone successful move in a batch, newest first."""
    actions = [a for a in journal.batch_actions(batch_id) if a.status == "success" and not a.undone]
    return [undo_action(journal, a.id) for a in reversed(actions)]


def _remove_empty_category_dir(folder: Path, root: Path) -> None:
    """Remove a category folder SlothSort may have created, only if now empty."""
    try:
        if folder.parent.resolve() == root.resolve() and folder.name in ALLOWED_CATEGORIES:
            folder.rmdir()  # fails harmlessly if not empty
    except OSError:
        pass
