from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.knowledge_index_service import KnowledgeIndexService
from app.knowledge_retriever import KnowledgeRetriever
from app.knowledge_schemas import RetrievalQuery
from app.main import create_app
from app.platform_repository import PlatformRepository
from app.repository import MemoryRepository


def _project(database: str, project_id: str = "P1") -> None:
    PlatformRepository(database).create_project({
        "id": project_id, "name": "REV-022 Fixture", "description": "",
        "source_type": "MANUAL", "language": None, "framework": None,
        "target": None, "build_system": None, "source_directory": None,
        "created_at": "now", "updated_at": "now",
    })


def _indexed_repository(tmp_path) -> MemoryRepository:
    database = str(tmp_path / "knowledge.db")
    _project(database)
    repository = MemoryRepository(database)
    now = datetime.now(UTC).isoformat()
    repository.upsert_knowledge_document({
        "id": "DOC-REV022", "project_id": "P1", "intelligence_id": "MEM-REV022",
        "relative_path": "04-Knowledge/Facts/MEM-REV022.md",
        "content_hash": "rev022-content", "frontmatter": {
            "id": "MEM-REV022", "type": "PROJECT_FACT", "project_id": "P1",
            "status": "VERIFIED", "confidence": 1.0, "observation_count": 1,
            "title": "REV-022", "statement": "ordinary searchable knowledge",
        }, "body": "ordinary searchable knowledge", "sync_status": "SYNCED",
        "source_status": "GENERATED", "schema_version": 1,
        "created_at": now, "updated_at": now,
    })
    KnowledgeIndexService(repository).index_project("P1")
    return repository


def test_sanitized_empty_fts_inputs_return_typed_empty_tuples(tmp_path):
    repository = _indexed_repository(tmp_path)
    for query in ("***", '"""', "   "):
        rows, used_fts = repository.knowledge_fts_search_with_status("P1", query)
        assert rows == []
        assert used_fts is False


def test_normal_fts_query_reports_actual_backend_usage(tmp_path):
    repository = _indexed_repository(tmp_path)
    result = KnowledgeRetriever(repository).search(
        RetrievalQuery(project_id="P1", query="ordinary", cap=5)
    )
    assert result.matches
    assert result.used_fts is repository.fts_available
    if repository.fts_available:
        assert "fts" in result.matches[0].match_signals


def test_knowledge_search_api_returns_200_for_punctuation_only_inputs(tmp_path):
    database = str(tmp_path / "api.db")
    client = TestClient(create_app(database))
    project = client.post("/api/projects", json={"name": "REV-022 API", "source_type": "MANUAL"}).json()
    for query in ("***", '"""', "   "):
        response = client.post(
            f"/api/projects/{project['id']}/knowledge/search",
            json={"project_id": project["id"], "query": query},
        )
        assert response.status_code == 200, response.text
        assert response.json()["matches"] == []


def test_real_fts_operational_errors_are_not_hidden(tmp_path):
    repository = _indexed_repository(tmp_path)
    repository.fts_available = True

    class LockedConnection:
        def __enter__(self):
            return self

        def __exit__(self, _type, _value, _traceback):
            return False

        def execute(self, statement, _parameters=()):
            if "knowledge_fts MATCH" in statement:
                raise sqlite3.OperationalError("database is locked")
            raise AssertionError(f"unexpected SQL in test double: {statement}")

    repository._connect = lambda: LockedConnection()
    try:
        repository.knowledge_fts_search_with_status("P1", "ordinary")
    except sqlite3.OperationalError as error:
        assert str(error) == "database is locked"
    else:
        raise AssertionError("database errors must not be converted to LIKE fallback results")
