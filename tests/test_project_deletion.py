"""End-to-end coverage for safe project deletion (FS-DEV-004).

Deletion must remove exactly one project's persisted rows across platform,
legacy Memory, and Project Intelligence tables in one transaction, refuse to
run while background work is in flight, and never touch the engineer's local
firmware directory, other projects, or global settings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

SOURCE = """#include <freertos/semphr.h>
static SemaphoreHandle_t ota_mutex;
esp_err_t ota_install(void) {
  xSemaphoreTake(ota_mutex, portMAX_DELAY);
  return ESP_OK;
}
"""

PROJECT_TABLES = [
    "project_files", "project_symbols", "reviews", "findings", "chat_messages", "yaml_generations",
    "memory_proposals", "memories", "intelligence_evidence", "intelligence_links",
    "intelligence_observations", "intelligence_conflicts", "intelligence_learning_jobs",
    "review_learning_summaries",
]


def workspace(tmp_path: Path, name: str) -> Path:
    root = tmp_path / "firmware-workspace"
    src = root / name / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.c").write_text(SOURCE, encoding="utf-8")
    return root


def import_project(api: TestClient, root: Path, name: str) -> str:
    created = api.post("/api/projects/import-directory", json={"directory": str(root / name)})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def seed_everything(database: str, project_id: str, tag: str = "") -> None:
    """Write one row into every project-scoped table through the real repositories.

    `tag` keeps primary keys unique when two projects are seeded in one database.
    """
    from app.platform_repository import PlatformRepository
    from app.repository import MemoryRepository

    platform = PlatformRepository(database)
    memory = MemoryRepository(database)
    now = "2026-09-12T00:00:00+00:00"
    review_id, finding_id, message_id, yaml_id = f"REV-SEED{tag}", f"FS-SEED{tag}", f"MSG-SEED{tag}", f"YML-SEED{tag}"
    memory_id, proposal_id, job_id = f"MEM-SEED{tag}", f"MP-SEED{tag}", f"LRN-DONE{tag}"

    platform.create_review({"id": review_id, "project_id": project_id, "scope": "Full Project", "focus": ["ota"], "context_files": ["src/main.c"], "status": "COMPLETED", "progress": ["done"], "created_at": now, "completed_at": now})
    platform.create_finding({"id": finding_id, "project_id": project_id, "review_id": review_id, "payload": {"title": "Seeded finding", "classification": "CONFIRMED_BUG", "severity": "high", "category": "CONCURRENCY", "confidence": 0.9, "location": {"file": "src/main.c", "function": "ota_install", "line_start": 4, "line_end": 5}, "summary": "Seeded for deletion coverage.", "evidence": [], "execution_path": ["a", "b"], "runtime_scenario": "scenario", "impact": "impact", "assumptions": [], "recommendation": "fix", "verification": {"status": "PASSED", "notes": "ok"}}, "decision": "ACCEPTED", "decision_reason": None, "resolution": "OPEN", "resolved_at": None, "created_at": now})
    platform.add_message({"id": message_id, "project_id": project_id, "role": "user", "content": "seeded chat", "created_at": now})
    platform.add_yaml({"id": yaml_id, "project_id": project_id, "content": "version: 1\nproject:\nplatform:\nanalysis:\n", "valid": True, "errors": [], "generated_at": now})

    memory.create_memory({
        "id": memory_id, "project_id": project_id, "type": "PROJECT_FACT",
        "statement": "Seeded intelligence for deletion coverage.",
        "scope": {"type": "SYMBOL", "symbol": "ota_install"},
        "evidence": [{"symbol": "ota_install", "file": "src/main.c", "line": 4, "description": "seed"}],
        "source": {"type": "AUTOMATIC", "finding_id": finding_id, "engineer_note": "seed"},
        "status": "ACTIVE", "state": "REINFORCED", "proposed_by": "AI", "approved_by": "AUTOMATIC",
        "commit_sha": None, "created_at": now, "updated_at": now, "confidence": 0.8, "origin": "SYNTHESIZER",
        "observation_count": 2, "reinforcement_count": 1, "fingerprint": f"seed-fingerprint{tag}",
    })
    memory.create_proposal({"id": proposal_id, "project_id": project_id, "type": "PROJECT_FACT", "statement": "Seeded proposal.", "scope": {"type": "PROJECT"}, "evidence": [{"symbol": "ota_install"}], "source": {"type": "ENGINEER_AUTHORED", "engineer_note": "seed"}, "proposed_by": "ENGINEER", "commit_sha": None, "created_at": now})
    memory.add_revalidation_events([{"memory_id": memory_id, "commit_sha": None, "observation": {"symbol": "ota_install", "caller": "other_task"}, "reason": "seed", "created_at": now}])
    memory.add_intelligence_links([{"memory_id": memory_id, "project_id": project_id, "link_kind": "SYMBOL", "link_value": "ota_install", "role": "PRIMARY", "created_at": now}])
    memory.add_intelligence_evidence([{"memory_id": memory_id, "project_id": project_id, "kind": "SOURCE", "file": "src/main.c", "line": 4, "symbol": "ota_install", "file_hash": "abc", "description": "seed", "fingerprint": f"ev-fp{tag}", "created_at": now}])
    memory.add_intelligence_observation({"memory_id": memory_id, "project_id": project_id, "kind": "SYNTHESIS", "from_state": None, "to_state": "PROVISIONAL", "detail": "seed", "created_at": now})
    memory.add_intelligence_conflict({"memory_id": memory_id, "project_id": project_id, "evidence": {"items": []}, "snapshot": {}, "resolution_state": "OPEN", "detail": "seed", "created_at": now, "resolved_at": None})
    memory.create_learning_job({"id": job_id, "project_id": project_id, "trigger": "REVIEW_COMPLETED", "trigger_payload": {"review_id": review_id}, "status": "COMPLETED", "created_at": now})
    memory.upsert_review_learning_summary({"review_id": review_id, "project_id": project_id, "job_id": job_id, "status": "COMPLETED", "counts": {"provisional": 1}, "error": None, "created_at": now, "updated_at": now})


def rows_for(database: str, table: str, project_id: str) -> int:
    with sqlite3.connect(database) as conn:
        if table == "revalidation_events":
            return conn.execute("SELECT COUNT(*) FROM revalidation_events WHERE memory_id IN (SELECT id FROM memories WHERE project_id = ?)", (project_id,)).fetchone()[0]
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)).fetchone()[0]  # noqa: S608 - closed table list from PROJECT_TABLES


def test_delete_removes_all_project_scoped_rows_but_not_the_source_directory(tmp_path):
    database = str(tmp_path / "firmsight.db")
    root = workspace(tmp_path, "ota-device")
    api = TestClient(create_app(database, str(root)))
    project_id = import_project(api, root, "ota-device")
    seed_everything(database, project_id)

    assert rows_for(database, "memories", project_id) == 1 and rows_for(database, "reviews", project_id) == 1
    response = api.delete(f"/api/projects/{project_id}")
    assert response.status_code == 204, response.text

    assert api.get(f"/api/projects/{project_id}").status_code == 404
    assert api.get(f"/api/projects/{project_id}/files").status_code == 404
    assert api.get(f"/api/projects/{project_id}/findings").status_code == 404
    assert api.get(f"/api/projects/{project_id}/intelligence").status_code == 404
    assert api.get("/api/intelligence/MEM-SEED").status_code == 404
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0

    for table in PROJECT_TABLES:
        assert rows_for(database, table, project_id) == 0, table
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects WHERE id = ?", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 0  # shared tables preserved
    assert (root / "ota-device" / "src" / "main.c").is_file(), "local firmware directory must never be deleted"
    assert api.get("/api/projects").json() == []


def test_delete_preserves_other_projects_and_global_settings(tmp_path):
    database = str(tmp_path / "firmsight.db")
    root = workspace(tmp_path, "a")
    workspace(tmp_path, "b")
    api = TestClient(create_app(database, str(root)))
    id_a = import_project(api, root, "a")
    id_b = import_project(api, root, "b")
    seed_everything(database, id_a)
    seed_everything(database, id_b, tag="-B")
    # Global settings must survive deletion; the legacy four-role payload also
    # proves the backward-compatible update path stays intact.
    legacy_models = {role: "z-ai/glm-5.3-flash" for role in ("investigator", "verifier", "chat", "yaml_generator")}
    assert api.put("/api/settings", json={"provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions", "models": legacy_models}).status_code == 200

    assert api.delete(f"/api/projects/{id_a}").status_code == 204

    survivor = api.get(f"/api/projects/{id_b}").json()
    assert survivor["file_count"] == 1 and survivor["symbol_count"] >= 1
    assert rows_for(database, "memories", id_b) == 1
    assert rows_for(database, "reviews", id_b) == 1
    assert rows_for(database, "project_files", id_b) == 1
    settings = api.get("/api/settings").json()
    assert settings["provider"] == "openrouter"
    assert "memory_synthesizer" in settings["models"] and settings["models"]["memory_synthesizer"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM application_settings").fetchone()[0] == 1
    assert (root / "a" / "src" / "main.c").is_file() and (root / "b" / "src" / "main.c").is_file()


def test_running_review_blocks_deletion_with_409(tmp_path):
    database = str(tmp_path / "firmsight.db")
    root = workspace(tmp_path, "ota-device")
    api = TestClient(create_app(database, str(root)))
    project_id = import_project(api, root, "ota-device")
    seed_everything(database, project_id)
    from app.platform_repository import PlatformRepository
    platform = PlatformRepository(database)
    now = "2026-09-12T00:00:00+00:00"
    platform.create_review({"id": "REV-RUNNING", "project_id": project_id, "scope": "Full Project", "focus": ["ota"], "context_files": [], "status": "RUNNING", "progress": [], "created_at": now, "completed_at": None})

    blocked = api.delete(f"/api/projects/{project_id}")
    assert blocked.status_code == 409
    assert "running AI review" in blocked.json()["detail"]
    # Nothing was partially deleted.
    assert api.get(f"/api/projects/{project_id}").status_code == 200
    assert rows_for(database, "memories", project_id) == 1
    assert rows_for(database, "reviews", project_id) == 2


def test_queued_or_running_intelligence_job_blocks_deletion_with_409(tmp_path):
    for status_value in ("QUEUED", "RUNNING"):
        database = str(tmp_path / f"firmsight-{status_value}.db")
        root = workspace(tmp_path, f"device-{status_value}")
        api = TestClient(create_app(database, str(root)))
        project_id = import_project(api, root, f"device-{status_value}")
        seed_everything(database, project_id)
        from app.repository import MemoryRepository
        MemoryRepository(database).create_learning_job({"id": f"LRN-{status_value}", "project_id": project_id, "trigger": "REVIEW_COMPLETED", "trigger_payload": {}, "status": status_value, "created_at": "2026-09-12T00:00:00+00:00"})

        blocked = api.delete(f"/api/projects/{project_id}")
        assert blocked.status_code == 409
        assert "Project Intelligence" in blocked.json()["detail"]
        assert api.get(f"/api/projects/{project_id}").status_code == 200
        assert rows_for(database, "intelligence_learning_jobs", project_id) == 2


def test_unknown_project_returns_404_and_leaves_data_intact(tmp_path):
    database = str(tmp_path / "firmsight.db")
    root = workspace(tmp_path, "ota-device")
    api = TestClient(create_app(database, str(root)))
    project_id = import_project(api, root, "ota-device")
    seed_everything(database, project_id)

    assert api.delete("/api/projects/PRJ-NOPE").status_code == 404
    assert api.get(f"/api/projects/{project_id}").status_code == 200
    assert rows_for(database, "memories", project_id) == 1
