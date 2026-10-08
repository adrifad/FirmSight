from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.repository import MemoryRepository


def test_topology_api_and_server_side_vault_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("FIRMSIGHT_OBSIDIAN_VAULT_ROOT", raising=False)
    database = tmp_path / "firmsight.db"
    vault = tmp_path / "vault"
    client = TestClient(create_app(str(database)))
    assert client.get("/api/settings/knowledge-base").json()["status"] == "NOT_CONFIGURED"
    assert client.put("/api/settings/knowledge-base", json={"root": "relative/vault"}).status_code == 422
    configured = client.put("/api/settings/knowledge-base", json={"root": str(vault)})
    assert configured.status_code == 200
    assert configured.json()["status"] == "READY"
    project = client.post("/api/projects", json={"name": "Topology Fixture", "source_type": "MANUAL"}).json()
    source = """void function_a(void) {\n  char *p = malloc(8);\n  if (bad()) return;\n  free(p);\n}\nvoid task_b(void *arg) { function_a(); }\nvoid init(void) { xTaskCreate(task_b, \"b\", 1024, 0, 1, 0); }\n"""
    assert client.post(f"/api/projects/{project['id']}/files", json={"files": {"src/tasks.c": source}}).status_code == 200
    topology = client.get(f"/api/projects/{project['id']}/topology").json()
    assert any(item["relation_kind"] == "TASK_ENTRY" and item["relation_state"] == "OBSERVED" for item in topology["relations"])
    path = client.get(f"/api/projects/{project['id']}/topology/path", params={"symbol": "task_b"}).json()
    assert any(item["relation_kind"] == "CALLS" for item in path["relations"])


def test_knowledge_markdown_round_trip_is_project_scoped(tmp_path):
    database = tmp_path / "firmsight.db"
    vault = tmp_path / "vault"
    client = TestClient(create_app(str(database)))
    assert client.put("/api/settings/knowledge-base", json={"root": str(vault)}).status_code == 200
    project = client.post("/api/projects", json={"name": "Knowledge Fixture", "source_type": "MANUAL"}).json()
    memory = MemoryRepository(str(database))
    memory.create_memory({
        "id": "MEM-ABC123", "project_id": project["id"], "type": "ARCHITECTURE_KNOWLEDGE",
        "statement": "task_b owns the copied measurement snapshot for later consumers.",
        "scope": {"type": "SYMBOL", "symbol": "task_b"}, "evidence": [{"file": "src/tasks.c", "line": 2, "symbol": "task_b", "description": "The task is indexed in current source."}],
        "source": {"type": "AUTOMATIC"}, "status": "ACTIVE", "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00", "state": "REINFORCED", "fingerprint": "fingerprint-a",
    })
    memory.add_intelligence_evidence([{"memory_id": "MEM-ABC123", "project_id": project["id"], "kind": "SOURCE", "file": "src/tasks.c", "line": 2, "symbol": "task_b", "file_hash": "hash", "description": "The task is indexed in current source.", "fingerprint": "evidence-a", "created_at": "2026-01-01T00:00:00+00:00"}])
    report = client.post(f"/api/projects/{project['id']}/knowledge/sync")
    assert report.status_code == 200, report.text
    docs = client.get(f"/api/projects/{project['id']}/knowledge/documents")
    assert docs.status_code == 200
    assert any(item["intelligence_id"] == "MEM-ABC123" for item in docs.json())
    detail = client.get(f"/api/projects/{project['id']}/knowledge/documents/{next(item['id'] for item in docs.json() if item['intelligence_id'] == 'MEM-ABC123')}")
    assert detail.status_code == 200
    assert "task_b" in detail.json()["body"]
    markdown = next(vault.rglob("MEM-ABC123.md"))
    assert "untrusted" not in markdown.read_text(encoding="utf-8").casefold()
    search = client.post(f"/api/projects/{project['id']}/knowledge/search", json={"project_id": project["id"], "query": "task_b snapshot"})
    assert search.status_code == 200
    assert search.json()["matches"]


def test_intelligence_correction_is_audited_and_requires_reason(tmp_path):
    database = tmp_path / "firmsight.db"
    client = TestClient(create_app(str(database)))
    project = client.post("/api/projects", json={"name": "Correction Fixture", "source_type": "MANUAL"}).json()
    memory = MemoryRepository(str(database))
    memory.create_memory({
        "id": "MEM-CORRECT1", "project_id": project["id"], "type": "PROJECT_FACT",
        "statement": "The project owns its measurement queue.", "scope": {"type": "PROJECT"},
        "evidence": [], "source": {"type": "AUTOMATIC"}, "status": "ACTIVE", "proposed_by": "AI",
        "approved_by": "AUTOMATIC", "commit_sha": None, "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00", "state": "PROVISIONAL", "fingerprint": "correction-fingerprint",
    })
    assert client.post("/api/intelligence/MEM-CORRECT1/correction", json={"reason": "no"}).status_code == 422
    corrected = client.post("/api/intelligence/MEM-CORRECT1/correction", json={"reason": "Current source shows a shared queue owner."})
    assert corrected.status_code == 200
    detail = client.get("/api/intelligence/MEM-CORRECT1")
    assert any(item["kind"] == "ENGINEER_CORRECTION" for item in detail.json()["observations"])
