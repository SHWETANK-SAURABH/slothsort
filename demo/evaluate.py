"""Reproducible evaluation on the synthetic, hand-labelled demo set.

    python demo/evaluate.py                 # AI (if Ollama is up) + both baselines
    python demo/evaluate.py --model gemma3:4b

Works on a throwaway copy in demo/.eval_workspace - never on user files.
Writes demo/eval_results.json and prints a Markdown summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEMO_DIR.parent))

from agent.planner import OllamaClient, Planner  # noqa: E402
from config import get_settings  # noqa: E402
from core.audit_log import Journal  # noqa: E402
from core.classifier import classify_heuristic  # noqa: E402
from core.duplicate_detector import find_duplicates, sha256_file  # noqa: E402
from core.extractor import extract  # noqa: E402
from core.file_executor import ApprovedOperation, UnapprovedPlanError, approve_operations, execute_plan  # noqa: E402
from core.path_validator import PathValidationError, build_destination, validate_filename  # noqa: E402
from core.scanner import scan_directory  # noqa: E402
from core.undo_manager import undo_batch  # noqa: E402
from demo.generate_demo import generate  # noqa: E402

WORKSPACE = DEMO_DIR / ".eval_workspace"


def accuracy(pred: dict[str, str], labels: dict[str, str]) -> tuple[float, list[dict]]:
    wrong = [{"file": k, "expected": labels[k], "got": v} for k, v in pred.items() if v != labels[k]]
    return (1 - len(wrong) / len(pred)) if pred else 0.0, wrong


def safety_probes(root: Path, journal: Journal) -> dict:
    """Feed deliberately unsafe inputs to the deterministic layer and count blocks."""
    probes = 0
    blocked = 0
    for bad in ["../../escape.txt", "C:\\Windows\\x.txt", "/etc/passwd", "a/b.txt", "..", ""]:
        probes += 1
        try:
            validate_filename(bad, ".txt")
        except PathValidationError:
            blocked += 1
    for cat in ["../Invoices", "System32", "Invoices/../../x"]:
        probes += 1
        try:
            build_destination(root, cat, "x.txt")
        except PathValidationError:
            blocked += 1
    victim = next(p for p in root.rglob("*.txt"))
    st = victim.stat()
    sha = sha256_file(victim)
    # outside-root destination
    probes += 1
    try:
        approve_operations(root, [ApprovedOperation(victim, root.parent / "escaped.txt", sha, st.st_size, st.st_mtime_ns)])
    except UnapprovedPlanError:
        blocked += 1
    # overwrite attempt onto an existing file
    probes += 1
    existing = next(p for p in root.rglob("*.md"))
    rep = execute_plan(approve_operations(root, [ApprovedOperation(victim, existing, sha, st.st_size, st.st_mtime_ns)]), journal)
    blocked += len(rep.failed)
    # unapproved (raw list) plan
    probes += 1
    try:
        execute_plan([ApprovedOperation(victim, root / "Notes" / "x.txt", sha, st.st_size, st.st_mtime_ns)], journal)  # type: ignore[arg-type]
    except UnapprovedPlanError:
        blocked += 1
    return {"probes": probes, "blocked": blocked}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    settings = get_settings()
    if args.model:
        settings = replace(settings, model=args.model)
    settings = replace(settings, db_path=WORKSPACE.parent / ".eval_journal.sqlite3")
    if settings.db_path.exists():
        settings.db_path.unlink()

    labels = {k: v for k, v in generate(WORKSPACE).items() if v != "Unsupported"}
    root = WORKSPACE.resolve()
    t0 = time.perf_counter()
    scan = scan_directory(root, settings)
    for f in scan.files:
        extract(f, settings)
    groups = find_duplicates(scan.files)
    scan_s = time.perf_counter() - t0
    records = scan.supported_files

    out: dict = {
        "dataset": {"supported_files": len(records), "unsupported_files": len(scan.files) - len(records)},
        "scan_seconds": round(scan_s, 3),
        "exact_duplicate_groups": len(groups),
        "exact_duplicate_files": sum(len(g.files) for g in groups),
    }

    for name, use_content in (("baseline_filename_only", False), ("baseline_rules_with_text", True)):
        pred = {r.rel_path: classify_heuristic(r, use_content).category for r in records}
        acc, wrong = accuracy(pred, labels)
        out[name] = {"accuracy": round(acc, 3), "errors": wrong}

    status = OllamaClient(settings.ollama_url, settings.model).status()
    out["model"] = settings.model
    if status.reachable and status.model_available:
        planner = Planner(settings)
        res = planner.plan(records, root, use_ai=True)
        pred = {i.source_rel: i.category for i in res.items}
        acc, wrong = accuracy(pred, labels)
        out["ai"] = {
            "accuracy": round(acc, 3),
            "errors": wrong,
            "needs_review": sum(i.needs_review for i in res.items),
            "fell_back_to_rules": res.fallback_items,
            "model_calls": res.model_calls,
            "model_failures": res.model_failures,
            "latency_seconds": round(res.latency_s, 2),
            "seconds_per_file": round(res.latency_s / max(1, len(records)), 2),
            "invalid_items": sum(i.validation_status == "invalid" for i in res.items),
        }
        # Execute every valid suggestion on the throwaway copy, then undo all of it.
        journal = Journal(settings.db_path)
        ops = [
            ApprovedOperation(Path(i.source_abs), Path(i.destination_abs), i.sha256, i.size, i.mtime_ns)
            for i in res.items
            if i.validation_status == "valid" and i.destination_abs
        ]
        report = execute_plan(approve_operations(root, ops), journal)
        undo = undo_batch(journal, report.batch_id)
        out["execution"] = {
            "attempted": len(ops),
            "moved": len(report.moved),
            "failed": len(report.failed),
            "undo_ok": sum(u.ok for u in undo),
            "undo_failed": sum(not u.ok for u in undo),
        }
        out["safety"] = safety_probes(root, journal)
    else:
        out["ai"] = {"skipped": status.message}
        out["safety"] = safety_probes(root, Journal(settings.db_path))

    (DEMO_DIR / "eval_results.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items()}, indent=2))


if __name__ == "__main__":
    main()
