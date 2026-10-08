"""FS-FIX-013 regression tests.

Covers the confirmed import/index blocker (ownership/lifetime relations must
carry stable, deterministic, project-scoped ids) and Code Viewer local source
sync reliability (add/change/delete, safe errors, and project isolation).

The hypothesis under test for TASK-001 is that ``replace_topology`` must never
receive a relation without a stable ``id``; for TASK-002 it is that a sync
replaces the persisted snapshot without leaking partial or stale state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from app.main import create_app
from app.indexer import FirmwareIndexer
from app.intelligence_service import IntelligenceService
from app.platform_repository import PlatformRepository
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.schemas import canonical_memory_type


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _workspace(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path]:
    root = tmp_path / "firmware-workspace"
    directory = root / "controller"
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _write(target, content)
    return root, directory


def _make_client(tmp_path: Path, root: Path) -> TestClient:
    return TestClient(create_app(str(tmp_path / "firmsight.db"), str(root)))


def test_import_with_direct_call_and_lifetime_relations_succeeds_and_all_relations_have_ids(tmp_path):
    root, directory = _workspace(tmp_path, {
        "src/main.c": (
            "void helper(void) {}\n"
            "void caller(void) { char *p = malloc(8); helper(); hand_off(p); return; }\n"
            "void task_entry(void *arg) { caller(); }\n"
            "void init(void) { xTaskCreate(task_entry, \"t\", 1024, 0, 1, 0); }\n"
        ),
    })
    api = _make_client(tmp_path, root)
    imported = api.post("/api/projects/import-directory", json={"directory": str(directory)})
    assert imported.status_code == 201, imported.text
    project = imported.json()
    assert project["symbol_count"] > 0

    topology = api.get(f"/api/projects/{project['id']}/topology").json()
    kinds = {relation["relation_kind"] for relation in topology["relations"]}
    assert {"CALLS", "ALLOCATES", "TASK_ENTRY"} <= kinds
    assert topology["relations"], "fixture must emit source relations"
    for relation in topology["relations"]:
        assert relation.get("id"), f"relation missing stable id: {relation}"
        if relation["relation_kind"] in {"RETURNS_OWNERSHIP", "STORES_OWNERSHIP", "PASSES_TO_UNKNOWN"}:
            assert relation["relation_state"] == "OBSERVED"


def test_repeat_index_preserves_stable_relation_ids_for_unchanged_source(tmp_path):
    root, directory = _workspace(tmp_path, {
        "src/main.c": "void caller(void) { char *p = malloc(8); hand_off(p); return; }\n",
    })
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()
    first = api.get(f"/api/projects/{project['id']}/topology").json()["relations"]
    assert all(relation.get("id") for relation in first)
    first_ids = [relation["id"] for relation in first]

    assert api.post(f"/api/projects/{project['id']}/index").status_code == 200
    second = api.get(f"/api/projects/{project['id']}/topology").json()["relations"]
    assert [relation["id"] for relation in second] == first_ids


def test_forced_index_failure_leaves_no_partial_project(tmp_path, monkeypatch):
    root, directory = _workspace(tmp_path, {
        "src/main.c": "void caller(void) { char *p = malloc(8); hand_off(p); }\n",
    })
    api = _make_client(tmp_path, root)

    def explode(self, project_id, raw_files, result, lifetime):
        raise RuntimeError("forced index failure for regression test")

    monkeypatch.setattr(ProjectService, "_commit_index", explode)
    try:
        response = api.post("/api/projects/import-directory", json={"directory": str(directory)})
    except RuntimeError as error:
        assert str(error) == "forced index failure for regression test"
    else:
        raise AssertionError(f"expected RuntimeError, got HTTP {response.status_code}: {response.text}")

    assert api.get("/api/projects").json() == []
    assert _has_minimal_project_rows(tmp_path) is False


def _sqlite_connect(tmp_path: Path):
    import sqlite3
    return sqlite3.connect(str(tmp_path / "firmsight.db"))


def _has_minimal_project_rows(tmp_path: Path) -> bool:
    connection = _sqlite_connect(tmp_path)
    try:
        counts = {
            "projects": connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "project_files": connection.execute("SELECT COUNT(*) FROM project_files").fetchone()[0],
            "indexed_symbols": connection.execute("SELECT COUNT(*) FROM indexed_symbols").fetchone()[0],
            "source_relations": connection.execute("SELECT COUNT(*) FROM source_relations").fetchone()[0],
            "topology_snapshots": connection.execute("SELECT COUNT(*) FROM topology_snapshots").fetchone()[0],
        }
        return any(value for value in counts.values())
    finally:
        connection.close()


def test_source_sync_add_change_delete_and_snapshot_replacement(tmp_path):
    root, directory = _workspace(tmp_path, {
        "src/a.c": "void a(void) {}\n",
        "src/b.c": "void b(void) {}\n",
    })
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()

    paths = lambda: [item["path"] for item in api.get(f"/api/projects/{project['id']}/files").json()]

    # Add an eligible source file.
    _write(directory / "src" / "c.c", "void c(void) {}\n")
    synced = api.post(f"/api/projects/{project['id']}/sync-source")
    assert synced.status_code == 200, synced.text
    assert synced.json()["changed_files"] == ["src/c.c"]
    assert synced.json()["file_count"] == 3
    assert "src/c.c" in paths()

    # Change a file's content; the stored hash and index result update.
    _write(directory / "src" / "a.c", "void a(void) { c(); }\n")
    synced = api.post(f"/api/projects/{project['id']}/sync-source")
    assert synced.json()["changed_files"] == ["src/a.c"]
    content = api.get(f"/api/projects/{project['id']}/files/content", params={"path": "src/a.c"}).json()["content"]
    assert "c()" in content

    # Delete an eligible source file; it leaves the snapshot.
    (directory / "src" / "b.c").unlink()
    synced = api.post(f"/api/projects/{project['id']}/sync-source")
    assert synced.json()["changed_files"] == ["src/b.c"]
    assert "src/b.c" not in paths()


def test_sync_with_no_eligible_source_returns_safe_error(tmp_path):
    root, directory = _workspace(tmp_path, {
        "src/a.c": "void a(void) {}\n",
    })
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()
    (directory / "src" / "a.c").unlink()
    response = api.post(f"/api/projects/{project['id']}/sync-source")
    assert response.status_code == 422
    assert "No supported firmware source" in response.json()["detail"]


def test_sync_missing_directory_and_out_of_root_fail_safely(tmp_path):
    root, directory = _workspace(tmp_path, {
        "src/a.c": "void a(void) {}\n",
    })
    outside = tmp_path / "outside-project"
    outside.mkdir()
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()

    missing = api.post(f"/api/projects/{project['id']}/sync-source", json={"directory": str(root / "nope")})
    assert missing.status_code == 404

    out_of_root = api.post(f"/api/projects/{project['id']}/sync-source", json={"directory": str(outside)})
    assert out_of_root.status_code == 403


def test_manual_project_is_isolated_from_local_sync(tmp_path):
    root, directory = _workspace(tmp_path, {
        "src/a.c": "void a(void) {}\n",
    })
    api = _make_client(tmp_path, root)
    imported = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()
    manual = api.post("/api/projects", json={"name": "Manual", "source_type": "MANUAL"}).json()

    assert api.get(f"/api/projects/{imported['id']}/files").json()[0]["path"] == "src/a.c"
    assert api.get(f"/api/projects/{manual['id']}/files").json() == []

    # A local sync may not plant files into an unrelated project.
    imported_topology = api.get(f"/api/projects/{imported['id']}/topology").json()["symbols"]
    assert imported_topology
    assert api.get(f"/api/projects/{manual['id']}/topology").json()["symbols"] == []


# --- REV-023: lifetime ownership relations must affect the topology fingerprint ---

_NL = "\n"
# Both fixtures define the same function and the same allocation; the only
# difference is the ownership escape (RETURNS_OWNERSHIP) on the return line.
_SOURCE_WITH_ESCAPE = (
    f"void *f(void) {{{_NL}"
    f"  char *p = malloc(8);{_NL}"
    f"  return p;{_NL}"
    f"}}{_NL}"
)
_SOURCE_WITHOUT_ESCAPE = (
    f"void *f(void) {{{_NL}"
    f"  char *p = malloc(8);{_NL}"
    f"  return 0;{_NL}"
    f"}}{_NL}"
)


def _fingerprint(api: TestClient, project_id: str) -> str:
    return api.get(f"/api/projects/{project_id}/topology").json()["snapshot"]["relation_fingerprint"]


def _relations(api: TestClient, project_id: str) -> list[tuple[str, str | None, str, str, int]]:
    return sorted(
        (relation["relation_kind"], relation.get("target_name"), relation["relation_state"], relation["file"], relation["line"])
        for relation in api.get(f"/api/projects/{project_id}/topology").json()["relations"]
    )


def test_ownership_escape_only_change_alters_stored_topology_fingerprint(tmp_path):
    """A source change that only adds/removes a lifetime ownership escape must
    change the persisted topology fingerprint while direct/task/allocation facts
    stay identical, and an unchanged reindex must keep the same fingerprint."""
    root, directory = _workspace(tmp_path, {"src/main.c": _SOURCE_WITH_ESCAPE})
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()
    project_id = project["id"]

    topology = api.get(f"/api/projects/{project_id}/topology").json()
    assert any(relation["relation_kind"] == "RETURNS_OWNERSHIP" for relation in topology["relations"])
    fp_escape = _fingerprint(api, project_id)
    relations_escape = _relations(api, project_id)

    # Unchanged reindex preserves the same stored fingerprint.
    assert api.post(f"/api/projects/{project_id}/index").status_code == 200
    assert _fingerprint(api, project_id) == fp_escape

    # Removing only the ownership escape changes the fingerprint, with every
    # non-lifetime topology relation and allocation fact unchanged. Bounded
    # syntax-level data-flow facts are compared separately from topology.
    _write(directory / "src" / "main.c", _SOURCE_WITHOUT_ESCAPE)
    assert api.post(f"/api/projects/{project_id}/sync-source").status_code == 200
    assert _fingerprint(api, project_id) != fp_escape

    relations_without = _relations(api, project_id)
    assert any(relation[0] == "RETURNS_OWNERSHIP" for relation in relations_escape)
    non_lifetime_with_escape = [relation for relation in relations_escape if relation[0] not in {"RETURNS_OWNERSHIP", "STORES_OWNERSHIP", "PASSES_TO_UNKNOWN", "RETURNS_VALUE"}]
    non_lifetime_without = [relation for relation in relations_without if relation[0] not in {"RETURNS_OWNERSHIP", "STORES_OWNERSHIP", "PASSES_TO_UNKNOWN", "RETURNS_VALUE"}]
    assert non_lifetime_without == non_lifetime_with_escape

    # Changing it back restores the same deterministic fingerprint.
    _write(directory / "src" / "main.c", _SOURCE_WITH_ESCAPE)
    api.post(f"/api/projects/{project_id}/sync-source")
    assert _fingerprint(api, project_id) == fp_escape


def test_ownership_escape_change_marks_linked_intelligence_for_revalidation(tmp_path):
    """Project Intelligence linked to a topology whose ownership escape changes
    must be marked NEEDS_REVALIDATION by the deterministic revalidation path."""
    root, directory = _workspace(tmp_path, {"src/main.c": _SOURCE_WITH_ESCAPE})
    database = str(tmp_path / "firmsight.db")
    memory_repo = MemoryRepository(database)
    platform = PlatformRepository(database)
    projects = ProjectService(platform, FirmwareIndexer(), str(root), memory_repository=memory_repo)
    intelligence = IntelligenceService(memory_repo, platform, projects, lambda role: (_ for _ in ()).throw(RuntimeError("no provider")))

    project = projects.import_directory(_import_request(directory))
    fp_escape = platform.topology_snapshot(project.id)["relation_fingerprint"]

    memory_id = _seed_linked_memory(memory_repo, project.id, fp_escape)
    assert intelligence.repository.get_memory(memory_id)["state"] == "VERIFIED"

    # Unchanged source: revalidation keeps the record trusted.
    intelligence.deterministic_source_revalidation(project.id)
    assert intelligence.repository.get_memory(memory_id)["state"] in {"VERIFIED", "REINFORCED"}

    # Remove the ownership escape only, reindex, and revalidate: the changed
    # topology fingerprint deterministically marks the linked knowledge stale.
    (directory / "src" / "main.c").write_text(_SOURCE_WITHOUT_ESCAPE, encoding="utf-8")
    projects.refresh_local_directory(project.id)
    assert platform.topology_snapshot(project.id)["relation_fingerprint"] != fp_escape
    intelligence.deterministic_source_revalidation(project.id)
    assert intelligence.repository.get_memory(memory_id)["state"] == "NEEDS_REVALIDATION"


def _import_request(directory: Path):
    from app.platform_schemas import ProjectDirectoryImport
    return ProjectDirectoryImport(directory=str(directory))


def _seed_linked_memory(repository: MemoryRepository, project_id: str, topology_fingerprint: str) -> str:
    """Persist a verified intelligence record whose scope records the topology
    fingerprint and whose evidence references the indexed source file/symbol."""
    digest = FirmwareIndexer.digest
    now = datetime.now(timezone.utc).isoformat()
    memory_id = f"MEM-{uuid4().hex[:12].upper()}"
    record = {
        "id": memory_id,
        "project_id": project_id,
        "type": "ARCHITECTURE_KNOWLEDGE",
        "statement": "f() is the only owner of the heap buffer it allocates and it returns ownership to its caller.",
        "scope": {"type": "SYMBOL", "symbol": "f", "topology_fingerprint": topology_fingerprint},
        "evidence": [{"symbol": "f", "file": "src/main.c", "line": 2, "description": "Allocation inside f observed at src/main.c."}],
        "source": {"type": "ENGINEER", "engineer_note": "Seeded for REV-023 regression."},
        "status": "verified",
        "state": "VERIFIED",
        "proposed_by": "AI",
        "approved_by": "AUTOMATIC",
        "commit_sha": None,
        "created_at": now,
        "updated_at": now,
        "confidence": 0.9,
        "origin": "SYNTHESIZER",
        "observation_count": 2,
        "reinforcement_count": 1,
        "superseded_by": None,
        "conflict_summary": None,
        "fingerprint": digest(f"{project_id}:{memory_id}"),
    }
    canonical_memory_type(record["type"])
    repository.create_memory(record)
    repository.add_intelligence_evidence([
        {
            "memory_id": memory_id,
            "project_id": project_id,
            "kind": "SOURCE",
            "file": "src/main.c",
            "line": 2,
            "symbol": "f",
            "file_hash": digest("void *f(void) {\n  char *p = malloc(8);\n  return p;\n}\n"),
            "description": "Allocation inside f observed at src/main.c.",
            "fingerprint": digest(f"{project_id}:src/main.c:2:f"),
            "created_at": now,
        }
    ])
    return memory_id


# --- FS-FIX-014: collision-safe topology relations during directory import ---
#
# The live failure was a raw HTTP 500 / `sqlite3.IntegrityError: UNIQUE
# constraint failed: source_relations.id` when two distinct allocations were
# passed to the same unknown callee on one physical source line: the lifetime
# ownership relation identity omitted the allocation/event identity, so two
# different payloads shared one stable id. These tests exercise the real FastAPI
# import route, not a narrow unit seam.

_COLLISION_SOURCE = (
    "void transfer(void) {\n"
    "    char *first = malloc(8);\n"
    "    char *second = malloc(8);\n"
    "    hand_off(first); hand_off(second);\n"
    "}\n"
)


def _import_collision_client(tmp_path: Path) -> tuple[TestClient, Path]:
    root, directory = _workspace(tmp_path, {"src/main.c": _COLLISION_SOURCE})
    return _make_client(tmp_path, root), directory


def test_two_allocations_to_same_callee_on_one_line_import_and_persist_distinct_relations(tmp_path):
    """FS-FIX-014 TASK-014-01: the live collision import must succeed, and both
    distinct ownership escapes must persist with distinct stable ids."""
    api, directory = _import_collision_client(tmp_path)
    imported = api.post("/api/projects/import-directory", json={"directory": str(directory)})
    assert imported.status_code == 201, imported.text
    project = imported.json()
    # 1. Valid ProjectRead JSON body.
    assert set(project) >= {"id", "name", "source_type", "file_count", "symbol_count"}
    assert project["file_count"] > 0
    assert project["symbol_count"] > 0

    topology = api.get(f"/api/projects/{project['id']}/topology").json()
    passes = [relation for relation in topology["relations"] if relation["relation_kind"] == "PASSES_TO_UNKNOWN"]
    # 2. Both allocation ownership escapes persist; neither is silently dropped.
    assert len(passes) == 2, passes
    assert {relation["target_name"] for relation in passes} == {"hand_off"}
    # 3. Every persisted relation id is non-empty and unique.
    ids = [relation["id"] for relation in topology["relations"]]
    assert all(relation.get("id") for relation in topology["relations"])
    assert len(ids) == len(set(ids))
    # Both PASSES_TO_UNKNOWN relations must be distinct ids.
    assert len({relation["id"] for relation in passes}) == 2
    # 4. Provenance stays tied to the expected file/line and observed state; the
    # two relations describe two distinct allocation variables.
    for relation in passes:
        assert relation["file"] == "src/main.c"
        assert relation["line"] == 4
        assert relation["relation_state"] == "OBSERVED"
    assert {relation["metadata"].get("variable") for relation in passes} == {"first", "second"}
    # 5. The project has indexed files and symbols.
    project_id = project["id"]
    assert api.get(f"/api/projects/{project_id}/files").json()
    assert topology["symbols"]


def test_collision_relation_ids_are_stable_across_reindex(tmp_path):
    """FS-FIX-014 TASK-014-02 / REV-043: unchanged source keeps identical
    relation ids *and* an identical persisted topology fingerprint on reindex,
    including the two distinct ownership escapes."""
    api, directory = _import_collision_client(tmp_path)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()
    project_id = project["id"]

    first_topology = api.get(f"/api/projects/{project_id}/topology").json()
    first = first_topology["relations"]
    first_ids = sorted(relation["id"] for relation in first)
    assert len(first_ids) == len(set(first_ids)) and len(first_ids) == len(first)
    first_fingerprint = first_topology["snapshot"]["relation_fingerprint"]
    assert first_fingerprint
    # The two distinct ownership escapes are present before reindex.
    assert len([r for r in first if r["relation_kind"] == "PASSES_TO_UNKNOWN"]) == 2

    assert api.post(f"/api/projects/{project_id}/index").status_code == 200
    second_topology = api.get(f"/api/projects/{project_id}/topology").json()
    second = second_topology["relations"]
    # Identical stable ids across reindex.
    assert sorted(relation["id"] for relation in second) == first_ids
    # Identical persisted topology fingerprint across reindex.
    assert second_topology["snapshot"]["relation_fingerprint"] == first_fingerprint
    # Distinct payloads are retained (both ownership escapes persist).
    assert len([r for r in second if r["relation_kind"] == "PASSES_TO_UNKNOWN"]) == 2


def test_forced_topology_invariant_failure_through_sync_source_is_atomic(tmp_path, monkeypatch):
    """FS-FIX-014 REV-042: a forced same-id/different-payload invariant failure
    through the real POST /api/projects/{id}/sync-source route must return safe
    JSON and leave the previously persisted source snapshot, symbols, relations,
    allocations, and topology fingerprint completely unchanged."""
    from app.memory_lifetime_service import MemoryLifetimeService

    root, directory = _workspace(tmp_path, {
        "src/main.c": "void caller(void) { char *p = malloc(8); hand_off(p); }\n",
    })
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()
    project_id = project["id"]

    before_files = api.get(f"/api/projects/{project_id}/files").json()
    before_topology = api.get(f"/api/projects/{project_id}/topology").json()
    before_snapshot = {
        **{k: v for k, v in before_topology["snapshot"].items()},
    }
    before_symbols = before_topology["symbols"]
    before_relations = sorted(relation["id"] for relation in before_topology["relations"])
    before_allocations = sorted(allocation["id"] for allocation in before_topology["allocations"])
    assert before_files and before_relations

    # Change local source so a successful sync would alter the snapshot...
    _write(directory / "src" / "main.c", "void caller(void) { char *q = malloc(16); hand_off(q); return; }\n")

    # ...then force the final invariant conflict only through the sync derivation.
    original_analyze = MemoryLifetimeService.analyze

    def inject_conflict(self, pid, files, symbols, relations, allocations):
        real = original_analyze(self, pid, files, symbols, relations, allocations)
        if relations:
            poisoned = dict(relations[0])
            poisoned["relation_kind"] = "PASSES_TO_UNKNOWN"
            poisoned["target_name"] = "conflicting-payload"
            real.ownership_relations.append(poisoned)
        return real

    monkeypatch.setattr(MemoryLifetimeService, "analyze", inject_conflict)
    response = api.post(f"/api/projects/{project_id}/sync-source")
    assert response.status_code == 500, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "sqlite" not in response.text.lower()
    assert str(directory) not in response.text
    monkeypatch.undo()

    # The failed sync left the prior source snapshot and topology untouched.
    after_files = api.get(f"/api/projects/{project_id}/files").json()
    assert after_files == before_files
    after_topology = api.get(f"/api/projects/{project_id}/topology").json()
    assert after_topology["snapshot"] == before_snapshot
    assert [s["id"] for s in after_topology["symbols"]] == [s["id"] for s in before_symbols]
    assert sorted(relation["id"] for relation in after_topology["relations"]) == before_relations
    assert sorted(allocation["id"] for allocation in after_topology["allocations"]) == before_allocations

    # A normal local change still syncs and indexes successfully afterwards.
    response_ok = api.post(f"/api/projects/{project_id}/sync-source")
    assert response_ok.status_code == 200, response_ok.text


def test_forced_topology_invariant_conflict_returns_safe_json_and_rolls_back(tmp_path, monkeypatch):
    """FS-FIX-014 TASK-014-04: a forced same-id/different-payload invariant
    failure (injected at the service boundary, not via SQLite) must produce a
    safe JSON error and leave no persisted project, files, symbols, allocations,
    relations, or topology snapshot behind."""
    from app.memory_lifetime_service import MemoryLifetimeService

    root, directory = _workspace(tmp_path, {
        "src/main.c": "void caller(void) { char *p = malloc(8); hand_off(p); }\n",
    })
    api = _make_client(tmp_path, root)

    secret_marker = "SECRET-FIXTURE-MARKER-9f3a"

    def inject_conflict(self, project_id, files, symbols, relations, allocations):
        real = original_analyze(self, project_id, files, symbols, relations, allocations)
        if relations:
            # A relation whose stable id duplicates an existing indexer relation
            # but whose payload differs: the real production invariant (not
            # SQLite) must reject this narrow derivation failure.
            poisoned = dict(relations[0])
            poisoned["id"] = relations[0]["id"]
            poisoned["relation_kind"] = "PASSES_TO_UNKNOWN"
            poisoned["target_name"] = secret_marker
            real.ownership_relations.append(poisoned)
        return real

    original_analyze = MemoryLifetimeService.analyze
    monkeypatch.setattr(MemoryLifetimeService, "analyze", inject_conflict)

    response = api.post("/api/projects/import-directory", json={"directory": str(directory)})
    # 1 + 2. A JSON FastAPI error, not raw-text HTTP 500.
    assert response.status_code == 500, response.text
    assert response.headers["content-type"].startswith("application/json"), response.headers["content-type"]
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail
    # 4. No fixture path, sqlite internals, exception type, or secret marker.
    lowered = response.text.lower()
    for forbidden in ("sqlite", "integrityerror", "traceback", "source_relations", secret_marker.lower()):
        assert forbidden not in lowered, response.text
    assert str(directory) not in response.text
    # 3. Full rollback: no project rows survive.
    assert api.get("/api/projects").json() == []
    assert _has_minimal_project_rows(tmp_path) is False


def test_reindex_path_uses_the_same_topology_invariant(tmp_path, monkeypatch):
    """FS-FIX-014 TASK-014-03: the shared _compute_index()/_commit_index() path
    is used by POST /api/projects/{id}/index (reindex), so the same final
    relation invariant protects it too."""
    from app.memory_lifetime_service import MemoryLifetimeService

    root, directory = _workspace(tmp_path, {
        "src/main.c": "void caller(void) { char *p = malloc(8); hand_off(p); }\n",
    })
    api = _make_client(tmp_path, root)
    project = api.post("/api/projects/import-directory", json={"directory": str(directory)}).json()

    original_analyze = MemoryLifetimeService.analyze

    def inject_conflict(self, project_id, files, symbols, relations, allocations):
        real = original_analyze(self, project_id, files, symbols, relations, allocations)
        if relations:
            poisoned = dict(relations[0])
            poisoned["relation_kind"] = "PASSES_TO_UNKNOWN"
            poisoned["target_name"] = "conflicting-payload"
            real.ownership_relations.append(poisoned)
        return real

    monkeypatch.setattr(MemoryLifetimeService, "analyze", inject_conflict)
    response = api.post(f"/api/projects/{project['id']}/index")
    assert response.status_code == 500, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert "sqlite" not in response.text.lower()


def test_finalize_relations_deduplicates_equivalent_and_rejects_conflicts():
    """The invariant deduplicates byte-for-byte equivalent same-id relations and
    raises the narrow error for a same-id/different-payload conflict."""
    from app.project_service import ProjectService, TopologyDerivationError

    base = {
        "id": "IDX-REL-1",
        "project_id": "PRJ-A",
        "relation_kind": "PASSES_TO_UNKNOWN",
        "source_symbol_id": "IDX-SYM-1",
        "target_symbol_id": None,
        "target_name": "hand_off",
        "file": "src/main.c",
        "line": 4,
        "evidence_hash": "hash-a",
        "confidence": 1.0,
        "relation_state": "OBSERVED",
        "metadata": {"variable": "first"},
    }
    duplicate = dict(base)
    # Byte-for-byte equivalent duplicate collapses to one relation.
    assert ProjectService._finalize_relations([base, duplicate]) == [base]

    conflicting = dict(base)
    conflicting["target_name"] = "other_callee"
    try:
        ProjectService._finalize_relations([base, conflicting])
    except TopologyDerivationError:
        pass
    else:
        raise AssertionError("expected TopologyDerivationError for same-id/different-payload conflict")
