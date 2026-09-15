from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .ai_provider import AIProvider
from .environment import load_local_environment
from .indexer import FirmwareIndexer
from .interaction_service import ChatService, YamlService
from .intelligence_schemas import IntelligenceDetailRead, IntelligenceDisableRequest, IntelligenceListResponse, IntelligenceRecordRead, IntelligenceRevalidateResponse, IntelligenceSummaryResponse, ReviewLearningSummaryRead
from .intelligence_service import IntelligenceService
from .repository import MemoryRepository
from .platform_repository import PlatformRepository
from .schemas import (
    ContextResponse, MemoryApproval, MemoryProposalCreate, MemoryRead, MemoryStatus,
    MemoryProposalUpdate, MemoryUpdate, ProposalRead, RevalidationEvent, RevalidationRequest,
    RevalidationResponse,
)
from .platform_schemas import (
    ChatMessageRead, ChatRequest, ChatResponse, FindingDecisionUpdate, FindingFixVerificationRequest, FindingRead, FindingResolutionUpdate, IndexRead, ProjectSourceSyncRead,
    AISettingsRead, AISettingsUpdate, ProjectCreate, ProjectDirectoryImport, ProjectFileContent, ProjectFileRead, ProjectFilesUpload, ProjectRead, ProjectSourceSyncRequest,
    ReviewCreate, ReviewRead, SymbolRead, YamlGenerateRequest, YamlRead, YamlValidateRequest,
)
from .project_service import ProjectService
from .review_service import ReviewService
from .settings_service import SettingsService
from .service import MemoryService


load_local_environment(Path(__file__).parents[1] / ".env")


def create_app(database_path: str | None = None, import_root: str | None = None, provider_resolver: Callable[[str], AIProvider] | None = None) -> FastAPI:
    app = FastAPI(title="FirmSight", version="0.1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])
    resolved_database = database_path or os.getenv("FIRMSIGHT_DATABASE", "./data/firmsight.db")
    memory_repository = MemoryRepository(resolved_database)
    memory_service = MemoryService(memory_repository)
    platform_repository = PlatformRepository(resolved_database)
    settings_service = SettingsService(platform_repository)
    projects = ProjectService(platform_repository, FirmwareIndexer(), import_root, memory_repository=memory_repository)
    resolve_provider = provider_resolver or settings_service.provider
    intelligence = IntelligenceService(memory_repository, platform_repository, projects, resolve_provider)
    reviews = ReviewService(
        platform_repository,
        projects,
        resolve_provider,
        memory_service,
        intelligence=intelligence,
        review_context_resolver=lambda: settings_service.config().review_context_chars,
        review_parallel_resolver=lambda: settings_service.config().review_parallel_requests,
    )
    chat = ChatService(platform_repository, projects, reviews, memory_service, lambda: resolve_provider("chat"), intelligence=intelligence)
    yaml = YamlService(platform_repository, projects, resolve_provider)

    def get_service() -> MemoryService:
        return memory_service

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings", response_model=AISettingsRead)
    def settings() -> AISettingsRead:
        return settings_service.read()

    @app.put("/api/settings", response_model=AISettingsRead)
    def update_settings(update: AISettingsUpdate) -> AISettingsRead:
        return settings_service.update(update)

    @app.get("/api/projects", response_model=list[ProjectRead])
    def list_projects() -> list[ProjectRead]:
        return projects.list()

    @app.post("/api/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
    def create_project(request: ProjectCreate) -> ProjectRead:
        return projects.create(request)

    @app.post("/api/projects/import-directory", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
    def import_project_directory(request: ProjectDirectoryImport) -> ProjectRead:
        return projects.import_directory(request)

    @app.get("/api/projects/{project_id}", response_model=ProjectRead)
    def get_project(project_id: str) -> ProjectRead:
        return projects.get(project_id)

    @app.delete("/api/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_project(project_id: str) -> Response:
        # Removes only this project's persisted FirmSight records; the engineer's
        # local firmware directory is never touched. 409 while background work runs.
        projects.delete(project_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/projects/{project_id}/files", response_model=IndexRead)
    def import_project_files(project_id: str, request: ProjectFilesUpload) -> IndexRead:
        projects.add_files(project_id, request.files)
        return projects.index(project_id)

    @app.post("/api/projects/{project_id}/index", response_model=IndexRead)
    def index_project(project_id: str) -> IndexRead:
        return projects.index(project_id)

    @app.post("/api/projects/{project_id}/sync-source", response_model=ProjectSourceSyncRead)
    def sync_project_source(project_id: str, request: ProjectSourceSyncRequest = ProjectSourceSyncRequest()) -> ProjectSourceSyncRead:
        indexed, changed_files = projects.refresh_local_directory(project_id, request.directory)
        # Source authority: stale intelligence is marked deterministically
        # right after the index refresh. Failures never fail the sync.
        try:
            intelligence.deterministic_source_revalidation(project_id)
        except Exception:  # noqa: BLE001 - revalidation is best-effort
            pass
        return ProjectSourceSyncRead(**indexed.model_dump(), changed_files=changed_files)

    @app.get("/api/projects/{project_id}/files", response_model=list[ProjectFileRead])
    def project_files(project_id: str) -> list[ProjectFileRead]:
        return [ProjectFileRead.model_validate(item) for item in projects.files(project_id)]

    @app.get("/api/projects/{project_id}/files/content", response_model=ProjectFileContent)
    def project_file(project_id: str, path: str) -> ProjectFileContent:
        return ProjectFileContent.model_validate(projects.file(project_id, path))

    @app.get("/api/projects/{project_id}/symbols", response_model=list[SymbolRead])
    def project_symbols(project_id: str, search: str | None = None) -> list[SymbolRead]:
        return projects.symbols(project_id, search)

    @app.post("/api/projects/{project_id}/reviews", response_model=ReviewRead, status_code=status.HTTP_201_CREATED)
    def start_review(project_id: str, request: ReviewCreate, background_tasks: BackgroundTasks) -> ReviewRead:
        review = reviews.begin(project_id, request)
        background_tasks.add_task(reviews.execute, review.id)
        return review

    @app.get("/api/projects/{project_id}/reviews/{review_id}", response_model=ReviewRead)
    def get_review(project_id: str, review_id: str) -> ReviewRead:
        review = reviews.get(review_id)
        if review.project_id != project_id:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
        return review

    @app.post("/api/projects/{project_id}/reviews/{review_id}/retry", response_model=ReviewRead)
    def retry_review(project_id: str, review_id: str, background_tasks: BackgroundTasks) -> ReviewRead:
        review = reviews.get(review_id)
        if review.project_id != project_id:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Review not found")
        resumed = reviews.retry(review_id)
        background_tasks.add_task(reviews.execute, resumed.id)
        return resumed

    @app.get("/api/projects/{project_id}/findings", response_model=list[FindingRead])
    def findings(project_id: str) -> list[FindingRead]:
        return reviews.findings(project_id)

    @app.get("/api/projects/{project_id}/findings/{finding_id}", response_model=FindingRead)
    def finding(project_id: str, finding_id: str) -> FindingRead:
        return reviews.finding(project_id, finding_id)

    @app.patch("/api/projects/{project_id}/findings/{finding_id}/decision", response_model=FindingRead)
    def decide_finding(project_id: str, finding_id: str, update: FindingDecisionUpdate, background_tasks: BackgroundTasks) -> FindingRead:
        finding = reviews.decide(project_id, finding_id, update)
        # Learning triggers queue only after the decision is durable; the
        # API result is already final if learning later fails.
        intelligence.enqueue_decision_learning(project_id, finding.id, finding.decision, finding.decision_reason)
        background_tasks.add_task(intelligence.drain_pending_jobs, project_id)
        return finding

    @app.patch("/api/projects/{project_id}/findings/{finding_id}/resolution", response_model=FindingRead)
    def resolve_finding(project_id: str, finding_id: str, update: FindingResolutionUpdate, background_tasks: BackgroundTasks) -> FindingRead:
        finding = reviews.resolve(project_id, finding_id, update)
        intelligence.enqueue_resolution_learning(project_id, finding.id, finding.resolution)
        background_tasks.add_task(intelligence.drain_pending_jobs, project_id)
        return finding

    @app.post("/api/projects/{project_id}/findings/{finding_id}/verify-fix", response_model=FindingRead)
    def verify_finding_fix(project_id: str, finding_id: str, background_tasks: BackgroundTasks, request: FindingFixVerificationRequest = FindingFixVerificationRequest()) -> FindingRead:
        finding = reviews.verify_fix(project_id, finding_id, request.directory)
        intelligence.enqueue_fix_verification_learning(project_id, finding.id, finding.resolution)
        background_tasks.add_task(intelligence.drain_pending_jobs, project_id)
        return finding

    @app.get("/api/projects/{project_id}/chat", response_model=list[ChatMessageRead])
    def chat_messages(project_id: str) -> list[ChatMessageRead]:
        return chat.messages(project_id)

    @app.post("/api/projects/{project_id}/chat", response_model=ChatResponse)
    def ask_chat(project_id: str, request: ChatRequest, background_tasks: BackgroundTasks) -> ChatResponse:
        response = chat.ask(project_id, request)
        background_tasks.add_task(intelligence.drain_pending_jobs, project_id)
        return response

    @app.get("/api/projects/{project_id}/yaml", response_model=YamlRead | None)
    def latest_yaml(project_id: str) -> YamlRead | None:
        return yaml.latest(project_id)

    @app.post("/api/projects/{project_id}/yaml/generate", response_model=YamlRead)
    def generate_yaml(project_id: str, request: YamlGenerateRequest) -> YamlRead:
        return yaml.generate(project_id, request)

    @app.post("/api/projects/{project_id}/yaml/validate", response_model=YamlRead)
    def validate_yaml(project_id: str, request: YamlValidateRequest) -> YamlRead:
        return yaml.validate(request).model_copy(update={"project_id": project_id})

    web_dist = Path(__file__).parents[1] / "apps" / "web" / "dist"
    if (web_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="web-assets")

    @app.get("/", response_model=None)
    def web_application() -> FileResponse | HTMLResponse:
        index = web_dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse("<p>FirmSight web client is not built. Run <code>npm run dev</code> from <code>apps/web</code>.</p>")

    @app.post("/api/projects/{project_id}/memory/proposals", response_model=ProposalRead, status_code=status.HTTP_201_CREATED)
    def propose_memory(project_id: str, proposal: MemoryProposalCreate, memory_service: MemoryService = Depends(get_service)) -> ProposalRead:
        return memory_service.propose(project_id, proposal)

    @app.patch("/api/memory/proposals/{proposal_id}", response_model=ProposalRead)
    def update_proposal(proposal_id: str, update: MemoryProposalUpdate, memory_service: MemoryService = Depends(get_service)) -> ProposalRead:
        return memory_service.update_proposal(proposal_id, update)

    @app.delete("/api/memory/proposals/{proposal_id}", status_code=status.HTTP_204_NO_CONTENT)
    def ignore_proposal(proposal_id: str, memory_service: MemoryService = Depends(get_service)) -> None:
        memory_service.ignore_proposal(proposal_id)

    @app.post("/api/memory/proposals/{proposal_id}/approve", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
    def approve_memory(proposal_id: str, approval: MemoryApproval, memory_service: MemoryService = Depends(get_service)) -> MemoryRead:
        return memory_service.approve(proposal_id, approval)

    @app.get("/api/projects/{project_id}/memory", response_model=list[MemoryRead])
    def list_memory(project_id: str, memory_status: MemoryStatus | None = Query(default=None, alias="status"), memory_service: MemoryService = Depends(get_service)) -> list[MemoryRead]:
        return memory_service.list(project_id, memory_status)

    @app.get("/api/memory/{memory_id}", response_model=MemoryRead)
    def get_memory(memory_id: str, memory_service: MemoryService = Depends(get_service)) -> MemoryRead:
        return memory_service.get(memory_id)

    @app.patch("/api/memory/{memory_id}", response_model=MemoryRead)
    def update_memory(memory_id: str, update: MemoryUpdate, memory_service: MemoryService = Depends(get_service)) -> MemoryRead:
        return memory_service.update(memory_id, update)

    @app.post("/api/memory/{memory_id}/disable", response_model=MemoryRead)
    def disable_memory(memory_id: str, memory_service: MemoryService = Depends(get_service)) -> MemoryRead:
        return memory_service.disable(memory_id)

    @app.get("/api/memory/{memory_id}/revalidation-events", response_model=list[RevalidationEvent])
    def revalidation_history(memory_id: str, memory_service: MemoryService = Depends(get_service)) -> list[RevalidationEvent]:
        return memory_service.revalidation_history(memory_id)

    @app.get("/api/projects/{project_id}/memory/context", response_model=ContextResponse)
    def memory_context(project_id: str, symbol: str | None = None, component: str | None = None, memory_service: MemoryService = Depends(get_service)) -> ContextResponse:
        return memory_service.context(project_id, symbol, component)

    @app.post("/api/projects/{project_id}/memory/revalidate", response_model=RevalidationResponse)
    def revalidate_memory(project_id: str, request: RevalidationRequest, memory_service: MemoryService = Depends(get_service)) -> RevalidationResponse:
        return memory_service.revalidate(project_id, request)

    # --- Project Intelligence APIs (typed, source-authoritative) ---

    @app.get("/api/projects/{project_id}/intelligence", response_model=IntelligenceListResponse)
    def list_intelligence(project_id: str, state: str | None = None, type: str | None = None, limit: int = Query(default=200, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> IntelligenceListResponse:
        projects.get(project_id)
        return intelligence.list_records(project_id, state, type, limit, offset)

    @app.get("/api/projects/{project_id}/intelligence/summary", response_model=IntelligenceSummaryResponse)
    def intelligence_summary(project_id: str) -> IntelligenceSummaryResponse:
        projects.get(project_id)
        return intelligence.summary(project_id)

    @app.get("/api/intelligence/{memory_id}", response_model=IntelligenceDetailRead)
    def get_intelligence(memory_id: str) -> IntelligenceDetailRead:
        return intelligence.detail(memory_id)

    @app.post("/api/intelligence/{memory_id}/revalidate", response_model=IntelligenceRevalidateResponse)
    def revalidate_intelligence(memory_id: str) -> IntelligenceRevalidateResponse:
        record = intelligence.repository.get_memory(memory_id)
        if not record:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Intelligence record not found")
        return intelligence.revalidate_record(record["project_id"], memory_id)

    @app.post("/api/intelligence/{memory_id}/disable", response_model=IntelligenceRecordRead)
    def disable_intelligence(memory_id: str, request: IntelligenceDisableRequest = IntelligenceDisableRequest()) -> IntelligenceRecordRead:
        return intelligence.disable(memory_id, request.reason)

    @app.get("/api/projects/{project_id}/reviews/{review_id}/learning-summary", response_model=ReviewLearningSummaryRead)
    def review_learning_summary(project_id: str, review_id: str) -> ReviewLearningSummaryRead:
        return intelligence.learning_summary(project_id, review_id)

    return app


app = create_app()
