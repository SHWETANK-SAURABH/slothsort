"""Builds a reviewable organization plan using a local Ollama model.

The planner only ever *proposes*. Model output is parsed with Pydantic, then
every category, filename and destination is re-validated by deterministic code
in ``core.path_validator``. Invalid or missing suggestions fall back to the
rule-based classifier and are flagged for human review.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from agent.prompts import SYSTEM_PROMPT, build_retry_prompt, build_user_prompt
from agent.schemas import BatchResponse, ModelSuggestion, PlanItem, response_json_schema
from config import Settings
from core.classifier import classify_heuristic
from core.duplicate_detector import hash_record
from core.path_validator import (
    PathValidationError,
    build_destination,
    normalize_category,
    unique_destination,
    validate_filename,
)
from core.scanner import FileRecord


class ModelError(RuntimeError):
    """The local model could not be reached or returned unusable output."""


@dataclass
class OllamaStatus:
    reachable: bool
    model_available: bool
    models: list[str]
    message: str


class OllamaClient:
    """Minimal client for the local Ollama HTTP API (stdlib only, no telemetry)."""

    def __init__(self, base_url: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _request(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET"
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def status(self) -> OllamaStatus:
        try:
            tags = self._request("/api/tags", timeout=3)
        except (urllib.error.URLError, OSError, ValueError):
            return OllamaStatus(False, False, [], f"Ollama is not running at {self.base_url}.")
        models = sorted(m.get("name", "") for m in tags.get("models", []))
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        available = wanted in models
        msg = f"Connected. Model '{self.model}' is ready." if available else f"Model '{self.model}' is not downloaded."
        return OllamaStatus(True, available, models, msg)

    def chat_json(self, messages: list[dict], schema: dict) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": 8192},
            "keep_alive": "10m",
        }
        try:
            resp = self._request("/api/chat", payload)
        except urllib.error.HTTPError as exc:
            raise ModelError(f"Ollama returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ModelError(f"Could not reach Ollama: {exc}") from exc
        except ValueError as exc:
            raise ModelError("Ollama returned a non-JSON response") from exc
        content = (resp.get("message") or {}).get("content", "")
        if not content:
            raise ModelError("Model returned an empty response")
        return content


@dataclass
class PlanResult:
    items: list[PlanItem] = field(default_factory=list)
    ai_used: bool = False
    model: str = ""
    model_calls: int = 0
    model_failures: int = 0
    fallback_items: int = 0
    latency_s: float = 0.0
    notes: list[str] = field(default_factory=list)


def _describe(record: FileRecord, file_id: str, excerpt_chars: int) -> dict:
    info: dict = {
        "id": file_id,
        "filename": record.name,
        "extension": record.ext.lower(),
        "type": record.kind,
        "size_bytes": record.size,
        "extraction": record.extraction_status,
    }
    meta = {k: v for k, v in record.metadata.items() if isinstance(v, (str, int, float, bool))}
    if meta:
        info["metadata"] = meta
    if record.excerpt:
        info["text_excerpt"] = record.excerpt[:excerpt_chars]
    return info


def parse_batch_response(raw: str, expected_ids: set[str]) -> dict[str, ModelSuggestion]:
    """Parse raw model text. Raises ``ValueError`` if no usable items are present."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = BatchResponse.model_validate_json(text)
    except ValidationError:
        # Tolerate a bare list or a single object.
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not valid JSON ({exc.msg})") from exc
        if isinstance(obj, list):
            obj = {"items": obj}
        elif isinstance(obj, dict) and "items" not in obj:
            obj = {"items": [obj]}
        good = []
        for item in obj.get("items", []) if isinstance(obj, dict) else []:
            try:
                good.append(ModelSuggestion.model_validate(item))
            except ValidationError:
                continue
        parsed = BatchResponse(items=good)
    result = {s.id: s for s in parsed.items if s.id in expected_ids}
    if not result:
        raise ValueError("no items matched the requested file ids")
    return result


class Planner:
    def __init__(self, settings: Settings, client: OllamaClient | None = None):
        self.settings = settings
        self.client = client or OllamaClient(settings.ollama_url, settings.model, settings.request_timeout_s)
        self._cache: dict[tuple[str, str, str], ModelSuggestion] = {}

    def _ask_model(self, batch: list[tuple[str, FileRecord]], result: PlanResult) -> dict[str, ModelSuggestion]:
        files = [_describe(rec, fid, self.settings.excerpt_chars) for fid, rec in batch]
        expected = {fid for fid, _ in batch}
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": build_user_prompt(files)}]
        last_error = ""
        for attempt in range(self.settings.max_retries + 1):  # bounded: never loops forever
            result.model_calls += 1
            try:
                raw = self.client.chat_json(messages, response_json_schema())
                return parse_batch_response(raw, expected)
            except ModelError as exc:
                result.model_failures += 1
                last_error = str(exc)
                break  # transport failures are not fixed by re-prompting
            except ValueError as exc:
                result.model_failures += 1
                last_error = str(exc)
                messages = messages[:2] + [
                    {"role": "assistant", "content": raw[:2000]},
                    {"role": "user", "content": build_retry_prompt(last_error)},
                ]
        result.notes.append(f"Model output unusable for {len(batch)} file(s): {last_error}")
        return {}

    def plan(
        self,
        records: list[FileRecord],
        root: Path,
        use_ai: bool = True,
        progress: Callable[[int, int], None] | None = None,
    ) -> PlanResult:
        """Produce suggestions for ``records``. Reads files only to hash them."""
        started = time.perf_counter()
        result = PlanResult(ai_used=use_ai, model=self.client.model if use_ai else "")
        candidates = [r for r in records if r.supported]
        for r in candidates:
            try:
                hash_record(r)
            except OSError as exc:
                r.extraction_status = f"unreadable ({exc.strerror or exc})"
        candidates = [r for r in candidates if r.sha256]

        suggestions: dict[str, ModelSuggestion] = {}
        if use_ai:
            pending: list[FileRecord] = []
            for r in candidates:
                cached = self._cache.get((self.client.model, r.sha256, r.name))
                if cached:
                    suggestions[r.id] = cached
                else:
                    pending.append(r)
            size = max(1, self.settings.batch_size)
            done = len(candidates) - len(pending)
            for start in range(0, len(pending), size):
                chunk = pending[start : start + size]
                batch = [(f"f{i + 1}", rec) for i, rec in enumerate(chunk)]
                answers = self._ask_model(batch, result)
                for fid, rec in batch:
                    if fid in answers:
                        suggestions[rec.id] = answers[fid]
                        self._cache[(self.client.model, rec.sha256, rec.name)] = answers[fid]
                done += len(chunk)
                if progress:
                    progress(done, len(candidates))

        for r in candidates:
            result.items.append(self._make_item(r, suggestions.get(r.id), use_ai))
        result.fallback_items = sum(1 for i in result.items if i.origin == "rules")
        finalize_destinations(result.items, root)
        result.latency_s = time.perf_counter() - started
        return result

    def _make_item(self, record: FileRecord, suggestion: ModelSuggestion | None, use_ai: bool) -> PlanItem:
        base = dict(
            file_id=record.id,
            source_rel=record.rel_path,
            source_abs=str(record.path),
            sha256=record.sha256,
            size=record.size,
            mtime_ns=record.mtime_ns,
            ext=record.ext,
            duplicate_group=record.duplicate_group,
        )
        problems: list[str] = []
        if suggestion is not None:
            try:
                category = normalize_category(suggestion.category)
                filename = validate_filename(suggestion.suggested_filename, record.ext, self.settings.max_filename_len)
                reason = " ".join(suggestion.reason.split())[:240] or "No reason given by the model."
                needs_review = suggestion.confidence < self.settings.review_threshold
                return PlanItem(
                    **base,
                    category=category,
                    proposed_filename=filename,
                    reason=reason,
                    confidence=suggestion.confidence,
                    origin="ai",
                    needs_review=needs_review,
                )
            except PathValidationError as exc:
                problems.append(f"AI suggestion rejected: {exc}")

        h = classify_heuristic(record)
        if use_ai and not problems:
            problems.append("AI gave no valid suggestion.")
        return PlanItem(
            **base,
            category=h.category,
            proposed_filename=validate_filename(h.suggested_filename, record.ext, self.settings.max_filename_len),
            reason=h.reason,
            confidence=h.confidence,
            origin="rules",
            needs_review=True,
            validation_message=" ".join(problems),
        )


def apply_edit(item: PlanItem, root: Path, category: str | None, filename: str | None, max_len: int = 120) -> None:
    """Apply a user edit to one item, re-running full validation."""
    try:
        if category is not None:
            item.category = normalize_category(category)
        if filename is not None:
            item.proposed_filename = validate_filename(filename, item.ext, max_len)
        item.validation_message = ""
        item.validation_status = "valid"
    except PathValidationError as exc:
        item.validation_status = "invalid"
        item.validation_message = str(exc)


def finalize_destinations(items: list[PlanItem], root: Path) -> None:
    """Compute final, unique, validated destinations for every item.

    Names are made unique against existing files and against each other, so the
    preview shows exactly the name that will be used.
    """
    root = Path(root).resolve()
    reserved: set[str] = set()
    for item in items:
        if item.validation_status == "invalid" and item.validation_message:
            item.destination_rel = item.destination_abs = ""
            continue
        try:
            dest = build_destination(root, item.category, item.proposed_filename)
            if dest.resolve(strict=False) == Path(item.source_abs).resolve():
                item.validation_status = "unchanged"
                item.destination_abs = str(dest)
                item.destination_rel = dest.relative_to(root).as_posix()
                continue
            dest = unique_destination(dest, reserved)
            reserved.add(str(dest).lower())
            item.destination_abs = str(dest)
            item.destination_rel = dest.relative_to(root).as_posix()
            if item.validation_status != "invalid":
                item.validation_status = "valid"
        except PathValidationError as exc:
            item.validation_status = "invalid"
            item.validation_message = str(exc)
            item.destination_rel = item.destination_abs = ""
