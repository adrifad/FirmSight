"""FS-FIND-001 regression coverage: sequential finding ids and stronger dedup.

Covers the task acceptance criteria:
- new findings get project-scoped sequential ids (FS-001, FS-002, ...);
- existing/legacy random-hex ids are never renamed or renumbered;
- titles are normalized (whitespace, leading case, trailing period, length);
- duplicate detection catches rephrased titles across reviews (>= 0.70);
- duplicate detection catches the same symbol at different lines;
- duplicate detection uses shared evidence and keeps genuinely distinct bugs apart.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.ai_provider import AIProvider
from app.indexer import FirmwareIndexer
from app.main import create_app
from app.platform_schemas import FindingCandidate, ProjectCreate
from app.platform_repository import PlatformRepository
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.service import MemoryService


# --- shared fixtures -------------------------------------------------------

SOURCE = """#include <freertos/semphr.h>
static SemaphoreHandle_t ota_mutex;
esp_err_t ota_install(void) {
  xSemaphoreTake(ota_mutex, portMAX_DELAY);
  esp_err_t err = esp_ota_begin(NULL, OTA_SIZE_UNKNOWN, NULL);
  if (err != ESP_OK) {
    return err;
  }
  xSemaphoreGive(ota_mutex);
  return ESP_OK;
}
"""

INVESTIGATOR_RESULT = json.dumps({"findings": [{
    "title": "ota mutex is not released when setup fails.",
    "classification": "CONFIRMED_BUG",
    "severity": "high",
    "category": "CONCURRENCY",
    "confidence": 0.86,
    "location": {"file": "src/main.c", "function": "ota_install", "line_start": 4, "line_end": 9},
    "summary": "A failed esp_ota_begin returns after the mutex has been acquired.",
    "evidence": [{"description": "Mutex acquired before esp_ota_begin.", "file": "src/main.c", "line": 6}],
    "execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "ESP_FAIL", "return"],
    "runtime_scenario": "If esp_ota_begin fails, the mutex stays taken.",
    "impact": "Future OTA attempts block forever.",
    "assumptions": [{"statement": "No external cleanup exists.", "status": "UNVERIFIED"}],
    "recommendation": "Release the mutex on every exit path.",
}]})
VERIFIER_RESULT = json.dumps({"verdict": "SURVIVES", "notes": "No alternate release path was found.", "remaining_assumptions": []})


class ScriptedProvider:
    """Test-only provider that answers from a scripted queue of JSON payloads."""

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if not self.responses:
            raise RuntimeError("scripted provider exhausted")
        return self.responses.pop(0)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "firmware-workspace"
    src = root / "ota-device" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.c").write_text(SOURCE, encoding="utf-8")
    return root


def _review_app(tmp_path: Path) -> TestClient:
    def resolve(role: str) -> AIProvider:
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([])

    return TestClient(create_app(str(tmp_path / "firmsight.db"), str(_workspace(tmp_path)), provider_resolver=resolve))


def _run_review(api: TestClient, project_id: str) -> dict:
    review = api.post(f"/api/projects/{project_id}/reviews", json={"focus": ["ota", "concurrency"]}).json()
    for _ in range(60):
        status = api.get(f"/api/projects/{project_id}/reviews/{review['id']}").json()
        if status["status"] != "RUNNING":
            return status
        time.sleep(0.05)
    raise AssertionError("review did not finish")


def _service(tmp_path: Path, project_id: str) -> tuple[ReviewService, PlatformRepository]:
    database = str(tmp_path / "firmsight.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    service = ReviewService(
        repository,
        projects,
        lambda _role: None,  # type: ignore[arg-type]
        MemoryService(MemoryRepository(database)),
    )
    return service, repository


def _seed_review(repository: PlatformRepository, project_id: str, review_id: str) -> None:
    repository.create_review({
        "id": review_id, "project_id": project_id, "scope": "Full Project", "focus": ["ota"],
        "context_files": [], "source_snapshot_hash": "hash", "total_batches": 1, "validated_batches": 1,
        "unavailable_batches": 0, "status": "COMPLETED", "progress": [], "execution_progress": {},
        "created_at": "2026-01-01T00:00:00+00:00", "completed_at": "2026-01-01T00:00:00+00:00",
        "last_activity_at": "2026-01-01T00:00:00+00:00",
    })


def _seed_finding(repository: PlatformRepository, project_id: str, review_id: str, finding_id: str, title: str = "OTA mutex leak") -> None:
    payload = {
        "id": finding_id, "title": title, "classification": "CONFIRMED_BUG", "severity": "high",
        "category": "CONCURRENCY", "confidence": 0.9,
        "location": {"file": "src/main.c", "function": "ota_install", "line_start": 4, "line_end": 9},
        "summary": "The mutex is not released on the error path.", "evidence": [{"description": "acquire", "file": "src/main.c", "line": 6}],
        "execution_path": ["ota_install", "xSemaphoreTake"], "runtime_scenario": "Mutex stays locked.",
        "impact": "OTA blocks.", "assumptions": [], "recommendation": "Release on every path.",
        "verification": {"status": "PASSED", "notes": "ok"},
    }
    repository.create_finding({
        "id": finding_id, "project_id": project_id, "review_id": review_id, "payload": payload,
        "decision": "UNREVIEWED", "decision_reason": None, "created_at": "2026-01-01T00:00:00+00:00",
    })


# --- REV-002: sequential id generation -------------------------------------

def test_next_finding_id_starts_at_001_for_empty_project(tmp_path):
    service, _ = _service(tmp_path, "PRJ-X")
    assert service._next_finding_id("PRJ-X") == "FS-001"


def test_project_with_fs_001_and_fs_002_yields_fs_003(tmp_path):
    """REV-002: a project holding FS-001 and FS-002 produces FS-003 next."""
    database = str(tmp_path / "seq.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Sequential"))
    service = ReviewService(repository, projects, lambda _role: None, MemoryService(MemoryRepository(database)))
    _seed_review(repository, project.id, "REV-SEQ")
    _seed_finding(repository, project.id, "REV-SEQ", "FS-001")
    _seed_finding(repository, project.id, "REV-SEQ", "FS-002")

    assert service._next_finding_id(project.id) == "FS-003"


def test_sequence_is_per_project_and_ignores_non_numeric_ids(tmp_path):
    database = str(tmp_path / "perproject.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project_a = projects.create(ProjectCreate(name="A"))
    project_b = projects.create(ProjectCreate(name="B"))
    service = ReviewService(repository, projects, lambda _role: None, MemoryService(MemoryRepository(database)))
    _seed_review(repository, project_a.id, "REV-A")
    _seed_review(repository, project_b.id, "REV-B")
    _seed_finding(repository, project_a.id, "REV-A", "FS-005")
    # Legacy random-hex id and a foreign project's sequential id must not count.
    _seed_finding(repository, project_a.id, "REV-A", "FS-A1B2C3D4")
    _seed_finding(repository, project_b.id, "REV-B", "FS-009")

    assert service._next_finding_id(project_a.id) == "FS-006"
    assert service._next_finding_id(project_b.id) == "FS-010"


def test_new_finding_created_by_review_uses_sequential_id_and_keeps_legacy(tmp_path):
    """End-to-end: a real review creates FS-002 while a legacy FS-A1B2C3D4 stays."""
    api = _review_app(tmp_path)
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    database = str(tmp_path / "firmsight.db")
    repository = PlatformRepository(database)
    _seed_review(repository, project["id"], "REV-LEGACY")
    _seed_finding(repository, project["id"], "REV-LEGACY", "FS-A1B2C3D4", title="Legacy random id finding")

    status = _run_review(api, project["id"])
    assert status["status"] == "COMPLETED", status

    ids = {finding["id"] for finding in api.get(f"/api/projects/{project['id']}/findings").json()}
    assert "FS-A1B2C3D4" in ids, "legacy id must survive"
    assert "FS-001" in ids, f"new finding should be FS-001: {sorted(ids)}"


# --- REQ-2: title normalization --------------------------------------------

def test_normalize_title_trims_collapses_cases_and_drops_period():
    assert ReviewService._normalize_title("  ota   mutex   leak.  ") == "Ota mutex leak"
    assert ReviewService._normalize_title("already Fine") == "Already Fine"
    assert ReviewService._normalize_title("no trailing period") == "No trailing period"


def test_normalize_title_truncates_to_200_characters():
    normalized = ReviewService._normalize_title("x" * 500)
    assert len(normalized) == 200


# --- REQ-3: duplicate detection --------------------------------------------

def _candidate(title: str, summary: str, *, function: str | None = "ota_install", line_start: int = 40, line_end: int = 48, evidence: list[dict] | None = None):
    evidence_items = evidence if evidence is not None else [{"description": "e", "file": "src/ota.cpp", "line": 45}]
    return FindingCandidate.model_validate({
        "title": title, "classification": "PROBABLE_BUG", "severity": "high", "category": "CONCURRENCY",
        "confidence": 0.86, "location": {"file": "src/ota.cpp", "function": function, "line_start": line_start, "line_end": line_end},
        "summary": summary, "evidence": evidence_items, "execution_path": ["a", "b"],
        "runtime_scenario": "scenario text long enough", "impact": "impact text long enough",
        "assumptions": [], "recommendation": "recommendation text long enough",
    })


def test_rephrased_title_at_same_location_is_a_duplicate():
    base = _candidate("OTA mutex is not released when setup fails", "A failed OTA setup returns after the mutex has been acquired.")
    rephrased = _candidate("OTA mutex remains locked on setup error path", "When OTA setup fails, the acquired mutex is not released before returning.")
    # Token similarity of the two titles is 0.62 (< 0.70) but corroborated by
    # shared tokens and summary similarity, so they must still match.
    assert ReviewService._same_finding_candidate(base, rephrased)


def test_genuinely_distinct_bug_in_same_function_is_not_a_duplicate():
    base = _candidate("OTA mutex is not released when setup fails", "A failed OTA setup returns after the mutex has been acquired.")
    distinct = _candidate("OTA error result is ignored by the caller", "The caller continues without handling the failed OTA setup result.")
    assert not ReviewService._same_finding_candidate(base, distinct)


def test_same_symbol_across_wider_line_window_is_a_duplicate():
    base = _candidate("OTA mutex is not released when setup fails", "A failed OTA setup returns after the mutex has been acquired.", line_start=40, line_end=45)
    shifted = _candidate("OTA mutex is not released when setup fails", "A failed OTA setup returns after the mutex has been acquired.", line_start=60, line_end=66)
    assert ReviewService._same_finding_candidate(base, shifted)


def test_distant_lines_without_shared_symbol_are_not_duplicates():
    base = _candidate("OTA mutex is not released when setup fails", "A failed OTA setup returns after the mutex has been acquired.", function=None, line_start=1, line_end=5)
    far = _candidate("OTA mutex is not released when setup fails", "A failed OTA setup returns after the mutex has been acquired.", function=None, line_start=100, line_end=105)
    assert not ReviewService._same_finding_candidate(base, far)


def test_shared_evidence_positions_mark_a_duplicate():
    shared = [
        {"description": "a", "file": "src/ota.cpp", "line": 45},
        {"description": "b", "file": "src/ota.cpp", "line": 52},
    ]
    base = _candidate("Mutex acquire order is inverted", "Two pieces of evidence pin the same lines.", evidence=shared)
    other = _candidate("Inverted mutex acquisition ordering", "Unrelated wording entirely for the summary text here.", evidence=shared)
    assert ReviewService._same_finding_candidate(base, other)


def test_title_similarity_threshold_matches_requirement():
    high = ReviewService._title_similarity("OTA mutex leak on error path", "OTA mutex leak on the error path")
    low = ReviewService._title_similarity("OTA mutex leak on error path", "MQTT publish retries forever")
    assert high >= ReviewService.TITLE_DUPLICATE_THRESHOLD
    assert low < ReviewService.TITLE_DUPLICATE_THRESHOLD
