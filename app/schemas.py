from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


class MemoryType(StrEnum):
    ENGINEERING_FACT = "ENGINEERING_FACT"
    DESIGN_INTENT = "DESIGN_INTENT"
    LESSON_LEARNED = "LESSON_LEARNED"
    REJECTED_FINDING = "REJECTED_FINDING"
    ACCEPTED_FINDING = "ACCEPTED_FINDING"
    ENGINEERING_PATTERN = "ENGINEERING_PATTERN"
    # Project Intelligence vocabulary (FS-DEV-003). Legacy values remain valid
    # so existing proposal/list/context APIs and stored records keep working.
    PROJECT_FACT = "PROJECT_FACT"
    FALSE_POSITIVE_KNOWLEDGE = "FALSE_POSITIVE_KNOWLEDGE"
    BUG_PATTERN = "BUG_PATTERN"
    RESOLUTION_PATTERN = "RESOLUTION_PATTERN"
    ARCHITECTURAL_PATTERN = "ARCHITECTURAL_PATTERN"
    BEHAVIORAL_PATTERN = "BEHAVIORAL_PATTERN"
    REVIEW_LESSON = "REVIEW_LESSON"


# Canonical mapping from legacy memory types to Project Intelligence types.
LEGACY_TYPE_MIGRATION = {
    "ENGINEERING_FACT": "PROJECT_FACT",
    "DESIGN_INTENT": "DESIGN_INTENT",
    "REJECTED_FINDING": "FALSE_POSITIVE_KNOWLEDGE",
    "ACCEPTED_FINDING": "BUG_PATTERN",
    "ENGINEERING_PATTERN": "ARCHITECTURAL_PATTERN",
    "LESSON_LEARNED": "REVIEW_LESSON",
}


def canonical_memory_type(value: str) -> str:
    return LEGACY_TYPE_MIGRATION.get(value, value)


class MemoryScopeType(StrEnum):
    SYMBOL = "SYMBOL"
    COMPONENT = "COMPONENT"
    PROJECT = "PROJECT"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"
    SUPERSEDED = "SUPERSEDED"
    DISABLED = "DISABLED"
    # Lifecycle states surfaced through the legacy status field. REINFORCED and
    # VERIFIED records project to legacy ACTIVE; PROVISIONAL/CONFLICTED keep
    # their own value because no legacy bucket can represent them truthfully.
    PROVISIONAL = "PROVISIONAL"
    REINFORCED = "REINFORCED"
    VERIFIED = "VERIFIED"
    CONFLICTED = "CONFLICTED"


class MemorySourceType(StrEnum):
    ENGINEER_CONFIRMED = "ENGINEER_CONFIRMED"
    ENGINEER_REJECTED_FINDING = "ENGINEER_REJECTED_FINDING"
    ENGINEER_ACCEPTED_FINDING = "ENGINEER_ACCEPTED_FINDING"
    ENGINEER_AUTHORED = "ENGINEER_AUTHORED"
    # Automatic Project Intelligence records synthesised from review outcomes.
    AUTOMATIC = "AUTOMATIC"


class MemoryScope(BaseModel):
    type: MemoryScopeType
    symbol: str | None = Field(default=None, max_length=200)
    component: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def scope_has_its_required_target(self) -> "MemoryScope":
        if self.type is MemoryScopeType.SYMBOL and not self.symbol:
            raise ValueError("SYMBOL scope requires symbol")
        if self.type is MemoryScopeType.COMPONENT and not self.component:
            raise ValueError("COMPONENT scope requires component")
        return self


class EvidenceItem(BaseModel):
    symbol: str = Field(min_length=1, max_length=200)
    file: str | None = Field(default=None, max_length=1000)
    line: int | None = Field(default=None, ge=1)
    description: str | None = Field(default=None, max_length=2000)


class MemorySource(BaseModel):
    type: MemorySourceType
    finding_id: str | None = Field(default=None, max_length=100)
    engineer_note: str = Field(min_length=1, max_length=5000)


class MemoryProposalCreate(BaseModel):
    type: MemoryType
    statement: str = Field(min_length=1, max_length=5000)
    scope: MemoryScope
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=50)
    source: MemorySource
    proposed_by: str = Field(default="AI", pattern="^(AI|ENGINEER)$")
    commit_sha: str | None = Field(default=None, max_length=80)


class MemoryApproval(BaseModel):
    approved_by: str = Field(min_length=1, max_length=200)
    statement: str | None = Field(default=None, min_length=1, max_length=5000)
    scope: MemoryScope | None = None
    evidence: list[EvidenceItem] | None = Field(default=None, min_length=1, max_length=50)
    engineer_note: str | None = Field(default=None, min_length=1, max_length=5000)
    commit_sha: str | None = Field(default=None, max_length=80)


class MemoryProposalUpdate(BaseModel):
    statement: str | None = Field(default=None, min_length=1, max_length=5000)
    scope: MemoryScope | None = None
    evidence: list[EvidenceItem] | None = Field(default=None, min_length=1, max_length=50)
    source: MemorySource | None = None
    commit_sha: str | None = Field(default=None, max_length=80)


class MemoryUpdate(BaseModel):
    statement: str | None = Field(default=None, min_length=1, max_length=5000)
    scope: MemoryScope | None = None
    evidence: list[EvidenceItem] | None = Field(default=None, min_length=1, max_length=50)
    status: MemoryStatus | None = None
    commit_sha: str | None = Field(default=None, max_length=80)


class MemoryRead(BaseModel):
    id: str
    project_id: str
    type: MemoryType
    statement: str
    scope: MemoryScope
    evidence: list[EvidenceItem]
    source: MemorySource
    status: MemoryStatus
    proposed_by: str
    approved_by: str
    commit_sha: str | None
    created_at: datetime
    updated_at: datetime


class ProposalRead(BaseModel):
    id: str
    project_id: str
    type: MemoryType
    statement: str
    scope: MemoryScope
    evidence: list[EvidenceItem]
    source: MemorySource
    proposed_by: str
    commit_sha: str | None
    created_at: datetime


class ContextResponse(BaseModel):
    memories: list[MemoryRead]


class SourceObservation(BaseModel):
    symbol: str = Field(min_length=1, max_length=200)
    caller: str | None = Field(default=None, max_length=200)
    file: str | None = Field(default=None, max_length=1000)
    line: int | None = Field(default=None, ge=1)
    description: str | None = Field(default=None, max_length=2000)


class RevalidationRequest(BaseModel):
    observations: Annotated[list[SourceObservation], Field(min_length=1, max_length=5000)]
    commit_sha: str | None = Field(default=None, max_length=80)


class MemoryConflict(BaseModel):
    memory: MemoryRead
    observation: SourceObservation
    reason: str


class RevalidationResponse(BaseModel):
    checked: int
    conflicts: list[MemoryConflict]


class RevalidationEvent(BaseModel):
    memory_id: str
    commit_sha: str | None
    observation: SourceObservation
    reason: str
    created_at: datetime
