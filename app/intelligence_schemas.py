"""Pydantic schemas for FirmSight Project Intelligence.

The intelligence system extends Engineering Memory with a lifecycle
(PROVISIONAL -> REINFORCED -> VERIFIED, plus revalidation/conflict states),
normalized evidence, links, observations, learning jobs, and review learning
summaries. Legacy memory schemas in app/schemas.py remain the compatibility
surface for existing proposal/list/context APIs.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class IntelligenceState(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    REINFORCED = "REINFORCED"
    VERIFIED = "VERIFIED"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"
    CONFLICTED = "CONFLICTED"
    SUPERSEDED = "SUPERSEDED"
    DISABLED = "DISABLED"


class IntelligenceOrigin(StrEnum):
    SYNTHESIZER = "SYNTHESIZER"
    ENGINEER_APPROVED = "ENGINEER_APPROVED"
    LEGACY_ENGINEER_APPROVED = "LEGACY_ENGINEER_APPROVED"


class IntelligenceEvidenceKind(StrEnum):
    SOURCE = "SOURCE"
    FINDING = "FINDING"
    VERIFIER = "VERIFIER"
    CHAT = "CHAT"
    ENGINEER = "ENGINEER"


class IntelligenceLinkKind(StrEnum):
    SYMBOL = "SYMBOL"
    COMPONENT = "COMPONENT"
    REVIEW = "REVIEW"
    FINDING = "FINDING"
    CHAT_MESSAGE = "CHAT_MESSAGE"
    FILE = "FILE"


class IntelligenceLinkRole(StrEnum):
    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    CONFLICTING = "CONFLICTING"
    SUPERSEDES = "SUPERSEDES"


class IntelligenceObservationKind(StrEnum):
    SYNTHESIS = "SYNTHESIS"
    ENGINEER_APPROVAL = "ENGINEER_APPROVAL"
    REINFORCEMENT = "REINFORCEMENT"
    VERIFICATION = "VERIFICATION"
    REVALIDATION = "REVALIDATION"
    CONFLICT = "CONFLICT"
    SUPERSESSION = "SUPERSESSION"
    DISABLE = "DISABLE"
    MIGRATION = "MIGRATION"


class LearningTrigger(StrEnum):
    REVIEW_COMPLETED = "REVIEW_COMPLETED"
    FINDING_DECISION = "FINDING_DECISION"
    FINDING_RESOLVED = "FINDING_RESOLVED"
    FINDING_FIX_VERIFIED = "FINDING_FIX_VERIFIED"
    CHAT_CANDIDATE = "CHAT_CANDIDATE"


class LearningJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# --- Structured AI output schemas (Memory Synthesizer / Memory Verifier) ---


class CandidateEvidence(BaseModel):
    file: str = Field(min_length=1, max_length=1000)
    line: int = Field(default=1, ge=1)
    symbol: str | None = Field(default=None, max_length=200)
    description: str = Field(min_length=4, max_length=1000)


class CandidateMemory(BaseModel):
    type: str = Field(pattern="^(PROJECT_FACT|DESIGN_INTENT|FALSE_POSITIVE_KNOWLEDGE|BUG_PATTERN|RESOLUTION_PATTERN|ARCHITECTURAL_PATTERN|BEHAVIORAL_PATTERN|REVIEW_LESSON)$")
    statement: str = Field(min_length=16, max_length=600)
    confidence: float = Field(ge=0, le=1)
    observed: bool
    origin_reason: str = Field(min_length=8, max_length=1000)
    reuse_reason: str = Field(min_length=8, max_length=1000)
    symbols: list[str] = Field(default_factory=list, max_length=12)
    components: list[str] = Field(default_factory=list, max_length=12)
    evidence: list[CandidateEvidence] = Field(min_length=1, max_length=8)


class MemorySynthesisResult(BaseModel):
    """Structured Memory Synthesizer output. Empty candidates is valid."""

    candidates: list[CandidateMemory] = Field(default_factory=list, max_length=4)


class VerificationEvidence(BaseModel):
    file: str = Field(min_length=1, max_length=1000)
    line: int = Field(default=1, ge=1)
    description: str = Field(min_length=4, max_length=1000)


class MemoryVerificationResult(BaseModel):
    action: str = Field(pattern="^(ACCEPT_PROVISIONAL|REINFORCE_EXISTING|VERIFY_EXISTING|VERIFY_NEW|REJECT_UNSUPPORTED|MARK_NEEDS_REVALIDATION|MARK_CONFLICTED|SUPERSEDE_EXISTING)$")
    target_id: str | None = Field(default=None, max_length=100)
    confidence: float = Field(ge=0, le=1)
    source_support: str = Field(pattern="^(SUPPORTED|PARTIAL|NONE|CONTRADICTED)$")
    valid_evidence: list[VerificationEvidence] = Field(default_factory=list, max_length=8)
    conflict_target_id: str | None = Field(default=None, max_length=100)
    conflict_evidence: list[VerificationEvidence] = Field(default_factory=list, max_length=4)
    rationale: str = Field(min_length=8, max_length=2000)


# --- API read models ---


class IntelligenceEvidenceRead(BaseModel):
    id: int
    memory_id: str
    kind: str
    file: str | None = None
    line: int | None = None
    symbol: str | None = None
    file_hash: str | None = None
    description: str | None = None
    created_at: datetime


class IntelligenceLinkRead(BaseModel):
    id: int
    memory_id: str
    link_kind: str
    link_value: str
    role: str
    created_at: datetime


class IntelligenceObservationRead(BaseModel):
    id: int
    memory_id: str
    kind: str
    from_state: str | None = None
    to_state: str | None = None
    confidence: float | None = None
    confidence_delta: float | None = None
    fingerprint: str | None = None
    review_id: str | None = None
    finding_id: str | None = None
    chat_message_id: str | None = None
    detail: str | None = None
    created_at: datetime


class IntelligenceConflictRead(BaseModel):
    id: int
    memory_id: str
    evidence: dict[str, Any]
    snapshot: dict[str, Any]
    resolution_state: str
    detail: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


class IntelligenceRecordRead(BaseModel):
    id: str
    project_id: str
    type: str
    statement: str
    state: str
    status: str  # legacy compatibility projection
    confidence: float
    origin: str
    observation_count: int
    reinforcement_count: int
    superseded_by: str | None = None
    conflict_summary: str | None = None
    scope: dict[str, Any]
    links: list[IntelligenceLinkRead] = Field(default_factory=list)
    evidence_count: int = 0
    last_evidence: list[IntelligenceEvidenceRead] = Field(default_factory=list)
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    last_validated_at: datetime | None = None
    last_validated_commit: str | None = None
    last_validated_git_status: str | None = None
    created_at: datetime
    updated_at: datetime


class IntelligenceDetailRead(IntelligenceRecordRead):
    observations: list[IntelligenceObservationRead] = Field(default_factory=list)
    evidence: list[IntelligenceEvidenceRead] = Field(default_factory=list)
    conflicts: list[IntelligenceConflictRead] = Field(default_factory=list)


class IntelligenceCounts(BaseModel):
    total: int = 0
    provisional: int = 0
    reinforced: int = 0
    verified: int = 0
    needs_revalidation: int = 0
    conflicted: int = 0
    superseded: int = 0
    disabled: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)


class IntelligenceListResponse(BaseModel):
    records: list[IntelligenceRecordRead]
    counts: IntelligenceCounts


class LearningHealth(BaseModel):
    open_conflicts: int = 0
    stale_records: int = 0
    last_job_status: str | None = None
    last_job_at: datetime | None = None


class IntelligenceSummaryResponse(BaseModel):
    counts: IntelligenceCounts
    health: LearningHealth


class LearningJobRead(BaseModel):
    id: str
    project_id: str
    trigger: str
    status: str
    progress: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ReviewLearningSummaryRead(BaseModel):
    review_id: str
    project_id: str
    job_id: str | None = None
    status: str
    provisional: int = 0
    reinforced: int = 0
    verified: int = 0
    needs_revalidation: int = 0
    conflicted: int = 0
    superseded: int = 0
    rejected: int = 0
    error: str | None = None
    updated_at: datetime | None = None


class IntelligenceRevalidateResponse(BaseModel):
    record: IntelligenceRecordRead
    outcome: str
    detail: str


class IntelligenceDisableRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
