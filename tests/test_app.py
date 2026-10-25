"""End-to-end UI test with Streamlit's AppTest and a mocked Ollama."""

import json
import re
from pathlib import Path

import pytest

from agent.planner import OllamaClient, OllamaStatus

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def fake_ollama(monkeypatch):
    def status(self):
        return OllamaStatus(True, True, [self.model], "Connected (mock).")

    def chat_json(self, messages, schema):
        ids = re.findall(r'"id": "(f\d+)"', messages[1]["content"])
        return json.dumps(
            {"items": [{"id": i, "category": "Notes", "suggested_filename": "Meeting_Notes", "reason": "mock", "confidence": 0.9} for i in ids]}
        )

    monkeypatch.setattr(OllamaClient, "status", status)
    monkeypatch.setattr(OllamaClient, "chat_json", chat_json)


def _button(at, label):
    return next(b for b in at.button if b.label.startswith(label))


def test_full_workflow_with_undo(root, tmp_path, monkeypatch, fake_ollama):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("SLOTHSORT_DB", str(tmp_path / "ui.sqlite3"))
    for n in ("a.txt", "b.txt", "c.md"):
        (root / n).write_text(f"meeting notes {n}", encoding="utf-8")
    original = sorted(p.name for p in root.iterdir())

    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_input(key="root_input").set_value(str(root))
    at.run()
    _button(at, "Confirm folder").click()
    at.run()
    _button(at, "Scan now").click()
    at.run()
    _button(at, "Next: plan").click()
    at.run()
    _button(at, "Generate plan with local AI").click()
    at.run()
    assert sorted(p.name for p in root.iterdir()) == original  # planning changed nothing

    _button(at, "Select all safe suggestions").click()
    at.run()
    _button(at, "Continue to approval").click()
    at.run()
    assert _button(at, "Organize").disabled  # explicit confirmation required
    at.checkbox[0].check()
    at.run()
    _button(at, "Organize").click()
    at.run()
    assert not at.exception
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*.*")) == [
        "Notes/Meeting_Notes.md",
        "Notes/Meeting_Notes.txt",
        "Notes/Meeting_Notes_2.txt",
    ]

    _button(at, "Undo this whole batch").click()
    at.run()
    assert not at.exception
    assert sorted(p.name for p in root.iterdir()) == original
