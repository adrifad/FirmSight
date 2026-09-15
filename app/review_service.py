from __future__ import annotations

import json
import hashlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from threading import BoundedSemaphore
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException, status

from .ai_provider import AIProvider, ProviderEvent, structured_response
from .platform_repository import PlatformRepository
from .prompts import FIX_VERIFIER_SYSTEM, FIX_VERIFIER_PROMPT_VERSION, INVESTIGATOR_SYSTEM, INVESTIGATOR_PROMPT_VERSION, VERIFIER_SYSTEM, VERIFIER_PROMPT_VERSION
from .platform_schemas import (
    FindingCandidate, FindingClassification, FindingDecision, FindingDecisionUpdate, FindingRead, FindingRemediationStatus,
    FindingResolution, FindingResolutionUpdate, FindingVerification, FixVerificationResult, InvestigatorResult, ProjectSourceType, ReviewCacheEnvelope, ReviewCreate, ReviewOutputBudgetSnapshot, ReviewRead, ReviewUnitSegment, ReviewUnitState, VerifierResult,
)
from .project_service import ProjectService, now
from .service import MemoryService
from .settings_service import DEFAULT_INVESTIGATOR_MAX_TOKENS, DEFAULT_REVIEW_CONTEXT_CHARS, DEFAULT_VERIFIER_MAX_TOKENS, FIX_VERIFIER_ROLE, PROVIDER_DEFAULT, REVIEW_CONTEXT_CHAR_OPTIONS

if TYPE_CHECKING:
    from .intelligence_service import IntelligenceService


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
    # The Settings value is the maximum complete Investigator payload budget.
    # Reserve room for the role prompt, JSON schema, scope/batch instructions,
    # and a small framing margin before packing source excerpts.
    INVESTIGATOR_REQUEST_OVERHEAD_CHARS = 4_096
    REVIEW_ENGINE_VERSION = "review-engine-v2"
    # Focus profiles are planning hints only. They prioritize relevant indexed
    # symbols and paths, but the Investigator remains the sole source of
    # findings. Terms are normalized below so `error_handling` and
    # `error-handling` behave consistently.
    FOCUS_SIGNAL_TERMS = {
        "memory": {"malloc", "calloc", "realloc", "free", "memcpy", "memmove", "buffer", "heap", "stack", "pointer"},
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

    def __init__(self, repository: PlatformRepository, projects: ProjectService, provider_resolver: Callable[[str], AIProvider], memories: MemoryService, intelligence: "IntelligenceService | None" = None, review_context_resolver: Callable[[], int] | None = None, review_parallel_resolver: Callable[[], int] | None = None) -> None:
        self.repository, self.projects, self.provider_resolver, self.memories = repository, projects, provider_resolver, memories
        self.intelligence = intelligence
        self.review_context_resolver = review_context_resolver or (lambda: DEFAULT_REVIEW_CONTEXT_CHARS)
        self.review_parallel_resolver = review_parallel_resolver or (lambda: 1)

    def _review_parallel_limit(self) -> int:
        try:
            value = int(self.review_parallel_resolver())
        except (TypeError, ValueError):
            value = 1
        return value if value in {1, 2, 3} else 1

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
            raise HTTPException(status.HTTP_409_CONFLICT, "Import a local project directory before starting a review")
        investigator = self.provider_resolver("investigator")
        verifier = self.provider_resolver("verifier")
        if not investigator.available() or not verifier.available():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI Review requires configured investigator and verifier models with a server API key")

        indexed = self.projects.index(project_id)
        timestamp = now()
        review_id = f"REV-{uuid4().hex[:10].upper()}"
        context_chars = self._review_context_chars()
        source_snapshot_hash = ProjectService.snapshot_hash(raw_files)
        batches = self._context_batches(project_id, project.name, request.focus, raw_files, context_limit_chars=context_chars)
        if not batches:
            raise HTTPException(status.HTTP_409_CONFLICT, "No reviewable firmware source files were found for the selected review scope")
        context_files = list(dict.fromkeys(path for batch in batches for path in batch.files))
        rereview_count = len(self._rereview_candidates(project_id))
        progress = [
            f"Queued {request.scope} review for focus: {', '.join(request.focus)}",
            f"Indexed {indexed.file_count} files and {indexed.symbol_count} symbols",
            f"Prepared {len(batches)} full-coverage source batches from {len(context_files)} reviewable files (up to {context_chars:,} characters per AI request)",
        ]
        if rereview_count:
            progress.append(f"Queued AI recheck for {rereview_count} accepted confirmed finding{'s' if rereview_count != 1 else ''} before new discovery")
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
            source_refreshed = False
            changed_files: list[str] = []
            if project.source_type is ProjectSourceType.LOCAL_DIRECTORY:
                try:
                    _, changed_files = self.projects.refresh_local_directory(review.project_id)
                    source_refreshed = True
                    progress.append(f"Refreshed local source before AI recheck ({len(changed_files)} changed file{'s' if len(changed_files) != 1 else ''})")
                except HTTPException as error:
                    progress.append(f"Could not refresh local source; rechecking the indexed snapshot: {error.detail}")
                self.repository.update_review(review_id, status="RUNNING", progress=progress)
            raw_files = self.repository.raw_files(review.project_id)
            current_snapshot_hash = ProjectService.snapshot_hash(raw_files)
            has_prior_investigator_work = bool(
                review.validated_batches
                or review.unavailable_batches
                or any(event.get("role") == "investigator" for event in review.diagnostics)
            )
            if has_prior_investigator_work and (
                not review.source_snapshot_hash or current_snapshot_hash != review.source_snapshot_hash
            ):
                message = "Project source changed since this review snapshot; start a new review instead of resuming it."
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
            batches = self._context_batches(review.project_id, project.name, review.focus, raw_files, context_limit_chars=review.context_chars)
            if not batches:
                raise RuntimeError("No reviewable firmware source files were available for AI context")
            if retry_target_batches is None:
                retry_target_batches = set(range(1, len(batches) + 1))
            persisted = 0
            existing_candidate_signatures: set[tuple[str, int, int, str]] = {
                (item.location.file, item.location.line_start, item.location.line_end, item.title.casefold())
                for item in self.findings(review.project_id)
            }
            # Deduplicate candidates within this run. A validated cache hit is
            # deliberately allowed to create a fresh finding for the new
            # review, even when an older review already contains the same
            # signature; engineer decisions never cross review boundaries.
            seen_candidates: set[tuple[str, int, int, str]] = set()
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
                            investigator, InvestigatorResult, INVESTIGATOR_SYSTEM,
                            f"Required JSON Schema:\n{json.dumps(InvestigatorResult.model_json_schema(), ensure_ascii=False)}\n\nReview scope: {review.scope}\nFocus: {', '.join(review.focus)}\nThis is source batch {batch_number}/{len(batches)}. Return at most 2 high-confidence candidates from this batch; return an empty findings list if none qualify.\n\n{batch.context}",
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
                    f"Reused validated review unit {number}/{len(batches)} ({len(cached_result.findings)} candidate finding{'s' if len(cached_result.findings) != 1 else ''})"
                )
            pending = [(number, batch) for number, batch in enumerate(batches, start=1) if number not in validated_batch_numbers]
            for number, batch in pending:
                progress.append(f"Investigator is analyzing source batch {number}/{len(batches)} ({len(batch.files)} files)")
            execution["reused_units"] = reused_units
            # A unit is complete only after its candidates have passed through
            # verification. Cache hits already contain both validated stages.
            terminal_units: set[int] = set(validated_batch_numbers)
            failed_verification_units: set[int] = set()
            failed_investigator_units: set[int] = set()

            def persist_execution() -> None:
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
                    progress.append(f"Investigator batch {batch_number}/{len(batches)} returned {len(candidates.findings)} candidate findings")
                    if not candidates.findings:
                        terminal_units.add(batch_number)
                        envelope = ReviewCacheEnvelope(schema_version=1, kind="VALIDATED_EMPTY", unit_id=batches[batch_number - 1].unit_id, segments=batches[batch_number - 1].segments, investigator=candidates)
                        self.repository.set_review_unit_cache(review.project_id, unit_keys[batch_number], envelope.model_dump(mode="json"))
                else:
                    skipped_investigator_batches += 1
                    failed_investigator_units.add(batch_number)
                    progress.append(f"Investigator batch {batch_number}/{len(batches)} was skipped (stopped: {_safe_review_error(error)})")
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
                    key = (candidate.location.file, candidate.location.line_start, candidate.location.line_end, candidate.title.casefold())
                    cached_verification = cached_verifications.get((batch_number, candidate_number))
                    if key in seen_candidates or (key in existing_candidate_signatures and cached_verification is None):
                        cacheable_batches[batch_number] = False
                        progress.append(f"Skipped duplicate candidate from batch {batch_number}: {candidate.title}")
                        continue
                    seen_candidates.add(key)
                    if not self._candidate_has_real_evidence(candidate, raw_files, batch.segments):
                        cacheable_batches[batch_number] = False
                        progress.append(f"Discarded batch {batch_number} candidate {candidate_number}: unverifiable file or line evidence")
                        continue
                    state = {"candidate_number": candidate_number, "candidate": candidate, "verification": cached_verification, "error": None}
                    states.append(state)
                    if cached_verification is None:
                        pending_verifiers.append((batch_number, candidate_number, batch, candidate))
                    progress.append(f"Verifier is challenging batch {batch_number} candidate {candidate_number}/{len(candidates.findings)}: {candidate.title}")
                batch_states[batch_number] = states
                if not states:
                    terminal_units.add(batch_number)

            def run_verifier(job: tuple[int, int, ReviewContextBatch, FindingCandidate]):
                batch_number, candidate_number, batch, candidate = job
                events: list[dict] = []
                try:
                    with request_slots:
                        result = structured_response(
                            verifier, VerifierResult, VERIFIER_SYSTEM,
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
                                progress.append("Provider overload detected; limiting remaining Investigator and Verifier requests to 1")
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
                                progress.append("Provider overload detected; limiting remaining Investigator and Verifier requests to 1")

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
                        progress.append(f"Verifier could not validate batch {batch_number} candidate {candidate_number}; it was not added: {safe_reason}")
                        continue
                    batch_verifications.append(verification)
                    if verification.verdict != "SURVIVES":
                        progress.append(f"Verifier did not approve candidate {candidate_number} in batch {batch_number}: {candidate.title}")
                        continue
                    finding = FindingRead.model_validate({
                        "id": f"FS-{uuid4().hex[:8].upper()}", "project_id": review.project_id, "review_id": review_id,
                        **candidate.model_dump(mode="json"), "verification": FindingVerification(status="PASSED", notes=verification.notes).model_dump(mode="json"),
                        "assumptions": verification.remaining_assumptions or [item.model_dump(mode="json") for item in candidate.assumptions],
                        "decision": FindingDecision.UNREVIEWED, "decision_reason": None, "resolution": FindingResolution.OPEN, "resolved_at": None, "created_at": datetime.now(UTC),
                    })
                    payload = finding.model_dump(mode="json", exclude={"id", "project_id", "review_id", "decision", "decision_reason", "resolution", "resolved_at", "remediation", "created_at"})
                    self.repository.create_finding({"id": finding.id, "project_id": review.project_id, "review_id": review_id, "payload": payload, "decision": finding.decision, "decision_reason": None, "resolution": finding.resolution, "resolved_at": None, "created_at": now()})
                    persisted += 1
                    progress.append(f"Created verifier-approved finding {persisted}: {candidate.title}")
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
                f"Investigator coverage: {completed_investigator_batches}/{len(batches)} batches validated"
            )

            progress.append(f"Verification complete: {persisted} evidence-backed findings created")
            if unavailable_units:
                progress.append(
                    f"Review is partial: {unavailable_units} source batch{'es' if unavailable_units != 1 else ''} unavailable because an Investigator or Verifier response was unusable"
                )
            if not persisted:
                progress.append("No candidate survived verification; no findings were created")
            # The review result is durable before learning starts; intelligence
            # processing is best-effort and can never alter this outcome.
            progress.append("Project Intelligence learning queued")
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
        except RuntimeError as error:
            progress.append("AI review failed before a validated result was available")
            self.repository.update_review(
                review_id,
                status="FAILED",
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
            progress.append("AI review stopped because an unexpected internal error occurred")
            self.repository.update_review(
                review_id,
                status="FAILED",
                progress=progress,
                completed_at=now(),
                error="AI review stopped unexpectedly",
                execution_progress={**execution, "phase": "FAILED", "in_flight_requests": 0, "current_units": []},
                unit_states={key: value.model_dump() for key, value in current_unit_states.items()},
                total_batches=review.total_batches,
                validated_batches=completed_investigator_batches,
                unavailable_batches=max(skipped_investigator_batches, review.total_batches - completed_investigator_batches),
            )

    def retry(self, review_id: str) -> ReviewRead:
        """Resume review units that did not reach a complete validated outcome."""
        review = self.get(review_id)
        if review.status not in {"FAILED", "PARTIAL", "INTERRUPTED"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "Only failed, partial, or interrupted reviews can be resumed")
        if review.unavailable_batches <= 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "This review has no unavailable review units to resume")
        if not review.source_snapshot_hash:
            raise HTTPException(status.HTTP_409_CONFLICT, "This review has no source snapshot; start a new review instead of resuming it")
        current_snapshot_hash = self.projects.current_source_snapshot_hash(review.project_id)
        if current_snapshot_hash != review.source_snapshot_hash:
            raise HTTPException(status.HTTP_409_CONFLICT, "Project source changed since this review snapshot; start a new review instead of resuming it")
        unit_states = self._review_unit_states(review)
        retry_units = [number for number, state in unit_states.items() if state.state == "UNAVAILABLE"]
        if not retry_units:
            raise HTTPException(status.HTTP_409_CONFLICT, "This review has no unresolved review units to resume")
        progress = list(review.progress)
        progress.append(f"Queued retry for {len(retry_units)} unavailable review unit{'s' if len(retry_units) != 1 else ''}")
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
                    f"Project Intelligence revalidation: {len(marked)} stale record{'s' if len(marked) != 1 else ''} marked NEEDS_REVALIDATION against current source"
                )
                conflicts = [memory_id for memory_id, outcome in outcomes.items() if outcome == "CONFLICTED"]
                if conflicts:
                    progress.append(f"Project Intelligence revalidation: {len(conflicts)} record{'s' if len(conflicts) != 1 else ''} conflicted with current source")
            elif outcomes:
                progress.append("Project Intelligence revalidation: all related records remain supported by current source")
        except Exception as error:  # noqa: BLE001 - revalidation must never fail a review
            progress.append(f"Project Intelligence revalidation was unavailable: {' '.join(str(error).split())[:200]}")

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
                changes = ", ".join(f"{value} {key.replace('_', ' ')}" for key, value in counts.items() if value)
                progress.append(f"Project Intelligence learning completed: {changes or 'no new knowledge'}")
            elif learning_status == "SKIPPED":
                progress.append(f"Project Intelligence learning skipped: {summary.get('error') or 'no qualifying learning material'}")
            else:
                progress.append("Project Intelligence processing failed; the review result is unaffected")
            self.repository.update_review(
                review_id,
                status=review_status,
                progress=progress,
                total_batches=total_batches,
                validated_batches=validated_batches,
                unavailable_batches=unavailable_batches,
            )
        except Exception as error:  # noqa: BLE001 - learning must never alter the review
            progress.append(f"Project Intelligence processing failed; the review result is unaffected: {' '.join(str(error).split())[:160]}")
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
            progress.append("No accepted confirmed findings require AI recheck; starting new discovery")
            self.repository.update_review(review_id, status="RUNNING", progress=progress)
            return

        progress.append(f"Rechecking {len(findings)} accepted confirmed finding{'s' if len(findings) != 1 else ''} against current source before new discovery")
        self.repository.update_review(review_id, status="RUNNING", progress=progress)
        timestamp = now()
        for index, finding in enumerate(findings, start=1):
            progress.append(f"AI fix verifier is rechecking finding {index}/{len(findings)}: {finding.title}")
            self.repository.update_review(review_id, status="RUNNING", progress=progress)
            try:
                operation = f"review:{review_id}:fix-verifier:finding-{finding.id}"
                result = structured_response(
                    verifier,
                    FixVerificationResult,
                    FIX_VERIFIER_SYSTEM,
                    f"Required JSON Schema:\n{json.dumps(FixVerificationResult.model_json_schema(), ensure_ascii=False)}\n\n"
                    f"Previously accepted confirmed finding:\n{json.dumps(finding.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                    f"Current source relevant to this finding:\n{self._fix_context(finding, raw_files)}",
                    operation=operation,
                    on_event=self._diagnostic_sink(review_id, role="fix-verifier", batch_number=index, total_batches=len(findings), file_count=1),
                )
            except RuntimeError as error:
                progress.append(f"AI recheck could not complete for {finding.title}; it remains open: {error}")
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
            )
            if resolution is FindingResolution.SOLVED:
                progress.append(f"Finding {index}/{len(findings)} marked solved after AI recheck: {finding.title}")
            else:
                progress.append(f"AI recheck kept finding {index}/{len(findings)} open: {finding.title} ({result.verdict})")
            self.repository.update_review(review_id, status="RUNNING", progress=progress)

    def _context_batches(self, project_id: str, project_name: str, focus: list[str], raw_files: list[dict[str, str]], context_limit_chars: int | None = None) -> list[ReviewContextBatch]:
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

        # Ranked, state-labelled intelligence retrieval replaces the former
        # global ACTIVE-memory injection: each batch receives only records
        # relevant to its files/symbols, and terminal or provisional states
        # are never presented as current authority.
        def batch_shared(batch_files: list[str]) -> str:
            if self.intelligence:
                batch_symbols = sorted(set().union(*(symbols_by_file.get(path, set()) for path in batch_files))) if batch_files else []
                intelligence_text = self.intelligence.review_memory_context(project_id, batch_symbols, batch_files, cap=self.MAX_MEMORIES)
                intelligence_block = f"Project Intelligence (state-labelled, relevant only):\n{intelligence_text}"
            else:
                memories = self.memories.context(project_id, None, None).memories[:self.MAX_MEMORIES]
                memory_text = "\n".join(f"- {memory.statement[:240]}" for memory in memories) or "none"
                intelligence_block = f"Active Engineering Memory:\n{memory_text}"
            return f"Project: {project_name}\n\nIndexed symbols:\n{symbol_text}\n\n{intelligence_block}\n\nfirmware.ai.yaml (if present):\n{yaml_text}\n\nRelevant repository data:\n"

        shared = batch_shared([])

        source_budget = min(
            self.MAX_SOURCE_CHARS_PER_BATCH,
            max(
                320,
                context_limit
                - len(shared)
                - self.INVESTIGATOR_REQUEST_OVERHEAD_CHARS
                - self.MAX_CONTEXT_SEGMENTS_PER_BATCH * self.SOURCE_TAG_OVERHEAD_CHARS,
            ),
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
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
                progress.append("Review worker stopped before completion; no provider request is still running")
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
        finding = self.repository.finding(finding_id)
        if not finding or finding["project_id"] != project_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Finding not found")
        return FindingRead.model_validate(finding)

    def decide(self, project_id: str, finding_id: str, update: FindingDecisionUpdate) -> FindingRead:
        self.finding(project_id, finding_id)
        if update.decision is FindingDecision.REJECTED and not update.reason:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A rejection reason is required")
        self.repository.update_finding_decision(finding_id, update.decision, update.reason)
        return self.finding(project_id, finding_id)

    def resolve(self, project_id: str, finding_id: str, update: FindingResolutionUpdate) -> FindingRead:
        finding = self.finding(project_id, finding_id)
        if finding.classification is not FindingClassification.CONFIRMED_BUG:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Only CONFIRMED_BUG findings can be marked solved")
        resolved_at = now() if update.resolution is FindingResolution.SOLVED else None
        remediation = {
            "status": FindingRemediationStatus.MANUALLY_MARKED,
            "notes": "Resolution was recorded manually without a source recheck.",
            "verified_at": resolved_at,
            "source_refreshed": False,
            "changed_files": [],
        }
        self.repository.update_finding_remediation(finding_id, remediation, update.resolution, resolved_at)
        return self.finding(project_id, finding_id)

    def verify_fix(self, project_id: str, finding_id: str, directory_override: str | None = None) -> FindingRead:
        finding = self.finding(project_id, finding_id)
        if finding.classification is not FindingClassification.CONFIRMED_BUG:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Only CONFIRMED_BUG findings can be verified as solved")
        if finding.decision is not FindingDecision.ACCEPTED:
            raise HTTPException(status.HTTP_409_CONFLICT, "Accept this finding before verifying a source fix")
        verifier = self.provider_resolver(FIX_VERIFIER_ROLE)
        if not verifier.available():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Verify Fix requires a configured verifier model with a server API key")

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
            self.repository.update_finding_remediation(finding_id, remediation, FindingResolution.OPEN, None)
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
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Fix verification did not receive a validated verifier result") from error

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
        self.repository.update_finding_remediation(finding_id, remediation, resolution, resolved_at)
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
