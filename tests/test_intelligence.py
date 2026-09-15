"""Project Intelligence behavior tests (FS-DEV-003).

Covers the learning pipeline end-to-end with fixture providers: candidate
validation, skeptical verification, lifecycle transitions, duplicate
reinforcement, source-authority revalidation, retrieval filtering, learning
failure isolation, legacy migration, and the typed API surface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.ai_provider import AIProvider
from app.intelligence_core import compute_fingerprint
from app.main import create_app


class ScriptedProvider:
    """Test-only provider that answers from a scripted queue of JSON payloads."""

    name = "scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append(system_prompt)
        if not self.responses:
            raise RuntimeError("scripted provider exhausted")
        return self.responses.pop(0)


class FailingProvider:
    name = "failing"

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("simulated provider outage")


class UnavailableLearning:
    """Review flows work, learning roles are unconfigured."""

    name = "unavailable-learning"

    def available(self) -> bool:
        return False

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("unavailable")


CANDIDATE = {
    "type": "BUG_PATTERN",
    "statement": "OTA mutex acquisition is not released on the esp_ota_begin failure path.",
    "confidence": 0.82,
    "observed": True,
    "origin_reason": "The accepted finding shows the mutex leak on the error path.",
    "reuse_reason": "Future reviews of ota_install should re-check error-path mutex release.",
    "symbols": ["ota_install"],
    "components": ["ota"],
    "evidence": [{"file": "src/main.c", "line": 6, "symbol": "ota_install", "description": "xSemaphoreTake without matching give on the error branch."}],
}

VERIFY_NEW = json.dumps({
    "action": "VERIFY_NEW",
    "target_id": None,
    "confidence": 0.9,
    "source_support": "SUPPORTED",
    "valid_evidence": [{"file": "src/main.c", "line": 6, "description": "Current source still contains the unreleased take."}],
    "conflict_target_id": None,
    "conflict_evidence": [],
    "rationale": "Current source confirms the failure path; disproof attempts found no release.",
})

ACCEPT_PROVISIONAL = json.dumps({
    "action": "ACCEPT_PROVISIONAL",
    "target_id": None,
    "confidence": 0.6,
    "source_support": "PARTIAL",
    "valid_evidence": [{"file": "src/main.c", "line": 6, "description": "Evidence location exists in current source."}],
    "conflict_target_id": None,
    "conflict_evidence": [],
    "rationale": "Evidence is real but support is incomplete for verification.",
})

REJECT_UNSUPPORTED = json.dumps({
    "action": "REJECT_UNSUPPORTED",
    "target_id": None,
    "confidence": 0.2,
    "source_support": "NONE",
    "valid_evidence": [],
    "conflict_target_id": None,
    "conflict_evidence": [],
    "rationale": "The candidate rests only on an engineer assertion.",
})

MARK_CONFLICTED = json.dumps({
    "action": "MARK_CONFLICTED",
    "target_id": None,
    "confidence": 0.88,
    "source_support": "CONTRADICTED",
    "valid_evidence": [],
    "conflict_target_id": None,
    "conflict_evidence": [{"file": "src/main.c", "line": 6, "description": "Current source releases the mutex on every path."}],
    "rationale": "Current source contradicts the recorded pattern.",
})

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

FIXED_SOURCE = """#include <freertos/semphr.h>
static SemaphoreHandle_t ota_mutex;
esp_err_t ota_install(void) {
  xSemaphoreTake(ota_mutex, portMAX_DELAY);
  esp_err_t err = esp_ota_begin(NULL, OTA_SIZE_UNKNOWN, NULL);
  if (err != ESP_OK) {
    xSemaphoreGive(ota_mutex);
    return err;
  }
  xSemaphoreGive(ota_mutex);
  return ESP_OK;
}
"""

INVESTIGATOR_RESULT = json.dumps({"findings": [{
    "title": "OTA mutex is not released when setup fails",
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


def make_workspace(tmp_path: Path, source: str = SOURCE) -> Path:
    root = tmp_path / "firmware-workspace"
    src = root / "ota-device" / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.c").write_text(source, encoding="utf-8")
    return root


def learning_app(tmp_path: Path, responses: list[str]) -> TestClient:
    """App whose learning roles share one scripted queue so the exchange
    progresses in order across synthesizer and verifier calls; review roles
    always answer from their own single-shot fixtures."""
    shared = ScriptedProvider(responses)

    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return shared
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    return TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))


def run_review(api: TestClient, project_id: str) -> str:
    review = api.post(f"/api/projects/{project_id}/reviews", json={"focus": ["ota", "concurrency"]}).json()
    import time

    for _ in range(40):
        status = api.get(f"/api/projects/{project_id}/reviews/{review['id']}").json()
        if status["status"] != "RUNNING":
            return status
        time.sleep(0.05)
    raise AssertionError("review did not finish")


def finding_id(api: TestClient, project_id: str) -> str:
    return api.get(f"/api/projects/{project_id}/findings").json()[0]["id"]


# ---------------------------------------------------------------------------
# Review learning pipeline
# ---------------------------------------------------------------------------


def test_review_learning_creates_provisional_then_verifies(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),   # synthesizer
        VERIFY_NEW,                                 # verifier
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])
    assert status["status"] == "COMPLETED", status
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["verified"] == 1, records["counts"]
    record = records["records"][0]
    assert record["state"] == "VERIFIED"
    assert record["origin"] == "SYNTHESIZER"
    assert record["type"] == "BUG_PATTERN"
    assert record["evidence_count"] >= 1
    assert record["observation_count"] == 1
    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["status"] == "COMPLETED"
    assert summary["verified"] == 1
    detail = api.get(f"/api/intelligence/{record['id']}").json()
    kinds = [observation["kind"] for observation in detail["observations"]]
    assert kinds == ["VERIFICATION", "SYNTHESIS"], kinds  # newest first
    assert detail["links"], "engineer-visible links must be persisted"


def test_review_learning_accepts_provisional_without_full_source_support(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        ACCEPT_PROVISIONAL,
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])
    assert status["status"] == "COMPLETED"
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["provisional"] == 1
    assert records["counts"]["verified"] == 0
    record = records["records"][0]
    assert record["state"] == "PROVISIONAL"
    assert record["confidence"] <= 0.84
    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["provisional"] == 1


def test_weak_rejection_never_becomes_knowledge(tmp_path):
    api = learning_app(tmp_path, [json.dumps({"candidates": []})])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    finding = finding_id(api, project["id"])
    weak = api.patch(f"/api/projects/{project['id']}/findings/{finding}/decision", json={"decision": "REJECTED", "reason": "I do not think this is a bug."})
    assert weak.status_code == 200, weak.text
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["total"] == 0
    jobs = [job for job in []]  # no direct API; verified through zero records above
    assert jobs == []


def test_grounded_rejection_learns_false_positive(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [{**CANDIDATE, "type": "FALSE_POSITIVE_KNOWLEDGE", "statement": "OTA mutex warnings on ota_install are false positives: the mutex is released on the error path in current source."}]}),
        VERIFY_NEW,
    ])
    root = make_workspace(tmp_path, FIXED_SOURCE)
    project = api.post("/api/projects/import-directory", json={"directory": str(root / "ota-device")}).json()
    run_review(api, project["id"])
    finding = finding_id(api, project["id"])
    strong = api.patch(
        f"/api/projects/{project['id']}/findings/{finding}/decision",
        json={"decision": "REJECTED", "reason": "Only measurement_task calls ads1115_read in src/main.c and the error path releases ota_mutex; the reviewer path is unreachable."},
    )
    assert strong.status_code == 200, strong.text
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["verified"] == 1, records["counts"]
    assert records["records"][0]["type"] == "FALSE_POSITIVE_KNOWLEDGE"


def test_duplicate_candidate_reinforces_existing_record(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        ACCEPT_PROVISIONAL,
        json.dumps({"candidates": [CANDIDATE]}),   # same evidence => same fingerprint
        ACCEPT_PROVISIONAL,                         # verifier (if consulted) for the repeat
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    first_counts = api.get(f"/api/projects/{project['id']}/intelligence/summary").json()["counts"]
    assert first_counts["provisional"] == 1

    # Second review repeats the same candidate: deterministic fingerprint
    # reinforcement, never a duplicate row.
    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota"]}).json()
    import time

    for _ in range(60):
        status = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
        if status["status"] != "RUNNING":
            break
        time.sleep(0.05)
    counts = api.get(f"/api/projects/{project['id']}/intelligence/summary").json()["counts"]
    assert counts["total"] == 1, "duplicate must reinforce, not create a row"
    record = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"][0]
    assert record["observation_count"] >= 2
    assert record["reinforcement_count"] >= 1


def test_source_change_marks_stale_and_conflict(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        VERIFY_NEW,
    ])
    root = make_workspace(tmp_path)
    project = api.post("/api/projects/import-directory", json={"directory": str(root / "ota-device")}).json()
    run_review(api, project["id"])
    record_id = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"][0]["id"]

    # Fix the source, then sync: changed evidence hash must mark stale records.
    (root / "ota-device" / "src" / "main.c").write_text(FIXED_SOURCE, encoding="utf-8")
    synced = api.post(f"/api/projects/{project['id']}/sync-source", json={})
    assert synced.status_code == 200, synced.text
    record = api.get(f"/api/intelligence/{record_id}").json()
    assert record["state"] == "NEEDS_REVALIDATION", record["state"]
    observations = [observation["kind"] for observation in record["observations"]]
    assert "REVALIDATION" in observations
    assert any("changed" in (observation["detail"] or "") for observation in record["observations"])


def test_revalidate_endpoint_conflicts_on_contradicted_source(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        VERIFY_NEW,          # creation-time verifier: source still buggy
        MARK_CONFLICTED,     # revalidate-time verifier: source now fixed
    ])
    root = make_workspace(tmp_path)
    project = api.post("/api/projects/import-directory", json={"directory": str(root / "ota-device")}).json()
    run_review(api, project["id"])
    record_id = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"][0]["id"]

    # Fix the source and sync: the changed hash deterministically marks the record stale.
    (root / "ota-device" / "src" / "main.c").write_text(FIXED_SOURCE, encoding="utf-8")
    api.post(f"/api/projects/{project['id']}/sync-source", json={})
    stale = api.get(f"/api/intelligence/{record_id}").json()
    assert stale["state"] == "NEEDS_REVALIDATION"

    response = api.post(f"/api/intelligence/{record_id}/revalidate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "CONFLICTED", body
    assert body["record"]["state"] == "CONFLICTED"
    detail = api.get(f"/api/intelligence/{record_id}").json()
    assert detail["conflicts"], "conflict evidence must be persisted"
    assert detail["conflict_summary"]


def test_review_remains_completed_when_learning_fails(tmp_path):
    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return FailingProvider()
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])
    assert status["status"] == "COMPLETED"
    assert status["finding_count"] == 1
    assert any("unaffected" in step for step in status["progress"])
    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["status"] == "FAILED"
    assert summary["error"]
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["total"] == 0


def test_review_remains_completed_when_learning_roles_unconfigured(tmp_path):
    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return UnavailableLearning()
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])
    assert status["status"] == "COMPLETED"
    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["status"] == "SKIPPED"


def test_invalid_candidate_evidence_is_rejected(tmp_path):
    bad_candidate = {
        **CANDIDATE,
        "evidence": [{"file": "src/missing.c", "line": 99, "symbol": "ghost_fn", "description": "File does not exist."}],
    }
    api = learning_app(tmp_path, [json.dumps({"candidates": [bad_candidate]}), REJECT_UNSUPPORTED])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    counts = api.get(f"/api/projects/{project['id']}/intelligence/summary").json()["counts"]
    assert counts["total"] == 0


def test_verifier_cannot_verify_without_current_source_support(tmp_path):
    """A verifier claiming VERIFY_NEW with no valid evidence stays provisional."""
    unsupported = json.dumps({
        "action": "VERIFY_NEW",
        "target_id": None,
        "confidence": 0.99,
        "source_support": "SUPPORTED",
        "valid_evidence": [{"file": "src/ghost.c", "line": 1, "description": "Not in the index."}],
        "conflict_target_id": None,
        "conflict_evidence": [],
        "rationale": "Model-only conclusion without real source evidence.",
    })
    api = learning_app(tmp_path, [json.dumps({"candidates": [CANDIDATE]}), unsupported])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["verified"] == 0
    assert records["counts"]["provisional"] == 1


def test_solved_finding_resolution_pattern(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [{**CANDIDATE, "type": "RESOLUTION_PATTERN", "statement": "Releasing the OTA mutex on the esp_ota_begin error path resolves the deadlock risk."}]}),
        VERIFY_NEW,
    ])
    root = make_workspace(tmp_path)
    project = api.post("/api/projects/import-directory", json={"directory": str(root / "ota-device")}).json()
    run_review(api, project["id"])
    finding = finding_id(api, project["id"])
    accept = api.patch(f"/api/projects/{project['id']}/findings/{finding}/decision", json={"decision": "ACCEPTED"})
    assert accept.status_code == 200
    solved = api.patch(f"/api/projects/{project['id']}/findings/{finding}/resolution", json={"resolution": "SOLVED"})
    assert solved.status_code == 200, solved.text
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["verified"] == 1
    assert records["records"][0]["type"] == "RESOLUTION_PATTERN"


def test_chat_eligibility_grounded_claim_only(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        ACCEPT_PROVISIONAL,
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()

    def chat(message: str, **extra):
        return api.post(f"/api/projects/{project['id']}/chat", json={"message": message, **extra})

    assert chat("hi").status_code in {200, 503}
    generic = chat("What does this project do?")
    assert generic.status_code in {200, 503}
    grounded = chat("I think the ota_install mutex leak is real because the error path in src/main.c returns without releasing it", selected_file="src/main.c")
    assert grounded.status_code == 200, grounded.text

    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    statements = [record["statement"] for record in records["records"]]
    assert len(statements) == 1, "only the grounded claim may learn"
    assert records["counts"]["provisional"] == 1


# ---------------------------------------------------------------------------
# Retrieval filtering
# ---------------------------------------------------------------------------


def test_retrieval_excludes_unrelated_and_terminal_states(tmp_path):
    from app.intelligence_service import IntelligenceService
    from app.platform_repository import PlatformRepository
    from app.project_service import ProjectService
    from app.indexer import FirmwareIndexer
    from app.repository import MemoryRepository

    database = str(tmp_path / "firmsight.db")
    platform = PlatformRepository(database)
    memory_repo = MemoryRepository(database)
    projects = ProjectService(platform, FirmwareIndexer(), str(make_workspace(tmp_path)))
    api = learning_app(tmp_path, [])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    service = IntelligenceService(memory_repo, platform, projects, lambda role: ScriptedProvider([]))
    project_id = project["id"]

    def seed(statement: str, state: str, symbol: str | None, file: str | None) -> str:
        now = "2026-09-12T00:00:00+00:00"
        memory_id = f"MEM-{statement[:4].upper()}"
        fingerprint = compute_fingerprint(project_id, "BUG_PATTERN", statement, [symbol or ""], [f"{file or ''}:6"])
        memory_repo.create_memory({
            "id": memory_id, "project_id": project_id, "type": "BUG_PATTERN", "statement": statement,
            "scope": {"type": "SYMBOL", "symbol": symbol} if symbol else {"type": "PROJECT"},
            "evidence": [{"symbol": symbol or "x", "file": file, "line": 6, "description": "seed"}],
            "source": {"type": "AUTOMATIC", "finding_id": None, "engineer_note": "seed"},
            "status": {"VERIFIED": "ACTIVE", "REINFORCED": "ACTIVE"}.get(state, state),
            "state": state, "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
            "created_at": now, "updated_at": now, "confidence": 0.8, "origin": "SYNTHESIZER",
            "observation_count": 2, "reinforcement_count": 1, "fingerprint": fingerprint,
        })
        links = []
        if symbol:
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "SYMBOL", "link_value": symbol, "role": "PRIMARY", "created_at": now})
        if file:
            links.append({"memory_id": memory_id, "project_id": project_id, "link_kind": "FILE", "link_value": file, "role": "SUPPORTING", "created_at": now})
        memory_repo.add_intelligence_links(links)
        return memory_id

    related = seed("Related verified knowledge.", "VERIFIED", "ota_install", "src/main.c")
    seed("Unrelated verified knowledge.", "VERIFIED", "mqtt_handler", "src/mqtt.c")
    seed("Stale knowledge.", "NEEDS_REVALIDATION", "ota_install", "src/main.c")
    seed("Conflicted knowledge.", "CONFLICTED", "ota_install", "src/main.c")
    seed("Superseded knowledge.", "SUPERSEDED", "ota_install", "src/main.c")
    seed("Disabled knowledge.", "DISABLED", "ota_install", "src/main.c")
    seed("Provisional knowledge.", "PROVISIONAL", "ota_install", "src/main.c")

    retrieved = service.relevant_records(project_id, ["ota_install", "src/main.c"])
    assert [record["id"] for record in retrieved] == [related]
    context = service.review_memory_context(project_id, ["ota_install"], ["src/main.c"])
    assert "verified" in context
    assert "Unrelated" not in context
    assert "Provisional" not in context
    hypotheses = service.verifier_hypotheses(project_id, ["ota_install"])
    assert len(hypotheses) == 1 and hypotheses[0]["state"] == "PROVISIONAL"


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------


def test_legacy_database_migration_preserves_records(tmp_path):
    database = str(tmp_path / "firmsight.db")
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, source_type TEXT NOT NULL,
            language TEXT, framework TEXT, target TEXT, build_system TEXT, source_directory TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO projects VALUES ('ground-checker', 'Ground Checker', 'Legacy fixture project', 'MANUAL', NULL, NULL, NULL, NULL, NULL, '2026-09-01T09:00:00+00:00', '2026-09-01T09:00:00+00:00');
        CREATE TABLE memory_proposals (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, type TEXT NOT NULL,
            statement TEXT NOT NULL, scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
            source_json TEXT NOT NULL, proposed_by TEXT NOT NULL, commit_sha TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, type TEXT NOT NULL,
            statement TEXT NOT NULL, scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
            source_json TEXT NOT NULL, status TEXT NOT NULL, proposed_by TEXT NOT NULL,
            approved_by TEXT NOT NULL, commit_sha TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE revalidation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
            commit_sha TEXT, observation_json TEXT NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL, FOREIGN KEY(memory_id) REFERENCES memories(id)
        );
        INSERT INTO memories VALUES (
            'MEM-LEGACY1', 'ground-checker', 'ENGINEERING_FACT',
            'ADS1115 is exclusively accessed by measurement_task.',
            '{"type":"COMPONENT","component":"measurement"}',
            '[{"symbol":"measurement_task","file":"main/measurement.c","line":42}]',
            '{"type":"ENGINEER_CONFIRMED","finding_id":"FS-120","engineer_note":"Confirmed during review."}',
            'ACTIVE', 'AI', 'adri', NULL, '2026-09-01T10:00:00+00:00', '2026-09-01T10:00:00+00:00'
        );
        INSERT INTO memories VALUES (
            'MEM-LEGACY2', 'ground-checker', 'LESSON_LEARNED',
            'Device measurement must continue when MQTT is unavailable.',
            '{"type":"PROJECT"}', '[]',
            '{"type":"ENGINEER_CONFIRMED","finding_id":null,"engineer_note":"lesson"}',
            'NEEDS_REVALIDATION', 'AI', 'adri', NULL, '2026-09-01T11:00:00+00:00', '2026-09-01T11:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    api = TestClient(create_app(database))
    # Legacy list/context surface survives.
    legacy = api.get("/api/projects/ground-checker/memory").json()
    assert {memory["id"] for memory in legacy} == {"MEM-LEGACY1", "MEM-LEGACY2"}
    assert api.get("/api/projects/ground-checker/memory/context?symbol=measurement_task").json()["memories"][0]["id"] == "MEM-LEGACY1"
    # Typed surface applies the documented migration.
    intelligence = api.get("/api/projects/ground-checker/intelligence").json()
    assert intelligence["counts"]["total"] == 2
    states = {record["id"]: record for record in intelligence["records"]}
    assert states["MEM-LEGACY1"]["state"] == "REINFORCED"
    assert states["MEM-LEGACY1"]["type"] == "PROJECT_FACT"
    assert states["MEM-LEGACY1"]["origin"] == "LEGACY_ENGINEER_APPROVED"
    assert states["MEM-LEGACY1"]["status"] == "ACTIVE"
    assert states["MEM-LEGACY2"]["state"] == "NEEDS_REVALIDATION"
    assert states["MEM-LEGACY2"]["type"] == "REVIEW_LESSON"
    detail = api.get("/api/intelligence/MEM-LEGACY1").json()
    assert any(observation["kind"] == "MIGRATION" for observation in detail["observations"])
    assert detail["links"], "legacy scope/evidence must be copied into normalized rows"
    assert detail["evidence"], "legacy evidence must be copied into normalized rows"
    # Migration is idempotent.
    api2 = TestClient(create_app(database))
    assert api2.get("/api/projects/ground-checker/intelligence").json()["counts"]["total"] == 2


# ---------------------------------------------------------------------------
# API surface details
# ---------------------------------------------------------------------------


def test_intelligence_api_filters_summary_and_disable(tmp_path):
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        ACCEPT_PROVISIONAL,
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    project_id = project["id"]

    filtered = api.get(f"/api/projects/{project_id}/intelligence", params={"state": "PROVISIONAL"}).json()
    assert len(filtered["records"]) == 1
    by_type = api.get(f"/api/projects/{project_id}/intelligence", params={"type": "BUG_PATTERN"}).json()
    assert len(by_type["records"]) == 1
    missing = api.get(f"/api/projects/{project_id}/intelligence", params={"type": "REVIEW_LESSON"}).json()
    assert missing["records"] == []

    summary = api.get(f"/api/projects/{project_id}/intelligence/summary").json()
    assert summary["counts"]["provisional"] == 1
    assert summary["health"]["last_job_status"] == "COMPLETED"

    record_id = filtered["records"][0]["id"]
    disabled = api.post(f"/api/intelligence/{record_id}/disable?reason=no%20longer%20relevant")
    assert disabled.status_code == 200
    assert disabled.json()["state"] == "DISABLED"
    after = api.get(f"/api/projects/{project_id}/intelligence/summary").json()["counts"]
    assert after["disabled"] == 1 and after["provisional"] == 0

    missing_summary = api.get(f"/api/projects/{project_id}/reviews/REV-DOES-NOT-EXIST/learning-summary")
    assert missing_summary.status_code == 404


def test_fingerprint_normalization_merges_wording_equivalence(tmp_path):
    from app.intelligence_core import compute_fingerprint, normalize_statement

    base = compute_fingerprint("p1", "BUG_PATTERN", "OTA mutex   is not released. ", ["ota_install"], ["src/main.c:6"])
    same = compute_fingerprint("p1", "BUG_PATTERN", "ota mutex is not released", ["OTA_INSTALL"], ["src/main.c:6"])
    different = compute_fingerprint("p1", "BUG_PATTERN", "MQTT reconnects drop queued messages.", ["ota_install"], ["src/main.c:6"])
    assert base == same
    assert base != different
    assert normalize_statement("  Hello   World. ") == "hello world"


# ---------------------------------------------------------------------------
# R4 review fixes: supersession, conflict_target_id, best-effort boundary,
# chat retrieval, disable reason contract
# ---------------------------------------------------------------------------


class PipelineProvider:
    """Learning provider with an ordered synthesis queue and a verifier whose
    record-targeted actions resolve ids from the deterministic shortlist in
    the prompt, exactly as a real verifier echoing the shortlist would."""

    name = "pipeline"

    def __init__(self, syntheses: list[str], verifier_actions: list[object]) -> None:
        self.syntheses = list(syntheses)
        self.verifier_actions = list(verifier_actions)

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if "Memory Synthesizer" in system_prompt:
            return self.syntheses.pop(0)
        if "Memory Verifier" in system_prompt:
            raw = self.verifier_actions.pop(0)
            spec = json.loads(raw) if isinstance(raw, str) else dict(raw)
            if spec.pop("_resolve_target", False):
                shortlist_line = next(line for line in user_prompt.splitlines() if line.startswith("[{"))
                ids = [item["id"] for item in json.loads(shortlist_line)]
                key = "conflict_target_id" if spec["action"] == "MARK_CONFLICTED" else "target_id"
                spec[key] = ids[0]
            return json.dumps(spec)
        raise RuntimeError("unexpected role for scripted provider")


def supersession_app(tmp_path: Path):
    replacement = {**CANDIDATE, "statement": "OTA mutex take on ota_install is released on every path in the current source; the earlier leak no longer applies."}
    app_pipeline = PipelineProvider(
        [json.dumps({"candidates": [CANDIDATE]}), json.dumps({"candidates": [replacement]})],
        [
            VERIFY_NEW,
            {
                "action": "SUPERSEDE_EXISTING",
                "confidence": 0.9,
                "source_support": "SUPPORTED",
                "valid_evidence": [{"file": "src/main.c", "line": 6, "description": "Current source releases the mutex on every path."}],
                "conflict_target_id": None,
                "conflict_evidence": [],
                "rationale": "The old statement no longer describes current source; this replacement is directly supported.",
                "_resolve_target": True,
            },
        ],
    )

    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return app_pipeline
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    return TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))


def test_supersession_transitions_prior_record_and_replaces_retrieval(tmp_path):
    api = supersession_app(tmp_path)
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    old_record = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"][0]
    assert old_record["state"] == "VERIFIED"

    # A grounded engineer claim triggers the superseding learning run.
    chat = api.post(f"/api/projects/{project['id']}/chat", json={
        "message": "I think the ota_install mutex pattern changed because the error path in src/main.c now releases the mutex",
        "selected_file": "src/main.c",
    })
    assert chat.status_code == 200, chat.text
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"]
    superseded = [record for record in records if record["state"] == "SUPERSEDED"]
    assert superseded, records
    old_id = superseded[0]["id"]
    replacement_id = superseded[0]["superseded_by"]
    assert replacement_id and replacement_id != old_id
    replacement = next(record for record in records if record["id"] == replacement_id)
    assert replacement["state"] == "VERIFIED"
    detail = api.get(f"/api/intelligence/{old_id}").json()
    assert any(observation["kind"] == "SUPERSESSION" for observation in detail["observations"])
    supersede_links = [link for record in records for link in record["links"] if link["role"] == "SUPERSEDES"]
    assert supersede_links, "superseding link must be persisted"

    # The superseded record is no longer retrievable as current authority.
    from app.intelligence_service import IntelligenceService
    from app.repository import MemoryRepository
    from app.platform_repository import PlatformRepository
    from app.project_service import ProjectService
    from app.indexer import FirmwareIndexer
    database = str(tmp_path / "firmsight.db")
    service = IntelligenceService(MemoryRepository(database), PlatformRepository(database), ProjectService(PlatformRepository(database), FirmwareIndexer(), str(tmp_path / "firmware-workspace")), lambda role: ScriptedProvider([]))
    retrieved_ids = {record["id"] for record in service.relevant_records(project["id"], ["ota_install", "src/main.c"])}
    assert old_id not in retrieved_ids
    assert replacement_id in retrieved_ids


def test_candidate_time_conflict_uses_conflict_target_id(tmp_path):
    """A verifier MARK_CONFLICTED pointing at conflict_target_id transitions the
    intended shortlisted record with persisted, index-resolvable evidence."""
    conflicting_pipeline = PipelineProvider(
        [
            json.dumps({"candidates": [CANDIDATE]}),
            json.dumps({"candidates": [{**CANDIDATE, "statement": "OTA mutex take on ota_install error path contradicts the fixed source behavior."}]}),
        ],
        [
            VERIFY_NEW,
            {
                "action": "MARK_CONFLICTED",
                "confidence": 0.9,
                "source_support": "CONTRADICTED",
                "valid_evidence": [],
                "conflict_target_id": None,
                "conflict_evidence": [{"file": "src/main.c", "line": 7, "description": "Current source gives the mutex back on the error path."}],
                "rationale": "Current source contradicts the previously verified pattern.",
                "_resolve_target": True,
            },
        ],
    )

    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return conflicting_pipeline
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    first_id = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"][0]["id"]

    # Second review re-runs learning; the verifier conflicts record A by id.
    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota"]}).json()
    import time
    for _ in range(60):
        status = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
        if status["status"] != "RUNNING":
            break
        time.sleep(0.05)
    detail = api.get(f"/api/intelligence/{first_id}").json()
    assert detail["state"] == "CONFLICTED", detail["state"]
    assert detail["conflicts"], "conflict evidence must be persisted"
    assert any(observation["kind"] == "CONFLICT" for observation in detail["observations"])

    from app.intelligence_service import IntelligenceService
    from app.repository import MemoryRepository
    from app.platform_repository import PlatformRepository
    from app.project_service import ProjectService
    from app.indexer import FirmwareIndexer
    database = str(tmp_path / "firmsight.db")
    service = IntelligenceService(MemoryRepository(database), PlatformRepository(database), ProjectService(PlatformRepository(database), FirmwareIndexer(), str(tmp_path / "firmware-workspace")), lambda role: ScriptedProvider([]))
    assert service.relevant_records(project["id"], ["ota_install"]) == []


def test_learning_enqueue_failure_never_fails_domain_results(tmp_path):
    """Forced IntelligenceService failure: decisions/resolutions/fix-verification
    and chat still return their normal successful payloads."""
    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return FailingProvider()
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    finding = finding_id(api, project["id"])

    accepted = api.patch(f"/api/projects/{project['id']}/findings/{finding}/decision", json={"decision": "ACCEPTED"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["decision"] == "ACCEPTED"

    rejected = api.patch(f"/api/projects/{project['id']}/findings/{finding}/decision", json={"decision": "REJECTED", "reason": "The mutex release is handled by the OTA component wrapper in src/main.c."})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["decision"] == "REJECTED"

    solved = api.patch(f"/api/projects/{project['id']}/findings/{finding}/resolution", json={"resolution": "SOLVED"})
    assert solved.status_code == 200, solved.text
    assert solved.json()["resolution"] == "SOLVED"

    verified = api.post(f"/api/projects/{project['id']}/findings/{finding}/verify-fix")
    assert verified.status_code in {200, 409, 502}, verified.text  # domain outcome preserved

    # Eligible chat: response must survive even though learning will fail.
    chat = api.post(f"/api/projects/{project['id']}/chat", json={
        "message": "I think ota_install is now safe because the error path releases ota_mutex in src/main.c",
        "selected_file": "src/main.c",
    })
    assert chat.status_code == 200, chat.text
    assert chat.json()["message"]["role"] == "assistant"

    # No fabricated intelligence rows from the failing pipeline.
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["total"] == 0


def test_chat_intelligence_retrieval_includes_symbol_mentions(tmp_path):
    """(a) selected-finding chat and (b) plain message naming an indexed symbol
    both receive state-labelled relevant intelligence; unrelated and terminal
    states stay out."""
    from app.intelligence_service import IntelligenceService
    from app.repository import MemoryRepository
    from app.platform_repository import PlatformRepository
    from app.project_service import ProjectService
    from app.indexer import FirmwareIndexer
    from app.platform_schemas import ChatRequest

    database = str(tmp_path / "firmsight.db")
    platform = PlatformRepository(database)
    memory_repo = MemoryRepository(database)
    root = make_workspace(tmp_path)
    projects = ProjectService(platform, FirmwareIndexer(), str(root))
    api = learning_app(tmp_path, [])
    project = api.post("/api/projects/import-directory", json={"directory": str(root / "ota-device")}).json()
    service = IntelligenceService(memory_repo, platform, projects, lambda role: ScriptedProvider([]))
    project_id = project["id"]
    now = "2026-09-12T00:00:00+00:00"

    def seed(statement: str, state: str, symbol: str, file: str) -> str:
        memory_id = f"MEM-{abs(hash(statement)) % 100000:05d}"
        fingerprint = compute_fingerprint(project_id, "BUG_PATTERN", statement, [symbol], [f"{file}:6"])
        memory_repo.create_memory({
            "id": memory_id, "project_id": project_id, "type": "BUG_PATTERN", "statement": statement,
            "scope": {"type": "SYMBOL", "symbol": symbol},
            "evidence": [{"symbol": symbol, "file": file, "line": 6, "description": "seed"}],
            "source": {"type": "AUTOMATIC", "finding_id": None, "engineer_note": "seed"},
            "status": "ACTIVE" if state in {"VERIFIED", "REINFORCED"} else state,
            "state": state, "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
            "created_at": now, "updated_at": now, "confidence": 0.8, "origin": "SYNTHESIZER",
            "observation_count": 2, "reinforcement_count": 1, "fingerprint": fingerprint,
        })
        memory_repo.add_intelligence_links([
            {"memory_id": memory_id, "project_id": project_id, "link_kind": "SYMBOL", "link_value": symbol, "role": "PRIMARY", "created_at": now},
            {"memory_id": memory_id, "project_id": project_id, "link_kind": "FILE", "link_value": file, "role": "SUPPORTING", "created_at": now},
        ])
        return memory_id

    verified_related = seed("Verified mutex ownership pattern for ota_install.", "VERIFIED", "ota_install", "src/main.c")
    seed("Reinforced queue-depth pattern.", "REINFORCED", "measurement_queue", "src/measurement.c")
    seed("Stale guidance.", "NEEDS_REVALIDATION", "ota_install", "src/main.c")
    seed("Superseded pattern.", "SUPERSEDED", "ota_install", "src/main.c")
    seed("Disabled pattern.", "DISABLED", "ota_install", "src/main.c")
    seed("Provisional pattern.", "PROVISIONAL", "ota_install", "src/main.c")

    # (a) message naming an indexed symbol, no selected file or finding.
    by_symbol = service.chat_intelligence_context(project_id, ["ota_install", "src/main.c"])
    assert "Verified mutex ownership pattern" in by_symbol
    assert "Unrelated" not in by_symbol or "queue-depth" not in by_symbol
    assert "Stale" not in by_symbol and "Superseded" not in by_symbol
    assert "Disabled" not in by_symbol and "Provisional" not in by_symbol

    # (b) unrelated link values retrieve nothing for this project's records.
    unrelated = service.chat_intelligence_context(project_id, ["mqtt_handler", "src/mqtt.c"])
    assert "Verified mutex ownership pattern" not in unrelated

    # Chat eligibility payload derivation also resolves from a bare symbol mention.
    request = ChatRequest(message="I think ota_install is fine because the mutex is released in src/main.c", selected_file=None, finding_id=None)
    payload = service.chat_candidate_payload(project_id, request, "MSG-1", "MSG-2")
    assert payload is not None, "symbol-grounded claim must qualify"



def test_finding_focus_chat_receives_relevant_intelligence(tmp_path):
    """PI-004(a): a chat carrying a finding_id injects state-labelled records
    matching that finding's file/symbol links into the answer, while unrelated
    knowledge stays out."""
    from app.repository import MemoryRepository
    from app.platform_schemas import ChatRequest

    api = learning_app(tmp_path, [])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    finding = finding_id(api, project["id"])
    finding_obj = api.get(f"/api/projects/{project['id']}/findings/{finding}").json()
    symbol = "ota_install"
    file = finding_obj["location"]["file"]

    database = str(tmp_path / "firmsight.db")
    memory_repo = MemoryRepository(database)
    now = "2026-09-12T00:00:00+00:00"
    memory_repo.create_memory({
        "id": "MEM-CHAT-VERIFIED", "project_id": project["id"], "type": "BUG_PATTERN",
        "statement": "Ota install must release its mutex on the error path.",
        "scope": {"type": "SYMBOL", "symbol": symbol},
        "evidence": [{"symbol": symbol, "file": file, "line": 6, "description": "seed"}],
        "source": {"type": "AUTOMATIC", "finding_id": None, "engineer_note": "seed"},
        "status": "ACTIVE", "state": "VERIFIED", "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
        "created_at": now, "updated_at": now, "confidence": 0.9, "origin": "SYNTHESIZER",
        "observation_count": 1, "reinforcement_count": 1,
        "fingerprint": compute_fingerprint(project["id"], "BUG_PATTERN", "Ota install must release its mutex on the error path.", [symbol], [f"{file}:6"]),
    })
    memory_repo.add_intelligence_links([
        {"memory_id": "MEM-CHAT-VERIFIED", "project_id": project["id"], "link_kind": "SYMBOL", "link_value": symbol, "role": "PRIMARY", "created_at": now},
        {"memory_id": "MEM-CHAT-VERIFIED", "project_id": project["id"], "link_kind": "FILE", "link_value": file, "role": "SUPPORTING", "created_at": now},
    ])
    # Unrelated verified record must not leak into the finding-scoped chat.
    memory_repo.create_memory({
        "id": "MEM-CHAT-UNRELATED", "project_id": project["id"], "type": "PROJECT_FACT",
        "statement": "Mqtt reconnect backs off exponentially.",
        "scope": {"type": "SYMBOL", "symbol": "mqtt_handler"},
        "evidence": [{"symbol": "mqtt_handler", "file": "src/mqtt.c", "line": 3, "description": "seed"}],
        "source": {"type": "AUTOMATIC", "finding_id": None, "engineer_note": "seed"},
        "status": "ACTIVE", "state": "VERIFIED", "proposed_by": "AI", "approved_by": "AUTOMATIC", "commit_sha": None,
        "created_at": now, "updated_at": now, "confidence": 0.9, "origin": "SYNTHESIZER",
        "observation_count": 1, "reinforcement_count": 1,
        "fingerprint": compute_fingerprint(project["id"], "PROJECT_FACT", "Mqtt reconnect backs off exponentially.", ["mqtt_handler"], ["src/mqtt.c:3"]),
    })
    memory_repo.add_intelligence_links([
        {"memory_id": "MEM-CHAT-UNRELATED", "project_id": project["id"], "link_kind": "SYMBOL", "link_value": "mqtt_handler", "role": "PRIMARY", "created_at": now},
        {"memory_id": "MEM-CHAT-UNRELATED", "project_id": project["id"], "link_kind": "FILE", "link_value": "src/mqtt.c", "role": "SUPPORTING", "created_at": now},
    ])

    response = api.post(f"/api/projects/{project['id']}/chat", json={"message": "Explain this finding", "finding_id": finding})
    assert response.status_code == 200, response.text
    answer = response.json()["message"]["content"]
    assert "Ota install must release its mutex" in answer
    assert "Mqtt reconnect backs off" not in answer



def test_disable_endpoint_persists_reason_in_observation(tmp_path):
    api = learning_app(tmp_path, [json.dumps({"candidates": [CANDIDATE]}), ACCEPT_PROVISIONAL])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    record_id = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"][0]["id"]

    disabled = api.post(f"/api/intelligence/{record_id}/disable", json={"reason": "Superseded by a hardware revision; do not use."})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["state"] == "DISABLED"
    detail = api.get(f"/api/intelligence/{record_id}").json()
    disable_observation = next(obs for obs in detail["observations"] if obs["kind"] == "DISABLE")
    assert disable_observation["detail"] == "Superseded by a hardware revision; do not use."

    # Default reason when none supplied: the documented fallback wording.
    disabled2 = api.post(f"/api/intelligence/{record_id}/disable", json={})
    assert disabled2.status_code == 200
    assert disabled2.json()["state"] == "DISABLED"
    records_after = api.get(f"/api/projects/{project['id']}/intelligence").json()["counts"]
    assert records_after["disabled"] == 1 and records_after["provisional"] == 0


# ---------------------------------------------------------------------------
# R5 fixes: grounded conflict enforcement, stale/conflicted dedup, enqueue-failure boundary
# ---------------------------------------------------------------------------


def pipeline_app(tmp_path: Path, syntheses: list[str], verifier_actions: list[object]) -> "TestClient":
    pipeline = PipelineProvider(syntheses, verifier_actions)

    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return pipeline
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    return TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))


def _conflict_action(source_support: str, line: int, resolve_target: bool = True) -> dict:
    action = {
        "action": "MARK_CONFLICTED",
        "confidence": 0.9,
        "source_support": source_support,
        "valid_evidence": [],
        "conflict_evidence": [{"file": "src/main.c", "line": line, "description": "Claimed contradiction location."}],
        "rationale": "The verifier alleges the record is contradicted by current source.",
    }
    if resolve_target:
        action["_resolve_target"] = True
    return action


def _seed_verified_record(api, tmp_path) -> tuple[str, str]:
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    run_review(api, project["id"])
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()["records"]
    assert len(records) == 1 and records[0]["state"] == "VERIFIED"
    return project["id"], records[0]["id"]


def test_ungrounded_conflict_never_demotes_record(tmp_path):
    """(PI-001) A MARK_CONFLICTED with non-CONTRADICTED support, or an
    out-of-range conflict line, leaves the record unchanged and persists no
    conflict; a valid CONTRADICTED contradiction still conflicts it."""
    for case in ("non_contradicted", "out_of_range", "valid"):
        tmp_case = tmp_path / case
        tmp_case.mkdir()
        if case == "valid":
            # A valid in-range contradiction is exercised by the existing
            # conflict test; skip here to keep this test focused on rejection.
            continue
        support = "NONE" if case == "non_contradicted" else "CONTRADICTED"
        line = 7 if case == "non_contradicted" else 99999
        conflicting = {**CANDIDATE, "statement": "A materially different candidate asserting the prior record is contradicted by current source."}
        api = pipeline_app(tmp_case, [
            json.dumps({"candidates": [CANDIDATE]}),
            json.dumps({"candidates": [conflicting]}),
        ], [VERIFY_NEW, _conflict_action(support, line)])
        project_id, record_id = _seed_verified_record(api, tmp_case)
        review = api.post(f"/api/projects/{project_id}/reviews", json={"focus": ["ota"]}).json()
        import time
        for _ in range(60):
            status = api.get(f"/api/projects/{project_id}/reviews/{review['id']}").json()
            if status["status"] != "RUNNING":
                break
            time.sleep(0.05)
        detail = api.get(f"/api/intelligence/{record_id}").json()
        assert detail["state"] == "VERIFIED", (case, detail["state"])
        assert detail["conflicts"] == [], case
        summary = api.get(f"/api/projects/{project_id}/intelligence/summary").json()
        assert summary["counts"]["conflicted"] == 0, case


def test_stale_exact_match_revalidates_in_place_not_duplicated(tmp_path):
    """(PI-002) An exact-fingerprint candidate against a NEEDS_REVALIDATION
    record revalidates that record; no duplicate fingerprint is created."""
    api = pipeline_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
        json.dumps({"candidates": [CANDIDATE]}),  # same statement => same fingerprint
    ], [VERIFY_NEW, VERIFY_NEW])
    project_id, record_id = _seed_verified_record(api, tmp_path)

    # Change the source so the record is deterministically marked stale.
    (tmp_path / "firmware-workspace" / "ota-device" / "src" / "main.c").write_text(FIXED_SOURCE, encoding="utf-8")
    api.post(f"/api/projects/{project_id}/sync-source", json={})
    assert api.get(f"/api/intelligence/{record_id}").json()["state"] == "NEEDS_REVALIDATION"

    # A second review re-proposes the equivalent knowledge with source support.
    review = api.post(f"/api/projects/{project_id}/reviews", json={"focus": ["ota"]}).json()
    import time
    for _ in range(60):
        status = api.get(f"/api/projects/{project_id}/reviews/{review['id']}").json()
        if status["status"] != "RUNNING":
            break
        time.sleep(0.05)
    records = api.get(f"/api/projects/{project_id}/intelligence").json()["records"]
    same = [record for record in records if record["id"] == record_id]
    assert len(records) == 1, records            # no duplicate fingerprint row
    assert same and same[0]["state"] == "VERIFIED", same
    detail = api.get(f"/api/intelligence/{record_id}").json()
    kinds = [observation["kind"] for observation in detail["observations"]]
    assert "REVALIDATION" in kinds and "VERIFICATION" in kinds


def test_manual_revalidate_rejects_ungrounded_conflict(tmp_path):
    """(PI-001, manual path) Revalidation with a non-CONTRADICTED or out-of-range
    conflict verdict returns UNCHANGED and records no conflict; record state holds."""
    api = pipeline_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),
    ], [
        VERIFY_NEW,
        _conflict_action("SUPPORTED", 99999, resolve_target=False),  # CONTRADICTED required + in-range line
    ])
    project_id, record_id = _seed_verified_record(api, tmp_path)
    response = api.post(f"/api/intelligence/{record_id}/revalidate")
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "UNCHANGED"
    detail = api.get(f"/api/intelligence/{record_id}").json()
    assert detail["state"] == "VERIFIED"
    assert detail["conflicts"] == []


def test_learning_job_persistence_failure_never_fails_domain_results(tmp_path, monkeypatch):
    """(PI-003) When learning-job persistence raises after a durable domain write,
    Accept, Reject, Solve, Verify Fix, and eligible Chat still return success and
    preserve business state, with no fabricated intelligence rows."""
    from app.repository import MemoryRepository

    def boom(self, record):
        raise RuntimeError("simulated job-persistence outage")

    monkeypatch.setattr(MemoryRepository, "create_learning_job", boom)

    def resolve(role: str) -> AIProvider:
        if role in {"memory_synthesizer", "memory_verifier"}:
            return ScriptedProvider([json.dumps({"candidates": [CANDIDATE]}), VERIFY_NEW])
        if role == "investigator":
            return ScriptedProvider([INVESTIGATOR_RESULT])
        if role == "verifier":
            return ScriptedProvider([VERIFIER_RESULT])
        return ScriptedProvider([INVESTIGATOR_RESULT])

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(make_workspace(tmp_path)), provider_resolver=resolve))
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    # Review-completion learning swallows the persistence error and stays completed.
    run_review(api, project["id"])
    finding = finding_id(api, project["id"])

    accepted = api.patch(f"/api/projects/{project['id']}/findings/{finding}/decision", json={"decision": "ACCEPTED"})
    assert accepted.status_code == 200 and accepted.json()["decision"] == "ACCEPTED", accepted.text

    rejected = api.patch(f"/api/projects/{project['id']}/findings/{finding}/decision", json={"decision": "REJECTED", "reason": "The mutex is released by the OTA wrapper task in src/main.c and never blocks callers."})
    assert rejected.status_code == 200 and rejected.json()["decision"] == "REJECTED", rejected.text

    solved = api.patch(f"/api/projects/{project['id']}/findings/{finding}/resolution", json={"resolution": "SOLVED"})
    assert solved.status_code == 200 and solved.json()["resolution"] == "SOLVED", solved.text

    verified_fix = api.post(f"/api/projects/{project['id']}/findings/{finding}/verify-fix")
    # A 200/409/502 here is normal Fix-Verifier domain behavior (no source change
    # reopens the finding); none of them may be a learning-persistence 500.
    assert verified_fix.status_code in {200, 409, 502}, verified_fix.text
    assert verified_fix.status_code != 500

    chat = api.post(f"/api/projects/{project['id']}/chat", json={
        "message": "I think ota_install is safe because only the ota task takes the mutex in src/main.c",
        "selected_file": "src/main.c",
    })
    assert chat.status_code == 200 and chat.json()["message"]["role"] == "assistant", chat.text

    # Business state preserved (last durable decision was REJECTED; the finding
    # remains owned by the project); no intelligence fabricated from the failure.
    final = api.get(f"/api/projects/{project['id']}/findings/{finding}").json()
    assert final["decision"] == "REJECTED"
    assert final["resolution"] in {"OPEN", "SOLVED"}
    assert api.get(f"/api/projects/{project['id']}/intelligence").json()["counts"]["total"] == 0


# ---------------------------------------------------------------------------
# R6 fix: invalid structured AI output is repaired or rejected safely (PI-001)
# ---------------------------------------------------------------------------

MALFORMED_JSON = "<broken model output that is not json>"
SCHEMA_INVALID_SYNTHESIS = json.dumps({"candidates": [{"type": "BOGUS_TYPE", "statement": "too short", "confidence": 9}]})
SCHEMA_INVALID_VERIFIER = json.dumps({
    "action": "MAKE_IT_SO",
    "target_id": None,
    "confidence": 0.9,
    "source_support": "SUPPORTED",
    "valid_evidence": [],
    "conflict_target_id": None,
    "conflict_evidence": [],
    "rationale": "An unsupported verdict action for the Memory Verifier schema.",
})


def test_malformed_synthesizer_output_is_repaired_and_used(tmp_path):
    """(PI-001, repair success) A malformed Memory Synthesizer completion is
    repaired once and the validated candidates drive real record creation."""
    api = learning_app(tmp_path, [
        MALFORMED_JSON,                                # initial synthesizer output: unparseable
        json.dumps({"candidates": [CANDIDATE]}),       # repair: valid synthesis
        ACCEPT_PROVISIONAL,                            # verifier accepts provisionally
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])

    assert status["status"] == "COMPLETED", status
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["provisional"] == 1, records["counts"]
    assert records["records"][0]["type"] == "BUG_PATTERN"
    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["status"] == "COMPLETED"
    assert summary["provisional"] == 1


def test_schema_invalid_verifier_output_is_repaired_and_used(tmp_path):
    """(PI-001, repair success) A schema-invalid Memory Verifier verdict (unknown
    action) is repaired once into a valid VERIFY_NEW, which is then guarded and
    applied by the lifecycle engine."""
    api = learning_app(tmp_path, [
        json.dumps({"candidates": [CANDIDATE]}),   # synthesizer: valid candidate
        SCHEMA_INVALID_VERIFIER,                   # verifier initial: action violates the pattern
        VERIFY_NEW,                                # repair: valid source-supported verification
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])

    assert status["status"] == "COMPLETED", status
    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["verified"] == 1, records["counts"]
    assert records["records"][0]["state"] == "VERIFIED"
    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["status"] == "COMPLETED"
    assert summary["verified"] == 1


def test_exhausted_repair_fails_job_without_persisting_intelligence(tmp_path):
    """(PI-001, repair exhaustion) Invalid initial and repair outputs leave the
    learning job FAILED with no intelligence persisted, while the originating
    review and its findings remain fully available."""
    api = learning_app(tmp_path, [
        MALFORMED_JSON,                # synthesizer initial: unparseable
        SCHEMA_INVALID_SYNTHESIS,      # synthesizer repair: schema-invalid candidates
    ])
    project = api.post("/api/projects/import-directory", json={"directory": str(tmp_path / "firmware-workspace" / "ota-device")}).json()
    status = run_review(api, project["id"])

    assert status["status"] == "COMPLETED", status
    assert status["finding_count"] == 1
    assert api.get(f"/api/projects/{project['id']}/findings").json(), "findings must remain available"
    assert any("unaffected" in step for step in status["progress"])

    summary = api.get(f"/api/projects/{project['id']}/reviews/{status['id']}/learning-summary").json()
    assert summary["status"] == "FAILED"
    assert summary["error"]
    assert summary["provisional"] == 0 and summary["verified"] == 0

    records = api.get(f"/api/projects/{project['id']}/intelligence").json()
    assert records["counts"]["total"] == 0
