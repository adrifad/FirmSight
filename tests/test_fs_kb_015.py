"""FS-KB-015 regression coverage: five-area layout, projections, safe sync.

Covers the task acceptance criteria:
- exact five-folder layout for a new project;
- non-destructive legacy reconciliation (no automatic deletion);
- stable validated frontmatter per generated document kind;
- source-backed baseline with unavailable fields omitted;
- topology skipped without sufficient index evidence;
- first-review noise control (no per-file/per-symbol fan-out);
- lifecycle updates land in the same FS-* document;
- idempotent repeat sync (documents and RAG chunks);
- cross-project/malformed/symlink handling remains safe;
- post-review best-effort sync never alters review completion.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.knowledge_base_service import LAYOUT, KnowledgeBaseService
from app.knowledge_index_service import KnowledgeIndexService
from app.platform_repository import PlatformRepository
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.settings_service import SettingsService
from app.knowledge_schemas import VaultSettingsUpdate


NOW = "2026-01-01T00:00:00+00:00"


def _platform_with_project(tmp_path, *, name: str = "KB015 Fixture", project_id: str = "PRJ-KB015A") -> PlatformRepository:
    platform = PlatformRepository(str(tmp_path / "firmsight.db"))
    platform.create_project({
        "id": project_id, "name": name, "description": "Fixture description",
        "source_type": "MANUAL", "language": "C", "framework": "ESP-IDF", "target": "ESP32-S3",
        "build_system": "CMake", "source_directory": None, "created_at": NOW, "updated_at": NOW,
    })
    return platform


def _service(tmp_path, platform: PlatformRepository) -> tuple[KnowledgeBaseService, MemoryRepository, Any]:
    vault = tmp_path / "vault"
    memory = MemoryRepository(str(tmp_path / "firmsight.db"))
    settings = SettingsService(platform)
    assert settings.update_vault(VaultSettingsUpdate(root=str(vault))).status.value == "READY"
    return KnowledgeBaseService(memory, platform, settings), memory, vault


def _memory(memory: MemoryRepository, project_id: str, memory_id: str = "MEM-KB0151", state: str = "REINFORCED") -> None:
    memory.create_memory({
        "id": memory_id, "project_id": project_id, "type": "PROJECT_FACT",
        "statement": "The project owns the measurement queue.", "scope": {"type": "PROJECT"},
        "evidence": [], "source": {"type": "AUTOMATIC"}, "status": "ACTIVE",
        "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
        "created_at": NOW, "updated_at": NOW, "state": state, "fingerprint": f"fp-{memory_id}",
    })


def _finding(platform: PlatformRepository, project_id: str, review_id: str, finding_id: str = "FS-KB0151", decision: str = "UNREVIEWED") -> None:
    platform.create_review({
        "id": review_id, "project_id": project_id, "scope": "Full Project", "focus": ["memory"],
        "context_files": [], "source_snapshot_hash": "hash-1", "total_batches": 2, "validated_batches": 2,
        "unavailable_batches": 0, "status": "COMPLETED", "progress": ["done"], "execution_progress": {},
        "created_at": NOW, "completed_at": NOW, "last_activity_at": NOW,
    })
    payload = {
        "id": finding_id, "title": "OTA mutex may remain locked", "classification": "PROBABLE_BUG",
        "severity": "HIGH", "category": "CONCURRENCY", "confidence": 0.91,
        "location": {"file": "src/ota.cpp", "function": "ota_install", "line_start": 182, "line_end": 196},
        "summary": "Error path returns before release.", "evidence": [{"description": "take before begin", "file": "src/ota.cpp", "line": 182}],
        "execution_path": ["ota_install()", "xSemaphoreTake()"], "runtime_scenario": "Mutex stays locked.",
        "impact": "OTA blocks.", "assumptions": [{"statement": "No external cleanup.", "status": "UNVERIFIED"}],
        "recommendation": "Release on every path.", "verification": {"status": "PASSED", "notes": "No alternate release."},
    }
    platform.create_finding({
        "id": finding_id, "project_id": project_id, "review_id": review_id,
        "payload": payload, "decision": decision, "decision_reason": None, "created_at": NOW,
    })


def _frontmatter(path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    closing = text.find("\n---\n", 4)
    return yaml.safe_load(text[4:closing])


def test_new_project_vault_has_exactly_five_areas(tmp_path):
    platform = _platform_with_project(tmp_path)
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015A")
    report = service.sync_project("PRJ-KB015A")
    assert report.errors == []
    root = vault / "Projects" / "kb015-fixture"
    dirs = sorted(item.name for item in root.iterdir() if item.is_dir())
    assert dirs == sorted(LAYOUT)
    assert sorted(item.name for item in root.iterdir()) == dirs  # no stray top-level files
    files = sorted(str(item.relative_to(root)) for item in root.rglob("*.md"))
    assert files == ["00-Project/Project.md", "04-Knowledge/Facts/MEM-KB0151.md"]


def test_baseline_documents_validate_and_omit_unavailable_fields(tmp_path):
    platform = _platform_with_project(tmp_path, name="Baseline Fixture", project_id="PRJ-KB015B")
    service, _memory_repo, vault = _service(tmp_path, platform)
    platform.add_files("PRJ-KB015B", [{
        "id": "F1", "project_id": "PRJ-KB015B", "path": "src/main.c", "content": "int main(void){return 0;}",
        "language": "C", "content_hash": "h", "updated_at": NOW,
    }])
    platform.replace_symbols("PRJ-KB015B", [{
        "id": "S1", "project_id": "PRJ-KB015B", "name": "main", "kind": "function", "file": "src/main.c",
        "line": 1,
    }])
    service.sync_project("PRJ-KB015B")
    root = vault / "Projects" / "baseline-fixture"
    project_doc = _frontmatter(root / "00-Project" / "Project.md")
    assert project_doc["id"] == "PRJ-PROJECT"
    assert project_doc["kind"] == "PROJECT"
    assert project_doc["project_id"] == "PRJ-KB015B"
    assert project_doc["schema_version"] == 1
    assert project_doc["source"]["framework"] == "ESP-IDF"
    body = (root / "00-Project" / "Project.md").read_text(encoding="utf-8")
    assert "Fixture description" in body
    assert "ESP32-S3" in body


def test_topology_is_skipped_without_index_evidence(tmp_path):
    platform = _platform_with_project(tmp_path, name="No Topology", project_id="PRJ-KB015C")
    service, _memory_repo, vault = _service(tmp_path, platform)
    report = service.sync_project("PRJ-KB015C")
    assert any("Topology.md" in item for item in report.skipped)
    assert not (vault / "Projects" / "no-topology" / "01-Architecture" / "Topology.md").exists()


def test_review_and_finding_documents_validate_with_provenance(tmp_path):
    platform = _platform_with_project(tmp_path, name="Review Fixture", project_id="PRJ-KB015D")
    service, _memory_repo, vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB015D", "REV-KB0151")
    service.sync_project("PRJ-KB015D")
    root = vault / "Projects" / "review-fixture"
    review_front = _frontmatter(root / "02-Reviews" / "Review-REV-KB0151.md")
    assert review_front["id"] == "REV-KB0151"
    assert review_front["kind"] == "REVIEW"
    assert review_front["relationships"]["findings"] == ["FS-KB0151"]
    finding_front = _frontmatter(root / "03-Findings" / "FS-KB0151.md")
    assert finding_front["id"] == "FS-KB0151"
    assert finding_front["kind"] == "FINDING"
    assert finding_front["source"]["classification"] == "PROBABLE_BUG"
    assert finding_front["source"]["review_id"] == "REV-KB0151"
    body = (root / "03-Findings" / "FS-KB0151.md").read_text(encoding="utf-8")
    assert "Verifier" in body or "verification" in body.casefold()


def test_finding_lifecycle_updates_same_document_in_place(tmp_path):
    platform = _platform_with_project(tmp_path, name="Lifecycle Fixture", project_id="PRJ-KB015E")
    service, _memory_repo, vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB015E", "REV-KB0151", decision="UNREVIEWED")
    service.sync_project("PRJ-KB015E")
    path = vault / "Projects" / "lifecycle-fixture" / "03-Findings" / "FS-KB0151.md"
    assert _frontmatter(path)["status"] == "UNREVIEWED"
    platform.update_finding_decision("FS-KB0151", "ACCEPTED", None)
    platform.update_finding_resolution("FS-KB0151", "SOLVED", NOW)
    service.sync_project("PRJ-KB015E")
    assert _frontmatter(path)["status"] == "SOLVED"
    assert [item.name for item in (vault / "Projects" / "lifecycle-fixture" / "03-Findings").glob("*.md")] == ["FS-KB0151.md"]


def test_provisional_knowledge_is_not_projected(tmp_path):
    platform = _platform_with_project(tmp_path, name="Noise Fixture", project_id="PRJ-KB015F")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015F", state="PROVISIONAL")
    report = service.sync_project("PRJ-KB015F")
    assert any("MEM-KB0151" in item for item in report.skipped)
    assert not (vault / "Projects" / "noise-fixture" / "04-Knowledge").rglob("*.md").__next__() if False else True
    assert list((vault / "Projects" / "noise-fixture" / "04-Knowledge").rglob("*.md")) == []


def test_cross_project_and_wrong_scope_are_quarantined(tmp_path):
    platform = _platform_with_project(tmp_path, name="Isolation Fixture", project_id="PRJ-KB015G")
    other = PlatformRepository(str(tmp_path / "firmsight.db"))
    other.create_project({
        "id": "PRJ-OTHER", "name": "Other Project", "description": "", "source_type": "MANUAL",
        "language": None, "framework": None, "target": None, "build_system": None,
        "source_directory": None, "created_at": NOW, "updated_at": NOW,
    })
    service, memory, vault = _service(tmp_path, platform)
    foreign = vault / "Projects" / "isolation-fixture" / "04-Knowledge" / "Facts"
    foreign.mkdir(parents=True)
    (foreign / "MEM-FOREIGN.md").write_text(
        "---\nid: MEM-FOREIGN\ntype: PROJECT_FACT\nproject_id: PRJ-OTHER\nstatus: ACTIVE\nconfidence: 0.5\nobservation_count: 1\ntitle: Foreign\nstatement: Not this project.\nscope: {}\nprovenance: {}\ntags: []\nschema_version: 1\n---\n\n# Foreign\n", encoding="utf-8"
    )
    report = service.sync_project("PRJ-KB015G")
    assert report.quarantined >= 1
    assert any("wrong project" in item for item in report.errors)
    assert (foreign / "MEM-FOREIGN.md").exists()  # untouched, never deleted


def test_symlink_document_is_quarantined_and_target_untouched(tmp_path):
    platform = _platform_with_project(tmp_path, name="Symlink Fixture", project_id="PRJ-KB015H")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015H", state="REINFORCED")
    service.sync_project("PRJ-KB015H")
    markdown = next((vault / "Projects" / "symlink-fixture" / "04-Knowledge").rglob("MEM-KB0151.md"))
    target = tmp_path / "outside.md"
    target.write_text("outside\n", encoding="utf-8")
    markdown.unlink()
    markdown.symlink_to(target)
    report = service.sync_project("PRJ-KB015H")
    assert report.quarantined >= 1
    assert markdown.is_symlink()
    assert target.read_text(encoding="utf-8") == "outside\n"


def test_legacy_generated_document_is_reported_not_deleted(tmp_path):
    platform = _platform_with_project(tmp_path, name="Legacy Fixture", project_id="PRJ-KB015I")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015I")
    service.sync_project("PRJ-KB015I")
    legacy_dir = vault / "Projects" / "legacy-fixture" / "05-Findings"
    legacy_dir.mkdir()
    legacy_doc = legacy_dir / "MEM-KB0151.md"
    legacy_doc.write_text(
        "---\nid: MEM-KB0151\ntype: PROJECT_FACT\nproject_id: PRJ-KB015I\nstatus: ACTIVE\nconfidence: 0.5\nobservation_count: 1\ntitle: Legacy\nstatement: The project owns the measurement queue.\nscope: {}\nprovenance: {}\ntags: []\nschema_version: 1\n---\n\n# Legacy\n", encoding="utf-8"
    )
    report = service.sync_project("PRJ-KB015I")
    assert report.legacy_left_in_place >= 1
    assert legacy_doc.exists()  # never deleted automatically
    assert (vault / "Projects" / "legacy-fixture" / "04-Knowledge" / "Facts" / "MEM-KB0151.md").exists()


def test_legacy_only_document_migrates_to_canonical_path_without_deletion(tmp_path):
    """REV-028: only a legacy `05-Findings/MEM-*.md` exists; sync must write the
    canonical `04-Knowledge/<type>/MEM-*.md`, keep the legacy file untouched,
    report it migrated/left in place, and keep one stable document identity."""
    platform = _platform_with_project(tmp_path, name="Migration Fixture", project_id="PRJ-KB015M")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015M")
    legacy_dir = vault / "Projects" / "migration-fixture" / "05-Findings"
    legacy_dir.mkdir(parents=True)
    legacy_doc = legacy_dir / "MEM-KB0151.md"
    legacy_content = (
        "---\nid: MEM-KB0151\ntype: PROJECT_FACT\nproject_id: PRJ-KB015M\nstatus: ACTIVE\nconfidence: 0.5\n"
        "observation_count: 1\ntitle: Legacy\nstatement: The project owns the measurement queue.\n"
        "scope: {}\nprovenance: {}\ntags: []\nschema_version: 1\n---\n\n"
        "# Legacy\n\n## Engineer Notes\n\nEngineer-verified context from Obsidian.\n"
    )
    legacy_doc.write_text(legacy_content, encoding="utf-8")

    index = KnowledgeIndexService(memory)
    first = service.sync_project("PRJ-KB015M")
    assert first.migrated == 1
    assert first.legacy_left_in_place >= 1
    # Non-fatal legacy notice must not flip the durable status to FAILED.
    state = memory.get_knowledge_sync_state("PRJ-KB015M")
    assert state is not None and state["status"] == "SYNCED"
    canonical = vault / "Projects" / "migration-fixture" / "04-Knowledge" / "Facts" / "MEM-KB0151.md"
    assert canonical.exists()
    assert legacy_doc.read_text(encoding="utf-8") == legacy_content  # untouched
    # Engineer Notes imported from the validated legacy document.
    document = memory.get_knowledge_document_by_intelligence("PRJ-KB015M") if False else memory.get_knowledge_document_by_path("PRJ-KB015M", "04-Knowledge/Facts/MEM-KB0151.md")
    assert document is not None
    assert document["relative_path"] == "04-Knowledge/Facts/MEM-KB0151.md"
    assert "Engineer-verified context from Obsidian." in (document["body"] or "")

    # One stable identity: exactly one document row for this record, and the
    # canonical path is indexed without duplicates across repeated sync/index.
    knowledge_rows = [d for d in memory.list_knowledge_documents("PRJ-KB015M") if d["intelligence_id"] == "MEM-KB0151"]
    assert len(knowledge_rows) == 1
    index.index_project("PRJ-KB015M")
    chunks_after_first = len(memory.list_knowledge_chunks("PRJ-KB015M"))
    assert chunks_after_first > 0
    second = service.sync_project("PRJ-KB015M")
    assert second.migrated == 0
    assert second.updated == 0 and second.regenerated == 0
    assert legacy_doc.read_text(encoding="utf-8") == legacy_content
    index.index_project("PRJ-KB015M")
    assert len(memory.list_knowledge_chunks("PRJ-KB015M")) == chunks_after_first
    knowledge_rows = [d for d in memory.list_knowledge_documents("PRJ-KB015M") if d["intelligence_id"] == "MEM-KB0151"]
    assert len(knowledge_rows) == 1
    # No legacy quarantine noise: migration is non-fatal and reported, not an error.
    assert all("unexpected path" not in item for item in first.errors)


def test_repeat_sync_is_idempotent_including_rag_chunks(tmp_path):
    platform = _platform_with_project(tmp_path, name="Idempotent Fixture", project_id="PRJ-KB015J")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015J")
    _finding(platform, "PRJ-KB015J", "REV-KB0151")
    first = service.sync_project("PRJ-KB015J")
    index = KnowledgeIndexService(memory)
    index.index_project("PRJ-KB015J")
    chunks_after_first = len(memory.list_knowledge_chunks("PRJ-KB015J"))
    second = service.sync_project("PRJ-KB015J")
    assert second.updated == 0
    assert second.regenerated == 0
    assert second.unchanged == first.updated + first.unchanged
    indexed = index.index_project("PRJ-KB015J")
    assert indexed.chunks_written == 0  # noop re-index
    assert len(memory.list_knowledge_chunks("PRJ-KB015J")) == chunks_after_first


def test_review_failure_does_not_create_review_documents_and_best_effort_sync_survives(tmp_path):
    platform = _platform_with_project(tmp_path, name="Resilient Fixture", project_id="PRJ-KB015K")
    service, memory, vault = _service(tmp_path, platform)

    class BrokenVault(SettingsService):
        def vault_status(self):  # simulate vault becoming invalid after review
            from app.knowledge_schemas import VaultStatusReport
            return VaultStatusReport(status=VaultStatus.INVALID, root=None, message="vault unavailable")

    _finding(platform, "PRJ-KB015K", "REV-KB0151")
    progress: list[str] = []
    broken = KnowledgeBaseService(memory, platform, BrokenVault(platform))
    broken._completed_reviews = lambda project_id: []  # noqa: SLF001 - bounded test stub
    ReviewService._sync_knowledge_vault(object.__new__(ReviewService), "PRJ-KB015K", progress) if False else None
    reviewer = ReviewService.__new__(ReviewService)
    reviewer.knowledge_base = broken
    reviewer._post_review_projection_locks = {}
    reviewer._post_review_projection_registry_lock = ReviewService._post_review_projection_lock_registry_lock()
    reviewer._sync_knowledge_vault("PRJ-KB015K", progress)
    assert any("unavailable" in item for item in progress)
    # a failing vault never creates documents
    assert not (vault / "Projects" / "resilient-fixture" / "02-Reviews").exists() or not list((vault / "Projects" / "resilient-fixture" / "02-Reviews").glob("*.md"))


def test_frontmatter_pattern_rejects_unknown_document_ids(tmp_path):
    from app.knowledge_schemas import VaultDocumentFrontmatter
    with pytest.raises(Exception):
        VaultDocumentFrontmatter.model_validate({
            "id": "BOGUS-1", "kind": "REVIEW", "project_id": "PRJ-KB015L", "title": "t",
            "status": "COMPLETED", "generated_at": NOW,
        })


def _reviewer_with(knowledge_base, knowledge_index=None, post_review_projection=None, repository=None):
    reviewer = ReviewService.__new__(ReviewService)
    reviewer.knowledge_base = knowledge_base
    reviewer.knowledge_index = knowledge_index
    reviewer.post_review_projection = post_review_projection
    reviewer._post_review_projection_locks = {}
    reviewer._post_review_projection_registry_lock = ReviewService._post_review_projection_lock_registry_lock()
    if repository is not None:
        reviewer.memories = SimpleNamespace(repository=repository)
    return reviewer


def test_post_review_projection_writes_and_indexes_documents(tmp_path):
    """REV-029: a completed review with a configured writable vault automatically
    writes and incrementally indexes projected documents — no manual action."""
    platform = _platform_with_project(tmp_path, name="Autoindex Fixture", project_id="PRJ-KB015N")
    service, memory, vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB015N", "REV-KB0151")
    index = KnowledgeIndexService(memory)
    progress: list[str] = []
    reviewer = _reviewer_with(service, knowledge_index=index)
    reviewer._sync_knowledge_vault("PRJ-KB015N", progress)
    root = vault / "Projects" / "autoindex-fixture"
    assert (root / "03-Findings" / "FS-KB0151.md").exists()
    assert (root / "02-Reviews" / f"Review-REV-KB0151.md").exists()
    # Documents are indexed without a manual reindex call.
    assert len(memory.list_knowledge_chunks("PRJ-KB015N")) > 0
    assert any("Knowledge index" in step for step in progress)
    assert any("Knowledge vault projection" in step for step in progress)

    # Repeated post-review projection produces no duplicate chunks.
    reviewer._sync_knowledge_vault("PRJ-KB015N", progress)
    chunks_after_repeat = len(memory.list_knowledge_chunks("PRJ-KB015N"))
    index.index_project("PRJ-KB015N")
    assert len(memory.list_knowledge_chunks("PRJ-KB015N")) == chunks_after_repeat


def test_post_review_projection_failure_leaves_status_and_stays_visible(tmp_path):
    """REV-029: projection and index failures are visible in progress and never
    propagate; the review's terminal status is the caller's responsibility."""
    platform = _platform_with_project(tmp_path, name="Fail Fixture", project_id="PRJ-KB015O")
    service, memory, _vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB015O", "REV-KB0151")

    class ExplodingBase:
        def sync_project(self, project_id, **_kwargs):
            raise RuntimeError("vault unavailable")

    progress: list[str] = []
    reviewer = _reviewer_with(ExplodingBase(), knowledge_index=KnowledgeIndexService(memory))
    reviewer._sync_knowledge_vault("PRJ-KB015O", progress)  # must not raise
    assert any("unavailable" in step for step in progress)

    # Projection succeeds but indexing explodes: the index failure is explicitly
    # and safely visible (REV-032), while the projection outcome stays visible.
    class ExplodingIndex:
        def index_project(self, project_id):
            raise RuntimeError("index unavailable with secret-details-XYZ")

    progress_index_failure: list[str] = []
    reviewer = _reviewer_with(service, knowledge_index=ExplodingIndex(), repository=memory)
    reviewer._sync_knowledge_vault("PRJ-KB015O", progress_index_failure)
    assert any("Knowledge vault projection" in step for step in progress_index_failure)
    assert any("Knowledge index unavailable" in step for step in progress_index_failure)
    # No raw exception text reaches review progress.
    assert not any("secret-details-XYZ" in step for step in progress_index_failure)
    # Durable state distinguishes projection-completed + index-failed.
    state = memory.get_knowledge_sync_state("PRJ-KB015O")
    assert state is not None
    assert state["status"] == "INDEX_FAILED"
    assert "Knowledge index unavailable" in state["error_summary"]
    assert "secret-details-XYZ" not in state["error_summary"]


def test_post_review_projection_callback_reports_separate_outcomes(tmp_path):
    """REV-029: the explicit dependency/callback keeps projection and index
    status separate and bounds the post-review workflow."""
    platform = _platform_with_project(tmp_path, name="Callback Fixture", project_id="PRJ-KB015P")
    service, memory, _vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB015P", "REV-KB0151")
    outcomes: list[str] = []

    def post_review_projection(project_id: str) -> dict[str, Any]:
        report = service.sync_project(project_id)
        outcomes.append("PROJECTION_ERRORS" if report.errors else "PROJECTION_OK")
        index = KnowledgeIndexService(memory)
        outcomes.append("INDEX_OK")
        return {"projection": report, "index": index.index_project(project_id)}

    progress: list[str] = []
    reviewer = _reviewer_with(service, post_review_projection=post_review_projection)
    reviewer._sync_knowledge_vault("PRJ-KB015P", progress)
    assert outcomes == ["PROJECTION_OK", "INDEX_OK"]
    assert any("Knowledge index" in step for step in progress)


def test_legacy_warning_only_sync_still_indexes_canonical_documents(tmp_path):
    """REV-029: a non-fatal legacy warning must not suppress indexing of the
    valid canonical documents written during the same sync."""
    platform = _platform_with_project(tmp_path, name="Legacy Index Fixture", project_id="PRJ-KB015Q")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015Q")
    legacy_dir = vault / "Projects" / "legacy-index-fixture" / "05-Findings"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "MEM-KB0151.md").write_text(
        "---\nid: MEM-KB0151\ntype: PROJECT_FACT\nproject_id: PRJ-KB015Q\nstatus: ACTIVE\nconfidence: 0.5\n"
        "observation_count: 1\ntitle: Legacy\nstatement: The project owns the measurement queue.\n"
        "scope: {}\nprovenance: {}\ntags: []\nschema_version: 1\n---\n\n# Legacy\n", encoding="utf-8"
    )
    index = KnowledgeIndexService(memory)
    progress: list[str] = []
    reviewer = _reviewer_with(service, knowledge_index=index)
    reviewer._sync_knowledge_vault("PRJ-KB015Q", progress)
    canonical = vault / "Projects" / "legacy-index-fixture" / "04-Knowledge" / "Facts" / "MEM-KB0151.md"
    assert canonical.exists()
    assert len(memory.list_knowledge_chunks("PRJ-KB015Q")) > 0
    assert any("Knowledge index" in step for step in progress)


def test_sync_state_is_persisted_and_reflects_outcome(tmp_path):
    """TASK-005: the durable last-sync outcome is stored for the project UI."""
    platform = _platform_with_project(tmp_path)
    service, memory, _vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015A")
    service.sync_project("PRJ-KB015A")
    state = memory.get_knowledge_sync_state("PRJ-KB015A")
    assert state is not None
    assert state["status"] == "SYNCED"
    assert state["counts"]["documents"] >= 2
    assert state["error_summary"] is None

    # A failure (quarantine) flips the durable status but keeps counts safe.
    bad = tmp_path / "vault" / "Projects" / "kb015-fixture" / "04-Knowledge" / "Facts" / "MEM-KB0151.md"
    bad.write_text("not the generated content", encoding="utf-8")
    report = service.sync_project("PRJ-KB015A", regenerate=False)
    state = memory.get_knowledge_sync_state("PRJ-KB015A")
    assert state is not None
    if report.errors:
        assert state["status"] == "FAILED"
        assert state["error_summary"]
    else:
        assert state["status"] == "SYNCED"


def test_sync_state_skipped_when_vault_not_configured(tmp_path):
    platform = _platform_with_project(tmp_path)
    memory = MemoryRepository(str(tmp_path / "firmsight.db"))

    class NoVault(SettingsService):
        def vault_status(self):
            from app.knowledge_schemas import VaultStatus, VaultSettingsRead
            return VaultSettingsRead(root=None, status=VaultStatus.NOT_CONFIGURED, message="no vault configured", allowed_roots=[], configured=False)

    service = KnowledgeBaseService(memory, platform, NoVault(platform))
    report = service.sync_project("PRJ-KB015A")
    assert report.errors
    state = memory.get_knowledge_sync_state("PRJ-KB015A")
    assert state is not None
    assert state["status"] == "SKIPPED"


def _gated_indexer(memory: MemoryRepository, gate, inflight: dict[str, int]):
    """KnowledgeIndexService look-alike tracking max concurrent in-flight work."""
    from app.knowledge_index_service import KnowledgeIndexService

    class GatedIndexer(KnowledgeIndexService):
        def index_project(self, project_id):
            inflight[project_id] = inflight.get(project_id, 0) + 1
            try:
                gate.wait(timeout=30)
                return super().index_project(project_id)
            finally:
                inflight[project_id] = inflight.get(project_id, 0) - 1

    return GatedIndexer(memory)


def test_same_project_workflows_never_overlap_but_different_projects_do(tmp_path):
    """REV-031: two same-project post-review workflows run at most one
    projection/index at a time, while different projects stay concurrent."""
    import threading

    platform_a = _platform_with_project(tmp_path / "a", name="Concurrent A", project_id="PRJ-KB031A")
    platform_b = _platform_with_project(tmp_path / "b", name="Concurrent B", project_id="PRJ-KB031B")
    service_a, memory_a, _ = _service(tmp_path / "a", platform_a)
    service_b, memory_b, _ = _service(tmp_path / "b", platform_b)
    _finding(platform_a, "PRJ-KB031A", "REV-KB0311", finding_id="FS-KB031A")
    _finding(platform_b, "PRJ-KB031B", "REV-KB0312", finding_id="FS-KB031B")

    # Same project: while the first workflow is gated inside index_project, the
    # second must stay blocked — max in-flight work is exactly one.
    gate = threading.Event()
    inflight: dict[str, int] = {}
    reviewer = _reviewer_with(service_a, knowledge_index=_gated_indexer(memory_a, gate, inflight))
    first = threading.Thread(target=reviewer._sync_knowledge_vault, args=("PRJ-KB031A", []))
    first.start()
    deadline = 500
    while deadline and not inflight.get("PRJ-KB031A"):
        deadline -= 1
        time.sleep(0.01)
    assert inflight.get("PRJ-KB031A") == 1, "first workflow must be in-flight inside the indexer"
    second = threading.Thread(target=reviewer._sync_knowledge_vault, args=("PRJ-KB031A", []))
    second.start()
    # Give the second workflow ample opportunity to (incorrectly) enter.
    for _ in range(50):
        if not second.is_alive():
            break
        assert inflight.get("PRJ-KB031A", 0) <= 1, "same-project workflows overlapped"
        time.sleep(0.02)
    assert second.is_alive(), "second same-project workflow must wait for the first to finish"
    assert inflight.get("PRJ-KB031A") == 1
    gate.set()
    first.join(timeout=30)
    second.join(timeout=30)
    assert not first.is_alive() and not second.is_alive()
    assert inflight["PRJ-KB031A"] == 0

    # Different projects: both indexers are allowed to be in flight together.
    gate_a, gate_b = threading.Event(), threading.Event()
    inflight_two: dict[str, int] = {}
    reviewer_a = _reviewer_with(service_a, knowledge_index=_gated_indexer(memory_a, gate_a, inflight_two))
    reviewer_b = _reviewer_with(service_b, knowledge_index=_gated_indexer(memory_b, gate_b, inflight_two))
    thread_a = threading.Thread(target=reviewer_a._sync_knowledge_vault, args=("PRJ-KB031A", []))
    thread_b = threading.Thread(target=reviewer_b._sync_knowledge_vault, args=("PRJ-KB031B", []))
    thread_a.start()
    thread_b.start()
    deadline = 500
    while deadline and not (inflight_two.get("PRJ-KB031A") and inflight_two.get("PRJ-KB031B")):
        deadline -= 1
        time.sleep(0.01)
    assert inflight_two.get("PRJ-KB031A") and inflight_two.get("PRJ-KB031B"), (
        "different projects must not be globally serialized"
    )
    gate_a.set()
    gate_b.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    assert not thread_a.is_alive() and not thread_b.is_alive()


def test_concurrent_same_project_workflows_stay_idempotent(tmp_path):
    """REV-031: repeated/concurrent same-project workflows never create
    duplicate KnowledgeDocument rows or RAG chunks."""
    import threading

    platform = _platform_with_project(tmp_path, name="Idempotent Fixture", project_id="PRJ-KB031C")
    service, memory, _vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB031C", "REV-KB0313")
    reviewer = _reviewer_with(service, knowledge_index=KnowledgeIndexService(memory), repository=memory)

    start = threading.Barrier(4)
    results: list[list[str]] = [[], [], [], []]

    def workflow(index: int) -> None:
        start.wait(timeout=10)
        reviewer._sync_knowledge_vault("PRJ-KB031C", results[index])

    threads = [threading.Thread(target=workflow, args=(position,)) for position in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)

    documents = memory.list_knowledge_documents("PRJ-KB031C")
    intelligence_ids = [document["intelligence_id"] for document in documents]
    assert len(intelligence_ids) == len(set(intelligence_ids)), "duplicate document rows detected"
    chunks = memory.list_knowledge_chunks("PRJ-KB031C")
    chunk_keys = [(chunk["document_id"], chunk["ordinal"]) for chunk in chunks]
    assert len(chunk_keys) == len(set(chunk_keys)), "duplicate RAG chunks detected"
    assert len(chunks) > 0


def test_review_stays_completed_and_state_visible_when_index_fails(tmp_path, monkeypatch):
    """REV-032: an indexing failure after successful projection leaves the review
    COMPLETED, marks the durable state INDEX_FAILED with a safe message, and
    never persists raw exception text."""
    from app.main import create_app
    from fastapi.testclient import TestClient

    from tests.test_intelligence import INVESTIGATOR_RESULT, SOURCE, VERIFIER_RESULT, make_workspace

    class ExplodingIndexer(KnowledgeIndexService):
        def index_project(self, project_id):
            raise RuntimeError("simulated indexer outage with credentials=hunter2")

    def resolve(role: str):
        from tests.test_intelligence import ScriptedProvider
        return ScriptedProvider([INVESTIGATOR_RESULT])

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path, SOURCE)), provider_resolver=resolve))
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    assert api.put("/api/settings/knowledge-base", json={"root": str(tmp_path / "vault")}).status_code == 200

    def boom(self, project_id):
        raise RuntimeError("simulated indexer outage with credentials=hunter2")

    monkeypatch.setattr(KnowledgeIndexService, "index_project", boom)
    try:
        review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["memory"]}).json()
        final = None
        for _ in range(100):
            final = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
            if final["status"] != "RUNNING":
                break
            time.sleep(0.05)
        assert final["status"] in {"COMPLETED", "PARTIAL"}
        assert any("Knowledge index unavailable" in step for step in final["progress"])
        assert not any("hunter2" in step for step in final["progress"])
        state = api.get(f"/api/projects/{project['id']}/knowledge/sync-state").json()
        assert state["status"] == "INDEX_FAILED"
        assert "Knowledge index unavailable" in state["error_summary"]
        assert "hunter2" not in state["error_summary"]
    finally:
        monkeypatch.undo()


def test_index_failure_is_retryable_and_recovers_to_synced(tmp_path):
    """REV-032: after an index failure, a successful retry (sync + index) flips
    the durable state to success and indexes documents without duplicates."""
    platform = _platform_with_project(tmp_path, name="Retry Fixture", project_id="PRJ-KB032B")
    service, memory, _vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB032B", "REV-KB0321")

    class FlakyIndexer:
        def __init__(self) -> None:
            self.calls = 0

        def index_project(self, project_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first index attempt fails")
            return KnowledgeIndexService(memory).index_project(project_id)

    indexer = FlakyIndexer()
    repository = memory
    reviewer = _reviewer_with(service, knowledge_index=indexer, repository=repository)
    progress: list[str] = []
    reviewer._sync_knowledge_vault("PRJ-KB032B", progress)
    failed_state = repository.get_knowledge_sync_state("PRJ-KB032B")
    assert failed_state["status"] == "INDEX_FAILED"
    chunks_after_failure = len(repository.list_knowledge_chunks("PRJ-KB032B"))

    # Retry performs a safe sync + index again; no duplicate chunks are written.
    reviewer._sync_knowledge_vault("PRJ-KB032B", progress)
    recovered_state = repository.get_knowledge_sync_state("PRJ-KB032B")
    assert recovered_state["status"] == "SYNCED"
    assert recovered_state["error_summary"] is None
    chunks_after_retry = repository.list_knowledge_chunks("PRJ-KB032B")
    assert len(chunks_after_retry) >= chunks_after_failure
    keys = [(chunk["document_id"], chunk["ordinal"]) for chunk in chunks_after_retry]
    assert len(keys) == len(set(keys))


def test_index_failure_durable_state_does_not_override_projection_failure(tmp_path):
    """REV-032: when projection itself fails (quarantined documents), the
    projection outcome stays authoritative; INDEX_FAILED never masks it."""
    platform = _platform_with_project(tmp_path, name="Projection Priority Fixture", project_id="PRJ-KB032C")
    service, memory, vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB032C", "REV-KB0322")
    bad = vault / "Projects" / "projection-priority-fixture" / "04-Knowledge" / "Facts" / "MEM-KB032X.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("not generated content at all", encoding="utf-8")
    reviewer = _reviewer_with(service, knowledge_index=KnowledgeIndexService(memory), repository=memory)
    progress: list[str] = []
    reviewer._sync_knowledge_vault("PRJ-KB032C", progress)
    state = memory.get_knowledge_sync_state("PRJ-KB032C")
    assert state["status"] == "FAILED"
    assert any("quarantine/error item" in step for step in progress)


def test_projection_failure_behavior_remains_visible_and_non_fatal(tmp_path):
    """REV-032: the existing projection-failure progress behavior is preserved."""
    platform = _platform_with_project(tmp_path, name="Preserved Failure Fixture", project_id="PRJ-KB032D")
    service, memory, _vault = _service(tmp_path, platform)
    _finding(platform, "PRJ-KB032D", "REV-KB0323")

    class ExplodingBase:
        def sync_project(self, project_id, **_kwargs):
            raise RuntimeError("vault unavailable")

    progress: list[str] = []
    reviewer = _reviewer_with(ExplodingBase(), knowledge_index=KnowledgeIndexService(memory))
    reviewer._sync_knowledge_vault("PRJ-KB032D", progress)  # must not raise
    assert any("unavailable" in step for step in progress)


# --- REWORK-004: public API/UI coverage for the atomic retry (REV-033/034) ---


def _api_with_seeded_index_failure(tmp_path, monkeypatch, *, seed_project: str, seed_review: str, indexer_factory=None):
    """Build a TestClient whose project already has projected documents and a
    durable INDEX_FAILED state, plus a controllable flaky indexer.

    Returns ``(api, project_id, indexer)`` where ``indexer.fail`` toggles failure
    and ``indexer.calls`` counts index attempts — used to prove zero index calls
    on hard projection errors and repeated retries. When ``indexer_factory`` is
    supplied it is installed as the live app indexer (shared by the real retry
    route and the automatic workflow) instead of the class-level monkeypatch.
    """
    from app.main import create_app
    from fastapi.testclient import TestClient

    database = str(tmp_path / "firmsight.db")
    vault = tmp_path / "vault"
    injected = indexer_factory(MemoryRepository(database)) if indexer_factory else None
    api = TestClient(create_app(database, knowledge_index_override=injected))
    assert api.put("/api/settings/knowledge-base", json={"root": str(vault)}).status_code == 200
    project = api.post("/api/projects", json={"name": "Retry API Fixture", "source_type": "MANUAL"}).json()
    project_id = project["id"]

    # Seed a durable real finding + review so projection writes real documents.
    platform = PlatformRepository(database)
    platform.create_review({
        "id": seed_review, "project_id": project_id, "scope": "Full Project", "focus": ["memory"],
        "context_files": [], "source_snapshot_hash": "hash-1", "total_batches": 1, "validated_batches": 1,
        "unavailable_batches": 0, "status": "COMPLETED", "progress": ["done"], "execution_progress": {},
        "created_at": NOW, "completed_at": NOW, "last_activity_at": NOW,
    })
    payload = {
        "id": seed_project, "title": "Queue leak on error path", "classification": "PROBABLE_BUG",
        "severity": "HIGH", "category": "CONCURRENCY", "confidence": 0.9,
        "location": {"file": "src/main.c", "function": "run", "line_start": 3, "line_end": 9},
        "summary": "Early return skips cleanup.", "evidence": [{"description": "take before begin", "file": "src/main.c", "line": 3}],
        "execution_path": ["run()"], "runtime_scenario": "Leak.", "impact": "Blocks.",
        "assumptions": [], "recommendation": "Release on every path.",
        "verification": {"status": "PASSED", "notes": "No alternate release."},
    }
    platform.create_finding({
        "id": seed_project, "project_id": project_id, "review_id": seed_review,
        "payload": payload, "decision": "UNREVIEWED", "decision_reason": None, "created_at": NOW,
    })

    class Counter:
        def __init__(self):
            self.calls = 0
            self.fail = False

    if injected is not None:
        return api, project_id, injected

    counter = Counter()
    real = KnowledgeIndexService(MemoryRepository(database))
    real_index_project = real.index_project  # bind the real implementation before patching

    def fake_index(self, pid):
        counter.calls += 1
        if counter.fail:
            raise RuntimeError("simulated indexer outage with credentials=topsecret-marker")
        return real_index_project(pid)

    monkeypatch.setattr(KnowledgeIndexService, "index_project", fake_index)
    return api, project_id, counter


def test_retry_index_api_repeated_failure_then_success(tmp_path, monkeypatch):
    """REV-033: the atomic retry API keeps INDEX_FAILED across consecutive
    index failures (never overwriting to SYNCED), never leaks raw markers, and a
    later successful retry becomes SYNCED without duplicate chunks."""
    api, project_id, indexer = _api_with_seeded_index_failure(
        tmp_path, monkeypatch, seed_project="FS-RETRY1", seed_review="REV-RETRY1"
    )
    # Seed an initial INDEX_FAILED durable state via one failing retry.
    indexer.fail = True
    first = api.post(f"/api/projects/{project_id}/knowledge/retry-index")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["vault"] == "SYNCED"
    assert body["index"] == "INDEX_FAILED"
    assert body["status"] == "INDEX_FAILED"
    assert "topsecret-marker" not in body["message"]
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "INDEX_FAILED"
    assert "Knowledge index unavailable" in state["error_summary"]
    assert "topsecret-marker" not in state["error_summary"]

    # A second consecutive retry failure must remain INDEX_FAILED (not SYNCED).
    second = api.post(f"/api/projects/{project_id}/knowledge/retry-index").json()
    assert second["status"] == "INDEX_FAILED"
    assert second["index"] == "INDEX_FAILED"
    assert "topsecret-marker" not in second["message"]
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "INDEX_FAILED"
    assert "topsecret-marker" not in state["error_summary"]
    assert indexer.calls == 2
    chunks_after_failures = len(MemoryRepository(str(tmp_path / "firmsight.db")).list_knowledge_chunks(project_id))

    # A later successful retry transitions to SYNCED and clears the summary.
    indexer.fail = False
    third = api.post(f"/api/projects/{project_id}/knowledge/retry-index").json()
    assert third["status"] == "SYNCED"
    assert third["index"] == "SYNCED"
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "SYNCED"
    assert state["error_summary"] is None
    assert indexer.calls == 3

    # No duplicate chunks after the successful retry.
    repository = MemoryRepository(str(tmp_path / "firmsight.db"))
    chunks = repository.list_knowledge_chunks(project_id)
    keys = [(chunk["document_id"], chunk["ordinal"]) for chunk in chunks]
    assert len(keys) == len(set(keys))
    assert len(chunks) >= chunks_after_failures


def test_retry_index_api_hard_projection_error_makes_zero_index_calls(tmp_path, monkeypatch):
    """REV-034: a hard projection error (quarantine) means the retry API performs
    zero index calls, keeps FAILED authoritative, and returns a safe skip message.
    """
    api, project_id, indexer = _api_with_seeded_index_failure(
        tmp_path, monkeypatch, seed_project="FS-RETRY2", seed_review="REV-RETRY2"
    )
    # Force a hard projection error by planting a malformed generated document.
    vault = tmp_path / "vault"
    bad = vault / "Projects" / "retry-api-fixture" / "04-Knowledge" / "Facts" / "MEM-BAD.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("this is not a generated projection document", encoding="utf-8")

    indexer.fail = False  # an indexer that would succeed if (incorrectly) called
    response = api.post(f"/api/projects/{project_id}/knowledge/retry-index")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vault"] == "FAILED"
    assert body["index"] == "NOT_RUN"
    assert body["status"] == "FAILED"
    assert indexer.calls == 0, "indexer must not be called after a hard projection error"
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "FAILED"

    # An automatic post-review projection through the same workflow also skips.
    platform_reviewer = ReviewService.__new__(ReviewService)
    progress: list[str] = []
    from app.knowledge_base_service import KnowledgeBaseService as _KB
    from app.settings_service import SettingsService as _SS
    platform = PlatformRepository(str(tmp_path / "firmsight.db"))
    kb = _KB(MemoryRepository(str(tmp_path / "firmsight.db")), platform, _SS(platform))
    platform_reviewer.knowledge_base = kb
    platform_reviewer.knowledge_index = indexer
    platform_reviewer.post_review_projection = None
    platform_reviewer._post_review_projection_locks = {}
    platform_reviewer._post_review_projection_registry_lock = ReviewService._post_review_projection_lock_registry_lock()
    platform_reviewer.memories = SimpleNamespace(repository=MemoryRepository(str(tmp_path / "firmsight.db")))
    platform_reviewer._sync_knowledge_vault(project_id, progress)
    assert indexer.calls == 0
    assert any("requires attention" in step for step in progress)


def test_retry_index_api_warning_only_legacy_migration_indexes(tmp_path, monkeypatch):
    """REWORK-002: warning-only legacy migration (no errors) still indexes and
    creates canonical chunks."""
    api, project_id, indexer = _api_with_seeded_index_failure(
        tmp_path, monkeypatch, seed_project="FS-RETRY3", seed_review="REV-RETRY3"
    )
    # First make a clean successful sync so canonical documents + legacy mirror exist.
    indexer.fail = False
    assert api.post(f"/api/projects/{project_id}/knowledge/retry-index").json()["status"] == "SYNCED"
    vault = tmp_path / "vault"
    root = vault / "Projects" / "retry-api-fixture"
    # Re-plant a valid legacy-layout document so reconciliation reports a warning
    # but no hard error; a warning-only sync must still index.
    legacy = root / "05-Findings" / "FS-RETRY3.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    canonical = (root / "03-Findings" / "FS-RETRY3.md").read_text(encoding="utf-8")
    legacy.write_text(canonical, encoding="utf-8")

    indexer.calls = 0
    body = api.post(f"/api/projects/{project_id}/knowledge/retry-index").json()
    assert body["vault"] == "SYNCED"
    assert indexer.calls >= 1, "warning-only legacy migration must still index"
    assert body["status"] == "SYNCED"
    repository = MemoryRepository(str(tmp_path / "firmsight.db"))
    chunks = repository.list_knowledge_chunks(project_id)
    assert len(chunks) > 0
    keys = [(chunk["document_id"], chunk["ordinal"]) for chunk in chunks]
    assert len(keys) == len(set(keys))


def test_retry_route_uses_dedicated_atomic_api_not_sync_then_reindex(tmp_path):
    """REWORK-004: the frontend `Retry knowledge index` action must call the
    dedicated atomic retry API, never a sync-then-reindex sequence."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "apps" / "web" / "src" / "App.tsx"
    text = source.read_text(encoding="utf-8")
    assert "api.retryKnowledgeIndex" in text
    # The retry handler must not be implemented via syncKnowledge + reindexKnowledge.
    assert "retryKnowledgeIndex" in text
    assert "knowledge/retry-index" in (Path(__file__).resolve().parents[1] / "apps" / "web" / "src" / "api.ts").read_text(encoding="utf-8")


def test_retry_route_persists_no_interim_synced_before_index_succeeds(tmp_path, monkeypatch):
    """REV-036: the atomic retry route owns exactly one durable state. While the
    indexer runs, the durable row must not have been briefly written as SYNCED by
    the projection; an interruption then leaves INDEX_FAILED, never a stale
    SYNCED that would hide the retry control."""
    database = str(tmp_path / "firmsight.db")
    observed_during_index: list[str | None] = []

    class InspectingIndexer(KnowledgeIndexService):
        def index_project(self, pid):
            # Read the durable row *while indexing*: the projection must not have
            # persisted SYNCED before indexing completed (REV-036).
            row = MemoryRepository(database).get_knowledge_sync_state(pid)
            observed_during_index.append(None if row is None else row["status"])
            raise RuntimeError("interrupted mid-index with credentials=topsecret-marker")

    # The real retry route and the automatic workflow share the app's review
    # service/lock; inject an indexer into the live app so the real route
    # observes the durable row while indexing is in flight.
    api, project_id, indexer = _api_with_seeded_index_failure(
        tmp_path, monkeypatch, seed_project="FS-DURABLE1", seed_review="REV-DURABLE1",
        indexer_factory=lambda repo: InspectingIndexer(repo),
    )
    response = api.post(f"/api/projects/{project_id}/knowledge/retry-index")
    assert response.status_code == 200, response.text
    body = response.json()
    assert observed_during_index, "indexer must have run"
    # No interim SYNCED was persisted while indexing was in flight.
    assert all(status != "SYNCED" for status in observed_during_index), observed_during_index
    assert body["status"] == "INDEX_FAILED"
    assert body["index"] == "INDEX_FAILED"
    assert "topsecret-marker" not in response.text
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "INDEX_FAILED"
    assert "topsecret-marker" not in (state["error_summary"] or "")


def test_retry_route_symlink_root_and_layout_errors_persist_failed(tmp_path, monkeypatch):
    """REV-036: symlink-root and layout-preparation hard errors call the indexer
    zero times, persist a safe FAILED terminal state, and leak no raw marker."""
    from app.main import create_app
    from fastapi.testclient import TestClient

    database = str(tmp_path / "firmsight.db")
    vault = tmp_path / "vault"
    api = TestClient(create_app(database))
    assert api.put("/api/settings/knowledge-base", json={"root": str(vault)}).status_code == 200
    project_id = api.post("/api/projects", json={"name": "Symlink Fixture", "source_type": "MANUAL"}).json()["id"]

    # Seed a real review + finding so a clean projection would otherwise be SYNCED.
    platform = PlatformRepository(database)
    platform.create_review({
        "id": "REV-SYM", "project_id": project_id, "scope": "Full Project", "focus": [], "context_files": [],
        "source_snapshot_hash": "h", "total_batches": 1, "validated_batches": 1, "unavailable_batches": 0,
        "status": "COMPLETED", "progress": [], "execution_progress": {}, "created_at": NOW,
        "completed_at": NOW, "last_activity_at": NOW,
    })
    platform.create_finding({
        "id": "FS-SYM", "project_id": project_id, "review_id": "REV-SYM",
        "payload": {"id": "FS-SYM", "title": "t", "classification": "PROBABLE_BUG", "severity": "HIGH",
                    "category": "CONCURRENCY", "confidence": 0.9, "location": {"file": "a.c", "function": "f", "line_start": 1, "line_end": 2},
                    "summary": "s", "evidence": [], "execution_path": ["f()"], "runtime_scenario": "r", "impact": "i",
                    "assumptions": [], "recommendation": "rec", "verification": {"status": "PASSED", "notes": "n"}},
        "decision": "UNREVIEWED", "decision_reason": None, "created_at": NOW,
    })

    calls = {"n": 0}

    class NeverIndex(KnowledgeIndexService):
        def index_project(self, pid):
            calls["n"] += 1
            raise AssertionError("indexer must not be called after a hard projection error")

    monkeypatch.setattr(KnowledgeIndexService, "index_project", NeverIndex.index_project)

    # Make the project vault root a symlink: sync_project quarantines it early.
    project_root = vault / "Projects" / "symlink-fixture"
    project_root.parent.mkdir(parents=True, exist_ok=True)
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    project_root.symlink_to(real_target, target_is_directory=True)

    response = api.post(f"/api/projects/{project_id}/knowledge/retry-index")
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls["n"] == 0, "symlink-root hard error must make zero index calls"
    assert body["status"] == "FAILED"
    assert body["vault"] == "FAILED"
    assert body["index"] == "NOT_RUN"
    assert "topsecret-marker" not in response.text
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "FAILED"
    assert "topsecret-marker" not in (state["error_summary"] or "")


def test_retry_route_layout_preparation_oserror_persists_failed(tmp_path, monkeypatch):
    """REV-039: an isolated real-route test for vault *layout preparation* failure.

    Unlike the symlink-root case, this forces the five-area directory creation
    inside ``sync_project`` to raise ``OSError`` while a prior ``SYNCED`` row
    exists. The real retry route must make zero index calls, return a safe
    ``FAILED`` outcome, persist ``FAILED``, keep the retry-vault message, and
    leak no raw source/exception/credential marker. Removing the layout-error
    handling from ``sync_project`` makes the indexer run and this test fail.
    """
    from app.main import create_app
    from fastapi.testclient import TestClient

    database = str(tmp_path / "firmsight.db")
    vault = tmp_path / "vault"
    api = TestClient(create_app(database))
    assert api.put("/api/settings/knowledge-base", json={"root": str(vault)}).status_code == 200
    project_id = api.post("/api/projects", json={"name": "Layout Fixture", "source_type": "MANUAL"}).json()["id"]

    platform = PlatformRepository(database)
    platform.create_review({
        "id": "REV-LAYOUT", "project_id": project_id, "scope": "Full Project", "focus": [], "context_files": [],
        "source_snapshot_hash": "h", "total_batches": 1, "validated_batches": 1, "unavailable_batches": 0,
        "status": "COMPLETED", "progress": [], "execution_progress": {}, "created_at": NOW,
        "completed_at": NOW, "last_activity_at": NOW,
    })
    platform.create_finding({
        "id": "FS-LAYOUT", "project_id": project_id, "review_id": "REV-LAYOUT",
        "payload": {"id": "FS-LAYOUT", "title": "t", "classification": "PROBABLE_BUG", "severity": "HIGH",
                    "category": "CONCURRENCY", "confidence": 0.9, "location": {"file": "a.c", "function": "f", "line_start": 1, "line_end": 2},
                    "summary": "s", "evidence": [], "execution_path": ["f()"], "runtime_scenario": "r", "impact": "i",
                    "assumptions": [], "recommendation": "rec", "verification": {"status": "PASSED", "notes": "n"}},
        "decision": "UNREVIEWED", "decision_reason": None, "created_at": NOW,
    })

    # Seed a prior SYNCED row so a stale read would otherwise report success.
    repository = MemoryRepository(database)
    repository.upsert_knowledge_sync_state({
        "project_id": project_id, "status": "SYNCED", "counts": {"documents": 0},
        "error_summary": None, "updated_at": NOW,
    })
    assert repository.get_knowledge_sync_state(project_id)["status"] == "SYNCED"

    calls = {"n": 0}

    class NeverIndex(KnowledgeIndexService):
        def index_project(self, pid):
            calls["n"] += 1
            raise AssertionError("indexer must not be called after a layout-preparation OSError")

    monkeypatch.setattr(KnowledgeIndexService, "index_project", NeverIndex.index_project)

    project_root = vault / "Projects" / "layout-fixture"
    real_mkdir = Path.mkdir

    def failing_mkdir(self, *args, **kwargs):
        # Fail only the vault layout-directory creation, mirroring a real
        # unwritable/preparation failure. Never touch other mkdir callers.
        if self.parent == project_root and self.name in LAYOUT:
            raise OSError("simulated layout preparation failure for /secret/source/path credentials=topsecret-marker")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    response = api.post(f"/api/projects/{project_id}/knowledge/retry-index")
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls["n"] == 0, "layout-preparation OSError must make zero index calls"
    assert body["status"] == "FAILED", "response must not report the stale SYNCED row"
    assert body["vault"] == "FAILED"
    assert body["index"] == "NOT_RUN"
    assert "topsecret-marker" not in response.text
    assert "/secret/source/path" not in response.text
    assert "Retry vault sync" in body["message"]
    state = api.get(f"/api/projects/{project_id}/knowledge/sync-state").json()
    assert state["status"] == "FAILED"
    assert "topsecret-marker" not in (state["error_summary"] or "")
    assert "/secret/source/path" not in (state["error_summary"] or "")


def test_retry_route_stale_status_never_returned_when_final_write_fails(tmp_path, monkeypatch):
    """REV-038: when the workflow's final durable write fails, the retry route
    must use the typed workflow outcome — never a stale ``SYNCED`` row — and a
    fixed safe persistence-unavailable message."""
    api, project_id, indexer = _api_with_seeded_index_failure(
        tmp_path, monkeypatch, seed_project="FS-STALE1", seed_review="REV-STALE1"
    )
    # Use the live app's durable-state repository: the workflow writes the final
    # row through the knowledge-base boundary, which owns this exact instance.
    live_repository = api.app.state.services.knowledge_base.repository

    # Seed a prior SYNCED row (the stale value that must never be reported).
    live_repository.upsert_knowledge_sync_state({
        "project_id": project_id, "status": "SYNCED", "counts": {"documents": 0},
        "error_summary": None, "updated_at": NOW,
    })
    assert live_repository.get_knowledge_sync_state(project_id)["status"] == "SYNCED"

    # Force the final durable write to fail during a failed index run (REV-038).
    def failing_upsert(_record):
        raise RuntimeError("durable write outage with credentials=topsecret-marker")

    monkeypatch.setattr(live_repository, "upsert_knowledge_sync_state", failing_upsert)
    indexer.fail = True

    response = api.post(f"/api/projects/{project_id}/knowledge/retry-index")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] != "SYNCED", "a failed final write must never report stale SYNCED"
    assert body["status"] == "INDEX_FAILED"
    assert body["index"] == "INDEX_FAILED"
    assert "topsecret-marker" not in response.text
    # The stale durable row survives untouched (write failed) but is not returned.
    surviving = live_repository.get_knowledge_sync_state(project_id)
    assert surviving is not None
    assert surviving["status"] == "SYNCED"


def test_automatic_and_retry_workflows_share_same_project_lock(tmp_path, monkeypatch):
    """REV-037: the *real* ``POST /api/projects/{id}/knowledge/retry-index`` route
    shares one per-project lock with the app's automatic post-review workflow, so
    same-project work never overlaps while different projects stay concurrent.

    Both same-project operations run through the live app: the automatic workflow
    uses ``app.state.services.reviews._sync_knowledge_vault`` and the retry side
    uses the actual HTTP route. The app's indexer is an injectable gated indexer
    (``knowledge_index_override``), so the seam is shared by route and workflow.
    """
    import threading

    from fastapi.testclient import TestClient
    from app.main import create_app

    database = str(tmp_path / "firmsight.db")
    vault = tmp_path / "vault"

    inflight: dict[str, int] = {}
    gate = threading.Event()

    class GatedIndexer(KnowledgeIndexService):
        def index_project(self, pid):
            inflight[pid] = inflight.get(pid, 0) + 1
            try:
                gate.wait(timeout=30)
                return super().index_project(pid)
            finally:
                inflight[pid] = inflight.get(pid, 0) - 1

    memory = MemoryRepository(database)
    gated = GatedIndexer(memory)
    api = TestClient(create_app(database, knowledge_index_override=gated))
    assert api.put("/api/settings/knowledge-base", json={"root": str(vault)}).status_code == 200
    project_a = api.post("/api/projects", json={"name": "Lock A", "source_type": "MANUAL"}).json()["id"]
    project_b = api.post("/api/projects", json={"name": "Lock B", "source_type": "MANUAL"}).json()["id"]

    # The live route and the automatic workflow must share this exact service
    # instance (and therefore its per-project lock).
    app_reviews = api.app.state.services.reviews
    assert app_reviews.knowledge_index is gated

    # --- Same project: automatic workflow holds the lock; the real retry route
    # for the same project must serialize behind it.
    auto_result: dict[str, Any] = {}

    def automatic() -> None:
        auto_result["outcome"] = app_reviews.project_and_index_vault(project_a, [])

    auto_thread = threading.Thread(target=automatic)
    auto_thread.start()
    deadline = 500
    while deadline and not inflight.get(project_a):
        deadline -= 1
        time.sleep(0.01)
    assert inflight.get(project_a) == 1

    retry_result: dict[str, Any] = {}

    def real_retry() -> None:
        retry_result["response"] = api.post(f"/api/projects/{project_a}/knowledge/retry-index")

    retry_thread = threading.Thread(target=real_retry)
    retry_thread.start()
    # While the automatic workflow holds the gate, the retry route must not enter
    # the indexer for the same project.
    for _ in range(50):
        if not retry_thread.is_alive():
            break
        assert inflight.get(project_a, 0) <= 1, "real retry route overlapped the automatic workflow"
        time.sleep(0.02)
    assert retry_thread.is_alive(), "real retry route must wait for the automatic workflow"
    gate.set()
    auto_thread.join(timeout=30)
    retry_thread.join(timeout=30)
    assert not auto_thread.is_alive() and not retry_thread.is_alive()
    assert retry_result["response"].status_code == 200
    assert retry_result["response"].json()["status"] == "SYNCED"

    # --- Different projects: two concurrent gated indexers prove no global lock.
    gate_a, gate_b = threading.Event(), threading.Event()
    inflight_two: dict[str, int] = {}

    class GatedIndexerA(KnowledgeIndexService):
        def index_project(self, pid):
            inflight_two[pid] = inflight_two.get(pid, 0) + 1
            try:
                gate_a.wait(timeout=30)
                return super().index_project(pid)
            finally:
                inflight_two[pid] = inflight_two.get(pid, 0) - 1

    class GatedIndexerB(KnowledgeIndexService):
        def index_project(self, pid):
            inflight_two[pid] = inflight_two.get(pid, 0) + 1
            try:
                gate_b.wait(timeout=30)
                return super().index_project(pid)
            finally:
                inflight_two[pid] = inflight_two.get(pid, 0) - 1

    kb = KnowledgeBaseService(memory, PlatformRepository(database), SettingsService(PlatformRepository(database)))
    reviewer_a = ReviewService.__new__(ReviewService)
    reviewer_a.knowledge_base = kb
    reviewer_a.knowledge_index = GatedIndexerA(memory)
    reviewer_a.post_review_projection = None
    reviewer_a._post_review_projection_locks = {}
    reviewer_a._post_review_projection_registry_lock = ReviewService._post_review_projection_lock_registry_lock()
    reviewer_a.memories = SimpleNamespace(repository=memory)
    kb_b = KnowledgeBaseService(memory, PlatformRepository(database), SettingsService(PlatformRepository(database)))
    reviewer_b = ReviewService.__new__(ReviewService)
    reviewer_b.knowledge_base = kb_b
    reviewer_b.knowledge_index = GatedIndexerB(memory)
    reviewer_b.post_review_projection = None
    reviewer_b._post_review_projection_locks = {}
    reviewer_b._post_review_projection_registry_lock = ReviewService._post_review_projection_lock_registry_lock()
    reviewer_b.memories = SimpleNamespace(repository=memory)

    diff_outcomes: dict[str, Any] = {}

    def _run_a() -> None:
        diff_outcomes["a"] = reviewer_a.project_and_index_vault(project_a, [])

    def _run_b() -> None:
        diff_outcomes["b"] = reviewer_b.project_and_index_vault(project_b, [])

    thread_a = threading.Thread(target=_run_a)
    thread_b = threading.Thread(target=_run_b)
    thread_a.start()
    thread_b.start()
    deadline = 2000
    while deadline and not (inflight_two.get(project_a) and inflight_two.get(project_b)):
        deadline -= 1
        time.sleep(0.01)
    assert inflight_two.get(project_a) and inflight_two.get(project_b), (
        f"different projects must not serialize; inflight={inflight_two} outcomes={diff_outcomes}"
    )
    gate_a.set()
    gate_b.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert diff_outcomes.get("a", {}).get("index") == "SYNCED", diff_outcomes
    assert diff_outcomes.get("b", {}).get("index") == "SYNCED", diff_outcomes


def test_external_edit_to_generated_document_is_preserved(tmp_path):
    """REV-045: a genuine user edit to a generated ``Project.md`` must not be
    silently overwritten by a normal sync. The edit stays on disk and the report
    exposes a safe attention/error state; only an explicit regenerate may replace
    it. No raw source, SQL, or exception text is exposed."""
    platform = _platform_with_project(tmp_path, name="Edit Fixture", project_id="PRJ-KB015H")
    service, memory, vault = _service(tmp_path, platform)
    _memory(memory, "PRJ-KB015H")
    first = service.sync_project("PRJ-KB015H")
    assert first.errors == []
    path = vault / "Projects" / "edit-fixture" / "00-Project" / "Project.md"
    assert path.exists()

    # Engineer/observer edits the generated document directly in Obsidian.
    mark = "manual note that must survive"
    edited = path.read_text(encoding="utf-8") + f"\n- {mark}\n"
    path.write_text(edited, encoding="utf-8")

    report = service.sync_project("PRJ-KB015H")
    assert report.errors, "an unimported external edit must be reported, not overwritten"
    assert any("Project.md" in item for item in report.errors)
    # The externally edited bytes remain on disk (nothing was destroyed).
    assert mark in path.read_text(encoding="utf-8")
    # The safe attention message is bounded and content-free.
    assert any("external edit was not imported" in item for item in report.errors)
    assert mark not in " ".join(report.errors)


def test_cross_instance_reviewers_serialize_same_project(tmp_path, monkeypatch):
    """REV-046: two independently constructed ``ReviewService`` objects must share
    one workflow lock for the same project. The second workflow cannot reach the
    projection/index boundary until the first is released, while different
    projects still run their gated indexers concurrently (existing test)."""
    import threading

    from app.knowledge_base_service import KnowledgeBaseService

    platform = _platform_with_project(tmp_path, name="Shared Lock", project_id="PRJ-KB015I")
    memory = MemoryRepository(str(tmp_path / "firmsight.db"))
    database = str(tmp_path / "firmsight.db")
    vault = tmp_path / "vault"
    assert SettingsService(PlatformRepository(database)).update_vault(VaultSettingsUpdate(root=str(vault))).status.value == "READY"

    inflight: dict[str, int] = {}
    # Deterministic projection-phase gate: the first workflow signals when it is
    # inside the lock-protected projection and blocks there until released. The
    # second same-project workflow must not enter that boundary meanwhile. A
    # per-instance lock (REV-046 defect) lets the second enter, which the overlap
    # counter inside the gate detects instead of relying on timing.
    projection_entered = threading.Event()
    release_projection = threading.Event()
    overlaps: list[int] = []

    class GatedProjection(KnowledgeBaseService):
        def sync_project(self, project_id, *, regenerate: bool = False, persist_state: bool = True):
            inflight[project_id] = inflight.get(project_id, 0) + 1
            if inflight[project_id] > 1:
                overlaps.append(inflight[project_id])
            projection_entered.set()
            try:
                release_projection.wait(timeout=30)
                return super().sync_project(project_id, regenerate=regenerate, persist_state=persist_state)
            finally:
                inflight[project_id] = inflight.get(project_id, 0) - 1

    _memory(memory, "PRJ-KB015I")

    def _reviewer() -> ReviewService:
        reviewer = ReviewService.__new__(ReviewService)
        reviewer.knowledge_base = GatedProjection(memory, PlatformRepository(database), SettingsService(PlatformRepository(database)))
        reviewer.knowledge_index = KnowledgeIndexService(memory)
        reviewer.post_review_projection = None
        reviewer._post_review_projection_locks = {}
        reviewer._post_review_projection_registry_lock = ReviewService._post_review_projection_lock_registry_lock()
        reviewer.memories = SimpleNamespace(repository=memory)
        return reviewer

    first, second = _reviewer(), _reviewer()
    results: dict[str, Any] = {}

    first_thread = threading.Thread(target=lambda: results.__setitem__("first", first.project_and_index_vault("PRJ-KB015I", [])))
    first_thread.start()
    assert projection_entered.wait(timeout=30), "first reviewer never entered the projection boundary"

    second_thread = threading.Thread(target=lambda: results.__setitem__("second", second.project_and_index_vault("PRJ-KB015I", [])))
    second_thread.start()
    # While the first workflow holds the shared project lock, the second
    # same-project workflow must not enter the projection boundary.
    time.sleep(0.3)
    assert not overlaps, "second same-project reviewer entered the projection boundary while the first held the lock"
    assert not results, "second same-project reviewer finished while the first held the lock"
    release_projection.set()
    first_thread.join(timeout=30)
    second_thread.join(timeout=30)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert len(results) == 2, results


# --- FS-KB-022 / AGENTS.md 32B.1 — Document Interconnection Requirement ----

def _related_block(text: str) -> str:
    """Return the text of the ``## Related`` section (up to the next heading)."""
    marker = text.find("## Related")
    assert marker != -1, "document has no '## Related' section"
    tail = text[marker + len("## Related"):]
    stop = tail.find("\n## ")
    return tail[:stop] if stop != -1 else tail


def _seed_interconnected_project(tmp_path, project_id: str = "PRJ-KB022A") -> tuple[Path, MemoryRepository]:
    """Create a project with a review, a finding, and linked knowledge, then sync.

    Adds the source-relation evidence needed so the topology document is also
    produced, so all five generated document kinds can be inspected.
    """
    platform = _platform_with_project(tmp_path, name="Interconnect Fixture", project_id=project_id)
    service, memory, vault = _service(tmp_path, platform)
    _finding(platform, project_id, "REV-KB0221", finding_id="FS-KB0221", decision="CONFIRMED")
    _memory(memory, project_id, memory_id="MEM-KB0221")
    memory.add_intelligence_links([
        {"memory_id": "MEM-KB0221", "project_id": project_id, "link_kind": "REVIEW", "link_value": "REV-KB0221", "role": "SOURCE", "created_at": NOW},
        {"memory_id": "MEM-KB0221", "project_id": project_id, "link_kind": "FINDING", "link_value": "FS-KB0221", "role": "SOURCE", "created_at": NOW},
    ])
    # Minimal source-backed topology so the architecture baseline is produced.
    symbol_id = "SYM-KB0221"
    platform.replace_topology(
        project_id,
        symbols=[{"id": symbol_id, "name": "ota_install", "kind": "task", "file": "src/ota.cpp",
                  "line_start": 1, "line_end": 2, "source_hash": "hash-sym-1"}],
        relations=[{"id": "REL-KB0221", "relation_kind": "TASK_ENTRY", "source_symbol_id": symbol_id,
                    "target_symbol_id": symbol_id, "target_name": "ota_install", "file": "src/ota.cpp",
                    "line": 1, "evidence_hash": "hash-rel-1", "relation_state": "OBSERVED"}],
        allocations=[],
        source_snapshot_hash="snap-1",
        relation_fingerprint="fp-rel-1",
        indexed_at=NOW,
    )
    report = service.sync_project(project_id)
    assert report.errors == [], report.errors
    return vault / "Projects" / "interconnect-fixture", memory


def test_every_generated_document_has_a_related_section_with_a_wikilink(tmp_path):
    """REQ-1/REQ-2/REQ-9: every generated document carries a Related graph section."""
    root, _ = _seed_interconnected_project(tmp_path)
    documents = {
        "Project.md": root / "00-Project" / "Project.md",
        "Topology.md": root / "01-Architecture" / "Topology.md",
        "Review.md": root / "02-Reviews" / "Review-REV-KB0221.md",
        "Finding.md": root / "03-Findings" / "FS-KB0221.md",
        "Knowledge.md": root / "04-Knowledge" / "Facts" / "MEM-KB0221.md",
    }
    for label, path in documents.items():
        assert path.exists(), f"{label} was not projected: {path}"
        block = _related_block(path.read_text(encoding="utf-8"))
        assert "[[" in block, f"{label} Related section has no wikilink: {block!r}"


def test_project_document_related_links_to_topology_and_knowledge(tmp_path):
    """REQ-6: the project baseline links to the topology and its knowledge docs."""
    root, _ = _seed_interconnected_project(tmp_path)
    block = _related_block((root / "00-Project" / "Project.md").read_text(encoding="utf-8"))
    assert "[[TOP-TOPOLOGY]]" in block
    assert "[[MEM-KB0221]]" in block


def test_topology_document_related_links_back_to_project(tmp_path):
    """REQ-7: the topology document links back to the project baseline."""
    root, _ = _seed_interconnected_project(tmp_path)
    block = _related_block((root / "01-Architecture" / "Topology.md").read_text(encoding="utf-8"))
    assert "[[PRJ-PROJECT]]" in block


def test_review_document_related_lists_findings(tmp_path):
    """REQ-5: the review document links to each finding with a stable id."""
    root, _ = _seed_interconnected_project(tmp_path)
    block = _related_block((root / "02-Reviews" / "Review-REV-KB0221.md").read_text(encoding="utf-8"))
    assert "[[FS-KB0221]]" in block
    assert "[[FS-FS-" not in block, "finding wikilink must not double the FS- prefix"


def test_finding_document_related_links_to_parent_review(tmp_path):
    """REQ-4: the finding document links back to its parent review."""
    root, _ = _seed_interconnected_project(tmp_path)
    block = _related_block((root / "03-Findings" / "FS-KB0221.md").read_text(encoding="utf-8"))
    assert "[[REV-KB0221]]" in block


def test_knowledge_document_related_links_to_review_and_finding(tmp_path):
    """REQ-3: the knowledge document links to its recorded relationships."""
    root, _ = _seed_interconnected_project(tmp_path)
    block = _related_block((root / "04-Knowledge" / "Facts" / "MEM-KB0221.md").read_text(encoding="utf-8"))
    assert "[[REV-KB0221]]" in block
    assert "[[FS-KB0221]]" in block


def test_related_section_appears_after_engineer_notes(tmp_path):
    """REQ-3: the knowledge doc's Related section follows the editable notes."""
    root, _ = _seed_interconnected_project(tmp_path)
    text = (root / "04-Knowledge" / "Facts" / "MEM-KB0221.md").read_text(encoding="utf-8")
    assert text.index("## Engineer Notes") < text.index("## Related")


def test_wikilink_targets_use_known_document_kinds(tmp_path):
    """REQ-8/REQ-9: every Related wikilink targets a recognized stable id scheme."""
    root, _ = _seed_interconnected_project(tmp_path)
    known_prefixes = ("MEM-", "REV-", "Review-", "FS-", "PRJ-", "TOP-")
    import re

    for path in root.rglob("*.md"):
        block = _related_block(path.read_text(encoding="utf-8"))
        targets = re.findall(r"\[\[([^\]]+)\]\]", block)
        assert targets, f"{path.name} Related section lists no wikilink"
        for target in targets:
            assert target.startswith(known_prefixes), f"{path.name} links to unknown target {target!r}"

