from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .ai_provider import AIProvider
from .environment import load_local_environment
from .i18n import normalize_locale
from .indexer import FirmwareIndexer
from .interaction_service import YamlService
from .context_builder import ContextBuilder
from .flow_service import FlowService
from .knowledge_base_service import KnowledgeBaseService
from .knowledge_index_service import KnowledgeIndexService
from .knowledge_retriever import KnowledgeRetriever
from .intelligence_schemas import IntelligenceCorrectionRequest, IntelligenceDetailRead, IntelligenceDisableRequest, IntelligenceListResponse, IntelligenceRecordRead, IntelligenceRevalidateResponse, IntelligenceSummaryResponse, ReviewLearningSummaryRead
from .knowledge_schemas import KnowledgeDocumentDetailRead, KnowledgeDocumentRead, KnowledgeIndexReport, KnowledgeRetryOutcome, KnowledgeSearchResult, RetrievalQuery, RetrievalResult, RetrievalTraceList, RetrievalTraceRead, RelationshipGraph, RelationshipNode, RelationshipEdge, VaultSettingsRead, VaultSettingsUpdate, VaultSyncReport, VaultSyncStateRead
from .intelligence_service import IntelligenceService
from .repository import MemoryRepository
from .platform_repository import PlatformRepository
from .schemas import (
    ContextResponse, MemoryApproval, MemoryProposalCreate, MemoryRead, MemoryStatus,
    MemoryProposalUpdate, MemoryUpdate, ProposalRead, RevalidationEvent, RevalidationRequest,
    RevalidationResponse,
)
from .platform_schemas import (
    FindingDecisionUpdate, FindingFixVerificationRequest, FindingRead, FindingResolutionUpdate, IndexRead, ProjectSourceSyncRead,
    AISettingsRead, AISettingsUpdate, ProjectCreate, ProjectDirectoryImport, ProjectFileContent, ProjectFileRead, ProjectFilesUpload, ProjectRead, ProjectSourceSyncRequest,
    ReviewCreate, ReviewRead, SymbolRead, TopologyPathRead, TopologyRead, TopologyRelationRead, TopologySymbolRead, AllocationEventRead, YamlGenerateRequest, YamlRead, YamlValidateRequest,
    FlowIndexRead, FlowScenarioRead, FlowNodeRead, FlowResourceRead, SharedStateRead,
)
from .project_service import ProjectService, TopologyDerivationError
from .review_service import ReviewService
from .settings_service import SettingsService
from .service import MemoryService


load_local_environment(Path(__file__).parents[1] / ".env")


#: Fixed, non-content-bearing message returned by the atomic retry route when the
#: workflow could not persist its final durable state (REV-038). It never carries
#: raw exception text, source, provider output, reasoning, keys, or credentials.
PERSISTENCE_UNAVAILABLE_MESSAGE = "Knowledge retry outcome could not be recorded persistently; the knowledge base state may be stale. Retry from the Knowledge Base."

#: Fixed, non-content-bearing message for a server-side topology derivation
#: failure (FS-FIX-014). The route returns it as safe JSON instead of leaking a
#: raw ``sqlite3.IntegrityError``/traceback as an opaque HTTP 500. It never
#: carries source paths, directory names, SQL details, or exception text, and it
#: is valid for import, reindex, and local source sync (in every case the prior
#: persisted source and topology are left untouched).
TOPOLOGY_DERIVATION_FAILED_MESSAGE = "The project topology could not be derived consistently. No source or topology changes were applied; retry after checking the source."
TOPOLOGY_DERIVATION_FAILED_KEY = "error.topology_derivation_failed"


def create_app(database_path: str | None = None, import_root: str | None = None, provider_resolver: Callable[[str], AIProvider] | None = None, knowledge_index_override: KnowledgeIndexService | None = None) -> FastAPI:
    app = FastAPI(title="FirmSight", version="0.1.0")

    # Settings exist only after the repository is constructed; the closure below
    # resolves the UI language lazily so early startup errors stay English.
    settings_service: SettingsService | None = None

    def _locale() -> str:
        try:
            return normalize_locale(settings_service.config().language) if settings_service else "en"
        except Exception:  # noqa: BLE001 - error rendering must never raise
            return "en"

    def _t(key: str, locale: str | None = None) -> str:
        from .i18n import message
        return message(key, locale or _locale())

    def _topology_detail(locale: str) -> str:
        return _t(TOPOLOGY_DERIVATION_FAILED_KEY, locale)

    @app.exception_handler(TopologyDerivationError)
    async def _topology_derivation_error_handler(_request: Request, _exc: TopologyDerivationError) -> JSONResponse:
        # FS-FIX-014: narrow, server-side derivation failure. Return a stable,
        # safe JSON error (never the exception text) and keep user-input
        # validation statuses unchanged. The service already rolled back any
        # partial import before this handler runs.
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": _topology_detail(_locale())})

    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])
    resolved_database = database_path or os.getenv("FIRMSIGHT_DATABASE", "./data/firmsight.db")
    memory_repository = MemoryRepository(resolved_database)
    memory_service = MemoryService(memory_repository, language_resolver=lambda: settings_service.config().language)
    platform_repository = PlatformRepository(resolved_database)
    settings_service = SettingsService(platform_repository)
    projects = ProjectService(platform_repository, FirmwareIndexer(), import_root, memory_repository=memory_repository, language_resolver=lambda: settings_service.config().language)
    knowledge_base = KnowledgeBaseService(memory_repository, platform_repository, settings_service)
    knowledge_index = knowledge_index_override or KnowledgeIndexService(memory_repository)
    knowledge_retriever = KnowledgeRetriever(memory_repository, platform_repository, language_resolver=lambda: settings_service.config().language)
    flow_service = FlowService(platform_repository)
    context_builder = ContextBuilder(platform_repository, knowledge_retriever, projects.lifetime_service, flow_service)
    resolve_provider = provider_resolver or settings_service.provider
    intelligence = IntelligenceService(memory_repository, platform_repository, projects, resolve_provider, language_resolver=lambda: settings_service.config().language, context_builder=context_builder)
    reviews = ReviewService(
        platform_repository,
        projects,
        resolve_provider,
        memory_service,
        intelligence=intelligence,
        review_context_resolver=lambda: settings_service.config().review_context_chars,
        review_parallel_resolver=lambda: settings_service.config().review_parallel_requests,
        context_builder=context_builder,
        flow_service=flow_service,
        knowledge_base=knowledge_base,
        knowledge_index=knowledge_index,
        language_resolver=lambda: settings_service.config().language,
    )
    yaml = YamlService(platform_repository, projects, resolve_provider, language_resolver=lambda: settings_service.config().language)

    # Live service handles for tests and diagnostics: they are the exact instances
    # the routes and the automatic post-review workflow use, so a test seam (for
    # example a gated indexer) is shared by the retry route and the automatic
    # workflow and can prove they serialize on one per-project lock (REV-037).
    app.state.services = SimpleNamespace(
        memory=memory_service,
        memory_repository=memory_repository,
        platform=platform_repository,
        settings=settings_service,
        projects=projects,
        knowledge_base=knowledge_base,
        knowledge_index=knowledge_index,
        knowledge_retriever=knowledge_retriever,
        context_builder=context_builder,
        intelligence=intelligence,
        reviews=reviews,
        flows=flow_service,
    )

    def get_service() -> MemoryService:
        return memory_service

    def queue_learning_sync(project_id: str, background_tasks: BackgroundTasks) -> None:
        """Complete learning, then project and incrementally index it."""
        background_tasks.add_task(_complete_learning_and_sync, project_id)

    def _complete_learning_and_sync(project_id: str) -> None:
        # Learning job idempotency lives in SQLite. Projection and indexing use
        # ReviewService's per-project lock and one final durable sync-state write.
        intelligence.drain_pending_jobs(project_id)
        reviews.project_and_index_vault(project_id)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings", response_model=AISettingsRead)
    def settings() -> AISettingsRead:
        return settings_service.read()

    @app.put("/api/settings", response_model=AISettingsRead)
    def update_settings(update: AISettingsUpdate) -> AISettingsRead:
        return settings_service.update(update)

    @app.get("/api/settings/knowledge-base", response_model=VaultSettingsRead)
    def knowledge_base_settings() -> VaultSettingsRead:
        return settings_service.vault_status()

    @app.put("/api/settings/knowledge-base", response_model=VaultSettingsRead)
    def update_knowledge_base_settings(update: VaultSettingsUpdate) -> VaultSettingsRead:
        return settings_service.update_vault(update)

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
    def sync_project_source(project_id: str, background_tasks: BackgroundTasks, request: ProjectSourceSyncRequest = ProjectSourceSyncRequest()) -> ProjectSourceSyncRead:
        indexed, changed_files = projects.refresh_local_directory(project_id, request.directory)
        # Source authority: stale intelligence is marked deterministically
        # right after the index refresh. Failures never fail the sync.
        try:
            intelligence.deterministic_source_revalidation(project_id)
        except Exception:  # noqa: BLE001 - revalidation is best-effort
            pass
        if changed_files:
            intelligence.enqueue_source_revalidation_learning(project_id, changed_files)
            queue_learning_sync(project_id, background_tasks)
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

    @app.get("/api/projects/{project_id}/topology", response_model=TopologyRead)
    def project_topology(project_id: str, search: str | None = None) -> TopologyRead:
        topology = projects.topology(project_id, search)
        return TopologyRead(
            symbols=[TopologySymbolRead.model_validate(item) for item in topology["symbols"]],
            relations=[TopologyRelationRead.model_validate(item) for item in topology["relations"]],
            allocations=[AllocationEventRead.model_validate(item) for item in topology["allocations"]],
            snapshot=topology["snapshot"],
        )

    @app.get("/api/projects/{project_id}/topology/path", response_model=TopologyPathRead)
    def project_topology_path(project_id: str, symbol: str, depth: int = Query(default=1, ge=0, le=3), cap: int = Query(default=64, ge=1, le=128)) -> TopologyPathRead:
        return TopologyPathRead.model_validate(projects.topology_path(project_id, symbol, depth=depth, cap=cap))

    @app.get("/api/projects/{project_id}/flows", response_model=FlowIndexRead)
    def project_flows(project_id: str) -> FlowIndexRead:
        projects.get(project_id)
        return flow_service.build(project_id)

    @app.get("/api/projects/{project_id}/flows/{flow_id}", response_model=FlowScenarioRead)
    def project_flow(project_id: str, flow_id: str) -> FlowScenarioRead:
        projects.get(project_id)
        match = next((item for item in flow_service.build(project_id).scenarios if item.id == flow_id), None)
        if match is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Flow scenario not found")
        return match

    @app.get("/api/projects/{project_id}/flow/paths-to-symbol", response_model=list[FlowScenarioRead])
    def flow_paths_to_symbol(project_id: str, symbol: str, file: str | None = None, cap: int = Query(default=32, ge=1, le=64)) -> list[FlowScenarioRead]:
        projects.get(project_id)
        return flow_service.paths_to_symbol(project_id, symbol, cap, file)

    @app.get("/api/projects/{project_id}/flow/entry-points", response_model=list[FlowNodeRead])
    def flow_entry_points(project_id: str) -> list[FlowNodeRead]:
        projects.get(project_id)
        index = flow_service.build(project_id)
        unique = {scenario.nodes[0].id: scenario.nodes[0] for scenario in index.scenarios if scenario.nodes}
        return list(unique.values())

    @app.get("/api/projects/{project_id}/flow/resources", response_model=list[FlowResourceRead])
    def flow_resources(project_id: str) -> list[FlowResourceRead]:
        projects.get(project_id)
        return flow_service.build(project_id).resources

    @app.get("/api/projects/{project_id}/flow/shared-state", response_model=list[SharedStateRead])
    def flow_shared_state(project_id: str) -> list[SharedStateRead]:
        projects.get(project_id)
        return flow_service.build(project_id).shared_state

    @app.get("/api/projects/{project_id}/knowledge/documents", response_model=list[KnowledgeDocumentRead])
    def knowledge_documents(project_id: str) -> list[KnowledgeDocumentRead]:
        projects.get(project_id)
        return [KnowledgeDocumentRead.model_validate(item) for item in memory_repository.list_knowledge_documents(project_id)]

    @app.get("/api/projects/{project_id}/knowledge/sync-state", response_model=VaultSyncStateRead | None)
    def knowledge_sync_state(project_id: str) -> VaultSyncStateRead | None:
        projects.get(project_id)
        state = memory_repository.get_knowledge_sync_state(project_id)
        return VaultSyncStateRead.model_validate(state) if state else None

    @app.get("/api/projects/{project_id}/knowledge/documents/{document_id}", response_model=KnowledgeDocumentDetailRead)
    def knowledge_document(project_id: str, document_id: str) -> KnowledgeDocumentDetailRead:
        projects.get(project_id)
        document = memory_repository.get_knowledge_document(document_id)
        if not document or document["project_id"] != project_id:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, _t("error.knowledge_doc_not_found"))
        return KnowledgeDocumentDetailRead.model_validate(document)

    @app.post("/api/projects/{project_id}/knowledge/sync", response_model=VaultSyncReport)
    def sync_knowledge(project_id: str) -> VaultSyncReport:
        projects.get(project_id)
        report = knowledge_base.sync_project(project_id)
        if not report.errors:
            knowledge_index.index_project(project_id)
        return report

    @app.post("/api/projects/{project_id}/knowledge/reindex", response_model=KnowledgeIndexReport)
    def reindex_knowledge(project_id: str) -> KnowledgeIndexReport:
        projects.get(project_id)
        return knowledge_index.index_project(project_id)

    @app.post("/api/projects/{project_id}/knowledge/retry-index", response_model=KnowledgeRetryOutcome)
    def retry_knowledge_index(project_id: str) -> KnowledgeRetryOutcome:
        """One atomic projection + incremental index under the project workflow lock.

        REV-033/REV-034/REV-036/REV-038: replaces the browser-side ``sync`` then
        ``reindex`` sequence. The same locked workflow used by automatic
        post-review projection is reused, so a clean projection never overwrites
        ``INDEX_FAILED`` with ``SYNCED`` before indexing actually succeeds, a
        repeated index failure stays ``INDEX_FAILED``, a hard projection error
        performs zero index calls and persists a safe ``FAILED`` state, and
        warning-only legacy migration still indexes. The workflow persists exactly
        one final durable state while holding the lock and reports whether that
        write landed (``persisted``); when it did not, this route returns the
        current typed workflow outcome with a fixed safe persistence-unavailable
        message instead of a stale persisted row. The response carries only the
        fixed safe status/message.
        """
        projects.get(project_id)
        outcome = reviews.project_and_index_vault(project_id)
        if outcome is None:
            status = "SKIPPED"
            return KnowledgeRetryOutcome(project_id=project_id, status=status, vault="SKIPPED", index="NOT_RUN", message=_t("review.vault_skipped"))
        # REV-038: prefer the durable row only when this workflow actually wrote
        # its final state. Otherwise the row may be a stale pre-operation value
        # (for example an old SYNCED), so trust the typed workflow outcome and
        # return a fixed safe persistence-unavailable message.
        if outcome.get("persisted"):
            state = memory_repository.get_knowledge_sync_state(project_id)
            status = (state or {}).get("status") or outcome["status"]
            message = outcome["message"]
        else:
            status = outcome["status"]
            message = PERSISTENCE_UNAVAILABLE_MESSAGE
        return KnowledgeRetryOutcome(
            project_id=project_id,
            status=status,
            vault=outcome["vault"],
            index=outcome["index"],
            message=message,
        )

    @app.post("/api/projects/{project_id}/knowledge/search", response_model=KnowledgeSearchResult)
    def search_knowledge(project_id: str, request: RetrievalQuery) -> KnowledgeSearchResult:
        projects.get(project_id)
        if request.project_id != project_id:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _t("error.knowledge_scope_mismatch"))
        result = knowledge_retriever.search(request)
        return KnowledgeSearchResult(matches=result.matches, warnings=result.warnings)

    @app.post("/api/projects/{project_id}/rag-brain/retrieval-inspector", response_model=RetrievalResult)
    def inspect_knowledge_retrieval(project_id: str, request: RetrievalQuery) -> RetrievalResult:
        projects.get(project_id)
        if request.project_id != project_id:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, _t("error.knowledge_scope_mismatch"))
        return knowledge_retriever.inspect(request)

    @app.get("/api/projects/{project_id}/rag-brain/retrieval-traces", response_model=RetrievalTraceList)
    def retrieval_traces(project_id: str, review_id: str | None = None, finding_id: str | None = None, limit: int = Query(default=50, ge=1, le=100)) -> RetrievalTraceList:
        projects.get(project_id)
        return RetrievalTraceList(traces=[RetrievalTraceRead.model_validate(item) for item in memory_repository.list_retrieval_traces(project_id, review_id=review_id, finding_id=finding_id, limit=limit)])

    @app.get("/api/projects/{project_id}/rag-brain/retrieval-traces/{trace_id}", response_model=RetrievalTraceRead)
    def retrieval_trace(project_id: str, trace_id: str) -> RetrievalTraceRead:
        projects.get(project_id)
        record = memory_repository.get_retrieval_trace(project_id, trace_id)
        if not record:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, _t("error.retrieval_trace_not_found"))
        return RetrievalTraceRead.model_validate(record)

    @app.get("/api/projects/{project_id}/rag-brain/relationships", response_model=RelationshipGraph)
    def rag_brain_relationships(project_id: str, query: str = "", cap: int = Query(default=100, ge=1, le=200)) -> RelationshipGraph:
        projects.get(project_id)
        needle = query.casefold().strip()
        nodes: dict[str, RelationshipNode] = {}
        edges: dict[tuple[str, str, str], RelationshipEdge] = {}
        truncated = False

        def add_node(node: RelationshipNode) -> bool:
            nonlocal truncated
            if node.id in nodes:
                return True
            if len(nodes) >= cap:
                truncated = True
                return False
            nodes[node.id] = node
            return True

        def add_edge(source: str, target: str, relation: str) -> None:
            if source in nodes and target in nodes:
                edges[(source, target, relation)] = RelationshipEdge(source=source, target=target, relation=relation)

        records = memory_repository.list_intelligence(project_id, limit=min(cap, 100))
        for record in records:
            identifier = str(record["id"])
            label = str(record.get("statement") or identifier)
            if needle and needle not in (identifier + " " + label).casefold():
                continue
            if not add_node(RelationshipNode(id=identifier, kind="knowledge", label=label[:180])):
                break
            for link in memory_repository.list_intelligence_links(identifier):
                kind = str(link.get("link_kind") or "").upper()
                value = str(link.get("link_value") or "")
                target_kind = {"SYMBOL": "symbol", "COMPONENT": "component", "FILE": "file", "FINDING": "finding", "REVIEW": "review"}.get(kind)
                if not target_kind or not value or (needle and needle not in value.casefold() and needle not in label.casefold()):
                    continue
                target_id = f"{target_kind}:{value}"
                if add_node(RelationshipNode(id=target_id, kind=target_kind, label=value)):
                    add_edge(identifier, target_id, str(link.get("role") or kind.lower()))
            for evidence in memory_repository.list_intelligence_evidence(identifier, limit=24):
                file_path = str(evidence.get("file") or "")
                symbol_name = str(evidence.get("symbol") or "")
                if file_path and add_node(RelationshipNode(id=f"file:{file_path}", kind="file", label=file_path, path=file_path)):
                    add_edge(identifier, f"file:{file_path}", "source_evidence")
                if symbol_name and add_node(RelationshipNode(id=f"symbol:{symbol_name}", kind="symbol", label=symbol_name)):
                    add_edge(identifier, f"symbol:{symbol_name}", "source_evidence")
            for observation in memory_repository.list_intelligence_observations(identifier, limit=40):
                for field, node_kind, relation in (("finding_id", "finding", "observed_from"), ("review_id", "review", "observed_during")):
                    value = str(observation.get(field) or "")
                    if value:
                        target_id = f"{node_kind}:{value}"
                        if add_node(RelationshipNode(id=target_id, kind=node_kind, label=value)):
                            add_edge(identifier, target_id, relation)
            document = memory_repository.get_knowledge_document_by_intelligence(identifier)
            if document:
                doc_id = f"document:{document['id']}"
                if add_node(RelationshipNode(id=doc_id, kind="markdown", label=document["relative_path"], path=document["relative_path"])):
                    add_edge(identifier, doc_id, "projected_as")
                for link in memory_repository.list_knowledge_wikilinks(project_id, document["id"]):
                    target = link.get("target_id")
                    target_doc = memory_repository.get_knowledge_document(target) if target else None
                    if target_doc and target_doc.get("project_id") == project_id:
                        target_doc_id = f"document:{target_doc['id']}"
                        if add_node(RelationshipNode(id=target_doc_id, kind="markdown", label=target_doc["relative_path"], path=target_doc["relative_path"])):
                            add_edge(doc_id, target_doc_id, "wikilink")
        for trace in memory_repository.list_retrieval_traces(project_id, limit=min(cap, 100)):
            if trace.get("finding_id"):
                finding_id = str(trace["finding_id"])
                finding_node = f"finding:{finding_id}"
                if add_node(RelationshipNode(id=finding_node, kind="finding", label=finding_id)):
                    add_edge(str(trace.get("memory_id") or ""), finding_node, "retrieved_during")
        return RelationshipGraph(nodes=list(nodes.values()), edges=list(edges.values()), truncated=truncated)

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
            raise HTTPException(status.HTTP_404_NOT_FOUND, _t("error.review_not_found"))
        return review

    @app.post("/api/projects/{project_id}/reviews/{review_id}/retry", response_model=ReviewRead)
    def retry_review(project_id: str, review_id: str, background_tasks: BackgroundTasks) -> ReviewRead:
        review = reviews.get(review_id)
        if review.project_id != project_id:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, _t("error.review_not_found"))
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
        queue_learning_sync(project_id, background_tasks)
        return finding

    @app.patch("/api/projects/{project_id}/findings/{finding_id}/resolution", response_model=FindingRead)
    def resolve_finding(project_id: str, finding_id: str, update: FindingResolutionUpdate, background_tasks: BackgroundTasks) -> FindingRead:
        finding = reviews.resolve(project_id, finding_id, update)
        intelligence.enqueue_resolution_learning(project_id, finding.id, finding.resolution)
        queue_learning_sync(project_id, background_tasks)
        return finding

    @app.post("/api/projects/{project_id}/findings/{finding_id}/verify-fix", response_model=FindingRead)
    def verify_finding_fix(project_id: str, finding_id: str, background_tasks: BackgroundTasks, request: FindingFixVerificationRequest = FindingFixVerificationRequest()) -> FindingRead:
        finding = reviews.verify_fix(project_id, finding_id, request.directory)
        intelligence.enqueue_fix_verification_learning(project_id, finding.id, finding.resolution)
        queue_learning_sync(project_id, background_tasks)
        return finding

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
        detail = intelligence.detail(memory_id)
        current_files = {item["path"]: item for item in platform_repository.raw_files(detail.project_id)}
        evidence = []
        for item in detail.evidence:
            source = current_files.get(item.file or "")
            excerpt = None
            hash_matches = None
            if source and item.line:
                lines = source["content"].splitlines()
                start = max(1, item.line - 2)
                end = min(len(lines), item.line + 2)
                excerpt = "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))[:2400]
                hash_matches = bool(item.file_hash and item.file_hash == source.get("content_hash"))
            evidence.append(item.model_copy(update={"canonical_excerpt": excerpt, "source_hash_matches": hash_matches}))
        return detail.model_copy(update={"evidence": evidence})

    @app.post("/api/intelligence/{memory_id}/revalidate", response_model=IntelligenceRevalidateResponse)
    def revalidate_intelligence(memory_id: str) -> IntelligenceRevalidateResponse:
        record = intelligence.repository.get_memory(memory_id)
        if not record:
            from fastapi import HTTPException
            raise HTTPException(status.HTTP_404_NOT_FOUND, _t("error.intelligence_not_found"))
        return intelligence.revalidate_record(record["project_id"], memory_id)

    @app.post("/api/intelligence/{memory_id}/disable", response_model=IntelligenceRecordRead)
    def disable_intelligence(memory_id: str, request: IntelligenceDisableRequest = IntelligenceDisableRequest()) -> IntelligenceRecordRead:
        return intelligence.disable(memory_id, request.reason)

    @app.post("/api/intelligence/{memory_id}/correction", response_model=IntelligenceRecordRead)
    def correct_intelligence(memory_id: str, request: IntelligenceCorrectionRequest) -> IntelligenceRecordRead:
        return intelligence.correct(memory_id, request.reason)

    @app.get("/api/projects/{project_id}/reviews/{review_id}/learning-summary", response_model=ReviewLearningSummaryRead)
    def review_learning_summary(project_id: str, review_id: str) -> ReviewLearningSummaryRead:
        return intelligence.learning_summary(project_id, review_id)

    return app


app = create_app()
