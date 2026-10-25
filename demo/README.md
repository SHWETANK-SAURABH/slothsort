# SlothSort demo

## Prepare (before recording)

```bash
python demo/generate_demo.py        # fresh messy folder in demo/sample_files
ollama list                         # confirm the model is downloaded
streamlit run app.py
```

Optional: run one AI plan on the demo folder before recording. That loads the model into memory, so the
live run on camera is faster.

`demo/sample_files` holds 29 fictional files: invoices, receipts, assignments, meeting and study notes,
generic PDFs (one with no text layer), screenshots, photos, two exact-duplicate pairs (`doc_001 (1).pdf`,
`copy of notes_final_v3.txt`), files in sub-folders, two unsupported files (`.zip`, `.m4a`), and a
prompt-injection file (`readme_please.txt`). Every name, company, date and amount is made up.

To reset at any point: `python demo/generate_demo.py`, or **Select folder → Demo data → Generate / reset**
in the app.

## 2–3 minute script

| Time | Show | Say |
|---|---|---|
| 0:00 | Explorer window of `demo/sample_files` | "This is everyone's Downloads folder: `doc_001.pdf`, `scan_004.pdf`, `IMG_8291.jpg`. Nobody knows what's in here." |
| 0:15 | SlothSort home, sidebar **Local AI ● Model ready**, endpoint `localhost:11434` | "SlothSort runs an open-weight model on this laptop through Ollama. No API key, and nothing leaves the machine." |
| 0:25 | **Use demo folder → Confirm** | "I choose the folder explicitly. It never scans the whole drive." |
| 0:35 | **Scan now**: table + duplicate groups | "Scanning is read-only. It pulls short excerpts from PDFs, Word files and text, reads image metadata only, and finds exact duplicates by SHA-256. It doesn't guess." |
| 0:55 | **Generate plan with local AI**: progress bar, then caption with model name, calls, latency | "The local model proposes a category and a clear name for each file. Every suggestion is re-checked by plain Python: allowed categories, original extension, no paths, no escaping the folder." |
| 1:20 | Point at `readme_please.txt` | "This file tries a prompt injection, telling the model to move itself into `system32`. The model's output can't touch the disk, and the validator would block that path anyway." |
| 1:30 | Point at a row marked **review** and its *Check name* message | "If a name contains a year or ID that isn't in the file, SlothSort flags it for me instead of trusting it." Edit one filename inline. |
| 1:45 | **Select all safe suggestions → Continue to approval** | "Planning changed nothing. Here's the exact from→to list. Nothing is deleted or overwritten." Tick the confirmation box. |
| 2:00 | **Organize**: results cards and before→after table, then the Explorer window showing `Invoices/`, `Notes/`… | "Done. Every move is recorded in a persistent journal." |
| 2:20 | **Undo this whole batch**, Explorer back to messy | "And it's fully reversible. Undo checks the file is unchanged and never overwrites anything." |
| 2:35 | Terminal: `python -m pytest -q` (62 passed) | "Safety rules are covered by automated tests. SlothSort: local, private, reversible." |

## Evaluation

```bash
python demo/evaluate.py --model qwen2.5:3b     # or any pulled model
```

This works on a throwaway copy in `demo/.eval_workspace` and writes `demo/eval_results.json`. Methodology
and the latest measured numbers are in the main README.
