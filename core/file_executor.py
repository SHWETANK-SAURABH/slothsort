"""Security-critical execution layer.

Accepts only an ``ApprovedPlan`` built by ``approve_operations`` from operations
the user explicitly approved. Every operation is re-validated immediately
before it runs. Files are only ever moved/renamed inside the selected root;
nothing is deleted and nothing is overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from core.audit_log import Journal
from core.duplicate_detector import sha256_file
from core.path_validator import is_within_root
from core.scanner import is_link_or_junction


class UnapprovedPlanError(PermissionError):
    """Raised when something other than an intact, user-approved plan is executed."""


@dataclass(frozen=True)
class ApprovedOperation:
    source: Path
    destination: Path
    sha256: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ApprovedPlan:
    root: Path
    operations: tuple[ApprovedOperation, ...]
    fingerprint: str


@dataclass
class OperationResult:
    source: Path
    destination: Path
    status: str  # "moved" | "failed"
    message: str = ""
    action_id: str | None = None


@dataclass
class ExecutionReport:
    batch_id: str
    results: list[OperationResult] = field(default_factory=list)

    @property
    def moved(self) -> list[OperationResult]:
        return [r for r in self.results if r.status == "moved"]

    @property
    def failed(self) -> list[OperationResult]:
        return [r for r in self.results if r.status == "failed"]


def _fingerprint(root: Path, operations: tuple[ApprovedOperation, ...]) -> str:
    payload = json.dumps(
        [str(root)] + [[str(o.source), str(o.destination), o.sha256, o.size, o.mtime_ns] for o in operations]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approve_operations(root: Path, operations: list[ApprovedOperation]) -> ApprovedPlan:
    """Freeze the user-approved operations into a tamper-evident plan."""
    root = Path(root).resolve()
    seen_dest: set[str] = set()
    seen_src: set[str] = set()
    for op in operations:
        if not is_within_root(op.source, root) or not is_within_root(op.destination, root):
            raise UnapprovedPlanError(f"Operation leaves the selected folder: {op.source.name}")
        key_d, key_s = str(op.destination).lower(), str(op.source).lower()
        if key_d in seen_dest:
            raise UnapprovedPlanError(f"Two files would be moved to the same destination: {op.destination.name}")
        if key_s in seen_src:
            raise UnapprovedPlanError(f"The same file appears twice in the plan: {op.source.name}")
        seen_dest.add(key_d)
        seen_src.add(key_s)
    ops = tuple(operations)
    return ApprovedPlan(root=root, operations=ops, fingerprint=_fingerprint(root, ops))


def move_no_overwrite(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``; raise ``FileExistsError`` rather than overwrite.

    On Windows ``os.rename`` already refuses to replace an existing file. On
    POSIX ``os.rename`` would overwrite, so a hard link (which fails atomically
    if ``dst`` exists) is created and the original name is removed afterwards.
    """
    if os.name == "nt":
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(str(dst))
        os.rename(src, dst)
        return
    os.unlink(src)


def _check_operation(root: Path, op: ApprovedOperation) -> None:
    """Raise ``RuntimeError`` with a readable message if ``op`` is no longer safe."""
    if not is_within_root(op.source, root):
        raise RuntimeError("Source is outside the selected folder.")
    if not is_within_root(op.destination, root):
        raise RuntimeError("Destination is outside the selected folder.")
    if op.source.resolve() == op.destination.resolve(strict=False):
        raise RuntimeError("Source and destination are the same file.")
    if not op.source.exists():
        raise RuntimeError("Source file no longer exists.")
    if is_link_or_junction(op.source) or not op.source.is_file():
        raise RuntimeError("Source is no longer a regular file.")
    st = op.source.stat()
    if st.st_size != op.size or st.st_mtime_ns != op.mtime_ns:
        raise RuntimeError("Source file changed since it was reviewed. Please rescan.")
    if sha256_file(op.source) != op.sha256:
        raise RuntimeError("Source file content changed since it was reviewed. Please rescan.")
    if os.path.lexists(op.destination):
        raise RuntimeError("Destination already exists. Nothing was overwritten.")
    parent = op.destination.parent
    # Every existing ancestor between root and destination must be a real folder.
    probe = parent
    while probe != root and root in probe.parents:
        if os.path.lexists(probe) and (is_link_or_junction(probe) or not probe.is_dir()):
            raise RuntimeError(f"Destination folder '{probe.name}' is a link or not a folder.")
        probe = probe.parent


def execute_plan(plan: ApprovedPlan, journal: Journal) -> ExecutionReport:
    """Run an approved plan. Every attempt - success or failure - is journaled."""
    if not isinstance(plan, ApprovedPlan):
        raise UnapprovedPlanError("Only an ApprovedPlan can be executed.")
    if _fingerprint(plan.root, plan.operations) != plan.fingerprint:
        raise UnapprovedPlanError("The approved plan was modified after approval. Please review it again.")

    report = ExecutionReport(batch_id=journal.new_batch_id())
    for op in plan.operations:
        try:
            _check_operation(plan.root, op)
            op.destination.parent.mkdir(parents=True, exist_ok=True)
            if not is_within_root(op.destination.parent.resolve(), plan.root) or is_link_or_junction(
                op.destination.parent
            ):
                raise RuntimeError("Destination folder resolved outside the selected folder.")
            move_no_overwrite(op.source, op.destination)
        except (RuntimeError, OSError) as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc.strerror or exc}"
            if isinstance(exc, FileExistsError):
                message = "Destination already exists. Nothing was overwritten."
            action_id = journal.record(
                batch_id=report.batch_id,
                root=plan.root,
                action_type="move",
                original_path=op.source,
                final_path=op.destination,
                status="failed",
                sha256=op.sha256,
                size=op.size,
                error=message,
            )
            report.results.append(OperationResult(op.source, op.destination, "failed", message, action_id))
            continue
        action_id = journal.record(
            batch_id=report.batch_id,
            root=plan.root,
            action_type="move",
            original_path=op.source,
            final_path=op.destination,
            status="success",
            sha256=op.sha256,
            size=op.size,
        )
        report.results.append(OperationResult(op.source, op.destination, "moved", "", action_id))
    return report
