from __future__ import annotations

import json
import hashlib
import logging
import re
from difflib import SequenceMatcher
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from threading import BoundedSemaphore, Event as _ThreadEvent, Lock as _ThreadLock, Thread as _Thread
from typing import TYPE_CHECKING, Iterator
from uuid import uuid4

from fastapi import HTTPException, status

from .ai_provider import AIProvider, ProviderEvent, structured_response
from .i18n import message as msg
from .i18n import normalize_locale
from .platform_repository import PlatformRepository
from .prompts import FIX_VERIFIER_SYSTEM, FIX_VERIFIER_PROMPT_VERSION, INVESTIGATOR_SYSTEM, INVESTIGATOR_PROMPT_VERSION, VERIFIER_SYSTEM, VERIFIER_PROMPT_VERSION, with_language
from .platform_schemas import (
    FindingCandidate, FindingClassification, FindingDecision, FindingDecisionUpdate, FindingRead, FindingRemediationStatus,
    FindingResolution, FindingResolutionUpdate, FindingVerification, FixVerificationResult, InvestigatorResult, ProjectSourceType, ReviewCacheEnvelope, ReviewCreate, ReviewOutputBudgetSnapshot, ReviewRead, ReviewUnitSegment, ReviewUnitState, VerifierResult,
)
from .project_service import ProjectService, now
from .service import MemoryService
from .settings_service import DEFAULT_INVESTIGATOR_MAX_TOKENS, DEFAULT_REVIEW_CONTEXT_CHARS, DEFAULT_VERIFIER_MAX_TOKENS, FIX_VERIFIER_ROLE, PROVIDER_DEFAULT, REVIEW_CONTEXT_CHAR_OPTIONS

#: Module-level registry lock shared by all ReviewService instances (REV-031).
#: Per-project locks are get-or-created atomically under it and then guard the
#: full projection+index workflow, so same-project work serializes while
#: different projects stay concurrent regardless of instance construction.
_REVIEW_PROJECTION_REGISTRY_LOCK = _ThreadLock()

#: Module-level per-project workflow lock map (REV-046). This is the
#: application/shared-process scoped coordination point: every ReviewService
#: instance (including the lightweight ``__new__``-constructed reviewers used by
#: tests) resolves the *same* lock object for a given project here, so
#: same-project projection+index work serializes across instances while
#: different projects keep distinct locks and stay concurrent. Only the map's
#: get-or-create is performed under ``_REVIEW_PROJECTION_REGISTRY_LOCK``; the
#: returned per-project lock is released before any I/O.
_REVIEW_PROJECTION_LOCKS: dict[str, _ThreadLock] = {}

#: FS-FIX-015 server-side logger. Review tracebacks are written here (never to
#: the client) so an unexpected worker crash is diagnosable from the backend log.
logger = logging.getLogger("firmsight.review")


class _ReviewTerminalAbort(RuntimeError):
    """Raised inside ``execute()`` when the review became terminal mid-run.

    A still-running worker must never resurrect a review that the stale path or
    an error handler already marked INTERRUPTED/FAILED (FS-FIX-015 REQ-2). The
    worker notices the terminal status and unwinds via this sentinel, which is
    handled separately from genuine review failures so it does not overwrite the
    terminal write.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"review is already {status}")
        self.status = status

if TYPE_CHECKING:
    from .intelligence_service import IntelligenceService
    from .context_builder import ContextBuilder
    from .knowledge_base_service import KnowledgeBaseService
    from .knowledge_index_service import KnowledgeIndexService


@dataclass(frozen=True)
class ReviewUnit:
    context: str
    files: list[str]
    segments: list[ReviewUnitSegment]
    unit_id: str
    label: str


# Backwards-compatible name used by existing callers/tests.
ReviewContextBatch = ReviewUnit


def _safe_review_error(error: RuntimeError) -> str:
    """Map provider failures to stable, non-content-bearing review text."""
    message = str(error).casefold()
    if "output limit reached before final" in message:
        return "model exhausted output before final structured response"
    if "returned reasoning without final" in message:
        return "model returned reasoning without final structured response"
    if "provider format incompatible" in message:
        return "provider response format has no usable assistant message"
    if "could not be validated after one repair" in message:
        return "finding schema could not be matched after one repair"
    if "invalid json" in message:
        return "provider returned invalid JSON"
    if "timed out" in message:
        return "provider request timed out after 90 seconds"
    if "http " in message:
        import re
        status_code = re.search(r"http\s+(\d{3})", message)
        return f"provider request failed (HTTP {status_code.group(1)})" if status_code else "provider request failed"
    return "provider request failed before a validated result was available"


class ReviewService:
    """Runs full-project, evidence-first reviews in bounded source batches."""

    # A Full Project review must cover every reviewable source file.  Requests are
    # still bounded individually so a provider never receives an entire repository
    # as one opaque prompt.  Large files are split on line boundaries and appear in
    # successive batches, rather than being silently truncated after their prefix.
    MAX_CONTEXT_CHARS = 42_000
    MAX_SOURCE_CHARS_PER_BATCH = 30_000
    MAX_SOURCE_CHARS_PER_SEGMENT = 6_000
    MAX_CONTEXT_SEGMENTS_PER_BATCH = 8
    SOURCE_TAG_OVERHEAD_CHARS = 128
    MAX_SYMBOLS = 50
    MAX_MEMORIES = 12
    MAX_YAML_CHARS = 2_000
    MAX_REREVIEW_FINDINGS = 12
    MAX_REREVIEW_CONTEXT_CHARS = 8_000
    MAX_REREVIEW_CHARS_PER_FILE = 2_400
    RUNNING_STALE_SECONDS = 180
    # FS-FIX-015 REQ-1: the worker refreshes its liveness marker on this period
    # while ``execute()`` is alive, independent of unit completions. It must stay
    # comfortably below RUNNING_STALE_SECONDS so a live (even slow) review is
    # never marked INTERRUPTED by the stale path.
    HEARTBEAT_SECONDS = 30
    # The Settings value is the maximum complete Investigator payload budget.
    # Reserve room for the role prompt, JSON schema, scope/batch instructions,
    # and a small framing margin before packing source excerpts.
    INVESTIGATOR_REQUEST_OVERHEAD_CHARS = 4_096
    # Deterministic minimum/maximum for the per-request source envelope. Small
    # role output budgets pair with smaller source units so a bounded response
    # remains able to reference its evidence; higher budgets grow the unit up
    # to the existing hard caps. Coverage never depends on the size: every
    # reviewable segment is still assigned to exactly one unit.
    MIN_SOURCE_CHARS_PER_BATCH = 8_000
    # Investigator output budget (tokens) mapped to the maximum source chars
    # packed into one request. Buckets keep the mapping deterministic and
    # auditable; interpolation would make batch identity depend on subtle
    # budget changes and invalidate unit caches without a clear reason.
    SOURCE_CHARS_PER_BUDGET_BUCKETS: tuple[tuple[int, int], ...] = (
        (1_200, 12_000),
        (1_600, 16_000),
        (2_000, 20_000),
        (2_400, 24_000),
        (3_200, 30_000),
    )
    DEFAULT_SOURCE_CHARS_FOR_BUDGET = 20_000
    REVIEW_ENGINE_VERSION = "review-engine-v2"
    # Focus profiles are planning hints only. They prioritize relevant indexed
    # symbols and paths, but the Investigator remains the sole source of
    # findings. Terms are normalized below so `error_handling` and
    # `error-handling` behave consistently.
    FOCUS_SIGNAL_TERMS = {
        "memory": {"malloc", "calloc", "realloc", "free", "delete", "heap_caps", "ps_malloc", "memcpy", "memmove", "buffer", "heap", "stack", "pointer", "ownership", "lifetime", "leak"},
        "concurrency": {"mutex", "semaphore", "queue", "event", "shared", "lock", "race", "task"},
        "freertos": {"freertos", "task", "queue", "mutex", "semaphore", "eventgroup", "xqueue", "xsemaphore", "portmaxdelay"},
        "interrupt": {"isr", "interrupt", "fromisr", "critical", "portyield"},
        "watchdog": {"watchdog", "wdt", "feed", "reset"},
        "networking": {"wifi", "network", "socket", "tcp", "udp", "tls", "http", "mqtt"},
        "mqtt": {"mqtt", "broker", "topic", "publish", "subscribe", "disconnect"},
        "ota": {"ota", "upgrade", "firmware", "partition", "rollback", "espota"},
        "security": {"security", "crypto", "tls", "certificate", "credential", "auth", "nonce"},
        "peripheral": {"peripheral", "gpio", "spi", "i2c", "uart", "adc", "sensor", "device"},
        "errorhandling": {"error", "fail", "failure", "retry", "timeout", "result", "esp_err"},
        "architecture": {"component", "module", "state", "init", "config", "lifecycle"},
    }
    FINDING_TEXT_STOPWORDS = {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "for", "from", "has", "in", "is", "it",
        "may", "might", "no", "not", "of", "on", "or", "that", "the", "their", "this", "to", "when", "with", "without",
    }

    def __init__(self, repository: PlatformRepository, projects: ProjectService, provider_resolver: Callable[[str], AIProvider], memories: MemoryService, intelligence: "IntelligenceService | None" = None, review_context_resolver: Callable[[], int] | None = None, review_parallel_resolver: Callable[[], int] | None = None, context_builder: "ContextBuilder | None" = None, knowledge_base: "KnowledgeBaseService | None" = None, knowledge_index: "KnowledgeIndexService | None" = None, post_review_projection: "Callable[[str], dict[str, object]] | None" = None, language_resolver: Callable[[], str] | None = None) -> None:
        self.repository, self.projects, self.provider_resolver, self.memories = repository, projects, provider_resolver, memories
        self.intelligence = intelligence
        self.review_context_resolver = review_context_resolver or (lambda: DEFAULT_REVIEW_CONTEXT_CHARS)
        self.review_parallel_resolver = review_parallel_resolver or (lambda: 1)
        self.context_builder = context_builder
        self.knowledge_base = knowledge_base
        self.knowledge_index = knowledge_index
        self.post_review_projection = post_review_projection
        self.language_resolver: Callable[[], str] | None = language_resolver
        self._post_review_projection_locks: dict[str, _ThreadLock] = {}
        self._post_review_projection_registry_lock = self._post_review_projection_lock_registry_lock()

    def _locale(self) -> str:
        """UI language for user-facing review progress text (FS-I18N-016).

        Uses ``getattr`` because lightweight ``__new__``-constructed reviewer
        instances in tests never ran ``__init__``; missing state means English.
        """
        resolver = getattr(self, "language_resolver", None)
        return normalize_locale(resolver() if resolver else None)

    def _t(self, key: str, /, **params: object) -> str:
        return msg(key, self._locale(), **params)

    def _error(self, status_code: int, key: str, /, **params: object) -> HTTPException:
        return HTTPException(status_code, self._t(key, **params))

    def _plural(self, count: int, singular: str, plural: str | None = None) -> str:
        from .i18n import plural_units
        return plural_units(count, singular, plural)

    def _review_parallel_limit(self) -> int:
        try:
            value = int(self.review_parallel_resolver())
        except (TypeError, ValueError):
            value = 1
        return value if value in {1, 2, 3} else 1

    @classmethod
    def _finding_tokens(cls, value: str) -> set[str]:
        """Return stable issue tokens for conservative, in-run duplicate detection."""
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if token not in cls.FINDING_TEXT_STOPWORDS and len(token) > 1
        }

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Normalize an AI finding title for stable referencing (FS-FIND-001 REQ-2).

        Trims, collapses internal whitespace, uppercases the first character,
        drops a trailing period, and bounds the length. Existing ids are never
        derived from the title, so normalization cannot renumber anything.
        """
        normalized = " ".join((title or "").split()).strip()
        if normalized:
            normalized = normalized[0].upper() + normalized[1:]
        if normalized.endswith("."):
            normalized = normalized[:-1].rstrip()
        return normalized[:200]

    def _next_finding_id(self, project_id: str) -> str:
        """Return the next project-scoped sequential finding id (FS-FIND-001 REQ-1).

        Scans the project's existing finding ids, keeps the highest numeric
        ``FS-<digits>`` suffix, and returns the next one. Legacy random hex ids
        (for example ``FS-A1B2C3D4``) contribute no sequence number and are left
        untouched, and a project with no sequential ids starts at ``FS-001``.
        """
        highest = 0
        for item in self.repository.findings(project_id):
            match = re.fullmatch(r"FS-(\d+)", str(item.get("id") or ""))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"FS-{highest + 1:03d}"

    @classmethod
    def _title_similarity(cls, left_title: str, right_title: str) -> float:
        """Token-based similarity of two titles after normalization (FS-FIND-001 REQ-3.1)."""
        left_tokens = " ".join(sorted(cls._finding_tokens(cls._normalize_title(left_title))))
        right_tokens = " ".join(sorted(cls._finding_tokens(cls._normalize_title(right_title))))
        return SequenceMatcher(None, left_tokens, right_tokens).ratio()

    @staticmethod
    def _evidence_overlap(left: FindingCandidate | FindingRead, right: FindingCandidate | FindingRead) -> int:
        """Count evidence items shared by exact ``(file, line)`` position (FS-FIND-001 REQ-3.3)."""
        def positions(item: FindingCandidate | FindingRead) -> set[tuple[str, int]]:
            return {
                (str(entry.file).replace("\\", "/"), int(entry.line))
                for entry in (item.evidence or [])
            }

        return len(positions(left) & positions(right))

    # Two findings in the same file may be compared across a wider line window
    # when they also share an exact function symbol; otherwise the window stays
    # tight so unrelated defects in the same file are not merged (REQ-3.2).
    SAME_FUNCTION_LINE_WINDOW = 25
    LINE_WINDOW = 6
    TITLE_DUPLICATE_THRESHOLD = 0.70

    @classmethod
    def _same_finding_candidate(cls, left: FindingCandidate | FindingRead, right: FindingCandidate | FindingRead) -> bool:
        """Identify two model findings that describe the same source issue.

        Matching is scoped to the same file and category, because different
        defects can share words such as ``error`` or ``queue``. Two findings that
        share an exact function symbol are compared across a wider line window and
        may rely on an evidence overlap; otherwise the window stays tight and
        title/summary corroboration is required. A normalized-title similarity of
        at least ``0.70`` marks a duplicate on its own.
        """
        left_location, right_location = left.location, right.location
        if left_location.file.replace("\\", "/") != right_location.file.replace("\\", "/"):
            return False
        if left.category.casefold() != right.category.casefold():
            return False
        same_function = bool(left_location.function) and left_location.function == right_location.function
        if left_location.function and right_location.function and left_location.function != right_location.function:
            return False

        window = cls.SAME_FUNCTION_LINE_WINDOW if same_function else cls.LINE_WINDOW
        if left_location.line_start > right_location.line_end + window or right_location.line_start > left_location.line_end + window:
            return False

        # REQ-3.3: two shared evidence positions are strong independent proof.
        if cls._evidence_overlap(left, right) >= 2:
            return True

        # REQ-3.1: normalized title similarity is authoritative.
        title_similarity = cls._title_similarity(left.title, right.title)
        if title_similarity >= cls.TITLE_DUPLICATE_THRESHOLD:
            return True

        shared_title_tokens = cls._finding_tokens(left.title) & cls._finding_tokens(right.title)
        left_summary = " ".join(sorted(cls._finding_tokens(left.summary)))
        right_summary = " ".join(sorted(cls._finding_tokens(right.summary)))
        summary_similarity = SequenceMatcher(None, left_summary, right_summary).ratio()
        return len(shared_title_tokens) >= 2 and (summary_similarity >= 0.28 or same_function)

    @staticmethod
    def _is_overload_error(error: RuntimeError) -> bool:
        """Recognize transient provider saturation without inspecting source content."""
        message = str(error).casefold()
        return any(token in message for token in ("http 429", "http 503", "rate limit", "too many requests", "overloaded", "overload", "capacity"))

    def _unit_cache_key(self, project_id: str, review, batch: ReviewContextBatch, investigator: AIProvider, verifier: AIProvider) -> str:
        """Hash stable metadata and ordered segment identities; never persist source text."""
        memory_context = self._memory_context_fingerprint(project_id, batch.files)
        material = {
            "engine_version": self.REVIEW_ENGINE_VERSION,
            "investigator_prompt_version": INVESTIGATOR_PROMPT_VERSION,
            "verifier_prompt_version": VERIFIER_PROMPT_VERSION,
            "project_id": project_id,
            "scope": review.scope,
            "focus": list(review.focus),
            "unit_id": batch.unit_id,
            "segments": [item.model_dump(mode="json") for item in batch.segments],
            "investigator_model": getattr(investigator, "model", ""),
            "verifier_model": getattr(verifier, "model", ""),
            "investigator_budget": getattr(investigator, "max_tokens", None),
            "verifier_budget": getattr(verifier, "max_tokens", None),
            "structured_policy": getattr(investigator, "structured_output_mode", ""),
            "reasoning_policy": getattr(investigator, "reasoning_effort", ""),
            "verifier_structured_policy": getattr(verifier, "structured_output_mode", ""),
            "verifier_reasoning_policy": getattr(verifier, "reasoning_effort", ""),
            "memory_fingerprint": memory_context,
            "yaml_fingerprint": hashlib.sha256((self.repository.latest_yaml(project_id) or {}).get("content", "").encode()).hexdigest(),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _memory_context_fingerprint(self, project_id: str, files: list[str]) -> str:
        if self.intelligence:
            symbols = [item["name"] for item in self.repository.list_symbols(project_id) if item["file"] in files]
            context = self.intelligence.review_memory_context(project_id, sorted(symbols), sorted(files), cap=self.MAX_MEMORIES)
        else:
            context = "\n".join(item.statement for item in self.memories.context(project_id, None, None).memories[:self.MAX_MEMORIES])
        return hashlib.sha256(context.encode("utf-8")).hexdigest()

    def _review_context_chars(self) -> int:
        value = self.review_context_resolver()
        return value if value in REVIEW_CONTEXT_CHAR_OPTIONS else DEFAULT_REVIEW_CONTEXT_CHARS

    def _diagnostic_sink(
        self,
        review_id: str,
        *,
        role: str,
        batch_number: int | None = None,
        total_batches: int | None = None,
        file_count: int = 0,
        execution_attempt: int = 1,
        event_collector: list[dict] | None = None,
    ) -> Callable[[ProviderEvent], None]:
        def record(event: ProviderEvent) -> None:
            record = {
                "review_id": review_id,
                "request_id": event.request_id,
                "operation": event.operation,
                "role": role,
                "batch_number": batch_number,
                "total_batches": total_batches,
                "file_count": file_count,
                "state": event.state,
                "attempt": event.attempt,
                "execution_attempt": execution_attempt,
                "repair_attempted": event.repair_attempted,
                "provider": event.provider,
                "model": event.model,
                "endpoint": event.endpoint,
                "created_at": event.created_at,
                "elapsed_ms": event.elapsed_ms,
                "http_status": event.http_status,
                "content_type": event.content_type,
                "request_chars": event.request_chars,
                "response_chars": event.response_chars,
                "usage": event.usage.as_dict() if event.usage else None,
                "error_kind": event.error_kind,
                "error_message": event.error_message,
                "content_state": event.content_state,
                "finish_reason": event.finish_reason,
                "structured_mode": event.structured_mode,
                "finalization_policy": event.finalization_policy,
                "finalization_recovery": event.finalization_recovery,
                "effective_max_tokens": event.effective_max_tokens,
                "validation_category": event.validation_category,
                "validation_fields": list(event.validation_fields),
                "retry_suppressed": event.retry_suppressed,
            }
            # Provider workers never mutate SQLite directly.  The coordinator
            # drains this list after each future completes.
            if event_collector is not None:
                event_collector.append(record)
            else:
                self.repository.create_review_diagnostic(record)

        return record

    def begin(self, project_id: str, request: ReviewCreate) -> ReviewRead:
        project = self.projects.get(project_id)
        raw_files = self.repository.raw_files(project_id)
        if not raw_files:
            raise self._error(status.HTTP_409_CONFLICT, "error.import_before_review")
        investigator = self.provider_resolver("investigator")
        verifier = self.provider_resolver("verifier")
        if not investigator.available() or not verifier.available():
            raise self._error(status.HTTP_503_SERVICE_UNAVAILABLE, "error.review_requires_models")

        indexed = self.projects.index(project_id)
        timestamp = now()
        review_id = f"REV-{uuid4().hex[:10].upper()}"
        context_chars = self._review_context_chars()
        source_snapshot_hash = ProjectService.snapshot_hash(raw_files)
        batches = self._context_batches(project_id, project.name, request.focus, raw_files, context_limit_chars=context_chars, investigator_budget=getattr(investigator, "max_tokens", None))
        if not batches:
            raise self._error(status.HTTP_409_CONFLICT, "error.no_reviewable_files")
        context_files = list(dict.fromkeys(path for batch in batches for path in batch.files))
        rereview_count = len(self._rereview_candidates(project_id))
        progress = [
            self._t("review.queued_scope", scope=request.scope, focus=', '.join(request.focus)),
            self._t("review.indexed", files=indexed.file_count, symbols=indexed.symbol_count),
            self._t("review.prepared_batches", batches=len(batches), files=len(context_files), chars=context_chars),
        ]
        if rereview_count:
            progress.append(self._t("review.queued_rereview", count=rereview_count, s=self._plural(rereview_count, "")))
        output_budget_snapshot = self._output_budget_snapshot(investigator, verifier)
        self.repository.create_review({
            "id": review_id,
            "project_id": project_id,
            "scope": request.scope,
            "focus": request.focus,
            "context_files": context_files,
            "context_chars": context_chars,
            "source_snapshot_hash": source_snapshot_hash,
            "total_batches": len(batches),
            "validated_batches": 0,
            "unavailable_batches": 0,
            "status": "RUNNING",
            "progress": progress,
            "created_at": timestamp,
            "last_activity_at": timestamp,
            "completed_at": None,
            "execution_progress": {
                "phase": "PLANNING", "total_units": len(batches), "completed_units": 0,
                "reused_units": 0, "unavailable_units": 0, "in_flight_requests": 0,
                "parallel_request_limit": self._review_parallel_limit(), "current_units": [],
            },
            "execution_attempt": 1,
            "unit_states": {},
            "output_budget_snapshot": output_budget_snapshot.model_dump(mode="json"),
        })
        return self.get(review_id)

    @staticmethod
    def _output_budget_snapshot(investigator: AIProvider, verifier: AIProvider) -> ReviewOutputBudgetSnapshot:
        def budget(provider: AIProvider, fallback: int) -> int | str:
            value = getattr(provider, "max_tokens", fallback)
            if value is None:
                return PROVIDER_DEFAULT
            return value if isinstance(value, int) and not isinstance(value, bool) else fallback

        investigator_budget = budget(investigator, DEFAULT_INVESTIGATOR_MAX_TOKENS)
        verifier_budget = budget(verifier, DEFAULT_VERIFIER_MAX_TOKENS)
        verifier_fix_budget = verifier_budget if isinstance(verifier_budget, int) else DEFAULT_VERIFIER_MAX_TOKENS
        return ReviewOutputBudgetSnapshot(
            investigator=investigator_budget,
            verifier=verifier_budget,
            verifier_fix=verifier_fix_budget,
        )

    @staticmethod
    def _legacy_unit_states(review: ReviewRead) -> dict[str, ReviewUnitState]:
        """Recover retry targets for reviews created before per-unit state existed."""
        states: dict[str, ReviewUnitState] = {}
        for event in review.diagnostics:
            if event.batch_number is None:
                continue
            key = str(event.batch_number)
            current = states.get(key, ReviewUnitState(state="COMPLETED", investigator_validated=False))
            if event.role == "investigator":
                if event.state == "STRUCTURED_VALIDATED":
                    current.investigator_validated = True
                elif event.state in {"FAILED", "RETRY_SUPPRESSED"}:
                    current.state = "UNAVAILABLE"
                    current.investigator_validated = False
            elif event.role == "verifier":
                if event.state in {"FAILED", "RETRY_SUPPRESSED"}:
                    current.state = "UNAVAILABLE"
            states[key] = current
        if review.unavailable_batches and not any(item.state == "UNAVAILABLE" for item in states.values()):
            validated = {int(key) for key, item in states.items() if item.investigator_validated}
            for number in range(1, review.total_batches + 1):
                if number not in validated:
                    states[str(number)] = ReviewUnitState(state="UNAVAILABLE", investigator_validated=False)
                    if sum(item.state == "UNAVAILABLE" for item in states.values()) >= review.unavailable_batches:
                        break
        return states

    def _review_unit_states(self, review: ReviewRead) -> dict[str, ReviewUnitState]:
        return review.unit_states or self._legacy_unit_states(review)

    def _is_review_terminal(self, review_id: str) -> bool:
        """True once the review reached a terminal state (FS-FIX-015 REQ-2)."""
        return self.repository.review_status(review_id) not in (None, "RUNNING")

    @contextmanager
    def worker_heartbeat(self, review_id: str) -> Iterator[None]:
        """Keep ``last_activity_at`` fresh while the review worker is alive.

        A daemon thread refreshes only the liveness marker on a fixed period
        (well below ``RUNNING_STALE_SECONDS``), so a genuinely working review —
        including one blocked inside a single slow provider request — is never
        marked INTERRUPTED by the stale path. The thread stops cleanly when the
        ``with`` block exits, whether normally or via an exception. A failed
        heartbeat write is swallowed (logged) so it can never fail the review.
        """
        stop = _ThreadEvent()

        def _beat() -> None:
            while not stop.wait(self.HEARTBEAT_SECONDS):
                try:
                    self.repository.touch_review_activity(review_id)
                except Exception:  # noqa: BLE001 - liveness must never crash the worker
                    logger.warning("review heartbeat write failed for %s", review_id, exc_info=True)

        thread = _Thread(target=_beat, name=f"firmsight-heartbeat-{review_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5.0)

    def _persist_terminal_review(self, review_id: str, *, error: str | None = None, **fields) -> None:
        """Write a terminal review state with one bounded retry (FS-FIX-015 REQ-3).

        The handler for an unexpected worker error may itself race a SQLite lock;
        this retries the terminal write once and never lets the failure escape,
        so ``execute()`` always leaves a terminal record. ``status`` is forced to
        FAILED unless the caller passed one.
        """
        fields.setdefault("status", "FAILED")
        fields.setdefault("error", error)
        fields.setdefault("completed_at", now())
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self.repository.update_review(review_id, **fields)
                return
            except Exception as exc:  # noqa: BLE001 - terminal write must be best-effort
                last_error = exc
                logger.error(
                    "review %s terminal-state write attempt %d/2 failed: %s",
                    review_id, attempt + 1, exc, exc_info=True,
                )
        logger.critical(
            "review %s could not persist a terminal state after retries: %s",
            review_id, last_error,
        )

    def execute(self, review_id: str) -> None:
        review = self.get(review_id)
        if review.status != "RUNNING":
            return
        project = self.projects.get(review.project_id)
        investigator = self.provider_resolver("investigator")
        verifier = self.provider_resolver("verifier")
        fix_verifier = self.provider_resolver(FIX_VERIFIER_ROLE)
        progress = list(review.progress)
        previous_unit_states = self._review_unit_states(review)
        if review.execution_attempt == 1 and not previous_unit_states:
            retry_target_batches: set[int] | None = None
        else:
            retry_target_batches = {
                int(number) for number, state in previous_unit_states.items() if state.state == "UNAVAILABLE"
            }
        completed_investigator_batches = sum(
            state.investigator_validated for state in previous_unit_states.values()
            if state.state == "COMPLETED"
        )
        skipped_investigator_batches = 0
        current_unit_states: dict[str, ReviewUnitState] = dict(previous_unit_states)
        execution = review.execution_progress.model_dump()
        try:
            with self.worker_heartbeat(review_id):
                self._execute_body(
                    review,
                    review_id,
                    project,
                    investigator,
                    verifier,
                    fix_verifier,
                    progress,
                    previous_unit_states,
                    retry_target_batches,
                    completed_investigator_batches,
                    skipped_investigator_batches,
                    current_unit_states,
                    execution,
                )
        except _ReviewTerminalAbort as abort:
            # The review became terminal (INTERRUPTED/FAILED) while this worker
            # was running. Do not overwrite that terminal state with RUNNING.
            logger.info("review %s stopped: %s", review_id, abort)
        except RuntimeError as error:
            logger.warning("review %s failed before a validated result: %s", review_id, error)
            progress.append(self._t("review.failed_no_result"))
            self._persist_terminal_review(
                review_id,
                progress=progress,
                completed_at=now(),
                error=_safe_review_error(error),
                execution_progress={**execution, "phase": "FAILED", "in_flight_requests": 0, "current_units": []},
                unit_states={key: value.model_dump() for key, value in current_unit_states.items()},
                total_batches=review.total_batches,
                validated_batches=completed_investigator_batches,
                unavailable_batches=max(skipped_investigator_batches, review.total_batches - completed_investigator_batches),
            )
        except Exception:  # noqa: BLE001 - background jobs must become an explicit terminal state
            logger.exception("review %s stopped because an unexpected internal error occurred", review_id)
            progress.append(self._t("review.stopped_unexpected"))
            self._persist_terminal_review(
                review_id,
                progress=progress,
                completed_at=now(),
                error="AI review stopped unexpectedly",
                execution_progress={**execution, "phase": "FAILED", "in_flight_requests": 0, "current_units": []},
                unit_states={key: value.model_dump() for key, value in current_unit_states.items()},
                total_batches=review.total_batches,
                validated_batches=completed_investigator_batches,
                unavailable_batches=max(skipped_investigator_batches, review.total_batches - completed_investigator_batches),
            )

    def _execute_body(
        self,
        review: ReviewRead,
        review_id: str,
        project,
        investigator,
        verifier,
        fix_verifier,
        progress: list[str],
        previous_unit_states: dict[str, ReviewUnitState],
        retry_target_batches: set[int] | None,
        completed_investigator_batches: int,
        skipped_investigator_batches: int,
        current_unit_states: dict[str, ReviewUnitState],
        execution: dict,
    ) -> None:
        source_refreshed = False
        changed_files: list[str] = []
        if project.source_type is ProjectSourceType.LOCAL_DIRECTORY:
            try:
                _, changed_files = self.projects.refresh_local_directory(review.project_id)
                source_refreshed = True
                progress.append(self._t("review.refreshed_source", count=len(changed_files), s=self._plural(len(changed_files), "")))
            except HTTPException as error:
                progress.append(self._t("review.refresh_failed", detail=error.detail))
            self.repository.update_review(review_id, status="RUNNING", progress=progress)
        raw_files = self.repository.raw_files(review.project_id)
        current_snapshot_hash = ProjectService.snapshot_hash(raw_files)
        # ``review.diagnostics`` are typed ReviewDiagnosticRead models (not dicts),
        # so use the attribute. Reading it with ``.get`` crashed the retry path
        # whenever a review had diagnostics but zero validated/unavailable
        # batches (the short-circuit did not mask it) — FS-FIX-015.
        has_prior_investigator_work = bool(
            review.validated_batches
            or review.unavailable_batches
            or any(event.role == "investigator" for event in review.diagnostics)
        )
        if has_prior_investigator_work and (
            not review.source_snapshot_hash or current_snapshot_hash != review.source_snapshot_hash
        ):
            message = self._t("review.source_changed")
            progress.append(message)
            self.repository.update_review(
                review_id,
                status="FAILED",
                progress=progress,
                completed_at=now(),
                error=message,
                total_batches=review.total_batches,
                validated_batches=review.validated_batches,
                unavailable_batches=max(review.unavailable_batches, review.total_batches - review.validated_batches),
            )
            return
        if current_snapshot_hash != review.source_snapshot_hash:
            self.repository.update_review(
                review_id,
                status="RUNNING",
                progress=progress,
                source_snapshot_hash=current_snapshot_hash,
            )
        self._rereview_open_confirmed_findings(
            review_id,
            review.project_id,
            fix_verifier,
            raw_files,
            source_refreshed,
            changed_files,
            progress,
        )
        self._revalidate_intelligence(review.project_id, progress)
        batches = self._context_batches(review.project_id, project.name, review.focus, raw_files, context_limit_chars=review.context_chars, investigator_budget=getattr(investigator, "max_tokens", None))
        if not batches:
            raise RuntimeError("No reviewable firmware source files were available for AI context")
        if retry_target_batches is None:
            retry_target_batches = set(range(1, len(batches) + 1))
        persisted = 0
        # FS-FIND-001 REQ-3.1: the persistence signature uses the normalized
        # title, so a rephrased-but-identical candidate at the same location is
        # still caught before it is stored. Within-execution matching uses
        # ``seen_candidates`` (this review plus retry state) only, so a later
        # review may still legitimately re-surface an issue after a fix.
        project_findings = self.findings(review.project_id)
        seen_candidates: list[FindingCandidate | FindingRead] = [
            item for item in project_findings if item.review_id == review_id
        ]
        existing_candidate_signatures: set[tuple[str, int, int, str]] = {
            (item.location.file, item.location.line_start, item.location.line_end, self._normalize_title(item.title).casefold())
            for item in project_findings
        }
        validated_batch_numbers = {
            int(number) for number, state in previous_unit_states.items()
            if state.state == "COMPLETED" and int(number) not in retry_target_batches
        }
        parallel_limit = self._review_parallel_limit()
        # One shared admission controller is used by both worker phases.
        # The coordinator may reduce dispatch after an overload, but can
        # never exceed the configured provider concurrency budget.
        request_slots = BoundedSemaphore(parallel_limit)
        execution = {"phase": "REVIEWING", "total_units": len(batches), "completed_units": completed_investigator_batches, "reused_units": 0, "unavailable_units": skipped_investigator_batches, "in_flight_requests": 0, "parallel_request_limit": parallel_limit, "current_units": []}
        self.repository.update_review(review_id, status="RUNNING", progress=progress, total_batches=len(batches), validated_batches=completed_investigator_batches, unavailable_batches=skipped_investigator_batches, execution_progress=execution)

        def run_investigator(batch_number: int, batch: ReviewContextBatch):
            events: list[dict] = []
            try:
                with request_slots:
                    result = structured_response(
                        investigator, InvestigatorResult, with_language(INVESTIGATOR_SYSTEM, self._locale()),
                        f"Required JSON Schema:\n{json.dumps(InvestigatorResult.model_json_schema(), ensure_ascii=False)}\n\nReview scope: {review.scope}\nFocus: {', '.join(review.focus)}\nThis is source batch {batch_number}/{len(batches)}. Return at most 1 high-confidence candidate from this batch; return an empty findings list if none qualify. Keep the title, summary, runtime scenario, impact, and recommendation concise, and include only the evidence and execution steps needed to prove the issue.\n\n{batch.context}",
                        operation=f"review:{review_id}:investigator:batch-{batch_number}",
                        on_event=self._diagnostic_sink(review_id, role="investigator", batch_number=batch_number, total_batches=len(batches), file_count=len(batch.files), execution_attempt=review.execution_attempt, event_collector=events),
                    )
                return batch_number, batch, result, None, events
            except RuntimeError as error:
                return batch_number, batch, None, error, events

        investigator_results: dict[int, tuple[ReviewContextBatch, InvestigatorResult | None, RuntimeError | None]] = {}
        cached_verifications: dict[tuple[int, int], VerifierResult] = {}
        unit_keys = {number: self._unit_cache_key(review.project_id, review, batch, investigator, verifier) for number, batch in enumerate(batches, start=1)}
        reused_units = 0
        for number, batch in enumerate(batches, start=1):
            if number not in retry_target_batches or number in validated_batch_numbers:
                continue
            cached = self.repository.get_review_unit_cache(review.project_id, unit_keys[number])
            if not cached or cached.get("schema_version") != 1:
                continue
            try:
                envelope = ReviewCacheEnvelope.model_validate(cached.get("outcome") or {})
                expected_segments = [item.model_dump(mode="json") for item in batch.segments]
                if envelope.schema_version != 1 or envelope.unit_id != batch.unit_id or [item.model_dump(mode="json") for item in envelope.segments] != expected_segments:
                    continue
                cached_result = envelope.investigator
                if envelope.kind == "VALIDATED_FINDINGS":
                    if not cached_result.findings or len(envelope.verifications) != len(cached_result.findings):
                        continue
                    if not all(self._candidate_has_real_evidence(item, raw_files, batch.segments) for item in cached_result.findings):
                        continue
                    for index, value in enumerate(envelope.verifications, start=1):
                        cached_verifications[(number, index)] = value
                elif envelope.kind != "VALIDATED_EMPTY" or cached_result.findings or envelope.verifications:
                    continue
            except Exception:
                continue
            investigator_results[number] = (batch, cached_result, None)
            validated_batch_numbers.add(number)
            completed_investigator_batches += 1
            reused_units += 1
            progress.append(
                self._t("review.reused_unit", number=number, total=len(batches), count=len(cached_result.findings), s=self._plural(len(cached_result.findings), ""))
            )
        pending = [(number, batch) for number, batch in enumerate(batches, start=1) if number not in validated_batch_numbers]
        for number, batch in pending:
            progress.append(self._t("review.investigator_batch", number=number, total=len(batches), files=len(batch.files)))
        execution["reused_units"] = reused_units
        # A unit is complete only after its candidates have passed through
        # verification. Cache hits already contain both validated stages.
        terminal_units: set[int] = set(validated_batch_numbers)
        failed_verification_units: set[int] = set()
        failed_investigator_units: set[int] = set()

        def persist_execution() -> None:
            # FS-FIX-015 REQ-2: never flip a terminal review back to RUNNING. If
            # the stale path (or an error handler) already ended this review, the
            # worker aborts instead of resurrecting it.
            if self._is_review_terminal(review_id):
                raise _ReviewTerminalAbort(self.repository.review_status(review_id) or "TERMINAL")
            unavailable_units = skipped_investigator_batches + len(failed_verification_units)
            execution["completed_units"] = min(len(batches), len(terminal_units) + unavailable_units)
            execution["unavailable_units"] = unavailable_units
            self.repository.update_review(
                review_id, status="RUNNING", progress=progress,
                total_batches=len(batches), validated_batches=completed_investigator_batches,
                unavailable_batches=skipped_investigator_batches, execution_progress=execution,
            )

        def record_investigator_result(result) -> None:
            nonlocal completed_investigator_batches, skipped_investigator_batches
            batch_number, batch, candidates, error, events = result
            for event in events:
                self.repository.create_review_diagnostic(event)
            investigator_results[batch_number] = (batch, candidates, error)
            if error is None:
                completed_investigator_batches += 1
                validated_batch_numbers.add(batch_number)
                progress.append(self._t("review.investigator_batch_result", number=batch_number, total=len(batches), count=len(candidates.findings)))
                if not candidates.findings:
                    terminal_units.add(batch_number)
                    envelope = ReviewCacheEnvelope(schema_version=1, kind="VALIDATED_EMPTY", unit_id=batches[batch_number - 1].unit_id, segments=batches[batch_number - 1].segments, investigator=candidates)
                    self.repository.set_review_unit_cache(review.project_id, unit_keys[batch_number], envelope.model_dump(mode="json"))
            else:
                skipped_investigator_batches += 1
                failed_investigator_units.add(batch_number)
                progress.append(self._t("review.investigator_batch_skipped", number=batch_number, total=len(batches), reason=_safe_review_error(error)))
            persist_execution()

        batch_states: dict[int, list[dict]] = {}
        cacheable_batches: dict[int, bool] = {}
        pending_verifiers: list[tuple[int, int, ReviewContextBatch, FindingCandidate]] = []

        def prepare_verifier_batch(batch_number: int) -> None:
            batch, candidates, error = investigator_results[batch_number]
            if error is not None or candidates is None or batch_number in batch_states:
                return
            states: list[dict] = []
            cacheable_batches[batch_number] = True
            for candidate_number, candidate in enumerate(candidates.findings, start=1):
                # FS-FIND-001 REQ-3.1: the signature uses the normalized title
                # so it stays consistent with ``existing_candidate_signatures``
                # and catches rephrased-but-identical candidates. Matching stays
                # scoped to this review execution (plus retry state): a later
                # review may still legitimately re-surface an issue after a fix.
                key = (candidate.location.file, candidate.location.line_start, candidate.location.line_end, self._normalize_title(candidate.title).casefold())
                cached_verification = cached_verifications.get((batch_number, candidate_number))
                duplicate = next((item for item in seen_candidates if self._same_finding_candidate(candidate, item)), None)
                if duplicate is not None or (key in existing_candidate_signatures and cached_verification is None):
                    cacheable_batches[batch_number] = False
                    duplicate_title = f" (matches: {duplicate.title})" if duplicate is not None else ""
                    progress.append(self._t("review.skipped_duplicate", number=batch_number, title=candidate.title, match=duplicate_title))
                    continue
                if not self._candidate_has_real_evidence(candidate, raw_files, batch.segments):
                    cacheable_batches[batch_number] = False
                    progress.append(self._t("review.discarded_unverifiable", number=batch_number, candidate=candidate_number))
                    continue
                seen_candidates.append(candidate)
                state = {"candidate_number": candidate_number, "candidate": candidate, "verification": cached_verification, "error": None}
                states.append(state)
                if cached_verification is None:
                    pending_verifiers.append((batch_number, candidate_number, batch, candidate))
                progress.append(self._t("review.verifier_challenging", number=batch_number, candidate=candidate_number, count=len(candidates.findings), title=candidate.title))
            batch_states[batch_number] = states
            if not states:
                terminal_units.add(batch_number)

        def run_verifier(job: tuple[int, int, ReviewContextBatch, FindingCandidate]):
            batch_number, candidate_number, batch, candidate = job
            events: list[dict] = []
            try:
                with request_slots:
                    result = structured_response(
                        verifier, VerifierResult, with_language(VERIFIER_SYSTEM, self._locale()),
                        f"Required JSON Schema:\n{json.dumps(VerifierResult.model_json_schema(), ensure_ascii=False)}\n\nCandidate JSON:\n{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}\n\nRelevant project context (source batch {batch_number}/{len(batches)}):\n{batch.context}",
                        operation=f"review:{review_id}:verifier:batch-{batch_number}:candidate-{candidate_number}",
                        on_event=self._diagnostic_sink(review_id, role="verifier", batch_number=batch_number, total_batches=len(batches), file_count=len(batch.files), execution_attempt=review.execution_attempt, event_collector=events),
                    )
                return job, result, None, events
            except RuntimeError as error:
                return job, None, error, events

        # Seed cached Investigator results; newly completed Investigator
        # workers enqueue their Verifier jobs immediately in the same loop.
        for batch_number in sorted(investigator_results):
            prepare_verifier_batch(batch_number)

        active_limit = parallel_limit
        verifier_results: dict[tuple[int, int], tuple[VerifierResult | None, RuntimeError | None]] = {}
        active_labels: dict[object, str] = {}
        pending_investigators = list(pending)
        with ThreadPoolExecutor(max_workers=parallel_limit, thread_name_prefix="firmsight-review") as executor:
            futures: dict[object, tuple[str, object]] = {}
            while pending_investigators or pending_verifiers or futures:
                while len(futures) < active_limit and (pending_investigators or pending_verifiers):
                    if pending_verifiers:
                        job = pending_verifiers.pop(0)
                        future = executor.submit(run_verifier, job)
                        futures[future] = ("verifier", (job[0], job[1]))
                        active_labels[future] = f"Verifier batch {job[0]} candidate {job[1]}"
                    else:
                        number, batch = pending_investigators.pop(0)
                        future = executor.submit(run_investigator, number, batch)
                        futures[future] = ("investigator", number)
                        active_labels[future] = f"Investigator batch {number}/{len(batches)}"
                execution["in_flight_requests"] = len(futures)
                execution["current_units"] = list(active_labels.values())[:8]
                has_active_verifier = any(label.startswith("Verifier") for label in active_labels.values())
                execution["phase"] = "VERIFYING" if pending_verifiers or has_active_verifier else "REVIEWING"
                self.repository.update_review(review_id, status="RUNNING", progress=progress, execution_progress=execution)
                if not futures:
                    break
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    active_labels.pop(future, None)
                    role, identity = futures.pop(future)
                    if role == "investigator":
                        result = future.result()
                        if result[3] is not None and self._is_overload_error(result[3]) and active_limit != 1:
                            active_limit = 1
                            execution["parallel_request_limit"] = 1
                            progress.append(self._t("review.overload_limit"))
                        record_investigator_result(result)
                        if result[2] is not None:
                            prepare_verifier_batch(result[0])
                    else:
                        job, result, error, events = future.result()
                        for event in events:
                            self.repository.create_review_diagnostic(event)
                        verifier_results[(job[0], job[1])] = (result, error)
                        if error is not None and self._is_overload_error(error) and active_limit != 1:
                            active_limit = 1
                            execution["parallel_request_limit"] = 1
                            progress.append(self._t("review.overload_limit"))

        # FS-FIX-015 REQ-2: if the review became terminal while requests were in
        # flight, stop here rather than writing RUNNING again.
        if self._is_review_terminal(review_id):
            raise _ReviewTerminalAbort(self.repository.review_status(review_id) or "TERMINAL")
        execution["in_flight_requests"] = 0
        execution["current_units"] = []
        execution["phase"] = "VERIFYING"
        self.repository.update_review(review_id, status="RUNNING", progress=progress, execution_progress=execution)

        execution["in_flight_requests"] = 0
        execution["current_units"] = []
        execution["phase"] = "FINALIZING"
        self.repository.update_review(review_id, status="RUNNING", progress=progress, execution_progress=execution)
        for batch_number in sorted(batch_states):
            batch, candidates, _ = investigator_results[batch_number]
            batch_verifications: list[VerifierResult] = []
            batch_complete = True
            for state in batch_states[batch_number]:
                candidate_number = state["candidate_number"]
                candidate = state["candidate"]
                verification = state["verification"]
                if verification is None:
                    verification, error = verifier_results.get((batch_number, candidate_number), (None, RuntimeError("verifier result unavailable")))
                else:
                    error = None
                if error is not None or verification is None:
                    batch_complete = False
                    failed_verification_units.add(batch_number)
                    cacheable_batches[batch_number] = False
                    safe_reason = _safe_review_error(error) if error else "verifier result unavailable"
                    progress.append(self._t("review.verifier_failed", number=batch_number, candidate=candidate_number, reason=safe_reason))
                    continue
                batch_verifications.append(verification)
                if verification.verdict != "SURVIVES":
                    progress.append(self._t("review.verifier_rejected", candidate=candidate_number, number=batch_number, title=candidate.title))
                    continue
                finding = FindingRead.model_validate({
                    "id": self._next_finding_id(review.project_id), "project_id": review.project_id, "review_id": review_id,
                    **candidate.model_dump(mode="json"),
                    # REQ-2: persist a normalized title so references are stable.
                    "title": self._normalize_title(candidate.title),
                    "verification": FindingVerification(status="PASSED", notes=verification.notes).model_dump(mode="json"),
                    "assumptions": verification.remaining_assumptions or [item.model_dump(mode="json") for item in candidate.assumptions],
                    "topology_path": self._finding_topology(review.project_id, candidate),
                    "lifetime_evidence": self._finding_lifetime(review.project_id, candidate),
                    "decision": FindingDecision.UNREVIEWED, "decision_reason": None, "resolution": FindingResolution.OPEN, "resolved_at": None, "created_at": datetime.now(UTC),
                })
                payload = finding.model_dump(mode="json", exclude={"id", "project_id", "review_id", "decision", "decision_reason", "resolution", "resolved_at", "remediation", "created_at"})
                self.repository.create_finding({"id": finding.id, "project_id": review.project_id, "review_id": review_id, "payload": payload, "decision": finding.decision, "decision_reason": None, "resolution": finding.resolution, "resolved_at": None, "created_at": now()})
                persisted += 1
                progress.append(self._t("review.created_finding", count=persisted, title=candidate.title))
            if batch_complete:
                terminal_units.add(batch_number)
            if cacheable_batches.get(batch_number) and batch_complete and len(batch_verifications) == len(candidates.findings):
                envelope = ReviewCacheEnvelope(
                    schema_version=1,
                    kind="VALIDATED_FINDINGS" if candidates.findings else "VALIDATED_EMPTY",
                    unit_id=batch.unit_id,
                    segments=batch.segments,
                    investigator=candidates,
                    verifications=batch_verifications,
                )
                self.repository.set_review_unit_cache(review.project_id, unit_keys[batch_number], envelope.model_dump(mode="json"))
            persist_execution()
        if not completed_investigator_batches:
            raise RuntimeError("AI review could not obtain a usable Investigator result from any source batch")
        for batch_number in range(1, len(batches) + 1):
            if batch_number in failed_investigator_units:
                current_unit_states[str(batch_number)] = ReviewUnitState(state="UNAVAILABLE", investigator_validated=False)
            elif batch_number in failed_verification_units:
                current_unit_states[str(batch_number)] = ReviewUnitState(state="UNAVAILABLE", investigator_validated=True)
            elif batch_number in terminal_units:
                current_unit_states[str(batch_number)] = ReviewUnitState(state="COMPLETED", investigator_validated=True)
            elif batch_number in retry_target_batches:
                current_unit_states[str(batch_number)] = ReviewUnitState(state="UNAVAILABLE", investigator_validated=False)
        unavailable_units = sum(state.state == "UNAVAILABLE" for state in current_unit_states.values())
        review_status = "PARTIAL" if unavailable_units else "COMPLETED"
        progress.append(
            self._t("review.coverage", done=completed_investigator_batches, total=len(batches))
        )

        progress.append(self._t("review.verification_complete", count=persisted))
        if unavailable_units:
            progress.append(
                self._t("review.partial", count=unavailable_units, suffix="es" if unavailable_units != 1 else "")
            )
        if not persisted:
            progress.append(self._t("review.no_survivors"))
        # The review result is durable before learning starts; intelligence
        # processing is best-effort and can never alter this outcome.
        progress.append(self._t("review.learning_queued"))
        self.repository.update_review(
            review_id,
            status=review_status,
            progress=progress,
            total_batches=len(batches),
            validated_batches=completed_investigator_batches,
            unavailable_batches=unavailable_units,
            completed_at=now(),
            unit_states={key: value.model_dump() for key, value in current_unit_states.items()},
            execution_progress={**execution, "phase": "PARTIAL" if unavailable_units else "COMPLETED", "completed_units": min(len(batches), len(terminal_units) + unavailable_units), "unavailable_units": unavailable_units, "in_flight_requests": 0, "current_units": []},
        )
        self._run_learning(
            review_id,
            progress,
            review_status=review_status,
            total_batches=len(batches),
            validated_batches=completed_investigator_batches,
            unavailable_batches=unavailable_units,
        )
        self._sync_knowledge_vault(review.project_id, progress)
        self.repository.update_review(
            review_id,
            status=review_status,
            progress=progress,
            total_batches=len(batches),
            validated_batches=completed_investigator_batches,
            unavailable_batches=unavailable_units,
        )


    def retry(self, review_id: str) -> ReviewRead:
        """Resume review units that did not reach a complete validated outcome."""
        review = self.get(review_id)
        if review.status not in {"FAILED", "PARTIAL", "INTERRUPTED"}:
            raise self._error(status.HTTP_409_CONFLICT, "error.resume_not_allowed")
        if review.unavailable_batches <= 0:
            raise self._error(status.HTTP_409_CONFLICT, "error.no_unavailable_units")
        if not review.source_snapshot_hash:
            raise self._error(status.HTTP_409_CONFLICT, "error.no_snapshot")
        current_snapshot_hash = self.projects.current_source_snapshot_hash(review.project_id)
        if current_snapshot_hash != review.source_snapshot_hash:
            raise self._error(status.HTTP_409_CONFLICT, "review.source_changed")
        unit_states = self._review_unit_states(review)
        retry_units = [number for number, state in unit_states.items() if state.state == "UNAVAILABLE"]
        if not retry_units:
            raise self._error(status.HTTP_409_CONFLICT, "error.no_unresolved_units")
        progress = list(review.progress)
        progress.append(self._t("review.queued_retry", count=len(retry_units), s=self._plural(len(retry_units), "")))
        self.repository.update_review(
            review_id,
            status="RUNNING",
            progress=progress,
            total_batches=review.total_batches,
            validated_batches=review.validated_batches,
            unavailable_batches=0,
            execution_attempt=review.execution_attempt + 1,
            unit_states={number: state.model_dump() for number, state in unit_states.items()},
        )
        return self.get(review_id)

    def _revalidate_intelligence(self, project_id: str, progress: list[str]) -> None:
        """Source-authority check before discovery. Failures never block the review."""
        if not self.intelligence:
            return
        try:
            result = self.intelligence.revalidate_against_source(project_id)
            marked = result.get("marked") or []
            outcomes = result.get("outcomes") or {}
            if marked:
                progress.append(
                    self._t("review.revalidation_stale", count=len(marked), s=self._plural(len(marked), ""))
                )
                conflicts = [memory_id for memory_id, outcome in outcomes.items() if outcome == "CONFLICTED"]
                if conflicts:
                    progress.append(self._t("review.revalidation_conflicts", count=len(conflicts), s=self._plural(len(conflicts), "")))
            elif outcomes:
                progress.append(self._t("review.revalidation_ok"))
        except Exception as error:  # noqa: BLE001 - revalidation must never fail a review
            progress.append(self._t("review.revalidation_unavailable", detail=' '.join(str(error).split())[:200]))

    @staticmethod
    def _post_review_projection_lock_registry_lock() -> _ThreadLock:
        """One stable registry lock instance (REV-031): shared by every reviewer
        instance so per-project get-or-create stays atomic even across the
        lightweight `__new__`-constructed reviewers used in tests."""
        return _REVIEW_PROJECTION_REGISTRY_LOCK

    def _post_review_projection_lock(self, project_id: str) -> _ThreadLock:
        """Atomically get-or-create the shared per-project workflow lock (REV-046).

        The lock is resolved from the module-level ``_REVIEW_PROJECTION_LOCKS``
        map so every ``ReviewService`` instance in the process shares the *same*
        lock object for a given project: same-project work therefore serializes
        across instances (background projection task vs. live route vs. a second
        app instance), while different projects keep distinct locks and stay
        concurrent. The registry lock only guards the map lookup/insert and is
        released before any projection or indexing I/O. The instance dict is kept
        in sync for callers/tests that inspect it, but the shared map is the
        authoritative coordination point.
        """
        with self._post_review_projection_registry_lock:
            existing = _REVIEW_PROJECTION_LOCKS.get(project_id)
            if existing is None:
                existing = _ThreadLock()
                _REVIEW_PROJECTION_LOCKS[project_id] = existing
            self._post_review_projection_locks[project_id] = existing
            return existing

    #: Fixed, non-content-bearing messages for the atomic projection+index
    #: workflow (REWORK-002 / REV-034 / REV-036). None of these ever contain raw
    #: exception text, source, prompts, provider output, reasoning, or secrets.
    INDEX_ATTENTION_MESSAGE = "Knowledge index skipped: vault projection requires attention before indexing. Retry vault sync from the Knowledge Base."
    PROJECTION_FAILED_MESSAGE = "Knowledge vault projection could not complete safely; no index was attempted. Retry vault sync from the Knowledge Base."
    PROJECTION_UNAVAILABLE_MESSAGE = "Knowledge vault projection was unavailable; no index was attempted. Retry vault sync from the Knowledge Base."
    INDEX_FAILED_MESSAGE = "Knowledge index unavailable; vault projection remains available. Retry from the Knowledge Base."
    VAULT_SKIPPED_MESSAGE = "Knowledge vault is not configured for this workspace."
    NO_INDEXER_MESSAGE = "Vault projection completed; no indexer is configured for this workspace."

    def _vault_is_configured(self, project_id: str) -> bool:
        """Whether a configured vault root exists for this project (REV-036).

        Uses the knowledge-base boundary when it exposes ``project_root``. A
        custom boundary that does not implement it is treated as configured so
        the projection itself decides the outcome; the single durable write then
        reflects whatever ``sync_project`` reported.
        """
        project_root = getattr(self.knowledge_base, "project_root", None)
        if not callable(project_root):
            return True
        try:
            return project_root(project_id) is not None
        except Exception:  # noqa: BLE001 - boundary probing must never raise
            return True

    def project_and_index_vault(self, project_id: str, progress: list[str] | None = None) -> dict[str, Any] | None:
        """One atomic projection-plus-index workflow under the project lock.

        FS-KB-015 (REWORK-001/REWORK-002) and FS-KB-016 (REV-036) — both the
        automatic post-review path and the manual ``Retry knowledge index`` API
        call this single operation, so projection and incremental indexing are
        always one safe outcome acquired under the *same* per-project workflow
        lock. Same-project callers serialize; different projects stay concurrent.

        Returns a bounded, typed, non-content-bearing outcome:

        ``{"vault": "SYNCED" | "FAILED" | "SKIPPED" | "UNAVAILABLE", "index":
        "SYNCED" | "INDEX_FAILED" | "SKIPPED" | "NOT_RUN", "status": "SYNCED" |
        "FAILED" | "INDEX_FAILED" | "SKIPPED", "persisted": bool, "message":
        str}``

        or ``None`` when no vault boundary exists for this reviewer.

        ``status`` is the durable state this workflow *intended* to persist and
        ``persisted`` reports whether the final durable write actually landed
        (REV-038). When ``persisted`` is ``False`` the caller must use this typed
        outcome — never a stale pre-operation ``knowledge_sync_state`` row — so a
        persistence failure can never be reported as a successful ``SYNCED``.

        **Single durable state (REV-036).** The projection is asked to suppress
        its own ``knowledge_sync_state`` write (``persist_state=False``) and this
        method persists **exactly one** final row while it still holds the project
        lock: index success → ``SYNCED``, index failure/unavailable-with-failure
        → ``INDEX_FAILED``, no indexer → ``SKIPPED``, hard projection error or a
        raised projection → ``FAILED``, and a not-configured vault → ``SKIPPED``.
        A clean projection can therefore never briefly persist ``SYNCED`` before
        indexing succeeds, and every hard projection error persists a safe
        ``FAILED`` state instead of inheriting a stale row.

        State rules (REWORK-002 / REV-036):
        - ``report.errors`` is a hard projection error → **zero** indexer calls,
          one ``FAILED`` row, and a fixed safe message;
        - ``report.errors`` empty (including warning-only legacy migration) →
          indexing may run; success = ``SYNCED``, failure = ``INDEX_FAILED``,
          unavailable = ``SKIPPED``.

        No raw exception text, source, prompt, provider output, reasoning, keys,
        or credentials can reach progress or the durable sync-state row.
        """
        if not self.knowledge_base:
            return None
        lines = progress if progress is not None else []
        with self._post_review_projection_lock(project_id):
            if not self._vault_is_configured(project_id):
                # No configured vault: one SKIPPED row, no index attempt.
                persisted = self._write_sync_state(project_id, "SKIPPED", self.VAULT_SKIPPED_MESSAGE)
                lines.append(self.VAULT_SKIPPED_MESSAGE)
                return {"vault": "SKIPPED", "index": "NOT_RUN", "status": "SKIPPED", "persisted": persisted, "message": self.VAULT_SKIPPED_MESSAGE}
            try:
                report = self.knowledge_base.sync_project(project_id, persist_state=False)
            except Exception:  # noqa: BLE001 - vault projection must never alter the review
                persisted = self._write_sync_state(project_id, "FAILED", self.PROJECTION_UNAVAILABLE_MESSAGE)
                lines.append(self.PROJECTION_UNAVAILABLE_MESSAGE)
                return {"vault": "UNAVAILABLE", "index": "NOT_RUN", "status": "FAILED", "persisted": persisted, "message": self.PROJECTION_UNAVAILABLE_MESSAGE}
            if report.errors:
                # Hard projection error: never index a partial/stale corpus. The
                # single final durable state is FAILED.
                persisted = self._write_sync_state(project_id, "FAILED", self.PROJECTION_FAILED_MESSAGE)
                lines.append(
                    self._t("review.projection_errors", count=len(report.errors), s=self._plural(len(report.errors), ""))
                )
                lines.append(self._t("review.index_attention"))
                return {"vault": "FAILED", "index": "NOT_RUN", "status": "FAILED", "persisted": persisted, "message": self._t("review.index_attention")}
            written = report.updated + report.regenerated + report.migrated
            lines.append(
                self._t("review.projection_written", written=written, s=self._plural(written, ""))
                + (self._t("review.projection_legacy_part", count=report.legacy_left_in_place, s=self._plural(report.legacy_left_in_place, "")) if report.legacy_left_in_place else "")
                + self._t("review.projection_unchanged_part", count=report.unchanged)
            )
            index_outcome = self._index_knowledge_after_projection(project_id)
            self._append_index_progress(lines, index_outcome)
            # Exactly one final durable write for the whole workflow (REV-036).
            if index_outcome is None:
                persisted = self._write_sync_state(project_id, "SKIPPED", None, report)
                return {"vault": "SYNCED", "index": "NOT_RUN", "status": "SKIPPED", "persisted": persisted, "message": self._t("review.no_indexer")}
            if index_outcome["status"] == "SUCCESS":
                persisted = self._write_sync_state(project_id, "SYNCED", None, report)
                return {"vault": "SYNCED", "index": "SYNCED", "status": "SYNCED", "persisted": persisted, "message": self._t("review.index_updated")}
            if index_outcome["status"] == "SKIPPED":
                persisted = self._write_sync_state(project_id, "SKIPPED", self._t("review.index_skip_no_indexer"), report)
                return {"vault": "SYNCED", "index": "SKIPPED", "status": "SKIPPED", "persisted": persisted, "message": self._t("review.index_skip_no_indexer")}
            persisted = self._write_sync_state(project_id, "INDEX_FAILED", self._t("review.index_failed"), report)
            return {"vault": "SYNCED", "index": "INDEX_FAILED", "status": "INDEX_FAILED", "persisted": persisted, "message": self._t("review.index_failed")}

    def _write_sync_state(self, project_id: str, status: str, summary: str | None, report: Any = None) -> bool:
        """Persist the single, bounded, safe durable sync-state row (REV-036).

        Called exactly once per ``project_and_index_vault`` run while the project
        lock is held. When a projection ``report`` is available and the knowledge
        base exposes the durable-state boundary, bounded counts are recorded
        through ``KnowledgeBaseService.record_sync_state``; otherwise a minimal
        counts dict is written directly through the memory repository that owns
        ``knowledge_sync_state``. Only fixed safe messages and bounded counts are
        stored — never raw exception text, source, prompts, or provider output.

        Returns ``True`` only when the final durable write actually landed, and
        ``False`` when no durable-state boundary exists or the write raised
        (REV-038). ``project_and_index_vault`` propagates this so the retry route
        can use the typed workflow outcome instead of a stale persisted row when
        persistence is unavailable.
        """
        counts = {"documents": self._knowledge_document_count(project_id)}
        try:
            recorder = getattr(self.knowledge_base, "record_sync_state", None)
            report_repository = getattr(self.knowledge_base, "repository", None)
            if report is not None and callable(recorder) and report_repository is not None:
                # The recorder reports whether the durable upsert landed
                # (REV-038): a swallowed write failure must not look persisted.
                return bool(recorder(project_id, report, status, summary))
        except Exception:  # noqa: BLE001 - durable state bookkeeping must never alter the review
            pass
        memory_repository = getattr(getattr(self, "memories", None), "repository", None)
        if memory_repository is None:
            return False  # no durable-state boundary available in this construction
        try:
            memory_repository.upsert_knowledge_sync_state({
                "project_id": project_id,
                "status": status,
                "counts": counts,
                "error_summary": summary,
                "updated_at": datetime.now(UTC).isoformat(),
            })
            return True
        except Exception:  # noqa: BLE001 - durable state bookkeeping must never alter the review
            return False

    def _knowledge_document_count(self, project_id: str) -> int:
        """Bounded document count from whichever repository owns knowledge docs."""
        repository = getattr(self.knowledge_base, "repository", None)
        if repository is None:
            repository = getattr(getattr(self, "memories", None), "repository", None)
        if repository is None:
            return 0
        try:
            return len(repository.list_knowledge_documents(project_id))
        except Exception:  # noqa: BLE001 - counts are best-effort
            return 0

    def _sync_knowledge_vault(self, project_id: str, progress: list[str]) -> None:
        """Best-effort projection + index after a review is durable (REV-029/031/032).

        Thin wrapper over :meth:`project_and_index_vault` so automatic and manual
        paths share exactly one workflow. Any failure remains non-fatal and never
        alters the review's terminal status.
        """
        if not self.knowledge_base:
            return
        self.project_and_index_vault(project_id, progress)

    def project_vault_only(self, project_id: str) -> None:
        """Best-effort projection under the *shared* project workflow lock (REV-046).

        Used by callers that only need a Markdown projection (for example the
        durable-learning background projection). Routing through the module-level
        per-project workflow lock means such a background projection can never
        interleave its file/row writes with the automatic or manual
        projection+index workflow for the same project, which is what previously
        produced a spurious "external edit was not imported" hard error. Different
        projects keep distinct locks and stay concurrent; no global lock is added.
        Any failure remains non-fatal and never alters the API flow.
        """
        if not self.knowledge_base:
            return
        with self._post_review_projection_lock(project_id):
            try:
                self.knowledge_base.sync_project(project_id)
            except Exception:  # noqa: BLE001 - the vault is a projection; never break API flow
                pass

    def _append_index_progress(self, progress: list[str], index_outcome: dict[str, Any] | None) -> None:
        """Append the bounded, safe index outcome to review progress (REV-032)."""
        if index_outcome is None:
            return
        if index_outcome["status"] == "SUCCESS":
            report = index_outcome["report"]
            progress.append(
                self._t(
                    "review.index_success",
                    documents=report.documents_indexed, s=self._plural(report.documents_indexed, ""),
                    chunks=report.chunks_written,
                )
            )
        elif index_outcome["status"] == "SKIPPED":
            progress.append(self._t("review.index_skipped"))
        else:
            progress.append(self._t("review.index_unavailable"))

    def _index_knowledge_after_projection(self, project_id: str) -> dict[str, Any] | None:
        """Bounded best-effort incremental RAG indexing with a safe outcome (REV-032).

        Returns ``None`` when no indexing boundary exists at all, or a
        structured outcome dict with a fixed ``status`` of ``SUCCESS`` /
        ``SKIPPED`` / ``FAILED`` plus the report when one was produced. Raw
        exception text is intentionally discarded: failures are represented only
        by the fixed status and safe message, so no exception/source/prompt
        content can reach progress or durable state. Indexing must never
        propagate into review completion.
        """
        if self.post_review_projection is not None:
            try:
                outcome = self.post_review_projection(project_id)
                if isinstance(outcome, dict) and "index" in outcome:
                    index_value = outcome["index"]
                    if index_value is None:
                        return {"status": "SKIPPED", "report": None}
                    return {"status": "SUCCESS", "report": index_value}
            except Exception:  # noqa: BLE001 - indexed callback failures stay best-effort
                return {"status": "FAILED", "report": None}
            return None
        if self.knowledge_index is None:
            return None
        try:
            return {"status": "SUCCESS", "report": self.knowledge_index.index_project(project_id)}
        except Exception:  # noqa: BLE001 - indexing must never alter the review
            return {"status": "FAILED", "report": None}

    def _run_learning(
        self,
        review_id: str,
        progress: list[str],
        *,
        review_status: str,
        total_batches: int,
        validated_batches: int,
        unavailable_batches: int,
    ) -> None:
        """Best-effort learning after the review is durably COMPLETED."""
        if not self.intelligence:
            return
        try:
            summary = self.intelligence.run_review_learning(review_id)
            learning_status = summary.get("status", "FAILED")
            if learning_status == "COMPLETED":
                counts = summary.get("counts") or {}
                changes = ", ".join(f"{value} {self._t(f'count.{key}')}" for key, value in counts.items() if value)
                progress.append(self._t("review.learning_completed", changes=changes or self._t("count.none")))
            elif learning_status == "SKIPPED":
                progress.append(self._t("review.learning_skipped", reason=summary.get('error') or self._t('count.none')))
            else:
                progress.append(self._t("review.learning_failed"))
            self.repository.update_review(
                review_id,
                status=review_status,
                progress=progress,
                total_batches=total_batches,
                validated_batches=validated_batches,
                unavailable_batches=unavailable_batches,
            )
        except Exception as error:  # noqa: BLE001 - learning must never alter the review
            progress.append(self._t("review.learning_failed_detail", detail=' '.join(str(error).split())[:160]))
            try:
                self.repository.update_review(
                    review_id,
                    status=review_status,
                    progress=progress,
                    total_batches=total_batches,
                    validated_batches=validated_batches,
                    unavailable_batches=unavailable_batches,
                )
            except Exception:  # noqa: BLE001 - nothing more can be done safely
                pass

    def _rereview_candidates(self, project_id: str) -> list[FindingRead]:
        return [
            finding
            for finding in self.findings(project_id)
            if finding.classification is FindingClassification.CONFIRMED_BUG
            and finding.decision is FindingDecision.ACCEPTED
            and finding.resolution is FindingResolution.OPEN
        ][:self.MAX_REREVIEW_FINDINGS]

    def _rereview_open_confirmed_findings(
        self,
        review_id: str,
        project_id: str,
        verifier: AIProvider,
        raw_files: list[dict[str, str]],
        source_refreshed: bool,
        changed_files: list[str],
        progress: list[str],
    ) -> None:
        findings = self._rereview_candidates(project_id)
        if not findings:
            progress.append(self._t("review.recheck_none"))
            self.repository.update_review(review_id, status="RUNNING", progress=progress)
            return

        progress.append(self._t("review.recheck_started", count=len(findings), s=self._plural(len(findings), "")))
        self.repository.update_review(review_id, status="RUNNING", progress=progress)
        timestamp = now()
        for index, finding in enumerate(findings, start=1):
            progress.append(self._t("review.recheck_finding", index=index, count=len(findings), title=finding.title))
            self.repository.update_review(review_id, status="RUNNING", progress=progress)
            try:
                operation = f"review:{review_id}:fix-verifier:finding-{finding.id}"
                result = structured_response(
                    verifier,
                    FixVerificationResult,
                    with_language(FIX_VERIFIER_SYSTEM, self._locale()),
                    f"Required JSON Schema:\n{json.dumps(FixVerificationResult.model_json_schema(), ensure_ascii=False)}\n\n"
                    f"Previously accepted confirmed finding:\n{json.dumps(finding.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                    f"Current source relevant to this finding:\n{self._fix_context(finding, raw_files)}",
                    operation=operation,
                    on_event=self._diagnostic_sink(review_id, role="fix-verifier", batch_number=index, total_batches=len(findings), file_count=1),
                )
            except RuntimeError as error:
                progress.append(self._t("review.recheck_error", title=finding.title, detail=error))
                self.repository.update_review(review_id, status="RUNNING", progress=progress)
                continue

            remediation_status = {
                "FIXED": FindingRemediationStatus.VERIFIED_FIXED,
                "STILL_PRESENT": FindingRemediationStatus.STILL_PRESENT,
                "INCONCLUSIVE": FindingRemediationStatus.INCONCLUSIVE,
            }[result.verdict]
            resolution = FindingResolution.SOLVED if remediation_status is FindingRemediationStatus.VERIFIED_FIXED else FindingResolution.OPEN
            remediation = {
                "status": remediation_status,
                "notes": f"AI re-review: {result.notes}",
                "verified_at": timestamp,
                "source_refreshed": source_refreshed,
                "changed_files": changed_files[:80],
            }
            self.repository.update_finding_remediation(
                finding.id,
                remediation,
                resolution,
                timestamp if resolution is FindingResolution.SOLVED else None,
                project_id,
            )
            if resolution is FindingResolution.SOLVED:
                progress.append(self._t("review.recheck_solved", index=index, count=len(findings), title=finding.title))
            else:
                progress.append(self._t("review.recheck_open", index=index, count=len(findings), title=finding.title, verdict=result.verdict))
            self.repository.update_review(review_id, status="RUNNING", progress=progress)

    @staticmethod
    def source_chars_for_budget(investigator_budget: int | str | None) -> int:
        """Deterministic per-request source envelope for the Investigator role.

        A compact structured output budget pairs with a smaller source unit so
        the bounded response can still reference its evidence; a higher budget
        grows the unit up to the existing hard cap. ``PROVIDER_DEFAULT``/``None``
        keep the documented default. Coverage is unaffected: every reviewable
        source segment is still assigned to exactly one unit; the budget only
        trades unit size against unit count.
        """
        if not isinstance(investigator_budget, int) or isinstance(investigator_budget, bool):
            return ReviewService.DEFAULT_SOURCE_CHARS_FOR_BUDGET
        capped = min(investigator_budget, ReviewService.SOURCE_CHARS_PER_BUDGET_BUCKETS[-1][0])
        for threshold, source_chars in ReviewService.SOURCE_CHARS_PER_BUDGET_BUCKETS:
            if capped <= threshold:
                return source_chars
        return ReviewService.DEFAULT_SOURCE_CHARS_FOR_BUDGET

    def _context_batches(self, project_id: str, project_name: str, focus: list[str], raw_files: list[dict[str, str]], context_limit_chars: int | None = None, investigator_budget: int | str | None = None) -> list[ReviewContextBatch]:
        focus_terms: set[str] = set()
        for selected_focus in focus:
            normalized_focus = selected_focus.casefold().replace("_", "").replace("-", "")
            focus_terms.update(self.FOCUS_SIGNAL_TERMS.get(normalized_focus, {normalized_focus}))
        signal_terms = {
            "xsemaphore", "xqueue", "isr", "interrupt", "watchdog", "mqtt", "ota", "nvs",
            "mutex", "semaphore", "task", "timer", "esp_err", "malloc", "free", "memcpy", "strncpy",
            "wifi", "socket", "tls", "http", "state", "event", "config", "init",
        }
        # Symbol metadata is part of planning, not merely prompt decoration.
        # Use it to prioritize files whose indexed functions/classes actually
        # match the requested review focus (for example MQTT task symbols for a
        # networking review), while still retaining a deterministic path tie
        # breaker.  This keeps the first units useful without dropping any file.
        all_symbols = self.projects.symbols(project_id, None)
        symbols_by_file: dict[str, set[str]] = {}
        for symbol in all_symbols:
            symbols_by_file.setdefault(symbol.file, set()).add(symbol.name)

        def rank(file: dict[str, str]) -> tuple[int, int, int, int, int, str]:
            pure_path = PurePosixPath(file["path"])
            path = file["path"].lower().replace("_", "")
            content = file["content"].casefold()
            core_metadata = pure_path.name in {"platformio.ini", "sdkconfig", "sdkconfig.defaults", "idf_component.yml"}
            firmware_source = pure_path.suffix.casefold() in {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
            path_signals = sum(term in path for term in focus_terms)
            local_signals = sum(content.count(term) for term in signal_terms)
            file_symbols = symbols_by_file.get(file["path"], set())
            symbol_focus_signals = sum(
                1 for symbol_name in file_symbols
                if any(term in symbol_name.casefold().replace("_", "") for term in focus_terms)
            )
            return (-int(core_metadata), -int(firmware_source), -symbol_focus_signals, -path_signals, -min(local_signals, 50), file["path"])
        selected = [file for file in sorted(raw_files, key=rank) if self._is_reviewable_source(file)]
        context_limit = context_limit_chars if context_limit_chars in REVIEW_CONTEXT_CHAR_OPTIONS else self._review_context_chars()
        symbols = all_symbols[:self.MAX_SYMBOLS]
        symbol_text = ", ".join(f"{item.kind}:{item.name}@{item.file}:{item.line}" for item in symbols) or "none"
        yaml = self.repository.latest_yaml(project_id)
        yaml_text = yaml["content"][:self.MAX_YAML_CHARS] if yaml else "none"
        topology = self.projects.topology(project_id)
        topology_symbols = {item["id"]: item for item in topology["symbols"]}
        selected_normalized = set(batch_file.casefold() for batch_file in (file["path"] for file in raw_files))
        lifetime = self.projects.lifetime_analysis(project_id) if any(focus.casefold().replace("_", "").replace("-", "") == "memory" for focus in focus) else None

        def topology_block(batch_files: list[str]) -> str:
            allowed = set(batch_files)
            relation_lines: list[str] = []
            for relation in topology["relations"]:
                source = topology_symbols.get(relation.get("source_symbol_id"))
                target = topology_symbols.get(relation.get("target_symbol_id"))
                if relation.get("file") not in allowed and not (source and source.get("file") in allowed):
                    continue
                left = source.get("name") if source else relation.get("source_symbol_id") or "unknown-source"
                right = target.get("name") if target else relation.get("target_name") or "unknown-target"
                relation_lines.append(f"{left} -[{relation['relation_kind']}/{relation['relation_state']}]-> {right} @ {relation['file']}:{relation['line']}")
                if len(relation_lines) >= 48:
                    break
            if lifetime is not None:
                for seed in lifetime.seeds:
                    if seed.file in allowed:
                        relation_lines.append(f"LIFETIME_CANDIDATE {seed.symbol} -> allocation {seed.variable or '?'} @ {seed.file}:{seed.allocation_line} -> error/return exit @ {seed.exit_line}; ownership={seed.ownership_state}; evidence={seed.evidence_hash}")
                for fact in lifetime.facts:
                    if fact.file in allowed and fact.ownership_state != "UNBALANCED_EXIT":
                        relation_lines.append(f"LIFETIME_FACT {fact.symbol} {fact.variable or '?'}: {fact.ownership_state} @ {fact.file}:{fact.allocation_line}; releases={','.join(map(str, fact.release_lines)) or 'none'}; exits={','.join(map(str, fact.exit_lines)) or 'none'}")
            return "\n".join(relation_lines[:64]) or "none"

        # Ranked, state-labelled intelligence retrieval replaces the former
        # global ACTIVE-memory injection: each batch receives only records
        # relevant to its files/symbols, and terminal or provisional states
        # are never presented as current authority.
        def batch_shared(batch_files: list[str]) -> str:
            batch_symbols = sorted(set().union(*(symbols_by_file.get(path, set()) for path in batch_files))) if batch_files else []
            if self.intelligence:
                intelligence_text = self.intelligence.review_memory_context(project_id, batch_symbols, batch_files, cap=self.MAX_MEMORIES)
                intelligence_block = f"Project Intelligence (state-labelled, relevant only):\n{intelligence_text}"
            else:
                memories = self.memories.context(project_id, None, None).memories[:self.MAX_MEMORIES]
                memory_text = "\n".join(f"- {memory.statement[:240]}" for memory in memories) or "none"
                intelligence_block = f"Active Engineering Memory:\n{memory_text}"
            topology_text = topology_block(batch_files)
            shared_builder = ""
            if self.context_builder:
                built = self.context_builder.build(project_id, " ".join(focus), selected_files=batch_files, symbols=batch_symbols, max_chars=4_000)
                shared_builder = f"\nShared Context Builder fingerprint: {built['fingerprint']}\n<retrieved_context untrusted_data=\"true\">\n{built['text'][:4_000]}\n</retrieved_context>\n"
            return f"Project: {project_name}\n\nIndexed symbols:\n{symbol_text}\n\nSource-backed topology (OBSERVED edges are direct parser evidence; INFERRED edges are unresolved hints):\n<topology untrusted_data=\"true\">\n{topology_text}\n</topology>\n\n{shared_builder}\n{intelligence_block}\n\nfirmware.ai.yaml (if present):\n{yaml_text}\n\nRelevant repository data:\n"

        shared = batch_shared([])

        # Budget-aware source envelope: the effective Investigator output
        # budget bounds how much source one request may carry, so a compact
        # response budget is paired with a smaller source unit instead of the
        # largest envelope merely because the global context setting is high.
        # The result never exceeds the context-derived ceiling below.
        budget_source_chars = self.source_chars_for_budget(investigator_budget)
        context_source_ceiling = max(
            320,
            context_limit
            - len(shared)
            - self.INVESTIGATOR_REQUEST_OVERHEAD_CHARS
            - self.MAX_CONTEXT_SEGMENTS_PER_BATCH * self.SOURCE_TAG_OVERHEAD_CHARS,
        )
        source_budget = min(
            self.MAX_SOURCE_CHARS_PER_BATCH,
            context_source_ceiling,
            max(self.MIN_SOURCE_CHARS_PER_BATCH, budget_source_chars) if context_source_ceiling >= self.MIN_SOURCE_CHARS_PER_BATCH else context_source_ceiling,
        )

        def bounded_context(batch_files: list[str], batch_excerpts: list[str]) -> str:
            """Keep shared metadata bounded while preserving every source excerpt."""
            source_text = "\n\n".join(batch_excerpts)
            shared_text = batch_shared(batch_files)
            shared_budget = max(0, context_limit - len(source_text))
            return shared_text[:shared_budget] + source_text

        batches: list[ReviewContextBatch] = []
        excerpts: list[str] = []
        files: list[str] = []
        segments: list[ReviewUnitSegment] = []
        remaining = source_budget

        def flush_batch() -> None:
            nonlocal excerpts, files, segments, remaining
            if not excerpts:
                return
            ordered_segments = list(segments)
            unit_material = json.dumps([item.model_dump(mode="json") for item in ordered_segments], sort_keys=True, separators=(",", ":"))
            unit_id = "UNIT-" + hashlib.sha256(unit_material.encode("utf-8")).hexdigest()[:16].upper()
            label = ordered_segments[0].file if len(ordered_segments) == 1 else f"{ordered_segments[0].file} + {len(ordered_segments) - 1} related segments"
            batches.append(ReviewContextBatch(context=bounded_context(list(files), list(excerpts)), files=list(files), segments=ordered_segments, unit_id=unit_id, label=label))
            excerpts, files, segments, remaining = [], [], [], source_budget

        # Explicit coverage map: every source segment gets one and only one
        # review unit.  The map is intentionally local and content-hash based,
        # so it can also be used to audit full-project coverage in tests without
        # persisting source text.
        coverage_map: dict[tuple[str, int, int, str], str] = {}
        for file in selected:
            for start_line, end_line, excerpt in self._source_segments(file):
                segment_hash = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
                coverage_key = (file["path"], start_line, end_line, segment_hash)
                if coverage_key in coverage_map:
                    continue
                rendered = f"<source path=\"{file['path']}\" lines=\"{start_line}-{end_line}\">\n{excerpt}\n</source>"
                if excerpts and (len(excerpts) >= self.MAX_CONTEXT_SEGMENTS_PER_BATCH or len(rendered) > remaining):
                    flush_batch()
                excerpts.append(rendered)
                if file["path"] not in files:
                    files.append(file["path"])
                segments.append(ReviewUnitSegment(file=file["path"], line_start=start_line, line_end=end_line, content_hash=segment_hash))
                # The unit id is assigned when the batch is flushed. Keep a
                # deterministic pending marker here, then replace it below.
                coverage_map[coverage_key] = "pending"
                remaining -= len(rendered)
                if len(excerpts) >= self.MAX_CONTEXT_SEGMENTS_PER_BATCH or remaining < 320:
                    flush_batch()
        flush_batch()
        # Replace pending markers with the generated unit id and assert exact
        # one-to-one coverage. This is an internal invariant, not user input.
        for batch in batches:
            for segment in batch.segments:
                key = (segment.file, segment.line_start, segment.line_end, segment.content_hash)
                coverage_map[key] = batch.unit_id
        if len(coverage_map) != sum(len(batch.segments) for batch in batches):
            raise RuntimeError("review coverage map contains duplicate source segments")
        return batches

    def _source_segments(self, file: dict[str, str]) -> list[tuple[int, int, str]]:
        """Split source on line boundaries so every reviewable byte is examined."""
        lines = file["content"].splitlines(keepends=True)
        if not lines:
            return []
        segments: list[tuple[int, int, str]] = []
        current: list[str] = []
        current_size = 0
        start_line = 1
        for line_number, line in enumerate(lines, start=1):
            if current and current_size + len(line) > self.MAX_SOURCE_CHARS_PER_SEGMENT:
                segments.append((start_line, line_number - 1, "".join(current)))
                current, current_size, start_line = [], 0, line_number
            if len(line) > self.MAX_SOURCE_CHARS_PER_SEGMENT:
                if current:
                    segments.append((start_line, line_number - 1, "".join(current)))
                    current, current_size = [], 0
                for offset in range(0, len(line), self.MAX_SOURCE_CHARS_PER_SEGMENT):
                    segments.append((line_number, line_number, line[offset:offset + self.MAX_SOURCE_CHARS_PER_SEGMENT]))
                start_line = line_number + 1
                continue
            current.append(line)
            current_size += len(line)
        if current:
            segments.append((start_line, len(lines), "".join(current)))
        return segments

    @staticmethod
    def _is_reviewable_source(file: dict[str, str]) -> bool:
        path = file["path"].casefold()
        content = file["content"]
        is_font_path = any("font" in part for part in PurePosixPath(path).parts)
        is_large_hex_blob = len(content) > 64_000 and content.count("0x") > 1_000
        return not (is_font_path and len(content) > 32_000) and not is_large_hex_blob

    @staticmethod
    def _candidate_has_real_evidence(candidate: FindingCandidate, raw_files: list[dict[str, str]], segments: list[ReviewUnitSegment] | None = None) -> bool:
        lines_by_path = {file["path"]: len(file["content"].splitlines()) for file in raw_files}
        location_lines = lines_by_path.get(candidate.location.file)
        if not location_lines or candidate.location.line_start > location_lines or candidate.location.line_end > location_lines:
            return False
        if not all(item.file in lines_by_path and item.line <= lines_by_path[item.file] for item in candidate.evidence):
            return False
        if segments is None:
            return True
        ranges: dict[str, list[tuple[int, int]]] = {}
        for segment in segments:
            ranges.setdefault(segment.file, []).append((segment.line_start, segment.line_end))

        def covered(path: str, start: int, end: int) -> bool:
            return any(path == segment_file and start >= line_start and end <= line_end for segment_file, values in ranges.items() for line_start, line_end in values)

        return covered(candidate.location.file, candidate.location.line_start, candidate.location.line_end) and all(covered(item.file, item.line, item.line) for item in candidate.evidence)

    def get(self, review_id: str) -> ReviewRead:
        record = self.repository.get_review(review_id)
        if not record:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.review_not_found")
        if record["status"] == "RUNNING":
            activity_value = record.get("last_activity_at") or record.get("created_at")
            try:
                activity_at = datetime.fromisoformat(activity_value) if activity_value else datetime.now(UTC)
                if activity_at.tzinfo is None:
                    activity_at = activity_at.replace(tzinfo=UTC)
                stale = (datetime.now(UTC) - activity_at).total_seconds() > self.RUNNING_STALE_SECONDS
            except (TypeError, ValueError):
                stale = False
            if stale:
                progress = list(record.get("progress", []))
                progress.append(self._t("review.worker_stopped"))
                total_batches = int(record.get("total_batches") or 0)
                validated_batches = int(record.get("validated_batches") or 0)
                unavailable_batches = max(
                    int(record.get("unavailable_batches") or 0),
                    total_batches - validated_batches,
                )
                self.repository.update_review(
                    review_id,
                    status="INTERRUPTED",
                    progress=progress,
                    total_batches=total_batches,
                    validated_batches=validated_batches,
                    unavailable_batches=unavailable_batches,
                    completed_at=now(),
                    error="Review worker stopped before completion",
                )
                record = self.repository.get_review(review_id) or record
        return ReviewRead.model_validate(record)

    def findings(self, project_id: str) -> list[FindingRead]:
        self.projects.get(project_id)
        return [FindingRead.model_validate(item) for item in self.repository.findings(project_id)]

    def finding(self, project_id: str, finding_id: str) -> FindingRead:
        finding = self.repository.finding(finding_id, project_id)
        if not finding or finding["project_id"] != project_id:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.finding_not_found")
        return FindingRead.model_validate(finding)

    def decide(self, project_id: str, finding_id: str, update: FindingDecisionUpdate) -> FindingRead:
        self.finding(project_id, finding_id)
        if update.decision is FindingDecision.REJECTED and not update.reason:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.rejection_reason_required")
        self.repository.update_finding_decision(finding_id, update.decision, update.reason, project_id)
        return self.finding(project_id, finding_id)

    def resolve(self, project_id: str, finding_id: str, update: FindingResolutionUpdate) -> FindingRead:
        finding = self.finding(project_id, finding_id)
        if finding.classification is not FindingClassification.CONFIRMED_BUG:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.only_confirmed_solved")
        resolved_at = now() if update.resolution is FindingResolution.SOLVED else None
        remediation = {
            "status": FindingRemediationStatus.MANUALLY_MARKED,
            "notes": "Resolution was recorded manually without a source recheck.",
            "verified_at": resolved_at,
            "source_refreshed": False,
            "changed_files": [],
        }
        self.repository.update_finding_remediation(finding_id, remediation, update.resolution, resolved_at, project_id)
        return self.finding(project_id, finding_id)

    def verify_fix(self, project_id: str, finding_id: str, directory_override: str | None = None) -> FindingRead:
        finding = self.finding(project_id, finding_id)
        if finding.classification is not FindingClassification.CONFIRMED_BUG:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.only_confirmed_verify")
        if finding.decision is not FindingDecision.ACCEPTED:
            raise self._error(status.HTTP_409_CONFLICT, "error.accept_before_fix")
        verifier = self.provider_resolver(FIX_VERIFIER_ROLE)
        if not verifier.available():
            raise self._error(status.HTTP_503_SERVICE_UNAVAILABLE, "error.fix_verify_requires_model")

        _, changed_files = self.projects.refresh_local_directory(project_id, directory_override)
        timestamp = now()
        if not changed_files:
            remediation = {
                "status": FindingRemediationStatus.INCONCLUSIVE,
                "notes": "No indexed source changes were found since the last import. Apply the fix in the local project directory, then verify again.",
                "verified_at": timestamp,
                "source_refreshed": True,
                "changed_files": [],
            }
            self.repository.update_finding_remediation(finding_id, remediation, FindingResolution.OPEN, None, project_id)
            return self.finding(project_id, finding_id)

        current_files = self.repository.raw_files(project_id)
        try:
            result = structured_response(
                verifier,
                FixVerificationResult,
                FIX_VERIFIER_SYSTEM,
                f"Required JSON Schema:\n{json.dumps(FixVerificationResult.model_json_schema(), ensure_ascii=False)}\n\n"
                f"Previously accepted finding:\n{json.dumps(finding.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                f"Files changed since the prior source snapshot:\n{json.dumps(changed_files[:80], ensure_ascii=False)}\n\n"
                f"Current source relevant to this finding:\n{self._fix_context(finding, current_files)}",
                operation=f"verify-fix:{project_id}:{finding_id}",
            )
        except RuntimeError as error:
            raise self._error(status.HTTP_502_BAD_GATEWAY, "error.fix_no_verifier_result") from error

        remediation_status = {
            "FIXED": FindingRemediationStatus.VERIFIED_FIXED,
            "STILL_PRESENT": FindingRemediationStatus.STILL_PRESENT,
            "INCONCLUSIVE": FindingRemediationStatus.INCONCLUSIVE,
        }[result.verdict]
        resolution = FindingResolution.SOLVED if remediation_status is FindingRemediationStatus.VERIFIED_FIXED else FindingResolution.OPEN
        resolved_at = timestamp if resolution is FindingResolution.SOLVED else None
        remediation = {
            "status": remediation_status,
            "notes": result.notes,
            "verified_at": timestamp,
            "source_refreshed": True,
            "changed_files": changed_files[:80],
        }
        self.repository.update_finding_remediation(finding_id, remediation, resolution, resolved_at, project_id)
        return self.finding(project_id, finding_id)

    def _fix_context(self, finding: FindingRead, raw_files: list[dict[str, str]]) -> str:
        files_by_path = {file["path"]: file["content"] for file in raw_files}
        paths = [finding.location.file, *(item.file for item in finding.evidence)]
        excerpts: list[str] = []
        remaining = self.MAX_REREVIEW_CONTEXT_CHARS
        for path in dict.fromkeys(paths):
            if remaining < 160:
                break
            content = files_by_path.get(path)
            if content is None:
                excerpts.append(f"<source path=\"{path}\" status=\"missing\" />")
            else:
                allowed = min(self.MAX_REREVIEW_CHARS_PER_FILE, remaining)
                excerpt = content[:allowed]
                if len(content) > allowed:
                    excerpt += "\n/* FirmSight excerpt truncated for AI recheck. */"
                excerpts.append(f"<source path=\"{path}\">\n{excerpt}\n</source>")
                remaining -= len(excerpt)
        return "\n\n".join(excerpts)

    def _finding_topology(self, project_id: str, candidate: FindingCandidate) -> list[dict[str, object]]:
        symbol = candidate.location.function
        if not symbol:
            return []
        path = self.projects.topology_path(project_id, symbol, depth=1, cap=24)
        result: list[dict[str, object]] = []
        for relation in path.get("relations", [])[:24]:
            result.append({"relation_kind": relation.get("relation_kind"), "relation_state": relation.get("relation_state"), "file": relation.get("file"), "line": relation.get("line"), "target_name": relation.get("target_name"), "target_symbol_id": relation.get("target_symbol_id"), "source_symbol_id": relation.get("source_symbol_id")})
        return result

    def _finding_lifetime(self, project_id: str, candidate: FindingCandidate) -> list[dict[str, object]]:
        if candidate.category.casefold() not in {"memory", "memory_lifetime", "resource_lifetime"} and "alloc" not in candidate.category.casefold() and "leak" not in candidate.title.casefold():
            return []
        analysis = self.projects.lifetime_analysis(project_id)
        return [{"symbol": fact.symbol, "variable": fact.variable, "file": fact.file, "allocation_line": fact.allocation_line, "release_lines": list(fact.release_lines), "exit_lines": list(fact.exit_lines), "ownership_state": fact.ownership_state, "evidence_hash": fact.evidence_hash} for fact in analysis.facts[:12] if fact.file == candidate.location.file]
