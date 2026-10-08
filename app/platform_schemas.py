from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ProjectSourceType(StrEnum):
    ARCHIVE = "ARCHIVE"
    LOCAL_DIRECTORY = "LOCAL_DIRECTORY"
    MANUAL = "MANUAL"


class FindingClassification(StrEnum):
    CONFIRMED_BUG = "CONFIRMED_BUG"
    PROBABLE_BUG = "PROBABLE_BUG"
    DESIGN_RISK = "DESIGN_RISK"
    SUGGESTION = "SUGGESTION"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingDecision(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INTENTIONAL = "INTENTIONAL"
    NEEDS_MORE_EVIDENCE = "NEEDS_MORE_EVIDENCE"


class FindingResolution(StrEnum):
    OPEN = "OPEN"
    SOLVED = "SOLVED"


class FindingRemediationStatus(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    VERIFIED_FIXED = "VERIFIED_FIXED"
    STILL_PRESENT = "STILL_PRESENT"
    INCONCLUSIVE = "INCONCLUSIVE"
    MANUALLY_MARKED = "MANUALLY_MARKED"


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    source_type: ProjectSourceType = ProjectSourceType.MANUAL


class ProjectDirectoryImport(BaseModel):
    directory: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class ProjectSourceSyncRequest(BaseModel):
    directory: str | None = Field(default=None, min_length=1, max_length=4000)


class ProjectRead(BaseModel):
    id: str
    name: str
    description: str
    source_type: ProjectSourceType
    language: str | None
    framework: str | None
    target: str | None
    build_system: str | None
    file_count: int
    symbol_count: int
    source_sync_available: bool = False
    created_at: datetime
    updated_at: datetime


class ProjectFileRead(BaseModel):
    path: str
    language: str
    size: int


class ProjectFileContent(ProjectFileRead):
    content: str


class ProjectFilesUpload(BaseModel):
    files: dict[str, str] = Field(min_length=1, max_length=500)


class SymbolRead(BaseModel):
    name: str
    kind: str
    file: str
    line: int


class TopologySymbolRead(BaseModel):
    id: str
    project_id: str
    name: str
    kind: str
    file: str
    line_start: int
    line_end: int
    signature: str = ""
    component: str | None = None
    source_hash: str
    confidence: float = Field(ge=0, le=1)


class TopologyRelationRead(BaseModel):
    id: str
    project_id: str
    relation_kind: str
    source_symbol_id: str | None = None
    target_symbol_id: str | None = None
    target_name: str | None = None
    file: str
    line: int
    evidence_hash: str
    confidence: float = Field(ge=0, le=1)
    relation_state: str
    metadata: dict[str, str] = Field(default_factory=dict)


class AllocationEventRead(BaseModel):
    id: str
    project_id: str
    symbol_id: str | None = None
    variable: str | None = None
    event_kind: str
    allocator_or_releaser: str
    file: str
    line: int
    evidence_hash: str
    ownership_state: str
    confidence: float = Field(ge=0, le=1)
    metadata: dict[str, str] = Field(default_factory=dict)


class TopologyRead(BaseModel):
    symbols: list[TopologySymbolRead] = Field(default_factory=list)
    relations: list[TopologyRelationRead] = Field(default_factory=list)
    allocations: list[AllocationEventRead] = Field(default_factory=list)
    snapshot: dict[str, str] | None = None


class TopologyPathRead(BaseModel):
    nodes: list[TopologySymbolRead] = Field(default_factory=list)
    relations: list[TopologyRelationRead] = Field(default_factory=list)
    allocations: list[AllocationEventRead] = Field(default_factory=list)
    truncated: bool = False
    fingerprint: str = ""


class IndexRead(BaseModel):
    project_id: str
    file_count: int
    symbol_count: int
    language: str | None
    framework: str | None
    target: str | None
    build_system: str | None


class ProjectSourceSyncRead(IndexRead):
    """Result of reading the persisted local project directory again."""

    changed_files: list[str] = Field(default_factory=list)


class ReviewCreate(BaseModel):
    scope: str = Field(default="Full Project", max_length=100)
    focus: list[str] = Field(default_factory=lambda: ["memory", "concurrency", "freertos", "error_handling"])


class ReviewTokenUsage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class ReviewExecutionProgress(BaseModel):
    """Structured, persisted progress for a review run."""
    phase: str = "PLANNING"
    total_units: int = Field(default=0, ge=0)
    completed_units: int = Field(default=0, ge=0)
    reused_units: int = Field(default=0, ge=0)
    unavailable_units: int = Field(default=0, ge=0)
    in_flight_requests: int = Field(default=0, ge=0)
    parallel_request_limit: int = Field(default=1, ge=1, le=3)
    current_units: list[str] = Field(default_factory=list, max_length=8)


OutputBudget = int | Literal["PROVIDER_DEFAULT"]


class ReviewOutputBudgetSnapshot(BaseModel):
    """Safe historical output-budget metadata; no provider content is stored."""
    investigator: OutputBudget = 2_000
    verifier: OutputBudget = 1_200
    verifier_fix: int = Field(default=1_200, ge=800, le=2_000)


class StructuredOutputMode(StrEnum):
    PROMPT_ONLY = "PROMPT_ONLY"
    JSON_OBJECT = "JSON_OBJECT"
    JSON_SCHEMA = "JSON_SCHEMA"


class StructuredFinalizationPolicy(StrEnum):
    """Bounded structured-finalization recovery behavior for reasoning providers."""

    AUTO = "AUTO"
    ALWAYS = "ALWAYS"
    NEVER = "NEVER"


class ReasoningEffort(StrEnum):
    UNSPECIFIED = "UNSPECIFIED"
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ReviewDiagnosticRead(BaseModel):
    id: int
    review_id: str
    request_id: str
    operation: str
    role: str
    batch_number: int | None = None
    total_batches: int | None = None
    file_count: int = 0
    state: str
    attempt: int = 1
    execution_attempt: int = 1
    repair_attempted: bool = False
    provider: str
    model: str
    endpoint: str
    created_at: datetime
    elapsed_ms: int | None = None
    http_status: int | None = None
    content_type: str | None = None
    request_chars: int | None = None
    response_chars: int | None = None
    usage: ReviewTokenUsage | None = None
    error_kind: str | None = None
    error_message: str | None = None
    content_state: str | None = None
    finish_reason: str | None = None
    structured_mode: str | None = None
    finalization_policy: str | None = None
    finalization_recovery: bool = False
    effective_max_tokens: int | None = None
    validation_category: str | None = None
    validation_fields: list[str] = Field(default_factory=list)
    retry_suppressed: bool = False


class ReviewUnitState(BaseModel):
    state: Literal["COMPLETED", "UNAVAILABLE"]
    investigator_validated: bool = False


class ReviewRead(BaseModel):
    id: str
    project_id: str
    scope: str
    focus: list[str]
    context_files: list[str] = Field(default_factory=list)
    context_chars: int = 42_000
    source_snapshot_hash: str | None = None
    total_batches: int = Field(default=0, ge=0)
    validated_batches: int = Field(default=0, ge=0)
    unavailable_batches: int = Field(default=0, ge=0)
    status: str
    progress: list[str]
    error: str | None = None
    finding_count: int
    created_at: datetime
    completed_at: datetime | None
    last_activity_at: datetime | None = None
    diagnostics: list[ReviewDiagnosticRead] = Field(default_factory=list)
    execution_progress: ReviewExecutionProgress = Field(default_factory=ReviewExecutionProgress)
    execution_attempt: int = Field(default=1, ge=1)
    unit_states: dict[str, ReviewUnitState] = Field(default_factory=dict, exclude=True)
    output_budget_snapshot: ReviewOutputBudgetSnapshot = Field(default_factory=ReviewOutputBudgetSnapshot)


class FindingLocation(BaseModel):
    file: str
    function: str | None = None
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class FindingEvidence(BaseModel):
    description: str
    file: str
    line: int = Field(ge=1)


class FindingAssumption(BaseModel):
    statement: str
    status: str


class FindingVerification(BaseModel):
    status: str
    notes: str


class FindingRemediation(BaseModel):
    status: FindingRemediationStatus = FindingRemediationStatus.UNVERIFIED
    notes: str = "No source recheck has been run."
    verified_at: datetime | None = None
    source_refreshed: bool = False
    changed_files: list[str] = Field(default_factory=list)


class FindingRead(BaseModel):
    id: str
    project_id: str
    review_id: str
    title: str
    classification: FindingClassification
    severity: Severity
    category: str
    confidence: float = Field(ge=0, le=1)
    location: FindingLocation
    summary: str
    evidence: list[FindingEvidence]
    execution_path: list[str]
    runtime_scenario: str
    impact: str
    assumptions: list[FindingAssumption]
    recommendation: str
    verification: FindingVerification
    decision: FindingDecision
    topology_path: list[dict[str, object]] = Field(default_factory=list)
    lifetime_evidence: list[dict[str, object]] = Field(default_factory=list)
    decision_reason: str | None
    resolution: FindingResolution = FindingResolution.OPEN
    resolved_at: datetime | None = None
    remediation: FindingRemediation = Field(default_factory=FindingRemediation)
    created_at: datetime


class FindingCandidate(BaseModel):
    title: str = Field(min_length=8, max_length=300)
    classification: FindingClassification
    severity: Severity
    category: str = Field(min_length=2, max_length=100)
    confidence: float = Field(ge=0, le=1)
    location: FindingLocation
    summary: str = Field(min_length=20, max_length=3000)
    evidence: list[FindingEvidence] = Field(min_length=1, max_length=8)
    execution_path: list[str] = Field(min_length=2, max_length=20)
    runtime_scenario: str = Field(min_length=20, max_length=3000)
    impact: str = Field(min_length=10, max_length=2000)
    assumptions: list[FindingAssumption] = Field(default_factory=list, max_length=10)
    recommendation: str = Field(min_length=10, max_length=3000)


class InvestigatorResult(BaseModel):
    findings: list[FindingCandidate] = Field(default_factory=list, max_length=8)


class VerifierResult(BaseModel):
    verdict: str = Field(pattern="^(SURVIVES|REJECTED|NEEDS_MORE_EVIDENCE)$")
    notes: str = Field(min_length=8, max_length=3000)
    remaining_assumptions: list[FindingAssumption] = Field(default_factory=list, max_length=10)


class ReviewUnitSegment(BaseModel):
    """Safe identity for one source segment; source content is never persisted."""
    file: str = Field(min_length=1, max_length=4000)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)


class ReviewCacheEnvelope(BaseModel):
    """Only schema/evidence validated outcomes may be stored in the unit cache."""
    schema_version: int = 1
    kind: Literal["VALIDATED_EMPTY", "VALIDATED_FINDINGS"]
    unit_id: str = Field(min_length=1, max_length=128)
    segments: list[ReviewUnitSegment] = Field(min_length=1, max_length=128)
    investigator: InvestigatorResult
    verifications: list[VerifierResult] = Field(default_factory=list, max_length=8)


class FixVerificationResult(BaseModel):
    verdict: str = Field(pattern="^(FIXED|STILL_PRESENT|INCONCLUSIVE)$")
    notes: str = Field(min_length=8, max_length=3000)


class YamlGenerationResult(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


class FindingDecisionUpdate(BaseModel):
    decision: FindingDecision
    reason: str | None = Field(default=None, max_length=5000)
    propose_memory: bool = False


class FindingResolutionUpdate(BaseModel):
    resolution: FindingResolution


class FindingFixVerificationRequest(BaseModel):
    directory: str | None = Field(default=None, min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    selected_file: str | None = Field(default=None, max_length=1000)
    finding_id: str | None = Field(default=None, max_length=100)


class ChatMessageRead(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime


class ChatResponse(BaseModel):
    message: ChatMessageRead
    context_summary: list[str]


class YamlGenerateRequest(BaseModel):
    description: str = Field(min_length=1, max_length=10000)
    focus: list[str] = Field(default_factory=list)


class YamlRead(BaseModel):
    project_id: str
    content: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    generated_at: datetime


class YamlValidateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


class AISettingsUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=8, max_length=1000)
    models: dict[str, str] = Field(min_length=1, max_length=10)
    language: str | None = Field(default=None, min_length=2, max_length=8)
    review_context_chars: int | None = Field(default=None)
    structured_output_mode: StructuredOutputMode | None = None
    reasoning_effort: ReasoningEffort | None = None
    structured_finalization_policy: StructuredFinalizationPolicy | None = None
    investigator_max_tokens: OutputBudget | None = None
    verifier_max_tokens: OutputBudget | None = None
    review_parallel_requests: int | None = Field(default=None, ge=1, le=3)


class AISettingsRead(BaseModel):
    provider: str
    endpoint: str
    api_key_configured: bool
    api_key_masked: str | None
    api_key_environment: str
    models: dict[str, str]
    language: str = "en"
    review_context_chars: int
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.PROMPT_ONLY
    reasoning_effort: ReasoningEffort = ReasoningEffort.UNSPECIFIED
    structured_finalization_policy: StructuredFinalizationPolicy = StructuredFinalizationPolicy.AUTO
    investigator_max_tokens: OutputBudget = 2000
    verifier_max_tokens: OutputBudget = 1200
    review_parallel_requests: int = 2
