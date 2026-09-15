"""Project Intelligence orchestrator.

Implements the automatic learning pipeline specified by FS-DEV-003:
Memory Synthesizer -> deterministic validation -> Memory Verifier ->
lifecycle application, plus ranked retrieval and source-authoritative
revalidation. Learning is always best-effort: no exception thrown by this
service may fail a review, decision, resolution, or chat request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status

from . import intelligence_core as core
from .ai_provider import AIProvider, structured_response
from .intelligence_schemas import (
    CandidateMemory,
    IntelligenceCounts,
    IntelligenceDetailRead,
    IntelligenceListResponse,
    IntelligenceRecordRead,
    IntelligenceRevalidateResponse,
    IntelligenceSummaryResponse,
    LearningHealth,
    MemorySynthesisResult,
    MemoryVerificationResult,
    ReviewLearningSummaryRead,
)
from .platform_repository import PlatformRepository
from .prompts import (
    MEMORY_SYNTHESIZER_PROMPT_VERSION,
    MEMORY_SYNTHESIZER_SYSTEM,
    MEMORY_VERIFIER_PROMPT_VERSION,
    MEMORY_VERIFIER_SYSTEM,
)
from .project_service import ProjectService
from .repository import MemoryRepository

REVIEW_MEMORY_CAP = 12
CHAT_MEMORY_CAP = 6
VERIFIER_PROVISIONAL_CAP = 2
MAX_CANDIDATES = 4
MAX_SOURCE_EXCERPT_CHARS = 2_400
MAX_SYMBOLS_IN_PROMPT = 60
MAX_FINDINGS_PER_TRIGGER = 12
WEAK_REASON_MAX_LENGTH = 15
STRONG_REASON_MIN_LENGTH = 20
CLAIM_MARKERS = (
    "i think", "i believe", "because", "only ", "must be", "intentional",
    "actually", "in fact", "not a bug", "is safe", "are safe", "it's fine",
    "is correct", "by design", "exclusively",
)
GREETING_PATTERN = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay)\b[.!\s]*$", re.IGNORECASE)


class LearningSkipped(Exception):
    """Raised when learning cannot run (e.g. unconfigured models)."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded(text: str | None, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


class IntelligenceService:
    def __init__(
        self,
        repository: MemoryRepository,
        platform: PlatformRepository,
        projects: ProjectService,
        provider_resolver: Callable[[str], AIProvider],
    ) -> None:
        self.repository = repository
        self.platform = platform
        self.projects = projects
        self.provider_resolver = provider_resolver

    # ------------------------------------------------------------------
    # Durable learning jobs
    # ------------------------------------------------------------------

    def enqueue(self, project_id: str, trigger: str, payload: dict[str, Any]) -> str:
        job_id = f"LRN-{uuid4().hex[:12].upper()}"
        self.repository.create_learning_job({
            "id": job_id,
            "project_id": project_id,
            "trigger": trigger,
            "trigger_payload": payload,
            "status": "QUEUED",
            "progress": [],
            "error": None,
            "prompt_version": MEMORY_SYNTHESIZER_PROMPT_VERSION,
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
        })
        return job_id

    def drain_pending_jobs(self, project_id: str) -> None:
        """Execute queued learning jobs for one project. Never raises."""
        try:
            for job in self.repository.list_learning_jobs(project_id, status="QUEUED", limit=20):
                self.execute_job(job["id"])
        except Exception:  # noqa: BLE001 - background safety boundary
            pass

    def run_review_learning(self, review_id: str) -> dict[str, Any]:
        """Enqueue and execute REVIEW_COMPLETED learning; returns the summary row."""
        job_id = self.enqueue_for_review(review_id)
        self.execute_job(job_id)
        summary = self.repository.get_review_learning_summary(review_id)
        return summary or {"review_id": review_id, "status": "FAILED", "counts": {}, "error": "learning summary unavailable"}

    def enqueue_for_review(self, review_id: str) -> str:
        review = self.platform.get_review(review_id)
        if not review:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
        return self.enqueue(review["project_id"], "REVIEW_COMPLETED", {"review_id": review_id})

    def execute_job(self, job_id: str) -> None:
        """Claim and run one learning job. Best-effort: never raises."""
        if not self.repository.claim_learning_job(job_id):
            return
        job = self.repository.get_learning_job(job_id) or {}
        project_id = job.get("project_id", "")
        counts: dict[str, int] = {key: 0 for key in ("provisional", "reinforced", "verified", "needs_revalidation", "conflicted", "superseded", "rejected")}
        progress: list[str] = list(job.get("progress") or [])
        try:
            synthesizer = self.provider_resolver("memory_synthesizer")
            verifier = self.provider_resolver("memory_verifier")
            if not synthesizer.available() or not verifier.available():
                raise LearningSkipped("Memory Synthesizer and Memory Verifier models must be configured with a server API key")
            trigger = job.get("trigger", "")
            payload = job.get("trigger_payload") or {}
            progress.append(f"Memory Synthesizer is analyzing the {trigger.lower().replace('_', ' ')} trigger")
            self.repository.update_learning_job(job_id, {"status": "RUNNING", "progress": progress})

            context = self._trigger_context(project_id, trigger, payload)
            if context.get("skip_reason"):
                progress.append(f"Learning skipped: {context['skip_reason']}")
                self.repository.update_learning_job(job_id, {"status": "SKIPPED", "progress": progress, "error": _bounded(context["skip_reason"]), "completed_at": _now()})
                self._persist_review_summary(job, "SKIPPED", counts, _bounded(context["skip_reason"]))
                return

            synthesis = structured_response(
                synthesizer,
                MemorySynthesisResult,
                MEMORY_SYNTHESIZER_SYSTEM,
                self._synthesizer_prompt(trigger, context),
                operation=f"memory-synthesizer:{job_id}",
            )
            progress.append(f"Memory Synthesizer proposed {len(synthesis.candidates)} candidate record(s)")
            self.repository.update_learning_job(job_id, {"progress": progress})

            for candidate in synthesis.candidates[:MAX_CANDIDATES]:
                outcome = self._process_candidate(project_id, candidate, context, verifier, job_id, counts)
                progress.append(f"Candidate outcome: {outcome}")
                self.repository.update_learning_job(job_id, {"progress": progress})

            progress.append(
                "Project Intelligence learning completed: "
                + ", ".join(f"{value} {key.replace('_', ' ')}" for key, value in counts.items() if value)
                or "no knowledge changes"
            )
            self.repository.update_learning_job(job_id, {"status": "COMPLETED", "progress": progress, "error": None, "completed_at": _now()})
            self._persist_review_summary(job, "COMPLETED", counts, None)
        except LearningSkipped as skipped:
            progress.append(f"Learning skipped: {skipped}")
            self.repository.update_learning_job(job_id, {"status": "SKIPPED", "progress": progress, "error": _bounded(str(skipped)), "completed_at": _now()})
            self._persist_review_summary(job, "SKIPPED", counts, _bounded(str(skipped)))
        except Exception as error:  # noqa: BLE001 - learning must never propagate
            message = _bounded(str(error) or error.__class__.__name__)
            progress.append("Project Intelligence processing failed; the review result is unaffected")
            self.repository.update_learning_job(job_id, {"status": "FAILED", "progress": progress, "error": message, "completed_at": _now()})
            self._persist_review_summary(job, "FAILED", counts, message)

    def _persist_review_summary(self, job: dict[str, Any], job_status: str, counts: dict[str, int], error: str | None) -> None:
        if job.get("trigger") != "REVIEW_COMPLETED":
            return
        review_id = (job.get("trigger_payload") or {}).get("review_id")
        if not review_id:
            return
        self.repository.upsert_review_learning_summary({
            "review_id": review_id,
            "project_id": job.get("project_id", ""),
            "job_id": job.get("id"),
            "status": job_status,
            "counts": counts,
            "error": error,
            "created_at": _now(),
            "updated_at": _now(),
        })

    # ------------------------------------------------------------------
    # Trigger context construction (bounded, source-authoritative)
    # ------------------------------------------------------------------

    def _trigger_context(self, project_id: str, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw_files = self.platform.raw_files(project_id)
        files_by_path = {file["path"]: file for file in raw_files}
        symbols = [symbol["name"] for symbol in self.platform.list_symbols(project_id, None)]
        context: dict[str, Any] = {
            "project": self.platform.get_project(project_id) or {},
            "files_by_path": files_by_path,
            "symbols": symbols[:400],
            "trigger": trigger,
            "payload": payload,
            "snapshot": self.source_snapshot(project_id),
        }
        if trigger == "REVIEW_COMPLETED":
            review = self.platform.get_review(payload.get("review_id", ""))
            findings = [f for f in self.platform.findings(project_id) if f.get("review_id") == payload.get("review_id")][:MAX_FINDINGS_PER_TRIGGER]
            context.update(review=review, findings=findings)
        elif trigger in {"FINDING_DECISION", "FINDING_RESOLVED", "FINDING_FIX_VERIFIED"}:
            finding = self.platform.finding(payload.get("finding_id", "")) or {}
            context.update(finding=finding)
            if trigger == "FINDING_DECISION":
                reason = _bounded(payload.get("reason"), 2000)
                if not self._rejection_reason_is_grounded(reason, symbols, files_by_path):
                    context["skip_reason"] = "the rejection reason does not reference concrete source evidence, so it cannot become project knowledge"
                context.update(decision=payload.get("decision"), reason=reason)
        elif trigger == "CHAT_CANDIDATE":
            messages = {m["id"]: m for m in self.platform.messages(project_id)}
            user_message = messages.get(payload.get("user_message_id"), {})
            assistant_message = messages.get(payload.get("assistant_message_id"), {})
            context.update(user_message=user_message, assistant_message=assistant_message)
        return context

    @staticmethod
    def _rejection_reason_is_grounded(reason: str, symbols: list[str], files_by_path: dict[str, dict[str, str]]) -> bool:
        """A rejection may only teach false-positive knowledge with concrete evidence.

        Weak assertions ("I do not think this is a bug") never qualify; the
        reason must name an indexed symbol or a known source file.
        """
        if len(reason) < STRONG_REASON_MIN_LENGTH or len(reason) <= WEAK_REASON_MAX_LENGTH:
            return False
        lowered = reason.casefold()
        if any(symbol.casefold() in lowered for symbol in symbols):
            return True
        return any(path.casefold() in lowered for path in files_by_path)

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    def _synthesizer_prompt(self, trigger: str, context: dict[str, Any]) -> str:
        sections: list[str] = [f"Required JSON Schema:\n{json.dumps(MemorySynthesisResult.model_json_schema(), ensure_ascii=False)}"]
        sections.append(f"Learning trigger: {trigger}")
        project = context.get("project") or {}
        sections.append(f"Project: {project.get('name', 'unknown')} ({project.get('framework') or 'framework unknown'})")
        findings = context.get("findings") or []
        if findings:
            rendered = []
            for finding in findings:
                rendered.append({
                    "title": finding.get("title"),
                    "classification": finding.get("classification"),
                    "severity": finding.get("severity"),
                    "decision": finding.get("decision"),
                    "resolution": finding.get("resolution"),
                    "location": finding.get("location"),
                    "summary": _bounded(finding.get("summary"), 600),
                    "verification": (finding.get("verification") or {}).get("status"),
                })
            sections.append(f"Review findings (untrusted data):\n{json.dumps(rendered, ensure_ascii=False)}")
        finding = context.get("finding")
        if finding:
            compact = {
                "title": finding.get("title"),
                "classification": finding.get("classification"),
                "severity": finding.get("severity"),
                "location": finding.get("location"),
                "summary": _bounded(finding.get("summary"), 600),
                "evidence": finding.get("evidence"),
                "verification": finding.get("verification"),
                "decision": finding.get("decision"),
                "decision_reason": _bounded(finding.get("decision_reason"), 600),
                "resolution": finding.get("resolution"),
                "remediation": finding.get("remediation"),
            }
            sections.append(f"Finding (untrusted data):\n{json.dumps(compact, ensure_ascii=False)}")
        if context.get("decision"):
            sections.append(f"Engineer decision: {context['decision']}")
            sections.append(f"Engineer reason (declared input, not observed evidence): {context.get('reason') or '(none)'}")
        if trigger == "CHAT_CANDIDATE":
            user_message = context.get("user_message") or {}
            assistant_message = context.get("assistant_message") or {}
            sections.append(
                "Qualifying chat exchange (untrusted data):\n"
                f"Engineer: {_bounded(user_message.get('content'), 800)}\n"
                f"Assistant: {_bounded(assistant_message.get('content'), 800)}"
            )
        sections.append(f"Indexed symbols (observed):\n{', '.join(context['symbols'][:MAX_SYMBOLS_IN_PROMPT]) or 'none'}")
        sections.append(f"Current source snapshot authority (file hashes):\n{json.dumps(self._compact_snapshot(context.get('snapshot')), ensure_ascii=False)}")
        excerpts = self._trigger_excerpts(context)
        if excerpts:
            sections.append(f"Current source excerpts at evidence locations (untrusted data):\n{'\n\n'.join(excerpts)}")
        related = self._related_intelligence_text(context)
        sections.append(f"Existing related intelligence (states are authoritative):\n{related or 'none'}")
        sections.append(
            "Remember: propose at most a few bounded, reusable candidates. Empty output is valid. "
            "Never turn an engineer assertion or chat claim alone into knowledge."
        )
        return "\n\n".join(sections)

    def _verifier_prompt(self, candidate: CandidateMemory, context: dict[str, Any], shortlist: list[dict[str, Any]]) -> str:
        sections: list[str] = [f"Required JSON Schema:\n{json.dumps(MemoryVerificationResult.model_json_schema(), ensure_ascii=False)}"]
        sections.append(f"Candidate knowledge (unverified hypothesis):\n{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}")
        sections.append(f"Indexed symbols (observed):\n{', '.join(context['symbols'][:MAX_SYMBOLS_IN_PROMPT]) or 'none'}")
        sections.append(f"Current source snapshot authority (file hashes):\n{json.dumps(self._compact_snapshot(context.get('snapshot')), ensure_ascii=False)}")
        excerpts = self._trigger_excerpts(context) or self._candidate_excerpts(candidate, context)
        if excerpts:
            sections.append(f"Current source excerpts (untrusted data):\n{'\n\n'.join(excerpts)}")
        if shortlist:
            rendered = [
                {"id": record["id"], "state": record.get("state"), "type": record.get("type"), "statement": _bounded(record.get("statement"), 240)}
                for record in shortlist
            ]
            sections.append(f"Existing related intelligence shortlist (PROVISIONAL entries are unverified hypotheses):\n{json.dumps(rendered, ensure_ascii=False)}")
        sections.append("Attempt to disprove the candidate. Only VERIFY actions with quoted current-source support are eligible for the VERIFIED state.")
        return "\n\n".join(sections)

    def _trigger_excerpts(self, context: dict[str, Any]) -> list[str]:
        files_by_path: dict[str, dict[str, str]] = context["files_by_path"]
        targets: list[tuple[str, int]] = []
        for finding in context.get("findings") or []:
            location = finding.get("location") or {}
            if location.get("file"):
                targets.append((location["file"], int(location.get("line_start") or 1)))
            for item in (finding.get("evidence") or [])[:4]:
                if item.get("file"):
                    targets.append((item["file"], int(item.get("line") or 1)))
        finding = context.get("finding")
        if finding:
            location = finding.get("location") or {}
            if location.get("file"):
                targets.append((location["file"], int(location.get("line_start") or 1)))
            for item in (finding.get("evidence") or [])[:4]:
                if item.get("file"):
                    targets.append((item["file"], int(item.get("line") or 1)))
        return self._render_excerpts(targets, files_by_path)

    def _candidate_excerpts(self, candidate: CandidateMemory, context: dict[str, Any]) -> list[str]:
        targets = [(item.file, item.line) for item in candidate.evidence[:6]]
        return self._render_excerpts(targets, context["files_by_path"])

    @staticmethod
    def _render_excerpts(targets: list[tuple[str, int]], files_by_path: dict[str, dict[str, str]]) -> list[str]:
        excerpts: list[str] = []
        seen: set[str] = set()
        for path, line in targets:
            if path in seen or path not in files_by_path:
                continue
            seen.add(path)
            content = files_by_path[path]["content"]
            lines = content.splitlines()
            start = max(1, line - 6)
            end = min(len(lines), line + 10)
            excerpt = "\n".join(lines[start - 1:end])[:MAX_SOURCE_EXCERPT_CHARS]
            excerpts.append(f'<source path="{path}" lines="{start}-{end}" hash="{files_by_path[path]["content_hash"][:12]}">\n{excerpt}\n</source>')
        return excerpts[:6]

    @staticmethod
    def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, str]:
        files = snapshot.get("files") or {}
        return {path: digest[:12] for path, digest in list(files.items())[:80]}

    def _related_intelligence_text(self, context: dict[str, Any]) -> str:
        link_values = self._context_link_values(context)
        records = self.repository.find_memories_by_link_values(context["project"].get("id", ""), link_values, limit=6)
        if not records:
            return ""
        return "\n".join(
            f"- [{record.get('state')}] {record.get('type')}: {_bounded(record.get('statement'), 200)}"
            for record in records
        )

    @staticmethod
    def _context_link_values(context: dict[str, Any]) -> list[str]:
        values: list[str] = []
        for finding in context.get("findings") or []:
            location = finding.get("location") or {}
            if location.get("file"):
                values.append(location["file"])
            values.extend((finding.get("evidence") or [{}])[0].get("symbol", "") and [(finding.get("evidence") or [{}])[0]["symbol"]] or [])
        finding = context.get("finding")
        if finding:
            location = finding.get("location") or {}
            if location.get("file"):
                values.append(location["file"])
            for item in (finding.get("evidence") or [])[:4]:
                if item.get("symbol"):
                    values.append(item["symbol"])
        payload = context.get("payload") or {}
        if payload.get("selected_file"):
            values.append(payload["selected_file"])
        return [value for value in values if value]

    # ------------------------------------------------------------------
    # Candidate processing and lifecycle application
    # ------------------------------------------------------------------

    def _process_candidate(
        self,
        project_id: str,
        candidate: CandidateMemory,
        context: dict[str, Any],
        verifier: AIProvider,
        job_id: str,
        counts: dict[str, int],
    ) -> str:
        files_by_path = context["files_by_path"]
        if not self._candidate_evidence_is_valid(candidate, files_by_path):
            counts["rejected"] += 1
            return "rejected: no evidence location matches the current source index"

        primary_links = [candidate.symbols[0]] if candidate.symbols else []
        if not primary_links and candidate.components:
            primary_links = [candidate.components[0]]
        evidence_locations = [f"{item.file}:{item.line}" for item in candidate.evidence]
        fingerprint = core.compute_fingerprint(project_id, candidate.type, candidate.statement, primary_links, evidence_locations)

        existing = self.repository.find_memory_by_fingerprint(project_id, fingerprint)
        if existing and existing.get("state") in {core.PROVISIONAL, core.REINFORCED, core.VERIFIED}:
            return self._reinforce_existing(existing, candidate, context, job_id, counts)

        shortlist = self._shortlist(project_id, candidate)
        if existing:
            shortlist = [existing] + [record for record in shortlist if record["id"] != existing["id"]]
            shortlist = shortlist[:6]
        verification = structured_response(
            verifier,
            MemoryVerificationResult,
            MEMORY_VERIFIER_SYSTEM,
            self._verifier_prompt(candidate, context, shortlist),
            operation=f"memory-verifier:{job_id}",
        )
        return self._apply_verification(project_id, candidate, context, verification, existing, shortlist, job_id, counts)

    @staticmethod
    def _candidate_evidence_is_valid(candidate: CandidateMemory, files_by_path: dict[str, dict[str, str]]) -> bool:
        valid = 0
        for item in candidate.evidence:
            file = files_by_path.get(item.file)
            if not file:
                continue
            line_count = len(file["content"].splitlines())
            if 1 <= item.line <= max(1, line_count):
                valid += 1
        return valid >= 1

    def _shortlist(self, project_id: str, candidate: CandidateMemory) -> list[dict[str, Any]]:
        link_values = [*candidate.symbols, *candidate.components, *[item.file for item in candidate.evidence]]
        records = self.repository.find_memories_by_link_values(project_id, link_values, memory_type=candidate.type, limit=6)
        if len(records) < 3:
            records = self.repository.find_memories_by_link_values(project_id, link_values, limit=6)
        return records

    def _reinforce_existing(self, existing: dict[str, Any], candidate: CandidateMemory, context: dict[str, Any], job_id: str, counts: dict[str, int]) -> str:
        """Deterministic duplicate handling: same fingerprint reinforces, never duplicates."""
        memory_id = existing["id"]
        project_id = existing["project_id"]
        now = _now()
        evidence_rows = self._candidate_evidence_rows(project_id, memory_id, candidate, context, kind="FINDING" if context.get("finding") or context.get("findings") else "SOURCE")
        new_fingerprints = [row["fingerprint"] for row in evidence_rows]
        known = self.repository.evidence_fingerprints(memory_id)
        independent = [fingerprint for fingerprint in new_fingerprints if fingerprint not in known]
        self.repository.add_intelligence_evidence(evidence_rows)
        observation_count = int(existing.get("observation_count") or 1) + 1
        reinforcement_count = int(existing.get("reinforcement_count") or 0) + (1 if independent else 0)
        confidence = core.bounded_confidence(float(existing.get("confidence") or 0.7), 0.05 if independent else 0.0)
        state = existing.get("state")
        detail = "Independent matching observation reinforced existing knowledge." if independent else "Repeated observation matched existing knowledge fingerprint."
        transitioned = False
        if state == core.PROVISIONAL and reinforcement_count >= core.REINFORCEMENT_OBSERVATIONS:
            state = core.REINFORCED
            transitioned = True
            detail += " Two independent observations now support it, so it is labelled reinforced (still not source-verified)."
        self.repository.update_memory(memory_id, {
            "state": state,
            "confidence": confidence,
            "observation_count": observation_count,
            "reinforcement_count": reinforcement_count,
            "last_observed_at": now,
            "updated_at": now,
        })
        self.repository.add_intelligence_observation({
            "memory_id": memory_id,
            "project_id": project_id,
            "kind": "REINFORCEMENT",
            "from_state": existing.get("state"),
            "to_state": state,
            "confidence": confidence,
            "confidence_delta": round(confidence - float(existing.get("confidence") or 0.7), 3),
            "fingerprint": existing.get("fingerprint"),
            "review_id": (context.get("payload") or {}).get("review_id"),
            "finding_id": (context.get("payload") or {}).get("finding_id"),
            "chat_message_id": (context.get("payload") or {}).get("user_message_id"),
            "job_id": job_id,
            "detail": detail,
            "created_at": now,
        })
        if transitioned:
            counts["reinforced"] += 1
            return f"reinforced: {memory_id} moved to REINFORCED"
        counts["reinforced"] += 1
        return f"reinforced: {memory_id} observation count now {observation_count}"

    def _apply_verification(
        self,
        project_id: str,
        candidate: CandidateMemory,
        context: dict[str, Any],
        verification: MemoryVerificationResult,
        existing: dict[str, Any] | None,
        shortlist: list[dict[str, Any]],
        job_id: str,
        counts: dict[str, int],
    ) -> str:
        action = verification.action
        target = None
        conflict_target = None

        # Exact-fingerprint equivalent already in a revalidation/conflict state is
        # the canonical record: a source-supported VERIFY_NEW must revalidate that
        # row through the audited lifecycle, never insert a duplicate. SUPERSEDE
        # is reserved for a materially different statement/fingerprint, so an
        # exact match collapses to in-place verification.
        if existing and existing.get("state") in {core.CONFLICTED, core.NEEDS_REVALIDATION} and action in {"VERIFY_NEW", "SUPERSEDE_EXISTING"}:
            verification = verification.model_copy(update={"action": "VERIFY_EXISTING", "target_id": existing["id"]})
            action = "VERIFY_EXISTING"

        # Existing-record actions resolve their target exclusively from the
        # deterministic shortlist (never an unverified model reference). The
        # verifier schema distinguishes the record a contradiction points at
        # (conflict_target_id) from a record to reinforce/verify/supersede
        # (target_id).
        if action in {"REINFORCE_EXISTING", "VERIFY_EXISTING", "MARK_NEEDS_REVALIDATION", "SUPERSEDE_EXISTING"}:
            target_id = verification.target_id
            target = existing if existing and existing["id"] == target_id else next((record for record in shortlist if record["id"] == target_id), None)
            if target is None and target_id:
                counts["rejected"] += 1
                return "rejected: verifier referenced a target that is not in the deterministic shortlist"

        if action == "MARK_CONFLICTED":
            conflict_id = verification.conflict_target_id or (verification.target_id if verification.target_id and verification.conflict_evidence else None)
            conflict_target = existing if existing and existing["id"] == conflict_id else next((record for record in shortlist if record["id"] == conflict_id), None)
            # The contradiction must name an existing shortlisted record AND be a
            # current-source-grounded CONTRADICTED verdict citing resolvable
            # file/line evidence. An ungrounded or non-CONTRADICTED response never
            # changes record state.
            if conflict_target is None or not self._conflict_evidence_is_valid(verification, context["files_by_path"]):
                counts["rejected"] += 1
                return "rejected: conflict action requires a shortlisted target and current-source CONTRADICTED evidence at a valid line"
            self._mark_conflicted(project_id, conflict_target, verification, context, job_id)
            counts["conflicted"] += 1
            return f"conflicted: {conflict_target['id']} contradicted by current source"

        # A model-only conclusion can never set VERIFIED: direct current-source
        # support, quoted evidence, and >= 0.85 confidence are all required and
        # re-validated against the current index.
        wants_verification = action in {"VERIFY_NEW", "VERIFY_EXISTING", "SUPERSEDE_EXISTING"}
        if wants_verification:
            evidence_valid = bool(verification.valid_evidence) and self._verification_evidence_is_valid(verification, context["files_by_path"])
            if verification.source_support != "SUPPORTED" or verification.confidence < core.VERIFIED_CONFIDENCE_THRESHOLD or not evidence_valid:
                action = "REINFORCE_EXISTING" if target else "ACCEPT_PROVISIONAL"

        if action == "REJECT_UNSUPPORTED":
            counts["rejected"] += 1
            return f"rejected: {_bounded(verification.rationale, 200)}"

        if action == "MARK_NEEDS_REVALIDATION" and target:
            self._transition(project_id, target, core.NEEDS_REVALIDATION, job_id, context, detail=_bounded(verification.rationale, 400))
            counts["needs_revalidation"] += 1
            return f"needs revalidation: {target['id']}"

        if action == "REINFORCE_EXISTING" and target:
            return self._reinforce_existing(target, candidate, context, job_id, counts)

        if action in {"VERIFY_EXISTING", "SUPERSEDE_EXISTING"} and target:
            self._verify_existing(project_id, target, verification, context, job_id)
            if action == "SUPERSEDE_EXISTING":
                # Create the verified replacement, then move the prior record to
                # SUPERSEDED through the audited lifecycle helper so it is no
                # longer retrievable as current truth.
                replacement_id = self._create_record(project_id, candidate, context, verification, job_id, provisional=False)
                self._transition(project_id, target, core.SUPERSEDED, job_id, context,
                                 detail=f"Superseded by verified record {replacement_id}.")
                self.repository.update_memory(target["id"], {"superseded_by": replacement_id, "updated_at": _now()})
                self.repository.add_intelligence_links([{
                    "memory_id": replacement_id, "project_id": project_id, "link_kind": "FINDING",
                    "link_value": target["id"], "role": "SUPERSEDES", "created_at": _now(),
                }])
                counts["superseded"] += 1
                return f"superseded: {target['id']} replaced by {replacement_id}"
            counts["verified"] += 1
            return f"verified: {target['id']}"

        # ACCEPT_PROVISIONAL / VERIFY_NEW (possibly downgraded) / SUPERSEDE without target.
        memory_id = self._create_record(project_id, candidate, context, verification, job_id, provisional=action != "VERIFY_NEW")
        if action == "VERIFY_NEW":
            counts["verified"] += 1
            return f"verified: {memory_id} created with current-source support"
        counts["provisional"] += 1
        return f"provisional: {memory_id}"

    @staticmethod
    def _conflict_evidence_is_valid(verification: MemoryVerificationResult, files_by_path: dict[str, dict[str, str]]) -> bool:
        """A contradiction must be reported as CONTRADICTED and cite at least one
        conflict location that resolves in the current indexed file and line range."""
        if verification.source_support != "CONTRADICTED":
            return False
        return IntelligenceService._conflict_references_resolvable(verification.conflict_evidence, files_by_path)

    @staticmethod
    def _conflict_references_resolvable(items, files_by_path: dict[str, dict[str, str]]) -> bool:
        for item in items or []:
            file = files_by_path.get(item.file)
            if not file:
                continue
            if 1 <= item.line <= max(1, len(file["content"].splitlines())):
                return True
        return False

    @staticmethod
    def _resolvable_conflict_items(items, files_by_path: dict[str, dict[str, str]]) -> list:
        resolved = []
        for item in items or []:
            file = files_by_path.get(item.file)
            if file and 1 <= item.line <= max(1, len(file["content"].splitlines())):
                resolved.append(item)
        return resolved

    @staticmethod
    def _verification_evidence_is_valid(verification: MemoryVerificationResult, files_by_path: dict[str, dict[str, str]]) -> bool:
        for item in verification.valid_evidence:
            file = files_by_path.get(item.file)
            if not file:
                continue
            if 1 <= item.line <= max(1, len(file["content"].splitlines())):
                return True
        return False

    def _candidate_evidence_rows(self, project_id: str, memory_id: str, candidate: CandidateMemory, context: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        files_by_path = context["files_by_path"]
        rows = []
        for item in candidate.evidence[:6]:
            file = files_by_path.get(item.file)
            digest = file["content_hash"] if file else None
            description = f"{candidate.origin_reason}: {item.description}"[:2000]
            rows.append({
                "memory_id": memory_id,
                "project_id": project_id,
                "kind": kind,
                "file": item.file,
                "line": item.line,
                "symbol": item.symbol,
                "file_hash": digest,
                "description": description,
                "fingerprint": core.evidence_fingerprint(kind, item.file, item.line, item.symbol, description),
                "created_at": _now(),
            })
        return rows

    def _create_record(self, project_id: str, candidate: CandidateMemory, context: dict[str, Any], verification: MemoryVerificationResult, job_id: str, provisional: bool) -> str:
        """Persist a new automatic record. Automatic synthesis always starts
        PROVISIONAL; a source-supported verifier result transitions it to
        VERIFIED as a second, audited step."""
        now = _now()
        memory_id = f"MEM-{uuid4().hex[:12].upper()}"
        primary_symbol = candidate.symbols[0] if candidate.symbols else None
        component = candidate.components[0] if candidate.components else None
        scope = {"type": "SYMBOL", "symbol": primary_symbol} if primary_symbol else ({"type": "COMPONENT", "component": component} if component else {"type": "PROJECT"})
        payload = context.get("payload") or {}
        source = {
            "type": "AUTOMATIC",
            "finding_id": payload.get("finding_id"),
            "engineer_note": _bounded(candidate.origin_reason, 5000) or "Synthesized from review outcomes.",
        }
        primary_links = [primary_symbol] if primary_symbol else ([component] if component else [])
        evidence_locations = [f"{item.file}:{item.line}" for item in candidate.evidence]
        fingerprint = core.compute_fingerprint(project_id, candidate.type, candidate.statement, primary_links, evidence_locations)
        # Deterministic guard: an equivalent fingerprint that is still live
        # (non-terminal) must never be inserted a second time. Terminal matches
        # are allowed so superseded/disabled knowledge can be legitimately
        # re-learned from fresh evidence.
        collision = self.repository.find_memory_by_fingerprint(project_id, fingerprint)
        if collision and collision.get("state") in core.NON_TERMINAL_STATES:
            return collision["id"]
        state = core.PROVISIONAL
        confidence = min(candidate.confidence, 0.84) if provisional else min(max(verification.confidence, core.VERIFIED_CONFIDENCE_THRESHOLD), 0.95)
        record = {
            "id": memory_id,
            "project_id": project_id,
            "type": candidate.type,
            "statement": candidate.statement,
            "scope": scope,
            "evidence": [{"symbol": item.symbol or item.file, "file": item.file, "line": item.line, "description": _bounded(item.description, 2000)} for item in candidate.evidence[:8]],
            "source": source,
            "status": core.legacy_status_for(state),
            "state": state,
            "proposed_by": "AI",
            "approved_by": "AUTOMATIC",
            "commit_sha": (context.get("snapshot") or {}).get("git", {}).get("head"),
            "created_at": now,
            "updated_at": now,
            "confidence": round(confidence, 3),
            "origin": "SYNTHESIZER",
            "first_observed_at": now,
            "last_observed_at": now,
            "last_validated_at": now if not provisional else None,
            "last_validated_commit": (context.get("snapshot") or {}).get("git", {}).get("head") if not provisional else None,
            "last_validated_git_status": None,
            "observation_count": 1,
            "reinforcement_count": 1 if provisional else 0,
            "superseded_by": None,
            "conflict_summary": None,
            "fingerprint": fingerprint,
        }
        self.repository.create_memory(record)
        links: list[dict[str, Any]] = []
        for index, symbol in enumerate(candidate.symbols[:6]):
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "SYMBOL", "link_value": symbol, "role": "PRIMARY" if index == 0 else "SUPPORTING", "created_at": now})
        for index, comp in enumerate(candidate.components[:4]):
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "COMPONENT", "link_value": comp, "role": "PRIMARY" if index == 0 and not candidate.symbols else "SUPPORTING", "created_at": now})
        for item in candidate.evidence[:6]:
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "FILE", "link_value": item.file, "role": "SUPPORTING", "created_at": now})
        if payload.get("review_id"):
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "REVIEW", "link_value": payload["review_id"], "role": "SUPPORTING", "created_at": now})
        if payload.get("finding_id"):
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "FINDING", "link_value": payload["finding_id"], "role": "SUPPORTING", "created_at": now})
        if payload.get("user_message_id"):
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "CHAT_MESSAGE", "link_value": payload["user_message_id"], "role": "SUPPORTING", "created_at": now})
        if links:
            self.repository.add_intelligence_links(links)
        kind = "FINDING" if (context.get("finding") or context.get("findings")) else ("CHAT" if context.get("trigger") == "CHAT_CANDIDATE" else "SOURCE")
        self.repository.add_intelligence_evidence(self._candidate_evidence_rows(project_id, memory_id, candidate, context, kind=kind))
        self.repository.add_intelligence_observation({
            "memory_id": memory_id,
            "project_id": project_id,
            "kind": "SYNTHESIS",
            "from_state": None,
            "to_state": core.PROVISIONAL,
            "confidence": round(confidence, 3),
            "fingerprint": fingerprint,
            "review_id": payload.get("review_id"),
            "finding_id": payload.get("finding_id"),
            "chat_message_id": payload.get("user_message_id"),
            "job_id": job_id,
            "detail": _bounded(candidate.origin_reason, 2000),
            "created_at": now,
        })
        if not provisional:
            self._transition_record(record, core.VERIFIED, job_id, context, detail=f"Memory Verifier confirmed direct current-source support: {_bounded(verification.rationale, 400)}")
        return memory_id

    def _verify_existing(self, project_id: str, target: dict[str, Any], verification: MemoryVerificationResult, context: dict[str, Any], job_id: str) -> None:
        now = _now()
        git_head = (context.get("snapshot") or {}).get("git", {}).get("head")
        confidence = max(float(target.get("confidence") or 0.7), min(verification.confidence, 0.95))
        self.repository.update_memory(target["id"], {
            "state": core.VERIFIED,
            "confidence": round(confidence, 3),
            "last_validated_at": now,
            "last_validated_commit": git_head,
            "last_validated_git_status": None,
            "updated_at": now,
        })
        self.repository.add_intelligence_observation({
            "memory_id": target["id"],
            "project_id": project_id,
            "kind": "VERIFICATION",
            "from_state": target.get("state"),
            "to_state": core.VERIFIED,
            "confidence": round(confidence, 3),
            "finding_id": (context.get("payload") or {}).get("finding_id"),
            "job_id": job_id,
            "detail": _bounded(verification.rationale, 2000),
            "created_at": now,
        })
        self.repository.resolve_conflicts(target["id"], "RESOLVED_VERIFIED", now)

    def _mark_conflicted(self, project_id: str, target: dict[str, Any], verification: MemoryVerificationResult, context: dict[str, Any], job_id: str) -> None:
        now = _now()
        files_by_path = context["files_by_path"]
        # Persist only conflict references validated against the current index
        # (file present AND line within range); ungrounded references are dropped.
        conflict_evidence = [
            {"file": item.file, "line": item.line, "description": _bounded(item.description, 1000)}
            for item in self._resolvable_conflict_items(verification.conflict_evidence, files_by_path)
        ][:4]
        conflict_id = self.repository.add_intelligence_conflict({
            "memory_id": target["id"],
            "project_id": project_id,
            "evidence": {"verifier_rationale": _bounded(verification.rationale, 2000), "items": conflict_evidence},
            "snapshot": self._compact_snapshot(context.get("snapshot") or {}),
            "resolution_state": "OPEN",
            "resolved_observation_id": None,
            "detail": _bounded(verification.rationale, 2000),
            "created_at": now,
            "resolved_at": None,
        })
        summary = f"Current source contradicts this record: {_bounded(verification.rationale, 400)} (conflict #{conflict_id})"
        self._transition(project_id, target, core.CONFLICTED, job_id, context, detail=summary)
        self.repository.update_memory(target["id"], {"conflict_summary": summary, "updated_at": now})

    def _transition(self, project_id: str, target: dict[str, Any], to_state: str, job_id: str, context: dict[str, Any], detail: str) -> None:
        from_state = target.get("state")
        if not core.transition_allowed(from_state, to_state):
            return
        now = _now()
        self.repository.update_memory(target["id"], {"state": to_state, "updated_at": now})
        self.repository.add_intelligence_observation({
            "memory_id": target["id"],
            "project_id": project_id,
            "kind": {
                core.NEEDS_REVALIDATION: "REVALIDATION",
                core.CONFLICTED: "CONFLICT",
                core.SUPERSEDED: "SUPERSESSION",
                core.DISABLED: "DISABLE",
                core.REINFORCED: "REINFORCEMENT",
                core.VERIFIED: "VERIFICATION",
            }.get(to_state, "REVALIDATION"),
            "from_state": from_state,
            "to_state": to_state,
            "finding_id": (context.get("payload") or {}).get("finding_id"),
            "job_id": job_id,
            "detail": detail,
            "created_at": now,
        })

    def _transition_record(self, record: dict[str, Any], to_state: str, job_id: str, context: dict[str, Any], detail: str) -> None:
        self._transition(record["project_id"], record, to_state, job_id, context, detail)

    # ------------------------------------------------------------------
    # Ranked retrieval (review + chat authority)
    # ------------------------------------------------------------------

    def relevant_records(self, project_id: str, link_values: list[str], cap: int = REVIEW_MEMORY_CAP) -> list[dict[str, Any]]:
        """VERIFIED/REINFORCED records ranked by exact symbol, then file/component,
        then state, confidence, and observation count. Terminal and provisional
        states are never returned as current authority."""
        records = self.repository.retrievable_intelligence(project_id)
        if not records or not link_values:
            return []
        links = self.repository.links_for_memories([record["id"] for record in records])
        values = {value.strip().casefold() for value in link_values if value and value.strip()}
        scored: list[tuple[tuple[int, int, float, int, str], dict[str, Any]]] = []
        for record in records:
            record_links = links.get(record["id"], [])
            has_symbol = any(link["link_kind"] == "SYMBOL" and link["link_value"].casefold() in values for link in record_links)
            has_file = any(link["link_kind"] == "FILE" and link["link_value"].casefold() in values for link in record_links)
            has_component = any(link["link_kind"] == "COMPONENT" and link["link_value"].casefold() in values for link in record_links)
            if not (has_symbol or has_file or has_component):
                continue
            state_rank = 0 if record.get("state") == core.VERIFIED else 1
            link_rank = 0 if has_symbol else 1
            key = (state_rank, link_rank, -float(record.get("confidence") or 0.0), -int(record.get("observation_count") or 0), record.get("updated_at") or "")
            scored.append((key, record))
        scored.sort(key=lambda item: item[0])
        return [record for _, record in scored[:cap]]

    def review_memory_context(self, project_id: str, symbols: list[str], files: list[str], cap: int = REVIEW_MEMORY_CAP) -> str:
        records = self.relevant_records(project_id, [*symbols, *files], cap=cap)
        if not records:
            return "none"
        lines = []
        for record in records:
            label = "verified" if record.get("state") == core.VERIFIED else "reinforced observation (not yet source-verified)"
            lines.append(f"- [{label}] {_bounded(record.get('statement'), 240)}")
        return "\n".join(lines)

    def chat_intelligence_context(self, project_id: str, link_values: list[str], cap: int = CHAT_MEMORY_CAP) -> str:
        records = self.relevant_records(project_id, link_values, cap=cap)
        if not records:
            return ""
        lines = []
        for record in records:
            label = "verified" if record.get("state") == core.VERIFIED else "reinforced observation (not yet source-verified)"
            lines.append(f"- [{label}] {_bounded(record.get('statement'), 240)}")
        return "\n".join(lines)

    def verifier_hypotheses(self, project_id: str, link_values: list[str], cap: int = VERIFIER_PROVISIONAL_CAP) -> list[dict[str, Any]]:
        """Exact-match PROVISIONAL records, supplied only to the Memory Verifier."""
        records = self.repository.provisional_intelligence(project_id)
        if not records or not link_values:
            return []
        links = self.repository.links_for_memories([record["id"] for record in records])
        values = {value.strip().casefold() for value in link_values if value and value.strip()}
        matched = [
            record for record in records
            if any(link["link_value"].casefold() in values for link in links.get(record["id"], []))
        ]
        return matched[:cap]

    # ------------------------------------------------------------------
    # Source snapshots and deterministic revalidation
    # ------------------------------------------------------------------

    def source_snapshot(self, project_id: str) -> dict[str, Any]:
        files = {file["path"]: file["content_hash"] for file in self.platform.raw_files(project_id)}
        return {"files": files, "git": self._git_metadata(project_id)}

    def _git_metadata(self, project_id: str) -> dict[str, Any]:
        """Optional provenance read directly from .git files; never executes Git."""
        project = self.platform.get_project(project_id) or {}
        source_directory = project.get("source_directory")
        if not source_directory:
            return {}
        git_dir = None
        try:
            from pathlib import Path

            candidate = Path(source_directory) / ".git"
            if candidate.is_dir():
                git_dir = candidate
            elif candidate.is_file():
                return {}
        except OSError:
            return {}
        if git_dir is None:
            return {}
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        branch = None
        sha = head if not head.startswith("ref: ") else None
        if head.startswith("ref: "):
            branch = head[5:]
            try:
                sha = (git_dir / branch).read_text(encoding="utf-8").strip()
            except OSError:
                sha = None
        return {"head": sha[:12] if sha else None, "branch": branch}

    def deterministic_source_revalidation(self, project_id: str) -> dict[str, Any]:
        """Fast, synchronous stale-marking against the current index.

        Records whose evidence files were deleted, whose file hashes changed,
        or whose evidence symbols disappeared become NEEDS_REVALIDATION with a
        persisted observation. No AI calls; safe to run inside any request.
        """
        current_files = {file["path"]: file["content_hash"] for file in self.platform.raw_files(project_id)}
        current_symbols = {symbol["name"] for symbol in self.platform.list_symbols(project_id, None)}
        references = self.repository.find_evidence_files(project_id)
        marked: list[str] = []
        reasons: dict[str, str] = {}
        by_memory: dict[str, list[dict[str, Any]]] = {}
        for reference in references:
            by_memory.setdefault(reference["memory_id"], []).append(reference)
        records = {record["id"]: record for record in self.repository.list_intelligence(project_id)}
        for memory_id, refs in by_memory.items():
            record = records.get(memory_id)
            if not record or record.get("state") in {core.SUPERSEDED, core.DISABLED, core.NEEDS_REVALIDATION}:
                continue
            reason = None
            for ref in refs:
                if ref["file"] not in current_files:
                    reason = f"Evidence file {ref['file']} is no longer part of the indexed source."
                    break
                if ref["file_hash"] and current_files[ref["file"]] != ref["file_hash"]:
                    reason = f"Evidence file {ref['file']} changed since this record was last validated."
                    break
                if ref["symbol"] and ref["symbol"] not in current_symbols:
                    reason = f"Evidence symbol {ref['symbol']} no longer exists in the indexed source."
                    break
            if reason:
                marked.append(memory_id)
                reasons[memory_id] = reason
                now = _now()
                self.repository.update_memory(memory_id, {"state": core.NEEDS_REVALIDATION, "updated_at": now})
                self.repository.add_intelligence_observation({
                    "memory_id": memory_id,
                    "project_id": project_id,
                    "kind": "REVALIDATION",
                    "from_state": record.get("state"),
                    "to_state": core.NEEDS_REVALIDATION,
                    "detail": reason,
                    "created_at": now,
                })
        return {"checked": len(by_memory), "marked": marked, "reasons": reasons}

    def revalidate_against_source(self, project_id: str) -> dict[str, Any]:
        """Full revalidation: deterministic stale marking plus Memory Verifier
        review of affected records. Used before new review discovery."""
        deterministic = self.deterministic_source_revalidation(project_id)
        affected_ids = list(deterministic["marked"])
        for record in self.repository.list_intelligence(project_id, state=core.CONFLICTED):
            affected_ids.append(record["id"])
        outcomes: dict[str, str] = {}
        verifier: AIProvider | None = None
        try:
            verifier = self.provider_resolver("memory_verifier")
        except Exception:  # noqa: BLE001 - provider resolution must not break revalidation
            verifier = None
        for memory_id in dict.fromkeys(affected_ids):
            if not verifier or not verifier.available():
                outcomes[memory_id] = "NEEDS_REVALIDATION"
                continue
            outcome = self._revalidate_one(project_id, memory_id, verifier)
            outcomes[memory_id] = outcome
        return {**deterministic, "outcomes": outcomes}

    def revalidate_record(self, project_id: str, memory_id: str) -> IntelligenceRevalidateResponse:
        record = self.repository.get_memory(memory_id)
        if not record or record.get("project_id") != project_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Intelligence record not found")
        if record.get("state") in {core.SUPERSEDED, core.DISABLED}:
            raise HTTPException(status.HTTP_409_CONFLICT, "Terminal records cannot be revalidated")
        verifier = self.provider_resolver("memory_verifier")
        if not verifier.available():
            # Deterministic-only revalidation keeps the API useful without models.
            deterministic = self.deterministic_source_revalidation(project_id)
            outcome = "NEEDS_REVALIDATION" if memory_id in deterministic["marked"] else "UNCHANGED"
            detail = deterministic["reasons"].get(memory_id, "No source change was detected for this record's evidence.")
            return IntelligenceRevalidateResponse(record=self._record_read(record), outcome=outcome, detail=detail)
        outcome = self._revalidate_one(project_id, memory_id, verifier)
        detail = "Revalidated against the current indexed source."
        record = self.repository.get_memory(memory_id) or record
        if outcome == "CONFLICTED":
            detail = "Current source contradicts this record; it is now marked conflicted."
        elif outcome == "NEEDS_REVALIDATION":
            detail = "The current source no longer matches this record's evidence."
        elif outcome == "VERIFIED":
            detail = "Direct current-source support confirmed by the Memory Verifier."
        return IntelligenceRevalidateResponse(record=self._record_read(record), outcome=outcome, detail=detail)

    def _revalidate_one(self, project_id: str, memory_id: str, verifier: AIProvider) -> str:
        record = self.repository.get_memory(memory_id)
        if not record:
            return "NOT_FOUND"
        files_by_path = {file["path"]: file for file in self.platform.raw_files(project_id)}
        symbols = [symbol["name"] for symbol in self.platform.list_symbols(project_id, None)]
        context = {
            "project": self.platform.get_project(project_id) or {},
            "files_by_path": files_by_path,
            "symbols": symbols[:400],
            "trigger": "REVALIDATION",
            "payload": {},
            "snapshot": self.source_snapshot(project_id),
        }
        targets: list[tuple[str, int]] = []
        for item in self.repository.list_intelligence_evidence(memory_id, limit=6):
            if item.get("file"):
                targets.append((item["file"], int(item.get("line") or 1)))
        excerpts = self._render_excerpts(targets, files_by_path)
        sections = [
            f"Required JSON Schema:\n{json.dumps(MemoryVerificationResult.model_json_schema(), ensure_ascii=False)}",
            f"Existing record to revalidate (id {memory_id}, state {record.get('state')}):\n{json.dumps({'type': record.get('type'), 'statement': record.get('statement'), 'state': record.get('state')}, ensure_ascii=False)}",
            f"Indexed symbols (observed):\n{', '.join(symbols[:MAX_SYMBOLS_IN_PROMPT]) or 'none'}",
            f"Current source snapshot authority (file hashes):\n{json.dumps(self._compact_snapshot(context['snapshot']), ensure_ascii=False)}",
        ]
        if excerpts:
            sections.append(f"Current source excerpts at the record's evidence locations (untrusted data):\n{'\n\n'.join(excerpts)}")
        sections.append("Decide whether this existing record is supported, contradicted, or needs revalidation based only on the current source.")
        try:
            verification = structured_response(verifier, MemoryVerificationResult, MEMORY_VERIFIER_SYSTEM, "\n\n".join(sections), operation=f"memory-revalidation:{memory_id}")
        except RuntimeError:
            return "NEEDS_REVALIDATION"

        action = verification.action
        evidence_valid = bool(verification.valid_evidence) and self._verification_evidence_is_valid(verification, files_by_path)
        if action in {"VERIFY_EXISTING", "SUPERSEDE_EXISTING"} and (
            verification.source_support != "SUPPORTED" or verification.confidence < core.VERIFIED_CONFIDENCE_THRESHOLD or not evidence_valid
        ):
            action = "MARK_NEEDS_REVALIDATION"

        if action == "MARK_CONFLICTED":
            # The record being revalidated is the contradictory target here.
            # Require a current-source CONTRADICTED verdict with conflict
            # evidence resolving to an indexed file and in-range line; otherwise
            # leave the record unchanged and persist no conflict.
            if not self._conflict_evidence_is_valid(verification, files_by_path):
                return "UNCHANGED"
            self._mark_conflicted(project_id, record, verification, context, job_id=None)
            return "CONFLICTED"
        if action in {"VERIFY_EXISTING", "SUPERSEDE_EXISTING"}:
            self._verify_existing(project_id, record, verification, context, job_id=None)
            return "VERIFIED"
        if action == "REINFORCE_EXISTING":
            self.repository.update_memory(memory_id, {"last_observed_at": _now(), "updated_at": _now()})
            return "REINFORCED"
        if action == "MARK_NEEDS_REVALIDATION":
            self._transition(project_id, record, core.NEEDS_REVALIDATION, None, context, detail=_bounded(verification.rationale, 400))
            return "NEEDS_REVALIDATION"
        if action == "ACCEPT_PROVISIONAL" and record.get("state") in {core.REINFORCED, core.VERIFIED}:
            self._transition(project_id, record, core.NEEDS_REVALIDATION, None, context, detail="Memory Verifier could not reconfirm prior support from current source.")
            return "NEEDS_REVALIDATION"
        return "UNCHANGED"

    # ------------------------------------------------------------------
    # Chat candidate eligibility
    # ------------------------------------------------------------------

    def chat_candidate_payload(self, project_id: str, request: Any, user_message_id: str, assistant_message_id: str) -> dict[str, Any] | None:
        """Deterministic eligibility: an engineer claim grounded in a selected
        file, finding, or indexed symbol. Greetings, generic questions, and
        ungrounded opinions never qualify."""
        message = (request.message or "").strip()
        if len(message) < STRONG_REASON_MIN_LENGTH or GREETING_PATTERN.match(message):
            return None
        lowered = message.casefold()
        if not any(marker in lowered for marker in CLAIM_MARKERS):
            return None
        symbols = [symbol["name"] for symbol in self.platform.list_symbols(project_id, None)]
        grounded_by_symbol = any(symbol.casefold() in lowered for symbol in symbols)
        grounded = bool(request.selected_file or request.finding_id or grounded_by_symbol)
        if not grounded:
            return None
        return {
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "selected_file": request.selected_file,
            "finding_id": request.finding_id,
            "message": message[:2000],
        }

    # ------------------------------------------------------------------
    # API read models
    # ------------------------------------------------------------------

    def list_records(self, project_id: str, state: str | None, memory_type: str | None, limit: int, offset: int) -> IntelligenceListResponse:
        records = self.repository.list_intelligence(project_id, state=state, memory_type=memory_type, limit=limit, offset=offset)
        counts = self.intelligence_counts(project_id)
        return IntelligenceListResponse(records=[self._record_read(record) for record in records], counts=counts)

    def intelligence_counts(self, project_id: str) -> IntelligenceCounts:
        raw = self.repository.intelligence_counts(project_id)
        return IntelligenceCounts(**raw)

    def summary(self, project_id: str) -> IntelligenceSummaryResponse:
        counts = self.intelligence_counts(project_id)
        jobs = self.repository.list_learning_jobs(project_id, limit=1)
        last_job = jobs[0] if jobs else None
        health = LearningHealth(
            open_conflicts=self.repository.open_conflict_count(project_id),
            stale_records=counts.needs_revalidation,
            last_job_status=last_job.get("status") if last_job else None,
            last_job_at=last_job.get("created_at") if last_job else None,
        )
        return IntelligenceSummaryResponse(counts=counts, health=health)

    def detail(self, memory_id: str) -> IntelligenceDetailRead:
        record = self.repository.get_memory(memory_id)
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Intelligence record not found")
        base = self._record_read(record, evidence_limit=50)
        observations = [
            {
                "id": row["id"], "memory_id": row["memory_id"], "kind": row["kind"],
                "from_state": row["from_state"], "to_state": row["to_state"],
                "confidence": row["confidence"], "confidence_delta": row["confidence_delta"],
                "fingerprint": row["fingerprint"], "review_id": row["review_id"], "finding_id": row["finding_id"],
                "chat_message_id": row["chat_message_id"], "detail": row["detail"], "created_at": row["created_at"],
            }
            for row in self.repository.list_intelligence_observations(memory_id)
        ]
        evidence = [
            {
                "id": row["id"], "memory_id": row["memory_id"], "kind": row["kind"], "file": row["file"],
                "line": row["line"], "symbol": row["symbol"], "file_hash": row["file_hash"],
                "description": row["description"], "created_at": row["created_at"],
            }
            for row in self.repository.list_intelligence_evidence(memory_id, limit=50)
        ]
        conflicts = [
            {
                "id": row["id"], "memory_id": row["memory_id"], "evidence": row["evidence"], "snapshot": row["snapshot"],
                "resolution_state": row["resolution_state"], "detail": row["detail"],
                "created_at": row["created_at"], "resolved_at": row["resolved_at"],
            }
            for row in self.repository.list_intelligence_conflicts(memory_id)
        ]
        return IntelligenceDetailRead(**base.model_dump(), observations=observations, evidence=evidence, conflicts=conflicts)

    def disable(self, memory_id: str, reason: str | None) -> IntelligenceRecordRead:
        record = self.repository.get_memory(memory_id)
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Intelligence record not found")
        if record.get("state") == core.DISABLED:
            return self._record_read(record)
        now = _now()
        self.repository.update_memory(memory_id, {"state": core.DISABLED, "updated_at": now})
        self.repository.add_intelligence_observation({
            "memory_id": memory_id,
            "project_id": record["project_id"],
            "kind": "DISABLE",
            "from_state": record.get("state"),
            "to_state": core.DISABLED,
            "detail": _bounded(reason, 2000) or "Disabled by an engineer.",
            "created_at": now,
        })
        return self._record_read(self.repository.get_memory(memory_id) or record)

    def learning_summary(self, project_id: str, review_id: str) -> ReviewLearningSummaryRead:
        review = self.platform.get_review(review_id)
        if not review or review["project_id"] != project_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
        row = self.repository.get_review_learning_summary(review_id)
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No learning summary exists for this review")
        counts = row.get("counts") or {}
        return ReviewLearningSummaryRead(
            review_id=review_id,
            project_id=project_id,
            job_id=row.get("job_id"),
            status=row.get("status", "UNKNOWN"),
            provisional=counts.get("provisional", 0),
            reinforced=counts.get("reinforced", 0),
            verified=counts.get("verified", 0),
            needs_revalidation=counts.get("needs_revalidation", 0),
            conflicted=counts.get("conflicted", 0),
            superseded=counts.get("superseded", 0),
            rejected=counts.get("rejected", 0),
            error=row.get("error"),
            updated_at=row.get("updated_at"),
        )

    def _record_read(self, record: dict[str, Any], evidence_limit: int = 5) -> IntelligenceRecordRead:
        links = [
            {"id": row["id"], "memory_id": row["memory_id"], "link_kind": row["link_kind"], "link_value": row["link_value"], "role": row["role"], "created_at": row["created_at"]}
            for row in self.repository.list_intelligence_links(record["id"])
        ]
        evidence_rows = self.repository.list_intelligence_evidence(record["id"], limit=evidence_limit)
        evidence = [
            {"id": row["id"], "memory_id": row["memory_id"], "kind": row["kind"], "file": row["file"], "line": row["line"], "symbol": row["symbol"], "file_hash": row["file_hash"], "description": row["description"], "created_at": row["created_at"]}
            for row in evidence_rows
        ]
        return IntelligenceRecordRead(
            id=record["id"],
            project_id=record["project_id"],
            type=record["type"],
            statement=record["statement"],
            state=record.get("state") or core.LEGACY_STATE_MIGRATION.get(record.get("status", ""), core.NEEDS_REVALIDATION),
            status=record.get("status") or "ACTIVE",
            confidence=float(record.get("confidence") or 0.0),
            origin=record.get("origin") or "ENGINEER_APPROVED",
            observation_count=int(record.get("observation_count") or 1),
            reinforcement_count=int(record.get("reinforcement_count") or 0),
            superseded_by=record.get("superseded_by"),
            conflict_summary=record.get("conflict_summary"),
            scope=record.get("scope") or {},
            links=links,
            evidence_count=len(evidence_rows),
            last_evidence=evidence,
            first_observed_at=record.get("first_observed_at") or record.get("created_at"),
            last_observed_at=record.get("last_observed_at") or record.get("updated_at"),
            last_validated_at=record.get("last_validated_at"),
            last_validated_commit=record.get("last_validated_commit"),
            last_validated_git_status=record.get("last_validated_git_status"),
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )


    # ------------------------------------------------------------------
    # Best-effort learning triggers: never fail a persisted domain result
    # ------------------------------------------------------------------

    def enqueue_best_effort(self, project_id: str, trigger: str, payload: dict[str, Any]) -> None:
        """Queue learning only if both memory models are configured; never raises.

        This is the single safe boundary between a persisted domain result
        (review, decision, resolution, fix verification, chat) and automatic
        intelligence processing. A failure here is invisible to the caller.
        """
        try:
            if not self.provider_resolver("memory_synthesizer").available() or not self.provider_resolver("memory_verifier").available():
                return
            self.enqueue(project_id, trigger, payload)
        except Exception:  # noqa: BLE001 - learning never blocks the primary operation; a queued-job failure stays visible via drain
            pass

    def enqueue_decision_learning(self, project_id: str, finding_id: str, decision: str, reason: str | None) -> None:
        self.enqueue_best_effort(project_id, "FINDING_DECISION", {"finding_id": finding_id, "decision": decision, "reason": reason})

    def enqueue_resolution_learning(self, project_id: str, finding_id: str, resolution: str) -> None:
        self.enqueue_best_effort(project_id, "FINDING_RESOLVED", {"finding_id": finding_id, "resolution": resolution})

    def enqueue_fix_verification_learning(self, project_id: str, finding_id: str, resolution: str) -> None:
        self.enqueue_best_effort(project_id, "FINDING_FIX_VERIFIED", {"finding_id": finding_id, "resolution": resolution})

    def enqueue_chat_learning(self, project_id: str, payload: dict[str, Any]) -> None:
        self.enqueue_best_effort(project_id, "CHAT_CANDIDATE", payload)
