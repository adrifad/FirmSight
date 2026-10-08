from __future__ import annotations

from datetime import UTC, datetime

from app.knowledge_index_service import KnowledgeIndexService
from app.knowledge_retriever import KnowledgeRetriever
from app.knowledge_schemas import RetrievalQuery
from app.platform_repository import PlatformRepository
from app.repository import MemoryRepository


def _project(repository: PlatformRepository, project_id: str) -> None:
    repository.create_project({
        "id": project_id, "name": project_id, "description": "", "source_type": "MANUAL",
        "language": None, "framework": None, "target": None, "build_system": None,
        "source_directory": None, "created_at": "now", "updated_at": "now",
    })


def _document(memory: MemoryRepository, document_id: str, project_id: str, body: str, *, state: str = "VERIFIED", symbols: list[str] | None = None, files: list[str] | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    memory.upsert_knowledge_document({
        "id": document_id, "project_id": project_id, "intelligence_id": document_id,
        "relative_path": f"04-Knowledge/{document_id}.md",
        "content_hash": f"hash-{body}", "frontmatter": {
            "id": document_id, "type": "PROJECT_FACT", "project_id": project_id,
            "status": state, "confidence": 0.9, "observation_count": 1,
            "title": document_id, "statement": body, "scope": {"symbols": symbols or [], "files": files or []},
        }, "body": body, "sync_status": "SYNCED", "source_status": "GENERATED",
        "schema_version": 1, "created_at": now, "updated_at": now,
    })


def test_incremental_index_skips_unchanged_and_rewrites_only_changed_document(tmp_path, monkeypatch):
    database = str(tmp_path / "knowledge.db")
    platform = PlatformRepository(database)
    _project(platform, "P1")
    memory = MemoryRepository(database)
    _document(memory, "MEM-ONE", "P1", "## Notes\nfirst")
    _document(memory, "MEM-TWO", "P1", "## Notes\nsecond")
    index = KnowledgeIndexService(memory)
    first = index.index_project("P1")
    assert first.chunks_written == 2

    calls: list[str] = []
    original = memory.replace_knowledge_chunks

    def counted(document_id, project_id, chunks):
        calls.append(document_id)
        return original(document_id, project_id, chunks)

    monkeypatch.setattr(memory, "replace_knowledge_chunks", counted)
    unchanged = index.index_project("P1")
    assert unchanged.noop is True
    assert unchanged.chunks_written == 0
    assert calls == []

    document = memory.get_knowledge_document("MEM-ONE")
    assert document is not None
    document["body"] = "## Notes\nchanged"
    document["content_hash"] = "hash-changed"
    memory.upsert_knowledge_document(document)
    changed = index.index_project("P1")
    assert changed.noop is False
    assert calls == ["MEM-ONE"]
    assert {item["document_id"] for item in memory.list_knowledge_chunks("P1")} == {"MEM-ONE", "MEM-TWO"}


def test_removed_document_cleans_only_its_chunks_and_links(tmp_path):
    database = str(tmp_path / "knowledge.db")
    platform = PlatformRepository(database)
    _project(platform, "P1")
    memory = MemoryRepository(database)
    _document(memory, "MEM-A", "P1", "## Notes\n[[MEM-B]]")
    _document(memory, "MEM-B", "P1", "## Notes\ntarget")
    index = KnowledgeIndexService(memory)
    index.index_project("P1")
    assert memory.list_knowledge_wikilinks("P1", "MEM-A")[0]["validation_status"] == "VALID"
    memory.delete_knowledge_document("MEM-B")
    report = index.index_project("P1")
    assert report.noop is False
    assert all(item["document_id"] == "MEM-A" for item in memory.list_knowledge_chunks("P1"))
    assert memory.list_knowledge_wikilinks("P1", "MEM-B") == []
    assert memory.list_knowledge_wikilinks("P1", "MEM-A")[0]["validation_status"] == "INVALID"


def test_unresolved_wikilink_is_revalidated_when_target_is_added(tmp_path):
    database = str(tmp_path / "knowledge.db")
    platform = PlatformRepository(database)
    _project(platform, "P1")
    memory = MemoryRepository(database)
    _document(memory, "MEM-A", "P1", "## Notes\n[[MEM-LATER]]")
    index = KnowledgeIndexService(memory)
    index.index_project("P1")
    assert memory.list_knowledge_wikilinks("P1", "MEM-A")[0]["validation_status"] == "INVALID"
    _document(memory, "MEM-LATER", "P1", "## Notes\nlater target")
    report = index.index_project("P1")
    assert report.noop is False
    assert memory.list_knowledge_wikilinks("P1", "MEM-A")[0]["validation_status"] == "VALID"


class _Topology:
    def list_indexed_symbols(self, project_id):
        return [
            {"id": "SYM-A", "name": "function_a", "file": "src/a.c"},
            {"id": "SYM-B", "name": "helper", "file": "src/helper.c"},
        ]

    def list_source_relations(self, project_id):
        return [{"source_symbol_id": "SYM-A", "target_symbol_id": "SYM-B", "target_name": "helper"}]

    def list_allocation_events(self, project_id):
        return [{"symbol_id": "SYM-A"}]


def test_hybrid_retrieval_is_scoped_filtered_and_auditable(tmp_path):
    database = str(tmp_path / "knowledge.db")
    platform = PlatformRepository(database)
    _project(platform, "P1")
    _project(platform, "P2")
    memory = MemoryRepository(database)
    _document(memory, "MEM-A", "P1", "function_a allocation [[MEM-B]]", symbols=["function_a"], files=["src/a.c"])
    _document(memory, "MEM-B", "P1", "helper topology evidence", symbols=["helper"], files=["src/helper.c"])
    _document(memory, "MEM-D", "P1", "disabled function_a", state="DISABLED", symbols=["function_a"])
    _document(memory, "MEM-X", "P2", "cross project function_a", symbols=["function_a"], files=["src/a.c"])
    index = KnowledgeIndexService(memory)
    index.index_project("P1")
    index.index_project("P2")

    result = KnowledgeRetriever(memory, _Topology()).search(
        RetrievalQuery(project_id="P1", query="function_a MEM-B", symbols=["function_a"], files=["src/a.c"], cap=10)
    )
    ids = {match.intelligence_id for match in result.matches}
    assert "MEM-D" not in ids
    assert "MEM-X" not in ids
    first = next(match for match in result.matches if match.intelligence_id == "MEM-A")
    assert {"exact-symbol", "exact-file", "topology", "allocation", "wikilink"} <= set(first.match_signals)
    assert any("fts" in match.match_signals for match in result.matches)
    assert result.used_fts is True
    assert any(match.intelligence_id == "MEM-B" and "topology" in match.match_signals for match in result.matches)


def test_retrieval_uses_validated_target_paths_and_reports_fts_fallback(tmp_path):
    database = str(tmp_path / "knowledge.db")
    platform = PlatformRepository(database)
    _project(platform, "P1")
    memory = MemoryRepository(database)
    _document(memory, "MEM-SOURCE", "P1", "source links to path", files=["src/source.c"])
    _document(memory, "MEM-TARGET", "P1", "target path evidence", files=["src/target.c"])
    source = memory.get_knowledge_document("MEM-SOURCE")
    assert source is not None
    source["body"] = "source links [[04-Knowledge/MEM-TARGET.md]]"
    source["content_hash"] = "hash-path-link"
    memory.upsert_knowledge_document(source)
    KnowledgeIndexService(memory).index_project("P1")
    memory.fts_available = False
    result = KnowledgeRetriever(memory).search(
        RetrievalQuery(project_id="P1", query="04-Knowledge/MEM-TARGET.md", files=["04-Knowledge/MEM-TARGET.md"], cap=10)
    )
    source_match = next(match for match in result.matches if match.intelligence_id == "MEM-SOURCE")
    assert "wikilink" in source_match.match_signals
    assert result.used_fts is False


def test_database_lifecycle_outvotes_tampered_markdown_metadata(tmp_path):
    database = str(tmp_path / "knowledge.db")
    platform = PlatformRepository(database)
    _project(platform, "P1")
    memory = MemoryRepository(database)
    now = datetime.now(UTC).isoformat()
    for memory_id, state in (("MEM-DISABLED", "DISABLED"), ("MEM-SUPERSEDED", "SUPERSEDED"), ("MEM-CONFLICTED", "CONFLICTED")):
        memory.create_memory({
            "id": memory_id, "project_id": "P1", "type": "PROJECT_FACT",
            "statement": f"{memory_id} must not be retrieved.", "scope": {"type": "PROJECT"},
            "evidence": [], "source": {"type": "AUTOMATIC"}, "status": state,
            "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
            "created_at": now, "updated_at": now, "state": state,
            "fingerprint": f"fingerprint-{memory_id}",
        })
        _document(memory, memory_id, "P1", f"function_a {memory_id}", state="VERIFIED", symbols=["function_a"])
    KnowledgeIndexService(memory).index_project("P1")
    result = KnowledgeRetriever(memory).search(RetrievalQuery(project_id="P1", query="function_a", symbols=["function_a"], cap=10))
    assert {match.intelligence_id for match in result.matches}.isdisjoint({"MEM-DISABLED", "MEM-SUPERSEDED", "MEM-CONFLICTED"})
