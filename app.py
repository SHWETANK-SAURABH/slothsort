"""SlothSort - local-first AI file organization agent.

Run with:  streamlit run app.py
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

from agent.planner import OllamaClient, Planner, PlanResult, apply_edit, finalize_destinations
from agent.schemas import PlanItem
from config import ALLOWED_CATEGORIES, Settings, get_settings
from core.audit_log import Journal
from core.duplicate_detector import find_duplicates
from core.extractor import extract
from core.file_executor import ApprovedOperation, UnapprovedPlanError, approve_operations, execute_plan
from core.scanner import InvalidRootError, scan_directory, validate_root
from core.undo_manager import undo_action, undo_batch

APP_DIR = Path(__file__).resolve().parent
DEMO_FOLDER = APP_DIR / "demo" / "sample_files"
STEPS = ["1 · Select folder", "2 · Scan files", "3 · Review AI plan", "4 · Approve & organize", "5 · Results & undo"]

st.set_page_config(page_title="SlothSort", page_icon="🦥", layout="wide", initial_sidebar_state="expanded")

LOGO_SVG = """
<svg width="46" height="46" viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-label="SlothSort logo">
  <circle cx="32" cy="32" r="30" fill="#8A6A4F"/>
  <ellipse cx="32" cy="35" rx="21" ry="18" fill="#E9DCC6"/>
  <ellipse cx="23" cy="33" rx="7" ry="5" fill="#5A4636" transform="rotate(-18 23 33)"/>
  <ellipse cx="41" cy="33" rx="7" ry="5" fill="#5A4636" transform="rotate(18 41 33)"/>
  <circle cx="24" cy="33" r="2" fill="#F7F3EA"/><circle cx="40" cy="33" r="2" fill="#F7F3EA"/>
  <ellipse cx="32" cy="40" rx="3" ry="2" fill="#3E2F24"/>
  <path d="M27 45 Q32 49 37 45" stroke="#3E2F24" stroke-width="2" fill="none" stroke-linecap="round"/>
</svg>
"""

CSS = """
<style>
:root { --moss: #5B7F5A; --bark: #8A6A4F; --line: rgba(128,128,128,.22); --soft: rgba(91,127,90,.08); }
.block-container { padding-top: 2.2rem; max-width: 1280px; }
.ss-header { display:flex; align-items:center; gap:14px; margin-bottom: .2rem; }
.ss-title { font-size: 2.0rem; font-weight: 700; letter-spacing: -.02em; margin: 0; line-height: 1.1; }
.ss-tag { opacity: .72; margin: 0; font-size: .98rem; }
.ss-card { border:1px solid var(--line); border-radius: 14px; padding: 14px 16px; background: var(--soft); }
.ss-card .k { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; opacity: .65; }
.ss-card .v { font-size: 1.55rem; font-weight: 650; margin-top: 2px; }
.ss-card .s { font-size: .8rem; opacity: .65; }
.ss-pill { display:inline-block; padding: 2px 10px; border-radius: 999px; font-size: .78rem; font-weight: 600; }
.ss-ok { background: rgba(91,127,90,.18); color: var(--moss); }
.ss-warn { background: rgba(201,140,40,.18); color: #b07a1f; }
.ss-bad { background: rgba(190,70,60,.16); color: #b8483d; }
.ss-note { border-left: 3px solid var(--moss); padding: 8px 12px; background: var(--soft); border-radius: 6px; font-size: .92rem; }
.ss-steps { display:flex; gap:6px; flex-wrap:wrap; margin: 10px 0 18px 0; }
.ss-step { padding: 6px 12px; border-radius: 999px; border: 1px solid var(--line); font-size: .85rem; opacity: .6; }
.ss-step.done { opacity: .9; border-color: rgba(91,127,90,.5); }
.ss-step.active { opacity: 1; background: var(--moss); color: white; border-color: var(--moss); font-weight: 600; }
</style>
"""


# ---------------------------------------------------------------- state
@st.cache_resource
def get_journal(db_path: str) -> Journal:
    return Journal(Path(db_path))


def init_state() -> None:
    defaults = {
        "step": 0,
        "root": None,
        "root_input": "",
        "scan": None,
        "dupes": [],
        "plan": None,
        "editor_version": 0,
        "report": None,
        "last_status": "No operations yet.",
        "model": get_settings().model,
        "planner": None,
        "planner_model": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def settings() -> Settings:
    return replace(get_settings(), model=st.session_state.model)


def planner() -> Planner:
    s = settings()
    if st.session_state.planner is None or st.session_state.planner_model != s.model:
        st.session_state.planner = Planner(s)  # keeps its per-session suggestion cache
        st.session_state.planner_model = s.model
    return st.session_state.planner


def go(step: int) -> None:
    st.session_state.step = step


def reset_after_root() -> None:
    st.session_state.scan = None
    st.session_state.dupes = []
    st.session_state.plan = None
    st.session_state.report = None


def allowed_steps() -> set[int]:
    """Steps the user may open; essential steps (scan, review) cannot be skipped."""
    allowed = {0}
    if st.session_state.root is not None:
        allowed |= {1, 4}
        if st.session_state.scan is not None:
            allowed.add(2)
        plan = st.session_state.plan
        if plan is not None and any(i.selected for i in plan.items):
            allowed.add(3)
    return allowed


# ---------------------------------------------------------------- widgets
def card(col, label: str, value, sub: str = "") -> None:
    col.markdown(
        f'<div class="ss-card"><div class="k">{label}</div><div class="v">{value}</div><div class="s">{sub}</div></div>',
        unsafe_allow_html=True,
    )


def pill(text: str, kind: str = "ok") -> str:
    return f'<span class="ss-pill ss-{kind}">{text}</span>'


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def render_sidebar(status) -> None:
    with st.sidebar:
        st.markdown(f'<div class="ss-header">{LOGO_SVG}<div><b>SlothSort</b><br><span class="ss-tag">local · private · reversible</span></div></div>', unsafe_allow_html=True)
        st.divider()
        st.markdown("**Workflow**")
        allowed = allowed_steps()
        for i, label in enumerate(STEPS):
            st.button(
                label,
                key=f"nav_{i}",
                width="stretch",
                disabled=i not in allowed,
                type="primary" if i == st.session_state.step else "secondary",
                on_click=go,
                args=(i,),
            )
        st.divider()
        st.markdown("**Local AI**")
        if status.reachable and status.model_available:
            st.markdown(pill("● Model ready", "ok"), unsafe_allow_html=True)
        elif status.reachable:
            st.markdown(pill("● Model missing", "warn"), unsafe_allow_html=True)
        else:
            st.markdown(pill("● Ollama offline", "bad"), unsafe_allow_html=True)
        options = sorted(set(status.models) | {st.session_state.model})
        st.selectbox("Model", options, key="model", help="Any model pulled into your local Ollama.")
        st.caption(f"Endpoint: `{settings().ollama_url}`")
        if st.button("Re-check connection", width="stretch"):
            st.rerun()
        if not (status.reachable and status.model_available):
            with st.expander("Setup instructions", expanded=True):
                st.markdown(
                    f"1. Install Ollama from ollama.com\n2. Start it (`ollama serve`)\n3. Download the model:\n\n"
                    f"```\nollama pull {st.session_state.model}\n```\n"
                    "Scanning and duplicate detection keep working without it; planning falls back to "
                    "clearly-labelled metadata rules."
                )
        st.divider()
        st.caption(
            "🔒 Files never leave this computer. The only network call SlothSort makes is to the "
            "Ollama server above (localhost by default). No telemetry."
        )


def render_header(status) -> None:
    st.markdown(
        f'<div class="ss-header">{LOGO_SVG}<div><p class="ss-title">SlothSort</p>'
        '<p class="ss-tag">Stop organizing files manually. Let local AI do the boring work — privately, safely, and reversibly.</p></div></div>',
        unsafe_allow_html=True,
    )
    scan = st.session_state.scan
    plan: PlanResult | None = st.session_state.plan
    journal = get_journal(str(settings().db_path))
    root = st.session_state.root
    done = [a for a in journal.list_actions(root=root)] if root else []
    completed = sum(1 for a in done if a.status == "success" and not a.undone)
    review = sum(1 for i in plan.items if i.needs_review and not i.rejected) if plan else 0

    cols = st.columns(6)
    ai_value = "Ready" if status.reachable and status.model_available else ("No model" if status.reachable else "Offline")
    card(cols[0], "Local AI", ai_value, st.session_state.model)
    card(cols[1], "Folder", Path(root).name if root else "—", "selected root" if root else "not selected")
    card(cols[2], "Files scanned", len(scan.supported_files) if scan else 0, "supported")
    card(cols[3], "Exact duplicates", sum(len(g.files) for g in st.session_state.dupes), f"{len(st.session_state.dupes)} groups")
    card(cols[4], "Awaiting review", review, "flagged for you")
    card(cols[5], "Organized", completed, "active moves in journal")
    st.markdown(f'<div class="ss-note" style="margin-top:12px">Last operation: {st.session_state.last_status}</div>', unsafe_allow_html=True)

    steps_html = "".join(
        f'<span class="ss-step {"active" if i == st.session_state.step else ("done" if i < st.session_state.step else "")}">{s}</span>'
        for i, s in enumerate(STEPS)
    )
    st.markdown(f'<div class="ss-steps">{steps_html}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- steps
def pick_folder_native() -> str | None:
    """Open a native folder dialog (only works when Streamlit runs on this machine)."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        tk_root = tk.Tk()
        tk_root.withdraw()
        tk_root.wm_attributes("-topmost", 1)
        chosen = filedialog.askdirectory(title="Choose a folder for SlothSort")
        tk_root.destroy()
        return chosen or None
    except Exception:
        return None


def _on_browse() -> None:
    chosen = pick_folder_native()
    if chosen:
        st.session_state.root_input = chosen
    st.session_state.picker_failed = not chosen


def _on_demo(reset: bool = False) -> None:
    from demo.generate_demo import generate

    if reset or not DEMO_FOLDER.exists():
        n = len(generate(DEMO_FOLDER))
        st.session_state.demo_msg = f"Created {n} demo files in {DEMO_FOLDER}"
        if st.session_state.root == DEMO_FOLDER.resolve():
            reset_after_root()
    st.session_state.root_input = str(DEMO_FOLDER)


def step_select() -> None:
    st.subheader("Select a folder")
    st.markdown(
        '<div class="ss-note">SlothSort runs on <b>your</b> computer and only looks inside the folder you choose. '
        "It never scans your whole drive and never changes anything until you approve a plan.</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    c1, c2, c3 = st.columns([5, 1.2, 1.6])
    path = c1.text_input("Folder path", key="root_input", placeholder=r"e.g. C:\Users\you\Downloads\to-sort", label_visibility="collapsed")
    c2.button("Browse…", width="stretch", on_click=_on_browse)
    c3.button("Use demo folder", width="stretch", on_click=_on_demo)
    if st.session_state.pop("picker_failed", False):
        st.warning("No folder chosen, or the native picker is unavailable here. You can type or paste the path.")
    if msg := st.session_state.pop("demo_msg", None):
        st.success(msg)

    if st.button("Confirm folder", type="primary", disabled=not path):
        try:
            root = validate_root(path)
        except InvalidRootError as exc:
            st.error(str(exc))
            return
        if st.session_state.root != root:
            reset_after_root()
        st.session_state.root = root
        go(1)
        st.rerun()

    with st.expander("Demo data"):
        st.write("Generate (or reset) a synthetic messy folder with ~29 fictional files.")
        st.button("Generate / reset demo folder", on_click=_on_demo, args=(True,))


def run_scan() -> None:
    s = settings()
    progress = st.progress(0.0, text="Scanning…")
    result = scan_directory(st.session_state.root, s)
    total = max(1, len(result.files))
    for i, rec in enumerate(result.files, 1):
        extract(rec, s)
        progress.progress(i / total, text=f"Reading {rec.rel_path}")
    progress.progress(1.0, text="Checking for exact duplicates (SHA-256)…")
    st.session_state.dupes = find_duplicates(result.files)
    st.session_state.scan = result
    st.session_state.plan = None
    progress.empty()


def step_scan() -> None:
    st.subheader("Scan files")
    st.caption(f"Folder: `{st.session_state.root}` · Scanning is read-only.")
    c1, c2, _ = st.columns([1.3, 1.3, 4])
    if c1.button("Scan now" if st.session_state.scan is None else "Rescan", type="primary", width="stretch"):
        run_scan()
        st.rerun()
    scan = st.session_state.scan
    if scan is None:
        st.info("Press **Scan now** to list files. Nothing will be moved.")
        return

    rows = [
        {
            "File": f.rel_path,
            "Ext": f.ext.lower() or "—",
            "Size": fmt_size(f.size),
            "Type": f.kind,
            "Extraction": f.extraction_status,
            "Duplicate": f"group {f.duplicate_group}" if f.duplicate_group else "",
        }
        for f in scan.files
    ]
    sup = len(scan.supported_files)
    st.markdown(
        f"{pill(f'{sup} supported', 'ok')} {pill(f'{len(scan.files) - sup} unsupported', 'warn')} "
        f"{pill(f'{len(scan.skipped)} skipped', 'warn')} {pill(f'{len(scan.errors)} errors', 'bad' if scan.errors else 'ok')}",
        unsafe_allow_html=True,
    )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=min(560, 38 + 35 * len(rows)))
    if scan.truncated:
        st.warning(f"File limit reached; only the first {settings().max_files} files were scanned.")
    for err in scan.errors:
        st.error(err)

    if st.session_state.dupes:
        with st.expander(f"Exact duplicates · {len(st.session_state.dupes)} groups", expanded=True):
            st.caption("Byte-for-byte identical files (SHA-256). Similar names alone are not duplicates. Nothing is deleted.")
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Group": g.group_id, "File": f.rel_path, "Size": fmt_size(f.size), "SHA-256": g.hash_prefix + "…"}
                        for g in st.session_state.dupes
                        for f in g.files
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
    if scan.skipped:
        with st.expander(f"Skipped · {len(scan.skipped)}"):
            st.dataframe(pd.DataFrame([{"Path": s.rel_path, "Reason": s.reason} for s in scan.skipped]), hide_index=True, width="stretch")

    if c2.button("Next: plan →", width="stretch", disabled=sup == 0):
        go(2)
        st.rerun()


def run_plan(use_ai: bool) -> None:
    scan = st.session_state.scan
    bar = st.progress(0.0, text="Asking the local model…" if use_ai else "Applying metadata rules…")

    def on_progress(done: int, total: int) -> None:
        bar.progress(done / max(1, total), text=f"Classified {done}/{total} files with {st.session_state.model}")

    result = planner().plan(scan.supported_files, st.session_state.root, use_ai=use_ai, progress=on_progress)
    bar.empty()
    st.session_state.plan = result
    st.session_state.editor_version += 1
    st.session_state.last_status = (
        f"Planned {len(result.items)} files in {result.latency_s:.1f}s "
        f"({'AI: ' + result.model if result.ai_used else 'metadata rules only'}). No files changed."
    )


def _plan_frame(items: list[PlanItem]) -> pd.DataFrame:
    def flag(i: PlanItem) -> str:
        if i.rejected:
            return "rejected"
        if i.validation_status == "invalid":
            return "invalid"
        if i.validation_status == "unchanged":
            return "already organized"
        return "review" if i.needs_review else "ok"

    return pd.DataFrame(
        [
            {
                "Approve": i.selected,
                "Original": i.source_rel,
                "Category": i.category,
                "New filename": i.proposed_filename,
                "Destination": i.destination_rel or "—",
                "Confidence": round(i.confidence, 2),
                "Status": flag(i),
                "Source": "AI" if i.origin == "ai" else "rules",
                "Reason": i.reason,
                "Validation": i.validation_message,
                "Dup": f"#{i.duplicate_group}" if i.duplicate_group else "",
            }
            for i in items
        ]
    )


def is_safe(i: PlanItem) -> bool:
    return i.validation_status == "valid" and not i.needs_review and not i.rejected and bool(i.destination_abs)


def step_review() -> None:
    st.subheader("Review the AI plan")
    st.markdown(
        '<div class="ss-note">Planning <b>never modifies files</b>. Edit any category or filename, tick the rows you '
        "want, then continue to the approval step.</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    status = OllamaClient(settings().ollama_url, st.session_state.model).status()
    ai_ok = status.reachable and status.model_available
    plan: PlanResult | None = st.session_state.plan

    c = st.columns([1.6, 1.8, 1.2, 1.2])
    if c[0].button("Generate plan with local AI" if plan is None else "Re-plan with AI", type="primary", disabled=not ai_ok, width="stretch"):
        run_plan(True)
        st.rerun()
    if c[1].button("Plan with metadata rules (no AI)", width="stretch"):
        run_plan(False)
        st.rerun()
    if c[2].button("Rescan folder", width="stretch"):
        run_scan()
        go(1)
        st.rerun()
    if c[3].button("Cancel plan", width="stretch", disabled=plan is None):
        st.session_state.plan = None
        st.session_state.last_status = "Plan cancelled. No files were changed."
        st.rerun()
    if not ai_ok:
        st.warning(f"{status.message} AI planning is disabled; you can still use the metadata rules (results are marked 'rules' and need review).")
    if plan is None:
        return

    if plan.ai_used:
        st.caption(
            f"Model **{plan.model}** via local Ollama · {plan.model_calls} model call(s) · "
            f"{plan.model_failures} malformed/failed · {plan.fallback_items} item(s) fell back to rules · {plan.latency_s:.1f}s"
        )
    for note in plan.notes:
        st.warning(note)

    items = plan.items
    b = st.columns(4)
    if b[0].button("Select all safe suggestions", width="stretch"):
        for i in items:
            i.selected = is_safe(i)
        st.session_state.editor_version += 1
        st.rerun()
    if b[1].button("Reject selected", width="stretch"):
        for i in items:
            if i.selected:
                i.rejected, i.selected = True, False
        st.session_state.editor_version += 1
        st.rerun()
    if b[2].button("Clear selection", width="stretch"):
        for i in items:
            i.selected = False
        st.session_state.editor_version += 1
        st.rerun()
    if b[3].button("Restore rejected", width="stretch"):
        for i in items:
            i.rejected = False
        st.session_state.editor_version += 1
        st.rerun()

    df = _plan_frame(items)
    edited = st.data_editor(
        df,
        key=f"plan_editor_{st.session_state.editor_version}",
        hide_index=True,
        width="stretch",
        height=min(640, 38 + 35 * len(df)),
        disabled=["Original", "Destination", "Confidence", "Status", "Source", "Reason", "Validation", "Dup"],
        column_config={
            "Approve": st.column_config.CheckboxColumn(width="small"),
            "Category": st.column_config.SelectboxColumn(options=list(ALLOWED_CATEGORIES), required=True),
            "New filename": st.column_config.TextColumn(help="Extension is always kept; unsafe names are rejected."),
            "Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f", help="Model self-report, not a calibrated probability."),
            "Reason": st.column_config.TextColumn(width="large"),
        },
    )

    changed = False
    for idx, item in enumerate(items):
        row = edited.iloc[idx]
        new_cat = row["Category"] if row["Category"] != item.category else None
        new_name = row["New filename"] if row["New filename"] != item.proposed_filename else None
        if new_cat is not None or new_name is not None:
            apply_edit(item, st.session_state.root, new_cat, new_name, settings().max_filename_len)
            item.needs_review = False  # a human has now looked at it
            changed = True
        want = bool(row["Approve"])
        if want != item.selected:
            item.selected = want and not item.rejected and item.validation_status == "valid"
            changed = True
    if changed:
        finalize_destinations(items, st.session_state.root)
        for i in items:
            if i.selected and i.validation_status != "valid":
                i.selected = False
        st.session_state.editor_version += 1
        st.rerun()

    n_sel = sum(i.selected for i in items)
    st.markdown(
        f"{pill(f'{n_sel} selected', 'ok')} {pill(str(sum(is_safe(i) for i in items)) + ' safe', 'ok')} "
        f"{pill(str(sum(i.needs_review and not i.rejected for i in items)) + ' need review', 'warn')} "
        f"{pill(str(sum(i.validation_status == 'invalid' for i in items)) + ' invalid', 'bad')} "
        f"{pill(str(sum(i.rejected for i in items)) + ' rejected', 'warn')}",
        unsafe_allow_html=True,
    )
    st.write("")
    if st.button(f"Continue to approval ({n_sel}) →", type="primary", disabled=n_sel == 0):
        go(3)
        st.rerun()


def step_approve() -> None:
    st.subheader("Approve & organize")
    plan: PlanResult | None = st.session_state.plan
    if plan is None:
        st.info("There is no plan to approve. Generate one in step 3.")
        return
    selected = [i for i in plan.items if i.selected]
    if not selected:
        st.info("No files selected. Go back to step 3 and tick the files you want organized.")
        return

    root: Path = st.session_state.root
    # Re-check the filesystem: if destinations changed since review, ask for a new review.
    before = {i.file_id: i.destination_abs for i in selected}
    finalize_destinations(plan.items, root)
    changed = [i for i in selected if i.destination_abs != before[i.file_id] or i.validation_status != "valid"]
    missing = [i for i in selected if not Path(i.source_abs).exists()]
    if changed or missing:
        for i in selected:
            i.selected = False
        st.session_state.editor_version += 1
        st.error(
            "The folder changed since you reviewed the plan "
            f"({len(changed)} destination(s) changed, {len(missing)} source file(s) missing). Please review again."
        )
        if st.button("Back to review"):
            go(2)
            st.rerun()
        return

    st.markdown(
        f'<div class="ss-note">These <b>{len(selected)}</b> files will be moved inside <code>{root}</code>. '
        "Nothing is deleted or overwritten, and every move can be undone.</div>",
        unsafe_allow_html=True,
    )
    st.write("")
    st.dataframe(
        pd.DataFrame([{"From": i.source_rel, "→ To": i.destination_rel, "Category": i.category} for i in selected]),
        width="stretch",
        hide_index=True,
        height=min(520, 38 + 35 * len(selected)),
    )
    confirmed = st.checkbox(f"I reviewed these {len(selected)} changes and want SlothSort to move them.")
    c1, c2, _ = st.columns([1.6, 1.2, 4])
    if c2.button("← Back to review", width="stretch"):
        go(2)
        st.rerun()
    if c1.button(f"Organize {len(selected)} files", type="primary", disabled=not confirmed, width="stretch"):
        ops = [
            ApprovedOperation(Path(i.source_abs), Path(i.destination_abs), i.sha256, i.size, i.mtime_ns) for i in selected
        ]
        journal = get_journal(str(settings().db_path))
        try:
            report = execute_plan(approve_operations(root, ops), journal)
        except UnapprovedPlanError as exc:
            st.error(f"Blocked: {exc}")
            return
        st.session_state.report = {
            "report": report,
            "skipped": len([i for i in plan.items if not i.selected]),
            "review": sum(1 for i in plan.items if i.needs_review and not i.selected),
        }
        st.session_state.last_status = f"Organized {len(report.moved)} file(s), {len(report.failed)} failed (batch {report.batch_id})."
        st.session_state.plan = None
        st.session_state.scan = None
        go(4)
        st.rerun()


def step_results() -> None:
    st.subheader("Results & undo")
    root = st.session_state.root
    journal = get_journal(str(settings().db_path))
    data = st.session_state.report
    if data:
        report = data["report"]
        cols = st.columns(5)
        card(cols[0], "Organized", len(report.moved), "moved & renamed")
        card(cols[1], "Failed", len(report.failed), "see details below")
        card(cols[2], "Not selected", data["skipped"], "left untouched")
        card(cols[3], "Needed review", data["review"], "not approved")
        card(cols[4], "Exact duplicates", sum(len(g.files) for g in st.session_state.dupes), "reported, not deleted")
        st.write("")
        st.markdown("**Before → after**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Before": Path(r.source).relative_to(root).as_posix(),
                        "After": Path(r.destination).relative_to(root).as_posix(),
                        "Result": "✓ moved" if r.status == "moved" else f"✗ {r.message}",
                    }
                    for r in report.results
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        for r in report.failed:
            st.error(f"{r.source.name}: {r.message}")
        if report.moved and st.button(f"Undo this whole batch ({len(report.moved)} moves)", type="primary"):
            results = undo_batch(journal, report.batch_id)
            ok = sum(r.ok for r in results)
            st.session_state.last_status = f"Undid {ok}/{len(results)} move(s) from batch {report.batch_id}."
            for r in results:
                if not r.ok:
                    st.error(r.message)
            st.session_state.report = None
            st.success(st.session_state.last_status)
            st.rerun()

    st.markdown("**Action history** (persistent journal)")
    actions = journal.list_actions(root=root) if root else journal.list_actions()
    if not actions:
        st.caption("No actions recorded yet.")
        return
    hist = pd.DataFrame(
        [
            {
                "When (UTC)": a.timestamp.replace("T", " ")[:19],
                "Batch": a.batch_id,
                "From": Path(a.original_path).name,
                "To": a.final_path and Path(a.final_path).relative_to(a.root).as_posix(),
                "Status": a.status + (" · undone" if a.undone else ""),
                "Note": a.error or a.undo_error or "",
            }
            for a in actions
        ]
    )
    st.dataframe(hist, width="stretch", hide_index=True, height=min(480, 38 + 35 * len(hist)))
    undoable = [a for a in actions if a.status == "success" and not a.undone]
    if undoable:
        labels = {a.id: f"{Path(a.final_path).name}  ←  {Path(a.original_path).name}  ({a.timestamp[:19]})" for a in undoable}
        c1, c2 = st.columns([4, 1])
        chosen = c1.selectbox("Undo a single move", list(labels), format_func=labels.get, label_visibility="collapsed")
        if c2.button("Undo", width="stretch"):
            res = undo_action(journal, chosen)
            st.session_state.last_status = res.message
            (st.success if res.ok else st.error)(res.message)
            if res.ok:
                st.rerun()


# ---------------------------------------------------------------- main
def main() -> None:
    init_state()
    st.markdown(CSS, unsafe_allow_html=True)
    status = OllamaClient(settings().ollama_url, st.session_state.model).status()
    render_sidebar(status)
    render_header(status)
    if st.session_state.step not in allowed_steps():
        st.session_state.step = max(s for s in allowed_steps() if s <= st.session_state.step)
    [step_select, step_scan, step_review, step_approve, step_results][st.session_state.step]()


main()
