from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, status

from .i18n import message as _msg
from .i18n import normalize_locale

from . import intelligence_core as core
from .repository import MemoryRepository
from .schemas import (
    ContextResponse, MemoryApproval, MemoryConflict, MemoryProposalCreate, MemoryRead,
    MemoryProposalUpdate, MemoryStatus, MemoryUpdate, ProposalRead, RevalidationRequest,
    RevalidationEvent, RevalidationResponse,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryService:
    def __init__(self, repository: MemoryRepository, language_resolver: Callable[[], str] | None = None) -> None:
        self.repository = repository
        self.language_resolver = language_resolver

    def _error(self, status_code: int, key: str, /, **params: object) -> HTTPException:
        try:
            locale = normalize_locale(self.language_resolver() if self.language_resolver else None)
        except Exception:  # noqa: BLE001 - error rendering must never raise
            locale = "en"
        return HTTPException(status_code, _msg(key, locale, **params))

    @staticmethod
    def _memory_id() -> str:
        return f"MEM-{uuid4().hex[:12].upper()}"

    @staticmethod
    def _proposal_id() -> str:
        return f"MP-{uuid4().hex[:12].upper()}"

    @staticmethod
    def _as_memory(record: dict) -> MemoryRead:
        return MemoryRead.model_validate(record)

    @staticmethod
    def _as_proposal(record: dict) -> ProposalRead:
        return ProposalRead.model_validate(record)

    @staticmethod
    def _claims_exclusive_ownership(memory: MemoryRead) -> bool:
        """Avoid treating every evidence reference as an ownership guarantee."""
        return bool(re.search(r"\b(exclusive(?:ly)?|only|sole|single[- ]task)\b", memory.statement, re.IGNORECASE))

    def propose(self, project_id: str, proposal: MemoryProposalCreate) -> ProposalRead:
        record = proposal.model_dump(mode="json")
        record.update(id=self._proposal_id(), project_id=project_id, created_at=_now())
        self.repository.create_proposal(record)
        return self._as_proposal(record)

    def approve(self, proposal_id: str, approval: MemoryApproval) -> MemoryRead:
        proposal = self.repository.get_proposal(proposal_id)
        if not proposal:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.proposal_not_found")
        now = _now()
        source = proposal["source"].copy()
        if approval.engineer_note:
            source["engineer_note"] = approval.engineer_note
        evidence = [item.model_dump(mode="json") for item in approval.evidence] if approval.evidence else proposal["evidence"]
        scope = (approval.scope or proposal["scope"]).model_dump(mode="json") if approval.scope else proposal["scope"]
        primary_links = [scope.get("symbol") or "", scope.get("component") or ""]
        evidence_locations = [f"{item.get('file') or ''}:{item.get('line') or 0}" for item in evidence]
        fingerprint = core.compute_fingerprint(proposal["project_id"], proposal["type"], approval.statement or proposal["statement"], primary_links, evidence_locations)
        # Engineer-approved knowledge enters as REINFORCED with explicit
        # provenance: strong engineer trust, but still below VERIFIED until
        # current-source revalidation confirms it.
        record = {
            **proposal,
            "id": self._memory_id(),
            "statement": approval.statement or proposal["statement"],
            "scope": scope,
            "evidence": evidence,
            "source": source,
            "status": MemoryStatus.ACTIVE,
            "state": core.REINFORCED,
            "confidence": 0.8,
            "origin": "ENGINEER_APPROVED",
            "first_observed_at": now,
            "last_observed_at": now,
            "observation_count": 1,
            "reinforcement_count": 1,
            "fingerprint": fingerprint,
            "approved_by": approval.approved_by,
            "commit_sha": approval.commit_sha or proposal["commit_sha"],
            "created_at": now,
            "updated_at": now,
        }
        self.repository.create_memory(record)
        memory_id = record["id"]
        links = []
        if scope.get("symbol"):
            links.append({"memory_id": memory_id, "project_id": proposal["project_id"], "link_kind": "SYMBOL", "link_value": scope["symbol"], "role": "PRIMARY", "created_at": now})
        if scope.get("component"):
            links.append({"memory_id": memory_id, "project_id": proposal["project_id"], "link_kind": "COMPONENT", "link_value": scope["component"], "role": "PRIMARY", "created_at": now})
        for item in evidence:
            if item.get("symbol"):
                links.append({"memory_id": memory_id, "project_id": proposal["project_id"], "link_kind": "SYMBOL", "link_value": item["symbol"], "role": "SUPPORTING", "created_at": now})
            if item.get("file"):
                links.append({"memory_id": memory_id, "project_id": proposal["project_id"], "link_kind": "FILE", "link_value": item["file"], "role": "SUPPORTING", "created_at": now})
        if links:
            self.repository.add_intelligence_links(links)
        self.repository.add_intelligence_evidence([
            {
                "memory_id": memory_id, "project_id": proposal["project_id"], "kind": "ENGINEER",
                "file": item.get("file"), "line": item.get("line"), "symbol": item.get("symbol"),
                "file_hash": None, "description": (item.get("description") or "Engineer-approved evidence.")[:2000],
                "fingerprint": core.evidence_fingerprint("ENGINEER", item.get("file"), item.get("line"), item.get("symbol"), item.get("description") or "Engineer-approved evidence."),
                "created_at": now,
            }
            for item in evidence[:12]
        ])
        self.repository.add_intelligence_observation({
            "memory_id": memory_id, "project_id": proposal["project_id"], "kind": "ENGINEER_APPROVAL",
            "from_state": None, "to_state": core.REINFORCED, "confidence": 0.8, "fingerprint": fingerprint,
            "detail": f"Approved by engineer {approval.approved_by}.", "created_at": now,
        })
        return self._as_memory(record)

    def update_proposal(self, proposal_id: str, update: MemoryProposalUpdate) -> ProposalRead:
        if not self.repository.get_proposal(proposal_id):
            raise self._error(status.HTTP_404_NOT_FOUND, "error.proposal_not_found")
        changes = update.model_dump(mode="json", exclude_unset=True)
        if not changes:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.no_updates")
        self.repository.update_proposal(proposal_id, changes)
        return self._as_proposal(self.repository.get_proposal(proposal_id))

    def ignore_proposal(self, proposal_id: str) -> None:
        if not self.repository.get_proposal(proposal_id):
            raise self._error(status.HTTP_404_NOT_FOUND, "error.proposal_not_found")
        self.repository.delete_proposal(proposal_id)

    def get(self, memory_id: str) -> MemoryRead:
        record = self.repository.get_memory(memory_id)
        if not record:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.memory_not_found")
        return self._as_memory(record)

    def list(self, project_id: str, status_filter: MemoryStatus | None = None) -> list[MemoryRead]:
        return [self._as_memory(record) for record in self.repository.list_memories(project_id, status_filter)]

    def update(self, memory_id: str, update: MemoryUpdate) -> MemoryRead:
        self.get(memory_id)
        changes = update.model_dump(mode="json", exclude_unset=True)
        if not changes:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.no_updates")
        changes["updated_at"] = _now()
        self.repository.update_memory(memory_id, changes)
        return self.get(memory_id)

    def disable(self, memory_id: str) -> MemoryRead:
        return self.update(memory_id, MemoryUpdate(status=MemoryStatus.DISABLED))

    def revalidation_history(self, memory_id: str) -> list[RevalidationEvent]:
        self.get(memory_id)
        return [RevalidationEvent.model_validate(event) for event in self.repository.list_revalidation_events(memory_id)]

    def context(self, project_id: str, symbol: str | None, component: str | None) -> ContextResponse:
        memories = self.list(project_id, MemoryStatus.ACTIVE)
        if symbol or component:
            memories = [memory for memory in memories if (
                not symbol or memory.scope.symbol == symbol or any(item.symbol == symbol for item in memory.evidence)
            ) and (
                not component or memory.scope.component == component
            )]
        return ContextResponse(memories=memories)

    def revalidate(self, project_id: str, request: RevalidationRequest) -> RevalidationResponse:
        active_memories = self.list(project_id, MemoryStatus.ACTIVE)
        conflicts: list[MemoryConflict] = []
        events = []
        for memory in active_memories:
            if not self._claims_exclusive_ownership(memory):
                continue
            evidence_symbols = {item.symbol for item in memory.evidence}
            for observation in request.observations:
                if observation.symbol not in evidence_symbols or not observation.caller:
                    continue
                owner_symbols = {item.symbol for item in memory.evidence}
                if observation.caller in owner_symbols:
                    continue
                reason = f"{observation.caller} now references {observation.symbol}, which conflicts with this memory's recorded ownership evidence."
                conflicts.append(MemoryConflict(memory=memory, observation=observation, reason=reason))
                events.append({"memory_id": memory.id, "commit_sha": request.commit_sha, "observation": observation.model_dump(mode="json"), "reason": reason, "created_at": _now()})
                self.repository.update_memory(memory.id, {"status": MemoryStatus.NEEDS_REVALIDATION, "updated_at": _now()})
                break
        if events:
            self.repository.add_revalidation_events(events)
        return RevalidationResponse(checked=len(active_memories), conflicts=conflicts)
