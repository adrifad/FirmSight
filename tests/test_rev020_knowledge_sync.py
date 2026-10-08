from __future__ import annotations

from datetime import UTC, datetime

from app.knowledge_base_service import KnowledgeBaseService
from app.knowledge_index_service import KnowledgeIndexService
from app.knowledge_schemas import VaultSettingsUpdate
from app.platform_repository import PlatformRepository
from app.repository import MemoryRepository
from app.settings_service import SettingsService


def _project_and_memory(tmp_path):
    database = tmp_path / "firmsight.db"
    vault = tmp_path / "vault"
    project_id = "PROJ-REV020"
    now = datetime.now(UTC).isoformat()
    platform = PlatformRepository(str(database))
    platform.create_project({
        "id": project_id, "name": "REV-020 Fixture", "description": "",
        "source_type": "MANUAL", "language": None, "framework": None, "target": None,
        "build_system": None, "source_directory": None, "created_at": now, "updated_at": now,
    })
    settings = SettingsService(platform)
    assert settings.update_vault(VaultSettingsUpdate(root=str(vault))).status.value == "READY"
    memory = MemoryRepository(str(database))
    memory.create_memory({
        "id": "MEM-REV0201", "project_id": project_id, "type": "PROJECT_FACT",
        "statement": "The project owns the measurement queue.", "scope": {"type": "PROJECT"},
        "evidence": [], "source": {"type": "AUTOMATIC"}, "status": "ACTIVE",
        "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
        "state": "REINFORCED", "fingerprint": "rev020-fingerprint",
    })
    return database, vault, project_id, memory, platform, settings


def _sync_once(service: KnowledgeBaseService, project_id: str):
    return service.sync_project(project_id)


def test_external_engineer_notes_survive_and_are_indexable(tmp_path):
    database, vault, project_id, memory, platform, settings = _project_and_memory(tmp_path)
    service = KnowledgeBaseService(memory, platform, settings)
    _sync_once(service, project_id)
    markdown = next(vault.rglob("MEM-REV0201.md"))
    markdown.write_text(markdown.read_text(encoding="utf-8").replace(
        "## Engineer Notes\n\n", "## Engineer Notes\n\nKeep this note from Obsidian.\n"
    ), encoding="utf-8")

    report = _sync_once(service, project_id)
    assert report.regenerated == 1
    assert "Keep this note from Obsidian." in markdown.read_text(encoding="utf-8")
    document = next(item for item in memory.list_knowledge_documents(project_id) if item["intelligence_id"] == "MEM-REV0201")
    assert document["source_status"] == "ENGINEER_EDITED"
    indexed = KnowledgeIndexService(memory).index_project(project_id)
    assert indexed.chunks_written > 0
    assert any("Keep this note from Obsidian." in chunk["body"] for chunk in memory.list_knowledge_chunks(project_id))
    observations = memory.list_intelligence_observations("MEM-REV0201")
    assert any(item["kind"] == "ENGINEER_MARKDOWN_EDIT" for item in observations)


def test_malformed_markdown_is_quarantined_and_not_overwritten(tmp_path):
    _database, vault, project_id, memory, platform, settings = _project_and_memory(tmp_path)
    service = KnowledgeBaseService(memory, platform, settings)
    _sync_once(service, project_id)
    markdown = next(vault.rglob("MEM-REV0201.md"))
    malformed = "---\nproject_id: [unterminated\n---\n\nengineer text\n"
    markdown.write_text(malformed, encoding="utf-8")

    report = _sync_once(service, project_id)
    assert report.quarantined == 1
    assert str(markdown.relative_to(markdown.parents[2])) in report.quarantined_paths
    assert markdown.read_text(encoding="utf-8") == malformed


def test_symlink_markdown_is_not_read_or_replaced(tmp_path):
    _database, vault, project_id, memory, platform, settings = _project_and_memory(tmp_path)
    service = KnowledgeBaseService(memory, platform, settings)
    _sync_once(service, project_id)
    markdown = next(vault.rglob("MEM-REV0201.md"))
    target = tmp_path / "outside.md"
    target.write_text("outside target must remain untouched\n", encoding="utf-8")
    markdown.unlink()
    markdown.symlink_to(target)

    report = _sync_once(service, project_id)
    assert report.quarantined == 1
    assert markdown.is_symlink()
    assert target.read_text(encoding="utf-8") == "outside target must remain untouched\n"


def test_symlinked_type_directory_is_not_used_for_writes(tmp_path):
    _database, vault, project_id, memory, platform, settings = _project_and_memory(tmp_path)
    service = KnowledgeBaseService(memory, platform, settings)
    _sync_once(service, project_id)
    facts = vault / "Projects" / "rev-020-fixture" / "04-Knowledge" / "Facts"
    outside = tmp_path / "outside-facts"
    outside.mkdir()
    for child in facts.iterdir():
        child.unlink()
    facts.rmdir()
    facts.symlink_to(outside, target_is_directory=True)

    report = _sync_once(service, project_id)
    assert report.quarantined >= 1
    assert facts.is_symlink()
    assert list(outside.iterdir()) == []


def test_explicit_regeneration_rewrites_generated_sections_but_keeps_notes(tmp_path):
    database, vault, project_id, memory, platform, settings = _project_and_memory(tmp_path)
    service = KnowledgeBaseService(memory, platform, settings)
    _sync_once(service, project_id)
    markdown = next(vault.rglob("MEM-REV0201.md"))
    markdown.write_text(markdown.read_text(encoding="utf-8").replace(
        "## Engineer Notes\n\n", "## Engineer Notes\n\nExplicit regeneration note.\n"
    ), encoding="utf-8")
    _sync_once(service, project_id)
    markdown.write_text(markdown.read_text(encoding="utf-8").replace(
        "The project owns the measurement queue.", "Engineer changed generated statement."
    ), encoding="utf-8")

    report = service.sync_project(project_id, regenerate=True)
    assert report.regenerated == 1
    content = markdown.read_text(encoding="utf-8")
    assert "The project owns the measurement queue." in content
    assert "Explicit regeneration note." in content
    assert "Engineer changed generated statement." not in content
