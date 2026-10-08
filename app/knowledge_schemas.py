"""Pydantic schemas for the FirmSight knowledge vault and RAG index (FS-DEV-012).

The Markdown vault is the portable, human-readable representation of Project
Intelligence; the database remains authoritative for transactional state.
Stable intelligence IDs connect database records, Markdown documents, chunks,
links, API results, and the UI. Filenames are never identity.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

KNOWLEDGE_SCHEMA_VERSION = 1


class RelationKind(StrEnum):
    CALLS = "CALLS"
    TASK_ENTRY = "TASK_ENTRY"
    ISR_ENTRY = "ISR_ENTRY"
    USES_RESOURCE = "USES_RESOURCE"
    PUBLISHES_TO_QUEUE = "PUBLISHES_TO_QUEUE"
    RECEIVES_FROM_QUEUE = "RECEIVES_FROM_QUEUE"
    ALLOCATES = "ALLOCATES"
    RELEASES = "RELEASES"
    RETURNS_OWNERSHIP = "RETURNS_OWNERSHIP"
    STORES_OWNERSHIP = "STORES_OWNERSHIP"
    PASSES_TO_UNKNOWN = "PASSES_TO_UNKNOWN"
    COMPONENT_MEMBER = "COMPONENT_MEMBER"


class RelationState(StrEnum):
    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"


class OwnershipState(StrEnum):
    RELEASED = "RELEASED"
    UNBALANCED_EXIT = "UNBALANCED_EXIT"
    RETURNS_OWNERSHIP = "RETURNS_OWNERSHIP"
    STORES_OWNERSHIP = "STORES_OWNERSHIP"
    PASSES_TO_UNKNOWN = "PASSES_TO_UNKNOWN"
    REALLOC_UNCERTAIN = "REALLOC_UNCERTAIN"
    UNKNOWN = "UNKNOWN"


class AllocationEventKind(StrEnum):
    ALLOCATE = "ALLOCATE"
    RELEASE = "RELEASE"
    REALLOC = "REALLOC"
    OWNERSHIP_ESCAPE = "OWNERSHIP_ESCAPE"


class VaultStatus(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    NOT_WRITABLE = "NOT_WRITABLE"
    INVALID = "INVALID"


class DocumentKind(StrEnum):
    """Stable document kinds projected into the five-area Obsidian vault."""

    PROJECT = "PROJECT"
    ARCHITECTURE = "ARCHITECTURE"
    REVIEW = "REVIEW"
    FINDING = "FINDING"
    KNOWLEDGE = "KNOWLEDGE"


VAULT_DOCUMENT_ID_PATTERN = r"^(PRJ|TOP|REV|FS|MEM)-[A-Z0-9]+$"


class VaultDocumentFrontmatter(BaseModel):
    """Validated frontmatter contract for project/architecture/review/finding documents.

    These documents are pure generated projections of database/source facts.
    They intentionally have no editable section; external edits are never
    imported for these kinds.
    """

    id: str = Field(min_length=4, max_length=100, pattern=VAULT_DOCUMENT_ID_PATTERN)
    kind: DocumentKind
    project_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=3, max_length=40)
    generated_at: str = Field(min_length=10, max_length=64)
    schema_version: int = KNOWLEDGE_SCHEMA_VERSION
    source: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=24)


class IndexedSymbol(BaseModel):
    id: str = Field(min_length=8, max_length=160)
    project_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    kind: str = Field(min_length=1, max_length=80)
    file: str = Field(min_length=1, max_length=1000)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    signature: str = Field(default="", max_length=1200)
    component: str | None = Field(default=None, max_length=300)
    source_hash: str = Field(min_length=8, max_length=128)
    confidence: float = Field(default=1.0, ge=0, le=1)


class SourceRelation(BaseModel):
    id: str = Field(min_length=8, max_length=160)
    project_id: str = Field(min_length=1, max_length=100)
    relation_kind: RelationKind
    source_symbol_id: str | None = None
    target_symbol_id: str | None = None
    target_name: str | None = Field(default=None, max_length=300)
    file: str = Field(min_length=1, max_length=1000)
    line: int = Field(ge=1)
    evidence_hash: str = Field(min_length=8, max_length=128)
    confidence: float = Field(default=1.0, ge=0, le=1)
    relation_state: RelationState
    metadata: dict[str, str] = Field(default_factory=dict)


class AllocationEvent(BaseModel):
    id: str = Field(min_length=8, max_length=160)
    project_id: str = Field(min_length=1, max_length=100)
    symbol_id: str | None = None
    variable: str | None = Field(default=None, max_length=300)
    event_kind: AllocationEventKind
    allocator_or_releaser: str = Field(min_length=1, max_length=100)
    file: str = Field(min_length=1, max_length=1000)
    line: int = Field(ge=1)
    evidence_hash: str = Field(min_length=8, max_length=128)
    ownership_state: OwnershipState
    confidence: float = Field(default=1.0, ge=0, le=1)
    metadata: dict[str, str] = Field(default_factory=dict)


class TopologyPath(BaseModel):
    project_id: str
    nodes: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    relations: list[SourceRelation] = Field(default_factory=list, max_length=128)
    allocations: list[AllocationEvent] = Field(default_factory=list, max_length=64)
    truncated: bool = False
    fingerprint: str


class VaultStatusRead(BaseModel):
    root: str | None = None
    status: VaultStatus
    message: str
    allowed_roots: list[str] = Field(default_factory=list, max_length=16)


class VaultSettingsUpdate(BaseModel):
    root: str | None = Field(default=None, max_length=4000)


class VaultSettingsRead(VaultStatusRead):
    configured: bool = False


class KnowledgeDocumentLink(BaseModel):
    target_id: str | None = None
    target_path: str | None = None
    link_kind: str = Field(min_length=1, max_length=80)
    validation_status: str = Field(min_length=1, max_length=40)


class KnowledgeChunk(BaseModel):
    id: str = Field(min_length=8, max_length=160)
    document_id: str
    project_id: str
    intelligence_id: str | None = None
    ordinal: int = Field(ge=0)
    heading_path: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=12000)
    content_hash: str = Field(min_length=8, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
VAULT_ROOT_DIRNAME = "FirmSight-Vault"


class KnowledgeDocStatus(StrEnum):
    SYNCED = "SYNCED"
    EDITED = "EDITED"          # external edit applied to the database record
    STALE = "STALE"            # database changed after the document was written
    QUARANTINED = "QUARANTINED"
    MISSING = "MISSING"


class KnowledgeDocSource(StrEnum):
    GENERATED = "GENERATED"
    ENGINEER_EDITED = "ENGINEER_EDITED"


class KnowledgeScope(BaseModel):
    component: str | None = Field(default=None, max_length=200)
    symbols: list[str] = Field(default_factory=list, max_length=24)
    files: list[str] = Field(default_factory=list, max_length=24)
    functions: list[str] = Field(default_factory=list, max_length=24)


class KnowledgeRelationships(BaseModel):
    reviews: list[str] = Field(default_factory=list, max_length=24)
    findings: list[str] = Field(default_factory=list, max_length=24)
    resolutions: list[str] = Field(default_factory=list, max_length=24)


class KnowledgeProvenance(BaseModel):
    origin: str = Field(default="SYNTHESIZER", max_length=100)
    commits: list[str] = Field(default_factory=list, max_length=12)
    first_observed_at: str | None = None
    last_validated_at: str | None = None
    last_validated_commit: str | None = None


class KnowledgeFrontmatter(BaseModel):
    """Validated YAML frontmatter contract for every knowledge document."""

    id: str = Field(min_length=4, max_length=100, pattern=VAULT_DOCUMENT_ID_PATTERN)
    type: str = Field(min_length=3, max_length=60)
    project_id: str = Field(min_length=1, max_length=100)
    status: str = Field(min_length=3, max_length=40)
    confidence: float = Field(ge=0, le=1)
    observation_count: int = Field(ge=1, le=1_000_000)
    title: str = Field(min_length=1, max_length=300)
    statement: str = Field(min_length=4, max_length=6000)
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    relationships: KnowledgeRelationships = Field(default_factory=KnowledgeRelationships)
    provenance: KnowledgeProvenance = Field(default_factory=KnowledgeProvenance)
    tags: list[str] = Field(default_factory=list, max_length=24)
    schema_version: int = KNOWLEDGE_SCHEMA_VERSION


class KnowledgeDocumentRead(BaseModel):
    id: str
    project_id: str
    intelligence_id: str
    relative_path: str
    content_hash: str
    sync_status: str
    source_status: str
    schema_version: int
    external_modified_at: datetime | None = None
    last_indexed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentDetailRead(KnowledgeDocumentRead):
    frontmatter: dict[str, Any]
    body: str


class VaultSyncReport(BaseModel):
    scanned: int = 0
    unchanged: int = 0
    updated: int = 0
    regenerated: int = 0
    quarantined: int = 0
    stale: int = 0
    errors: list[str] = Field(default_factory=list, max_length=50)
    quarantined_paths: list[str] = Field(default_factory=list, max_length=50)
    # FS-KB-015: legacy layout reconciliation status. `legacy_left_in_place`
    # counts generated legacy-layout documents that still exist on disk because
    # FirmSight never deletes user-owned files automatically. `migrated` counts
    # legacy generated documents whose stable frontmatter validated and were
    # re-projected into the five-area layout during this sync. `warnings`
    # carries non-fatal migration notices: they never block RAG indexing of the
    # canonical documents and never flip the durable sync status to FAILED.
    warnings: list[str] = Field(default_factory=list, max_length=50)
    legacy_left_in_place: int = 0
    migrated: int = 0
    skipped: list[str] = Field(default_factory=list, max_length=50)


class VaultSyncStateRead(BaseModel):
    """Durable last-sync outcome for one project's vault projection (FS-KB-015).

    ``INDEX_FAILED`` marks a completed projection whose incremental indexing
    failed or was unavailable; the vault remains available and the index is
    retryable from the Knowledge Base UI (REV-032).
    """

    project_id: str
    status: str
    counts: dict[str, int] = Field(default_factory=dict)
    error_summary: str | None = None
    updated_at: datetime


class KnowledgeRetryOutcome(BaseModel):
    """Typed, safe result of the atomic knowledge retry operation (REV-033).

    One backend call performs vault projection and incremental indexing as a
    single locked outcome, so the frontend never has to infer success/failure
    from two unrelated requests.

    - ``vault``: ``SYNCED`` (projection completed), ``FAILED`` (hard projection
      error/quarantine), ``SKIPPED`` (no vault configured), ``UNAVAILABLE``
      (projection raised).
    - ``index``: ``SYNCED`` (indexed), ``INDEX_FAILED`` (index unavailable),
      ``SKIPPED`` (no indexer configured), ``NOT_RUN`` (indexing was not
      attempted, e.g. hard projection error).
    - ``status``: the durable sync-state status that was persisted for this
      operation (``SYNCED`` / ``FAILED`` / ``INDEX_FAILED`` / ``SKIPPED``).
    - ``message``: a fixed, bounded, non-content-bearing summary suitable for
      display. It never contains raw exception text, source, prompts, provider
      output, reasoning, keys, or credentials.
    """

    project_id: str
    status: str
    vault: str
    index: str
    message: str


class KnowledgeIndexReport(BaseModel):
    documents_indexed: int = 0
    chunks_written: int = 0
    chunks_removed: int = 0
    noop: bool = False
    index_version: int = 1
    errors: list[str] = Field(default_factory=list, max_length=50)


class RetrievalQuery(BaseModel):
    project_id: str = Field(min_length=1, max_length=100)
    query: str = Field(default="", max_length=2000)
    symbols: list[str] = Field(default_factory=list, max_length=24)
    files: list[str] = Field(default_factory=list, max_length=24)
    component: str | None = Field(default=None, max_length=200)
    cap: int = Field(default=6, ge=1, le=25)


class RetrievalMatch(BaseModel):
    intelligence_id: str
    document_id: str
    chunk_id: str | None = None
    heading_path: str | None = None
    statement: str
    type: str
    state: str
    confidence: float
    score: float
    match_signals: list[str] = Field(default_factory=list, max_length=12)
    lifecycle_warning: str | None = None
    related_ids: list[str] = Field(default_factory=list, max_length=24)
    relative_path: str | None = None


class RetrievalResult(BaseModel):
    matches: list[RetrievalMatch] = Field(default_factory=list)
    used_semantic: bool = False
    used_fts: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=12)


class IntelligenceCorrectionRequest(BaseModel):
    statement: str = Field(min_length=8, max_length=6000)
    reason: str = Field(min_length=4, max_length=2000)


class KnowledgeSearchResult(BaseModel):
    matches: list[RetrievalMatch]
    warnings: list[str] = Field(default_factory=list, max_length=12)
