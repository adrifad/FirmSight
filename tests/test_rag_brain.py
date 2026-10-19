from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.knowledge_index_service import KnowledgeIndexService
from app.knowledge_schemas import RetrievalQuery
from app.main import create_app
from app.platform_repository import PlatformRepository
from app.repository import MemoryRepository


def _document(memory: MemoryRepository, identifier: str, project_id: str, body: str, *, state: str = "VERIFIED", symbols: list[str] | None = None, files: list[str] | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    memory.upsert_knowledge_document({
        "id": identifier,
        "project_id": project_id,
        "intelligence_id": identifier,
        "relative_path": f"04-Knowledge/{identifier}.md",
        "content_hash": f"hash-{body}",
        "frontmatter": {
            "id": identifier,
            "type": "PROJECT_FACT",
            "project_id": project_id,
            "status": state,
            "confidence": 0.9,
            "observation_count": 2,
            "title": identifier,
            "statement": body,
            "scope": {"symbols": symbols or [], "files": files or []},
        },
        "body": body,
        "sync_status": "SYNCED",
        "source_status": "GENERATED",
        "schema_version": 1,
        "created_at": now,
        "updated_at": now,
    })


class _Topology:
    def list_indexed_symbols(self, project_id: str):
        return [
            {"id": "SYM-A", "name": "function_a", "file": "src/a.c"},
            {"id": "SYM-B", "name": "helper", "file": "src/helper.c"},
        ]

    def list_source_relations(self, project_id: str):
        return [{"source_symbol_id": "SYM-A", "target_symbol_id": "SYM-B", "target_name": "helper"}]

    def list_allocation_events(self, project_id: str):
        return [{"symbol_id": "SYM-A"}]


def test_chat_routes_are_removed_and_legacy_messages_remain_persisted(tmp_path):
    database = str(tmp_path / "legacy-chat.db")
    client = TestClient(create_app(database))
    project = client.post("/api/projects", json={"name": "Legacy chat", "source_type": "MANUAL"}).json()
    platform = PlatformRepository(database)
    message = {"id": "MSG-LEGACY", "project_id": project["id"], "role": "user", "content": "old history", "created_at": "2026-01-01T00:00:00+00:00"}
    platform.add_message(message)

    assert client.get(f"/api/projects/{project['id']}/chat").status_code == 404
    assert client.post(f"/api/projects/{project['id']}/chat", json={"message": "new chat"}).status_code == 404
    assert platform.messages(project["id"]) == [message]
    with platform._connect() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_messages'").fetchone()


def test_retrieval_inspector_exposes_hybrid_signals_and_lifecycle_exclusions(tmp_path):
    database = str(tmp_path / "inspector.db")
    client = TestClient(create_app(database))
    first = client.post("/api/projects", json={"name": "P1", "source_type": "MANUAL"}).json()
    second = client.post("/api/projects", json={"name": "P2", "source_type": "MANUAL"}).json()
    memory = MemoryRepository(database)
    _document(memory, "MEM-A", first["id"], "function_a allocation [[MEM-B]]", symbols=["function_a"], files=["src/a.c"])
    _document(memory, "MEM-B", first["id"], "helper topology source evidence", symbols=["helper"], files=["src/helper.c"])
    _document(memory, "MEM-R", first["id"], "revalidation-needed function_a", state="NEEDS_REVALIDATION", symbols=["function_a"], files=["src/a.c"])
    _document(memory, "MEM-C", first["id"], "conflicted function_a", state="CONFLICTED", symbols=["function_a"], files=["src/a.c"])
    _document(memory, "MEM-S", first["id"], "superseded function_a", state="SUPERSEDED", symbols=["function_a"], files=["src/a.c"])
    _document(memory, "MEM-D", first["id"], "disabled function_a", state="DISABLED", symbols=["function_a"], files=["src/a.c"])
    _document(memory, "MEM-X", second["id"], "other project function_a", symbols=["function_a"], files=["src/a.c"])
    KnowledgeIndexService(memory).index_project(first["id"])
    KnowledgeIndexService(memory).index_project(second["id"])
    client.app.state.services.knowledge_retriever.platform = _Topology()

    response = client.post(
        f"/api/projects/{first['id']}/rag-brain/retrieval-inspector",
        json={"project_id": first["id"], "query": "function_a MEM-B", "symbols": ["function_a"], "files": ["src/a.c"], "cap": 12},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    matches = {item["intelligence_id"]: item for item in payload["matches"]}
    assert "MEM-X" not in matches
    assert {"exact-symbol", "exact-file", "topology", "allocation", "wikilink"} <= set(matches["MEM-A"]["match_signals"])
    assert any("fts" in item["match_signals"] for item in payload["matches"])
    for identifier, lifecycle in (("MEM-C", "Conflicted"), ("MEM-S", "Superseded"), ("MEM-D", "Disabled")):
        assert matches[identifier]["included"] is False
        assert f"{lifecycle} knowledge" in matches[identifier]["exclusion_reason"]
    assert matches["MEM-R"]["included"] is True
    assert matches["MEM-R"]["lifecycle_warning"]
    assert matches["MEM-R"]["score"] < matches["MEM-A"]["score"]

    context = client.app.state.services.context_builder.build(first["id"], "function_a", roles=[])
    assert "conflicted function_a" not in context["text"]
    assert "superseded function_a" not in context["text"]
    assert "disabled function_a" not in context["text"]


def test_context_builder_persists_redacted_role_trace_without_prompt_or_reasoning(tmp_path):
    database = str(tmp_path / "trace.db")
    client = TestClient(create_app(database))
    project = client.post("/api/projects", json={"name": "Trace fixture", "source_type": "MANUAL"}).json()
    memory = MemoryRepository(database)
    _document(memory, "MEM-TRACE", project["id"], "## Evidence\napi_key=sk-abcdefghijklmnopqrstuvwxyz123456", symbols=["ota_install"], files=["src/ota.c"])
    KnowledgeIndexService(memory).index_project(project["id"])

    built = client.app.state.services.context_builder.build(
        project["id"], "api_key=sk-abcdefghijklmnopqrstuvwxyz123456 ota_install",
        selected_files=["src/ota.c"], symbols=["ota_install"], roles=["investigator"],
        review_id="REV-TRACE", prompt_versions={"investigator": "investigator-v1"},
    )
    assert built["retrieval"].matches
    response = client.get(f"/api/projects/{project['id']}/rag-brain/retrieval-traces", params={"review_id": "REV-TRACE"})
    assert response.status_code == 200, response.text
    trace = response.json()["traces"][0]
    assert trace["role"] == "investigator"
    assert trace["source_snapshot_hash"]
    assert trace["selected_chunk_ids"]
    assert trace["results"][0]["match_signals"]
    assert trace["prompt_version"] == "investigator-v1"
    serialized = json.dumps(trace)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in serialized
    assert "chain_of_thought" not in serialized.casefold()
    assert "system_prompt" not in serialized.casefold()


def test_retrieval_trace_api_is_project_scoped(tmp_path):
    database = str(tmp_path / "trace-isolation.db")
    client = TestClient(create_app(database))
    project_a = client.post("/api/projects", json={"name": "Project A", "source_type": "MANUAL"}).json()
    project_b = client.post("/api/projects", json={"name": "Project B", "source_type": "MANUAL"}).json()
    client.app.state.services.context_builder.build(project_a["id"], "current source", roles=["verifier"])
    traces_a = client.get(f"/api/projects/{project_a['id']}/rag-brain/retrieval-traces").json()["traces"]
    assert traces_a
    assert client.get(f"/api/projects/{project_b['id']}/rag-brain/retrieval-traces/{traces_a[0]['id']}").status_code == 404
    assert client.get(f"/api/projects/{project_b['id']}/rag-brain/retrieval-traces").json()["traces"] == []
