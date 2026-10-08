import json
import re
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.ai_provider import UnavailableProvider
from app.indexer import FirmwareIndexer
from app.main import create_app
from app.platform_repository import PlatformRepository
from app.platform_schemas import ProjectCreate
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.service import MemoryService


class FixtureProvider:
    """A test-only provider proving the domain calls its provider boundary."""

    name = "fixture"

    def __init__(self, classification: str = "CONFIRMED_BUG", source_path: str = "main/main.c") -> None:
        self.classification = classification
        self.source_path = source_path

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if "Fix Verifier" in system_prompt:
            fixed_line = "        xSemaphoreGive(ota_mutex);"
            current_context = user_prompt.split("CURRENT SOURCE:", 1)[-1].split("SOURCE DIFF", 1)[0]
            fixed = fixed_line in current_context and current_context.find(fixed_line) < current_context.find("        return err;")
            evidence_line = 7
            evidence_snippet = fixed_line if fixed else "        return err;"
            evidence = {
                "file": self.source_path, "line": evidence_line, "symbol": "ota_install",
                "evidence_snippet": evidence_snippet,
                "description": "Current error handling releases the acquired mutex." if fixed else "Current error return still exits before releasing the mutex.",
            }
            return json.dumps({
                "verdict": "FIXED" if fixed else "STILL_PRESENT",
                "original_failure_condition": "The OTA setup error path can retain the mutex.",
                "original_execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "return"],
                "current_execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "xSemaphoreGive", "return"] if fixed else ["ota_install", "xSemaphoreTake", "esp_ota_begin", "return err"],
                "mitigations_found": [evidence] if fixed else [],
                "remaining_failure_evidence": [] if fixed else [evidence],
                "inspected_files": [self.source_path],
                "inspected_symbols": ["ota_install"],
                "missing_context": [],
                "alternative_mitigation": False,
                "confidence": 0.91,
                "reasoning_summary": "The current error path releases the acquired mutex before returning." if fixed else "The current error path still returns before releasing the acquired mutex.",
            })
        if "Investigator" in system_prompt:
            return json.dumps({"findings": [{
                "title": "OTA mutex is not released when setup fails",
                "classification": self.classification,
                "severity": "high",
                "category": "CONCURRENCY",
                "confidence": 0.86,
                "location": {"file": self.source_path, "function": "ota_install", "line_start": 4, "line_end": 9},
                "summary": "A failed esp_ota_begin returns after the mutex has been acquired.",
                "evidence": [{"description": "The error return occurs before xSemaphoreGive.", "file": self.source_path, "line": 7}],
                "execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "return"],
                "runtime_scenario": "A failed OTA setup leaves later OTA callers blocked on the mutex.",
                "impact": "Subsequent OTA operations may stop progressing.",
                "assumptions": [{"statement": "No cleanup wrapper releases the mutex.", "status": "UNVERIFIED"}],
                "recommendation": "Release the mutex on every error exit path.",
            }]})
        if "Verifier" in system_prompt:
            return json.dumps({"verdict": "SURVIVES", "notes": "No alternate release is visible in the supplied source.", "remaining_assumptions": [{"statement": "No cleanup wrapper releases the mutex.", "status": "UNVERIFIED"}]})
        if "YAML Generator" in system_prompt:
            return json.dumps({"content": "version: 1\nproject:\n  name: \\\"OTA fixture\\\"\nplatform:\n  target: \\\"ESP32-S3\\\"\nanalysis:\n  focus:\n    - freertos\n"})
        return "The configured fixture provider received the project-aware chat request."


class ReReviewFixtureProvider(FixtureProvider):
    """Returns one first-run candidate, then proves it fixed before later discovery."""

    def __init__(self, source_path: str) -> None:
        super().__init__(source_path=source_path)
        self.calls: list[str] = []
        self.investigator_calls = 0

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if "Fix Verifier" in system_prompt:
            self.calls.append("fix")
            return super().chat(system_prompt, user_prompt)
        if "Investigator" in system_prompt:
            self.calls.append("investigator")
            self.investigator_calls += 1
            if self.investigator_calls > 1:
                return json.dumps({"findings": []})
        if "Verifier" in system_prompt:
            self.calls.append("verifier")
        return super().chat(system_prompt, user_prompt)


class EmptyInvestigatorBatchProvider(FixtureProvider):
    """Simulates an intermittent provider that returns no content for one batch."""

    def __init__(self) -> None:
        super().__init__()
        self.investigator_calls = 0

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if "Investigator" in system_prompt:
            self.investigator_calls += 1
            if self.investigator_calls <= 3:
                raise RuntimeError("openrouter response did not contain usable chat content")
            return json.dumps({"findings": []})
        return super().chat(system_prompt, user_prompt)


def test_review_scheduler_mixes_roles_with_shared_concurrency_limit(tmp_path):
    class DelayedProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.investigator_calls = 0
            self.investigator_started = 0
            self.verifier_calls = 0
            self.active_roles: dict[str, int] = {}
            self.roles_seen: list[str] = []
            self.mixed_overlap = False
            self.sequential_delay = 0.0
            self.wall_elapsed = 0.0
            self._started_at = None

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            role = "investigator" if "Investigator" in system_prompt else "verifier" if "Verifier" in system_prompt else "other"
            # Review-phase bookkeeping covers Investigator/Verifier only: best-effort
            # Project Intelligence learning shares this provider after the review
            # completes and is not part of the scheduler's concurrency contract.
            review_phase = role in ("investigator", "verifier")
            if review_phase:
                with self._lock:
                    if self._started_at is None:
                        self._started_at = time.perf_counter()
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.active_roles[role] = self.active_roles.get(role, 0) + 1
                    self.roles_seen.append(role)
                    self.mixed_overlap = self.mixed_overlap or all(self.active_roles.get(name, 0) > 0 for name in ("investigator", "verifier"))
                    if role == "investigator":
                        self.investigator_started += 1
                        delay = 0.01 if self.investigator_started == 1 else 0.05
                    else:
                        delay = 0.03
                    self.sequential_delay += delay
            else:
                delay = 0.0
            try:
                time.sleep(delay)
                if "Investigator" in system_prompt:
                    self.investigator_calls += 1
                    match = re.search(r'<source path="([^"]+)" lines="(\d+)-(\d+)"', user_prompt)
                    path = match.group(1) if match else "src/main.c"
                    return json.dumps({"findings": [{
                        "title": f"Risk in {path}", "classification": "PROBABLE_BUG", "severity": "medium", "category": "CONCURRENCY", "confidence": 0.82,
                        "location": {"file": path, "function": "task", "line_start": 1, "line_end": 1},
                        "summary": "The task accesses shared state without an explicit ownership guarantee.",
                        "evidence": [{"description": "Shared state is accessed in the task.", "file": path, "line": 1}],
                        "execution_path": ["task", "shared_access"], "runtime_scenario": "Concurrent execution can expose the access.",
                        "impact": "State may become inconsistent.", "assumptions": [{"statement": "No external synchronization exists.", "status": "UNVERIFIED"}],
                        "recommendation": "Document or enforce ownership.",
                    }]})
                if "Verifier" in system_prompt:
                    self.verifier_calls += 1
                    return json.dumps({"verdict": "SURVIVES", "notes": "No alternate synchronization is visible."})
                return super().chat(system_prompt, user_prompt)
            finally:
                if review_phase:
                    with self._lock:
                        self.active -= 1
                        self.active_roles[role] = self.active_roles.get(role, 1) - 1
                        if self.active_roles[role] <= 0:
                            self.active_roles.pop(role, None)
                        if self.active == 0 and self._started_at is not None:
                            self.wall_elapsed = time.perf_counter() - self._started_at

    provider = DelayedProvider()
    api = TestClient(create_app(str(tmp_path / "scheduler.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Scheduler fixture", "source_type": "MANUAL"}).json()
    source_files = {f"src/task_{number}.c": "void task(void) { shared_state++; }\n" * 20 for number in range(17)}
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": source_files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    completed = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()

    assert completed["status"] == "COMPLETED"
    assert completed["total_batches"] >= 3
    assert provider.investigator_calls == completed["total_batches"]
    assert provider.verifier_calls == completed["finding_count"]
    assert provider.max_active <= 2
    assert provider.mixed_overlap, provider.roles_seen
    assert provider.wall_elapsed < provider.sequential_delay * 0.9, (provider.wall_elapsed, provider.sequential_delay)


def test_verifier_failure_is_partial_and_can_be_resumed(tmp_path):
    class VerifierFailureProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self.verifier_calls = 0

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                match = re.search(r'<source path="([^"]+)" lines=', user_prompt)
                path = match.group(1) if match else "src/main.c"
                return json.dumps({"findings": [{
                    "title": f"Risk in {path}", "classification": "PROBABLE_BUG", "severity": "medium", "category": "CONCURRENCY", "confidence": 0.82,
                    "location": {"file": path, "function": "task", "line_start": 1, "line_end": 1},
                    "summary": "The task accesses shared state without an explicit ownership guarantee.",
                    "evidence": [{"description": "Shared state is accessed in the task.", "file": path, "line": 1}],
                    "execution_path": ["task", "shared_access"], "runtime_scenario": "Concurrent execution can expose the access.",
                    "impact": "State may become inconsistent.", "assumptions": [{"statement": "No external synchronization exists.", "status": "UNVERIFIED"}],
                    "recommendation": "Document or enforce ownership.",
                }]})
            if "Verifier" in system_prompt:
                with self._lock:
                    self.verifier_calls += 1
                    call_number = self.verifier_calls
                if call_number == 1:
                    raise RuntimeError("provider request timed out after 90 seconds")
                return json.dumps({"verdict": "SURVIVES", "notes": "No alternate synchronization is visible."})
            return super().chat(system_prompt, user_prompt)

    provider = VerifierFailureProvider()
    database = str(tmp_path / "verifier-failure.db")
    api = TestClient(create_app(database, provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Verifier failure fixture", "source_type": "MANUAL"}).json()
    files = {f"src/task_{number}.c": "void task(void) { shared_state++; }\n" * 20 for number in range(9)}
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    partial = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert partial["status"] == "PARTIAL"
    assert partial["unavailable_batches"] == 1
    assert partial["execution_progress"]["unavailable_units"] == 1
    assert partial["finding_count"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_unit_cache").fetchone()[0] == 1

    resumed = api.post(f"/api/projects/{project['id']}/reviews/{review['id']}/retry")
    assert resumed.status_code == 200, resumed.text
    completed = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert completed["status"] == "COMPLETED"
    assert completed["unavailable_batches"] == 0
    assert completed["finding_count"] == 2


def test_retry_only_reprocesses_units_still_unavailable_after_prior_retry(tmp_path):
    class MultiRetryProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self.investigator_batches: list[int] = []
            self.verifier_calls: dict[int, int] = {}

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            batch_match = re.search(r"source batch (\d+)/(\d+)", user_prompt)
            batch = int(batch_match.group(1)) if batch_match else 0
            if "Investigator" in system_prompt:
                path_match = re.search(r'<source path="([^"]+)" lines=', user_prompt)
                path = path_match.group(1) if path_match else "src/main.c"
                with self._lock:
                    self.investigator_batches.append(batch)
                return json.dumps({"findings": [{
                    "title": f"Risk in {path}", "classification": "PROBABLE_BUG", "severity": "medium", "category": "CONCURRENCY", "confidence": 0.82,
                    "location": {"file": path, "function": "task", "line_start": 1, "line_end": 1},
                    "summary": "The task accesses shared state without an explicit ownership guarantee.",
                    "evidence": [{"description": "Shared state is accessed in the task.", "file": path, "line": 1}],
                    "execution_path": ["task", "shared_access"], "runtime_scenario": "Concurrent execution can expose the access.",
                    "impact": "State may become inconsistent.", "assumptions": [{"statement": "No external synchronization exists.", "status": "UNVERIFIED"}],
                    "recommendation": "Document or enforce ownership.",
                }]})
            if "Verifier" in system_prompt:
                with self._lock:
                    self.verifier_calls[batch] = self.verifier_calls.get(batch, 0) + 1
                    call_number = self.verifier_calls[batch]
                if (batch == 1 and call_number == 1) or (batch == 3 and call_number <= 2):
                    raise RuntimeError("provider request timed out after 90 seconds")
                return json.dumps({"verdict": "SURVIVES", "notes": "No alternate synchronization is visible."})
            return super().chat(system_prompt, user_prompt)

    provider = MultiRetryProvider()
    database = str(tmp_path / "multi-retry.db")
    api = TestClient(create_app(database, provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Multi retry fixture", "source_type": "MANUAL"}).json()
    files = {f"src/task_{number}.c": "void task(void) { shared_state++; }\n" * 20 for number in range(17)}
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    first = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert first["status"] == "PARTIAL"
    assert first["total_batches"] == 3
    assert first["unavailable_batches"] == 2

    second_retry = api.post(f"/api/projects/{project['id']}/reviews/{review['id']}/retry")
    assert second_retry.status_code == 200, second_retry.text
    second = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert second["status"] == "PARTIAL"
    assert second["unavailable_batches"] == 1
    assert second["execution_attempt"] == 2

    third_retry = api.post(f"/api/projects/{project['id']}/reviews/{review['id']}/retry")
    assert third_retry.status_code == 200, third_retry.text
    third = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert third["status"] == "COMPLETED"
    assert third["unavailable_batches"] == 0
    assert third["execution_attempt"] == 3
    assert third["finding_count"] == 3
    assert sorted(provider.investigator_batches) == [1, 1, 2, 3, 3, 3]
    assert provider.verifier_calls == {1: 2, 2: 1, 3: 3}
    diagnostics_by_attempt = {}
    for event in third["diagnostics"]:
        diagnostics_by_attempt.setdefault(event["execution_attempt"], set()).add(event["batch_number"])
    assert diagnostics_by_attempt[1] == {1, 2, 3}
    assert diagnostics_by_attempt[2] == {1, 3}
    assert diagnostics_by_attempt[3] == {3}


def test_output_limit_review_unit_is_unavailable_and_not_cached(tmp_path):
    class OutputLimitProvider(FixtureProvider):
        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                batch_match = re.search(r"source batch (\d+)/(\d+)", user_prompt)
                if batch_match and int(batch_match.group(1)) == 1:
                    raise RuntimeError("model output limit reached before final structured response")
                return json.dumps({"findings": []})
            return super().chat(system_prompt, user_prompt)

    database = str(tmp_path / "output-limit-cache.db")
    api = TestClient(create_app(database, provider_resolver=lambda _role: OutputLimitProvider()))
    project = api.post("/api/projects", json={"name": "Output limit fixture", "source_type": "MANUAL"}).json()
    files = {f"src/task_{number}.c": "void task(void) {}\n" * 20 for number in range(9)}
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    result = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert result["status"] == "PARTIAL"
    assert result["unavailable_batches"] == 1
    assert any(event["error_kind"] == "OUTPUT_LIMIT_BEFORE_FINAL" for event in result["diagnostics"])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_unit_cache").fetchone()[0] == result["total_batches"] - 1
        assert all("source batch" not in json.dumps(row) for row in connection.execute("SELECT outcome_json FROM review_unit_cache"))


def test_timeout_releases_scheduler_slot_and_preserves_diagnostic_groups(tmp_path):
    class TimeoutThenEmptyProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self.investigator_batches: list[int] = []

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                batch_match = re.search(r"source batch (\d+)/(\d+)", user_prompt)
                batch = int(batch_match.group(1)) if batch_match else 0
                with self._lock:
                    self.investigator_batches.append(batch)
                if batch == 1:
                    raise RuntimeError("provider request timed out after 90 seconds")
                return json.dumps({"findings": []})
            return super().chat(system_prompt, user_prompt)

    provider = TimeoutThenEmptyProvider()
    database = str(tmp_path / "timeout-slot.db")
    api = TestClient(create_app(database, provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Timeout slot fixture", "source_type": "MANUAL"}).json()
    files = {f"src/task_{number}.c": "void task(void) {}\n" * 20 for number in range(17)}
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    result = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert result["status"] == "PARTIAL"
    assert result["unavailable_batches"] == 1
    assert sorted(provider.investigator_batches) == [1, 2, 3]
    request_groups = {}
    for event in result["diagnostics"]:
        request_groups.setdefault(event["request_id"], set()).add(event["operation"])
        assert "void task" not in json.dumps(event)
    assert request_groups
    assert all(group for group in request_groups.values())
    assert any(event["error_kind"] == "timeout" for event in result["diagnostics"] if event["state"] == "FAILED")


def test_invalid_evidence_never_creates_review_unit_cache(tmp_path):
    class InvalidEvidenceProvider(FixtureProvider):
        def __init__(self):
            super().__init__(source_path="src/missing.c")

    database = str(tmp_path / "invalid-evidence-cache.db")
    api = TestClient(create_app(database, provider_resolver=lambda _role: InvalidEvidenceProvider()))
    project = api.post("/api/projects", json={"name": "Invalid evidence fixture", "source_type": "MANUAL"}).json()
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "void app_main(void) {}\n"}}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["memory"]}).json()
    result = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert result["status"] == "COMPLETED", result.get("progress")
    assert result["finding_count"] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_unit_cache").fetchone()[0] == 0


def test_malformed_investigator_result_never_creates_review_unit_cache(tmp_path):
    class MalformedProvider(FixtureProvider):
        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                return "{not valid json"
            return super().chat(system_prompt, user_prompt)

    database = str(tmp_path / "malformed-cache.db")
    api = TestClient(create_app(database, provider_resolver=lambda _role: MalformedProvider()))
    project = api.post("/api/projects", json={"name": "Malformed cache fixture", "source_type": "MANUAL"}).json()
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "void app_main(void) {}\n"}}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["memory"]}).json()
    result = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert result["status"] == "FAILED"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_unit_cache").fetchone()[0] == 0


def test_review_context_covers_all_reviewable_files_and_skips_generated_fonts(tmp_path):
    database = str(tmp_path / "firmsight.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Bounded review", source_type="MANUAL"))
    source_files = {
        f"src/task_{number}.c": (
            f"void measurement_task_{number}(void *arg) {{\n"
            + "  xQueueReceive(queue, &item, portMAX_DELAY);\n" * 90
            + "}\n"
        )
        for number in range(12)
    }
    source_files["src/utils_gui/plus_jakarta_sans_regular_26.c"] = "0x00, " * 30_000
    source_files["platformio.ini"] = "[env:esp32]\nplatform = espressif32\n"
    projects.add_files(project.id, source_files)
    projects.index(project.id)
    review = ReviewService(
        repository,
        projects,
        lambda _role: FixtureProvider(),
        MemoryService(MemoryRepository(database)),
    )

    batches = review._context_batches(project.id, project.name, ["concurrency"], repository.raw_files(project.id))

    assert len(batches) >= 2
    assert all(len(batch.context) <= review.MAX_CONTEXT_CHARS for batch in batches)
    assert all("plus_jakarta" not in path for batch in batches for path in batch.files)
    assert batches[0].files[0] == "platformio.ini"
    assert "src/task_0.c" in batches[0].files
    analyzed_files = {path for batch in batches for path in batch.files}
    assert {f"src/task_{number}.c" for number in range(12)} <= analyzed_files


def test_review_continues_after_an_unusable_investigator_batch(tmp_path):
    provider = EmptyInvestigatorBatchProvider()
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Flaky provider fixture", "source_type": "MANUAL"}).json()
    source_files = {
        f"src/task_{number}.c": f"void task_{number}(void) {{}}\n"
        for number in range(9)
    }
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": source_files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    completed = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()

    assert completed["status"] == "PARTIAL"
    assert completed["total_batches"] == 2
    assert completed["validated_batches"] == 1
    assert completed["unavailable_batches"] == 1
    assert provider.investigator_calls == 4
    assert any("was skipped (stopped: provider request failed" in step for step in completed["progress"])
    assert any("Review is partial: 1 source batch unavailable" in step for step in completed["progress"])

    resumed = api.post(f"/api/projects/{project['id']}/reviews/{review['id']}/retry")
    assert resumed.status_code == 200, resumed.text
    resumed_review = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert resumed_review["status"] == "COMPLETED"
    assert resumed_review["validated_batches"] == 2
    assert resumed_review["unavailable_batches"] == 0
    assert any("Queued retry for 1 unavailable review unit" in step for step in resumed_review["progress"])


def test_validated_empty_review_unit_is_reused_without_provider_call(tmp_path):
    class EmptyCountingProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self.investigator_calls = 0

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                self.investigator_calls += 1
                return json.dumps({"findings": []})
            return super().chat(system_prompt, user_prompt)

    provider = EmptyCountingProvider()
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Cache fixture", "source_type": "MANUAL"}).json()
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "void app_main(void) {}\n"}}).status_code == 200

    first = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    assert api.get(f"/api/projects/{project['id']}/reviews/{first['id']}").json()["status"] == "COMPLETED"
    second = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    second_read = api.get(f"/api/projects/{project['id']}/reviews/{second['id']}").json()
    assert second_read["status"] == "COMPLETED"
    assert second_read["execution_progress"]["reused_units"] == 1
    assert provider.investigator_calls == 1


def test_review_unit_cache_invalidates_after_source_change(tmp_path):
    class EmptyCountingProvider(FixtureProvider):
        def __init__(self):
            super().__init__()
            self.investigator_calls = 0

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                self.investigator_calls += 1
                return json.dumps({"findings": []})
            return super().chat(system_prompt, user_prompt)

    provider = EmptyCountingProvider()
    api = TestClient(create_app(str(tmp_path / "cache-invalidation.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Cache invalidation fixture", "source_type": "MANUAL"}).json()
    files_url = f"/api/projects/{project['id']}/files"
    assert api.post(files_url, json={"files": {"src/main.c": "void app_main(void) {}\n"}}).status_code == 200

    first = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    assert api.get(f"/api/projects/{project['id']}/reviews/{first['id']}").json()["status"] == "COMPLETED"
    assert provider.investigator_calls == 1

    assert api.post(files_url, json={"files": {"src/main.c": "void app_main(void) { queue_send(); }\n"}}).status_code == 200
    second = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    second_read = api.get(f"/api/projects/{project['id']}/reviews/{second['id']}").json()
    assert second_read["status"] == "COMPLETED"
    assert second_read["execution_progress"]["reused_units"] == 0
    assert provider.investigator_calls == 2


def test_validated_finding_unit_cache_reuses_investigator_and_verifier_result(tmp_path):
    class CountingProvider(FixtureProvider):
        def __init__(self):
            super().__init__(source_path="src/main.c")
            self.investigator_calls = 0
            self.verifier_calls = 0

        def chat(self, system_prompt: str, user_prompt: str) -> str:
            if "Investigator" in system_prompt:
                self.investigator_calls += 1
            if "Verifier" in system_prompt and "Fix Verifier" not in system_prompt:
                self.verifier_calls += 1
            return super().chat(system_prompt, user_prompt)

    provider = CountingProvider()
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Finding cache fixture", "source_type": "MANUAL"}).json()
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "\n" * 12}}).status_code == 200

    first = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    first_read = api.get(f"/api/projects/{project['id']}/reviews/{first['id']}").json()
    assert first_read["status"] == "COMPLETED"
    assert first_read["finding_count"] == 1
    assert provider.investigator_calls == 1
    assert provider.verifier_calls == 1

    second = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    second_read = api.get(f"/api/projects/{project['id']}/reviews/{second['id']}").json()
    assert second_read["status"] == "COMPLETED"
    assert second_read["finding_count"] == 1
    assert second_read["execution_progress"]["reused_units"] == 1
    assert any("Reused validated review unit 1/1 (1 candidate finding)" in step for step in second_read["progress"])
    assert provider.investigator_calls == 1
    assert provider.verifier_calls == 1
    findings = api.get(f"/api/projects/{project['id']}/findings").json()
    assert len(findings) == 2
    assert findings[0]["review_id"] == second["id"]
    assert findings[0]["decision"] == "UNREVIEWED"
    assert findings[0]["resolution"] == "OPEN"


def test_retry_rejects_when_project_source_snapshot_changed(tmp_path):
    provider = EmptyInvestigatorBatchProvider()
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Changed source fixture", "source_type": "MANUAL"}).json()
    source_files = {f"src/task_{number}.c": f"void task_{number}(void) {{}}\n" for number in range(9)}
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": source_files}).status_code == 200

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    partial = api.get(f"/api/projects/{project['id']}/reviews/{review['id']}").json()
    assert partial["status"] == "PARTIAL"

    changed = api.post(
        f"/api/projects/{project['id']}/files",
        json={"files": {"src/aaa_added.c": "void added(void) {}\n"}},
    )
    assert changed.status_code == 200

    resumed = api.post(f"/api/projects/{project['id']}/reviews/{review['id']}/retry")
    assert resumed.status_code == 409
    assert "source changed" in resumed.json()["detail"].lower()


def test_first_milestone_import_workflow(tmp_path):
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: FixtureProvider()))

    assert api.get("/api/projects").json() == []
    assert api.post("/api/projects/demo").status_code == 405
    assert api.post("/api/projects", json={"name": "Not a real source", "source_type": "DEMO"}).status_code == 422
    project_response = api.post("/api/projects", json={"name": "OTA fixture", "description": "Test fixture imported by the test only.", "source_type": "MANUAL"})
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    imported = api.post(
        f"/api/projects/{project['id']}/files",
        json={"files": {
            "CMakeLists.txt": "idf_component_register(SRCS \"main.c\" INCLUDE_DIRS \".\")\n",
            "sdkconfig.defaults": "CONFIG_IDF_TARGET_ESP32S3=y\n",
            "main/main.c": """#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <esp_ota_ops.h>

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

void measurement_task(void *arg) { while (true) { vTaskDelay(pdMS_TO_TICKS(500)); } }
void app_main(void) { xTaskCreate(measurement_task, \"measurement\", 4096, NULL, 5, NULL); }
""",
        }},
    )
    assert imported.status_code == 200, imported.text
    project = api.get(f"/api/projects/{project['id']}").json()
    assert project["framework"] == "ESP-IDF"
    assert project["file_count"] == 3

    files = api.get(f"/api/projects/{project['id']}/files")
    assert files.status_code == 200
    source_path = "main/main.c"
    source = api.get(f"/api/projects/{project['id']}/files/content", params={"path": source_path})
    assert source.status_code == 200
    assert "esp_ota_begin" in source.json()["content"]

    review = api.post(f"/api/projects/{project['id']}/reviews", json={"scope": "Full Project", "focus": ["concurrency", "ota"]})
    assert review.status_code == 201, review.text
    assert review.json()["status"] == "RUNNING"
    assert "main/main.c" in review.json()["context_files"]
    review = api.get(f"/api/projects/{project['id']}/reviews/{review.json()['id']}")
    assert review.status_code == 200
    assert review.json()["status"] == "COMPLETED"
    execution_progress = review.json()["execution_progress"]
    assert execution_progress["phase"] == "COMPLETED"
    assert execution_progress["total_units"] == execution_progress["completed_units"]
    assert execution_progress["parallel_request_limit"] in {1, 2, 3}
    assert any("Investigator is analyzing" in step for step in review.json()["progress"])
    assert any("Verifier is challenging" in step for step in review.json()["progress"])
    assert review.json()["finding_count"] == 1

    findings = api.get(f"/api/projects/{project['id']}/findings")
    assert findings.status_code == 200
    finding = findings.json()[0]
    assert finding["classification"] == "CONFIRMED_BUG"
    assert finding["evidence"]
    assert finding["execution_path"]
    assert finding["verification"]["status"] == "PASSED"

    solved = api.patch(f"/api/projects/{project['id']}/findings/{finding['id']}/resolution", json={"resolution": "SOLVED"})
    assert solved.status_code == 200, solved.text
    assert solved.json()["resolution"] == "SOLVED"
    assert solved.json()["resolved_at"] is not None

    decision = api.patch(f"/api/projects/{project['id']}/findings/{finding['id']}/decision", json={"decision": "REJECTED", "reason": "The test fixture uses a cleanup wrapper not shown here."})
    assert decision.status_code == 200, decision.text
    assert decision.json()["decision"] == "REJECTED"

    chat = api.post(f"/api/projects/{project['id']}/chat", json={"message": "Why is this considered a bug?", "finding_id": finding["id"]})
    assert chat.status_code == 200
    assert "execution path" in chat.json()["message"]["content"].lower()

    generated = api.post(f"/api/projects/{project['id']}/yaml/generate", json={"description": "ESP32-S3 runs a measurement task. MQTT monitoring must not interrupt it.", "focus": ["freertos", "mqtt"]})
    assert generated.status_code == 200, generated.text
    assert generated.json()["valid"] is True
    assert generated.json()["content"].startswith("version: 1")
    assert api.get("/").status_code == 200


def test_only_confirmed_findings_can_be_marked_solved(tmp_path):
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: FixtureProvider("PROBABLE_BUG")))
    project = api.post("/api/projects", json={"name": "Probable fixture", "source_type": "MANUAL"}).json()
    source = "\n" * 12
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"main/main.c": source}}).status_code == 200
    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    findings = api.get(f"/api/projects/{project['id']}/findings").json()
    assert len(findings) == 1, review
    response = api.patch(f"/api/projects/{project['id']}/findings/{findings[0]['id']}/resolution", json={"resolution": "SOLVED"})
    assert response.status_code == 422
    assert "only confirmed_bug" in response.json()["detail"].lower()


def test_follow_up_review_checks_accepted_confirmed_findings_before_discovery(tmp_path):
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "ota-device"
    source_directory = project_directory / "src"
    source_directory.mkdir(parents=True)
    source_path = source_directory / "main.c"
    source_path.write_text(
        """#include <freertos/semphr.h>
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
""",
        encoding="utf-8",
    )
    provider = ReReviewFixtureProvider(source_path="src/main.c")
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(import_root), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects/import-directory", json={"directory": str(project_directory)}).json()
    first_review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota", "concurrency"]}).json()
    first_finding = api.get(f"/api/projects/{project['id']}/findings").json()[0]
    assert api.patch(f"/api/projects/{project['id']}/findings/{first_finding['id']}/decision", json={"decision": "ACCEPTED"}).status_code == 200

    source_path.write_text(
        """#include <freertos/semphr.h>
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
""",
        encoding="utf-8",
    )
    provider.calls.clear()
    follow_up = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota", "concurrency"]}).json()
    follow_up = api.get(f"/api/projects/{project['id']}/reviews/{follow_up['id']}").json()
    updated = api.get(f"/api/projects/{project['id']}/findings/{first_finding['id']}").json()

    assert follow_up["status"] == "COMPLETED"
    assert provider.calls[:2] == ["fix", "investigator"]
    assert updated["resolution"] == "SOLVED"
    assert updated["remediation"]["status"] == "VERIFIED_FIXED"
    assert updated["remediation"]["source_refreshed"] is True
    assert any("Rechecking 1 accepted confirmed finding" in step for step in follow_up["progress"])
    assert any("marked solved after AI recheck" in step for step in follow_up["progress"])


def test_review_and_yaml_report_missing_ai_configuration(tmp_path):
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), provider_resolver=lambda _role: UnavailableProvider()))
    project = api.post("/api/projects", json={"name": "No AI fixture", "source_type": "MANUAL"}).json()
    imported = api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "void app_main(void) {}\n"}})
    assert imported.status_code == 200
    review = api.post(f"/api/projects/{project['id']}/reviews", json={})
    assert review.status_code == 503
    assert "requires configured" in review.json()["detail"].lower()
    yaml = api.post(f"/api/projects/{project['id']}/yaml/generate", json={"description": "A real project context."})
    assert yaml.status_code == 503
    assert "requires a configured" in yaml.json()["detail"].lower()


def test_import_rejects_path_traversal_and_executable_files(tmp_path):
    api = TestClient(create_app(str(tmp_path / "firmsight.db")))
    project = api.post("/api/projects", json={"name": "Safe import", "source_type": "MANUAL"}).json()
    traversal = api.post(f"/api/projects/{project['id']}/files", json={"files": {"../outside.c": "int main(void) {}"}})
    assert traversal.status_code == 422
    executable = api.post(f"/api/projects/{project['id']}/files", json={"files": {"scripts/build.sh": "rm -rf /"}})
    assert executable.status_code == 422


def test_imports_configured_local_directory_without_browser_upload(tmp_path):
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "motor-controller"
    project_directory.mkdir(parents=True)
    (project_directory / "sdkconfig.defaults").write_text("CONFIG_IDF_TARGET_ESP32S3=y\n", encoding="utf-8")
    (project_directory / "src").mkdir()
    (project_directory / "src" / "main.c").write_text("void app_main(void) {}\n", encoding="utf-8")
    (project_directory / "src" / "ui_font.c").write_text("x" * 1_000_001, encoding="utf-8")
    (project_directory / "components").mkdir()
    (project_directory / "components" / "ignored.c").write_text("void ignored(void) {}\n", encoding="utf-8")
    outside_directory = tmp_path / "outside-project"
    outside_directory.mkdir()
    (outside_directory / "main.c").write_text("void app_main(void) {}\n", encoding="utf-8")

    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(import_root)))
    imported = api.post("/api/projects/import-directory", json={"directory": str(project_directory), "description": "Imported by path."})
    assert imported.status_code == 201, imported.text
    project = imported.json()
    assert project["name"] == "motor-controller"
    assert project["source_type"] == "LOCAL_DIRECTORY"
    assert project["framework"] == "ESP-IDF"
    assert project["file_count"] == 2
    paths = [item["path"] for item in api.get(f"/api/projects/{project['id']}/files").json()]
    assert paths == ["sdkconfig.defaults", "src/main.c"]

    forbidden = api.post("/api/projects/import-directory", json={"directory": str(outside_directory)})
    assert forbidden.status_code == 403


def test_local_directory_import_requires_configured_root(tmp_path, monkeypatch):
    project_directory = tmp_path / "firmware"
    project_directory.mkdir()
    (project_directory / "src").mkdir()
    (project_directory / "src" / "main.c").write_text("void app_main(void) {}\n", encoding="utf-8")

    monkeypatch.delenv("FIRMSIGHT_IMPORT_ROOT", raising=False)
    api = TestClient(create_app(str(tmp_path / "firmsight.db")))
    response = api.post("/api/projects/import-directory", json={"directory": str(project_directory)})
    assert response.status_code == 409
    assert "FIRMSIGHT_IMPORT_ROOT" in response.json()["detail"]


def test_local_source_sync_updates_persisted_code_index(tmp_path):
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "controller"
    source_directory = project_directory / "src"
    source_directory.mkdir(parents=True)
    main_source = source_directory / "main.c"
    main_source.write_text("void app_main(void) {}\n", encoding="utf-8")
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(import_root)))

    project = api.post("/api/projects/import-directory", json={"directory": str(project_directory)}).json()
    main_source.write_text("void app_main(void) { start_network(); }\n", encoding="utf-8")
    (source_directory / "network.c").write_text("void start_network(void) {}\n", encoding="utf-8")

    synced = api.post(f"/api/projects/{project['id']}/sync-source")

    assert synced.status_code == 200, synced.text
    assert synced.json()["file_count"] == 2
    assert synced.json()["changed_files"] == ["src/main.c", "src/network.c"]
    paths = [item["path"] for item in api.get(f"/api/projects/{project['id']}/files").json()]
    assert paths == ["src/main.c", "src/network.c"]
    current_main = api.get(f"/api/projects/{project['id']}/files/content", params={"path": "src/main.c"})
    assert "start_network" in current_main.json()["content"]


def test_local_source_sync_can_attach_a_legacy_project_directory(tmp_path):
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "legacy-controller"
    source_directory = project_directory / "src"
    source_directory.mkdir(parents=True)
    (source_directory / "main.c").write_text("void app_main(void) {}\n", encoding="utf-8")
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(import_root)))
    legacy = api.post("/api/projects", json={"name": "Legacy controller", "source_type": "LOCAL_DIRECTORY"}).json()

    synced = api.post(f"/api/projects/{legacy['id']}/sync-source", json={"directory": str(project_directory)})

    assert synced.status_code == 200, synced.text
    assert synced.json()["changed_files"] == ["src/main.c"]
    assert api.get(f"/api/projects/{legacy['id']}").json()["source_sync_available"] is True


def test_verify_fix_refreshes_local_source_and_marks_confirmed_finding_solved(tmp_path):
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "ota-device"
    source_directory = project_directory / "src"
    source_directory.mkdir(parents=True)
    source_path = source_directory / "main.c"
    source_path.write_text(
        """#include <freertos/semphr.h>
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
""",
        encoding="utf-8",
    )
    api = TestClient(create_app(str(tmp_path / "firmsight.db"), str(import_root), provider_resolver=lambda _role: FixtureProvider(source_path="src/main.c")))
    project = api.post("/api/projects/import-directory", json={"directory": str(project_directory)}).json()
    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota", "concurrency"]}).json()
    assert review["status"] == "RUNNING"
    finding = api.get(f"/api/projects/{project['id']}/findings").json()[0]
    accepted = api.patch(f"/api/projects/{project['id']}/findings/{finding['id']}/decision", json={"decision": "ACCEPTED"})
    assert accepted.status_code == 200

    source_path.write_text(
        """#include <freertos/semphr.h>
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
""",
        encoding="utf-8",
    )
    verified = api.post(f"/api/projects/{project['id']}/findings/{finding['id']}/verify-fix")
    assert verified.status_code == 200, verified.text
    payload = verified.json()
    assert payload["resolution"] == "SOLVED"
    assert payload["remediation"]["status"] == "VERIFIED_FIXED"
    assert payload["remediation"]["source_refreshed"] is True
    assert payload["remediation"]["changed_files"] == ["src/main.c"]
    assert payload["remediation"]["verification"]["verdict"] == "FIXED"
    assert payload["remediation"]["verification"]["mitigations_found"][0]["file"] == "src/main.c"
    assert len(payload["remediation"]["baseline_snapshot_hash"]) == 64
    assert len(payload["remediation"]["current_snapshot_hash"]) == 64
    refreshed = api.get(f"/api/projects/{project['id']}/files/content", params={"path": "src/main.c"})
    assert "xSemaphoreGive(ota_mutex);\n        return err" in refreshed.json()["content"]


def test_verify_fix_checks_current_source_even_when_indexed_changed_file_list_is_empty(tmp_path):
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "ota-device"
    source_directory = project_directory / "src"
    source_directory.mkdir(parents=True)
    source_path = source_directory / "main.c"
    source_path.write_text(
        """#include <freertos/semphr.h>
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
""",
        encoding="utf-8",
    )
    provider = FixtureProvider(source_path="src/main.c")
    api = TestClient(create_app(str(tmp_path / "firmsight-no-diff.db"), str(import_root), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects/import-directory", json={"directory": str(project_directory)}).json()
    review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota"]}).json()
    finding = api.get(f"/api/projects/{project['id']}/findings").json()[0]
    api.patch(f"/api/projects/{project['id']}/findings/{finding['id']}/decision", json={"decision": "ACCEPTED"})

    verified = api.post(f"/api/projects/{project['id']}/findings/{finding['id']}/verify-fix")

    assert verified.status_code == 200, verified.text
    payload = verified.json()
    assert payload["remediation"]["changed_files"] == []
    assert payload["remediation"]["status"] == "STILL_PRESENT"
    assert payload["remediation"]["verification"]["remaining_failure_evidence"]


def test_provider_default_review_path_snapshots_budget_and_keeps_failed_unit_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "test-only-secret")
    captured: list[tuple[dict, object]] = []
    candidate_json = FixtureProvider(source_path="src/main.c").chat("Investigator", "")

    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def __init__(self, body: dict):
            self.body = json.dumps(body).encode()

        def read(self):
            return self.body

    def fake_urlopen(request, **kwargs):
        payload = json.loads(request.data.decode())
        captured.append((payload, kwargs.get("timeout")))
        system_prompt = payload["messages"][0]["content"]
        if "FirmSight Investigator" in system_prompt:
            body = {"choices": [{"finish_reason": "stop", "message": {"content": candidate_json}}]}
        else:
            body = {
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"completion_tokens": 1_200},
            }
        return Response(body)

    monkeypatch.setattr("app.ai_provider.urlopen", fake_urlopen)
    database = str(tmp_path / "provider-default-review.db")
    api = TestClient(create_app(database))
    settings = api.put("/api/settings", json={
        "provider": "9router",
        "endpoint": "http://127.0.0.1:20128/v1/chat/completions",
        "models": {"investigator": "glm/investigator", "verifier": "glm/verifier"},
        "investigator_max_tokens": 1_200,
        "verifier_max_tokens": "PROVIDER_DEFAULT",
    })
    assert settings.status_code == 200, settings.text
    project = api.post("/api/projects", json={"name": "Provider default review", "source_type": "MANUAL"}).json()
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "#include <freertos/semphr.h>\nstatic SemaphoreHandle_t ota_mutex;\nvoid app_main(void) {\n  queue_receive();\n  queue_receive();\n  queue_receive();\n  queue_receive();\n  queue_receive();\n  queue_receive();\n  queue_receive();\n  queue_receive();\n}\n"}}).status_code == 200

    started = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    review = api.get(f"/api/projects/{project['id']}/reviews/{started['id']}").json()
    verifier_payloads = [payload for payload, _timeout in captured if "Verifier" in payload["messages"][0]["content"] and "Investigator" not in payload["messages"][0]["content"]]
    assert verifier_payloads and "max_tokens" not in verifier_payloads[0]
    assert all(timeout == 90 for _payload, timeout in captured)
    assert review["status"] == "PARTIAL"
    assert review["unavailable_batches"] == 1
    assert review["output_budget_snapshot"] == {"investigator": 1_200, "verifier": "PROVIDER_DEFAULT", "verifier_fix": 1_200}
    assert any(event["error_kind"] == "OUTPUT_LIMIT_BEFORE_FINAL" for event in review["diagnostics"])
    assert "test-only-secret" not in json.dumps(review)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_unit_cache").fetchone()[0] == 0
        snapshot = connection.execute("SELECT output_budget_json FROM reviews WHERE id=?", (started["id"],)).fetchone()[0]
        assert json.loads(snapshot) == review["output_budget_snapshot"]
        assert all(sentinel not in snapshot for sentinel in ("queue_receive", "finish_reason", "test-only-secret"))


@pytest.mark.parametrize(
    ("configured_verifier_budget", "expected_normal_budget", "expected_fix_budget"),
    [(2_000, 2_000, 2_000), ("PROVIDER_DEFAULT", None, 1_200)],
    ids=["numeric-verifier-budget", "provider-default-verifier-budget"],
)
def test_verifier_budget_scope_does_not_change_fix_or_accepted_recheck_policy(
    monkeypatch, tmp_path, configured_verifier_budget, expected_normal_budget, expected_fix_budget,
):
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "test-only-secret")
    import_root = tmp_path / "firmware-workspace"
    project_directory = import_root / "ota-device"
    source_directory = project_directory / "src"
    source_directory.mkdir(parents=True)
    source_path = source_directory / "main.c"
    source_path.write_text(
        """#include <freertos/semphr.h>
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
""",
        encoding="utf-8",
    )
    captured: list[tuple[dict, object]] = []
    fix_calls = 0
    candidate_json = FixtureProvider(source_path="src/main.c").chat("Investigator", "")

    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __init__(self, body: dict):
            self.body = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.body

    def fake_urlopen(request, **kwargs):
        nonlocal fix_calls
        payload = json.loads(request.data.decode())
        captured.append((payload, kwargs.get("timeout")))
        system_prompt = payload["messages"][0]["content"]
        if "Fix Verifier" in system_prompt:
            fix_calls += 1
            verdict = "STILL_PRESENT" if fix_calls == 1 else "FIXED"
            current_line = "        return err;" if verdict == "STILL_PRESENT" else "        xSemaphoreGive(ota_mutex);"
            evidence = {"file": "src/main.c", "line": 7, "symbol": "ota_install", "evidence_snippet": current_line, "description": "Current error path evidence."}
            response = {
                "verdict": verdict,
                "original_failure_condition": "The OTA setup error path may retain the mutex.",
                "original_execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "return"],
                "current_execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "return err"] if verdict == "STILL_PRESENT" else ["ota_install", "xSemaphoreTake", "esp_ota_begin", "xSemaphoreGive", "return"],
                "mitigations_found": [evidence] if verdict == "FIXED" else [],
                "remaining_failure_evidence": [evidence] if verdict == "STILL_PRESENT" else [],
                "inspected_files": ["src/main.c"],
                "inspected_symbols": ["ota_install"],
                "missing_context": [],
                "alternative_mitigation": False,
                "confidence": 0.9,
                "reasoning_summary": "The structured verifier completed using its fixed output budget.",
            }
            body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]}
        elif "FirmSight Investigator" in system_prompt:
            body = {"choices": [{"finish_reason": "stop", "message": {"content": candidate_json}}]}
        else:
            body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"verdict": "SURVIVES", "notes": "The candidate remains supported by the supplied source."})}}]}
        return Response(body)

    monkeypatch.setattr("app.ai_provider.urlopen", fake_urlopen)
    api = TestClient(create_app(str(tmp_path / "fix-budget.db"), str(import_root)))
    settings = api.put("/api/settings", json={
        "provider": "9router",
        "endpoint": "http://127.0.0.1:20128/v1/chat/completions",
        "models": {"investigator": "glm/investigator", "verifier": "glm/verifier"},
        "investigator_max_tokens": 1_200,
        "verifier_max_tokens": configured_verifier_budget,
    })
    assert settings.status_code == 200, settings.text
    project = api.post(f"/api/projects/import-directory", json={"directory": str(project_directory)}).json()
    first_review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota"]}).json()
    first_review = api.get(f"/api/projects/{project['id']}/reviews/{first_review['id']}").json()
    assert first_review["output_budget_snapshot"]["verifier_fix"] == expected_fix_budget
    findings = api.get(f"/api/projects/{project['id']}/findings").json()
    assert findings, first_review
    finding = findings[0]
    assert api.patch(f"/api/projects/{project['id']}/findings/{finding['id']}/decision", json={"decision": "ACCEPTED"}).status_code == 200

    second_review = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["ota"]}).json()
    second_review = api.get(f"/api/projects/{project['id']}/reviews/{second_review['id']}").json()
    assert second_review["status"] in {"COMPLETED", "PARTIAL"}
    assert second_review["output_budget_snapshot"]["verifier_fix"] == expected_fix_budget
    source_path.write_text(source_path.read_text(encoding="utf-8").replace("        return err;", "        xSemaphoreGive(ota_mutex);\n        return err;"), encoding="utf-8")
    verified = api.post(f"/api/projects/{project['id']}/findings/{finding['id']}/verify-fix")
    assert verified.status_code == 200, verified.text
    assert verified.json()["resolution"] == "SOLVED"

    normal_verifier_payloads = [payload for payload, _timeout in captured if "Verifier" in payload["messages"][0]["content"] and "Fix Verifier" not in payload["messages"][0]["content"] and "Investigator" not in payload["messages"][0]["content"]]
    fix_payloads = [payload for payload, _timeout in captured if "Fix Verifier" in payload["messages"][0]["content"]]
    assert normal_verifier_payloads
    if expected_normal_budget is None:
        assert all("max_tokens" not in payload for payload in normal_verifier_payloads)
    else:
        assert all(payload["max_tokens"] == expected_normal_budget for payload in normal_verifier_payloads)
    assert len(fix_payloads) == 2 and all(payload["max_tokens"] == expected_fix_budget for payload in fix_payloads)
    assert all(timeout == 90 for _payload, timeout in captured)


def test_provider_settings_persist_without_exposing_api_key(tmp_path):
    api = TestClient(create_app(str(tmp_path / "firmsight.db")))
    payload = {
        "provider": "openai-compatible",
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "models": {
            "investigator": "z-ai/glm-5.3-flash",
            "verifier": "z-ai/glm-5.3-flash",
            "chat": "z-ai/glm-5.3-flash",
            "yaml_generator": "z-ai/glm-5.3-flash",
        },
    }
    updated = api.put("/api/settings", json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()["endpoint"] == payload["endpoint"]
    assert "api_key" not in updated.json()
    assert api.get("/api/settings").json()["models"]["chat"] == "z-ai/glm-5.3-flash"
    invalid = api.put("/api/settings", json={**payload, "endpoint": "ftp://invalid"})
    assert invalid.status_code == 422


def test_reasoning_only_review_recovers_via_bounded_finalization_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "test-only-secret")
    captured: list[dict] = []
    candidate_json = FixtureProvider(source_path="src/main.c").chat("Investigator", "")

    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_urlopen(request, **kwargs):
        payload = json.loads(request.data.decode())
        captured.append(payload)
        system_prompt = payload["messages"][0]["content"]
        if "FINALIZATION RETRY" in system_prompt and "Investigator" in system_prompt:
            body = {"choices": [{"finish_reason": "stop", "message": {"content": candidate_json}}]}
        elif "Investigator" in system_prompt:
            body = {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "private chain"}}], "usage": {"completion_tokens": 3_200}}
        else:
            body = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"verdict": "SURVIVES", "notes": "The candidate remains supported by the supplied source."})}}]}
        return Response(json.dumps(body).encode())

    Response.__init__ = lambda self, b: setattr(self, "body", b)
    Response.read = lambda self: self.body

    monkeypatch.setattr("app.ai_provider.urlopen", fake_urlopen)
    database = str(tmp_path / "finalization-recovery.db")
    api = TestClient(create_app(database))
    settings = api.put("/api/settings", json={
        "provider": "9router",
        "endpoint": "http://127.0.0.1:20128/v1/chat/completions",
        "models": {"investigator": "glm/investigator", "verifier": "glm/verifier"},
        "reasoning_effort": "LOW",
        "structured_finalization_policy": "ALWAYS",
    })
    assert settings.status_code == 200, settings.text
    project = api.post("/api/projects", json={"name": "Finalization recovery", "source_type": "MANUAL"}).json()
    source = (
        "#include <freertos/semphr.h>\n"
        "static SemaphoreHandle_t ota_mutex;\n"
        "esp_err_t ota_install(void) {\n"
        "    xSemaphoreTake(ota_mutex, portMAX_DELAY);\n"
        "    esp_err_t err = esp_ota_begin(NULL, OTA_SIZE_UNKNOWN, NULL);\n"
        "    if (err != ESP_OK) {\n"
        "        return err;\n"
        "    }\n"
        "    xSemaphoreGive(ota_mutex);\n"
        "    return ESP_OK;\n"
        "}\n"
    )
    assert api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": source}}).status_code == 200

    started = api.post(f"/api/projects/{project['id']}/reviews", json={"focus": ["concurrency"]}).json()
    review = api.get(f"/api/projects/{project['id']}/reviews/{started['id']}").json()
    assert review["status"] == "COMPLETED", review.get("error")
    findings = api.get(f"/api/projects/{project['id']}/findings").json()
    assert len(findings) == 1
    recovery_events = [event for event in review["diagnostics"] if event.get("finalization_recovery")]
    assert recovery_events
    assert any(event["state"] == "STRUCTURED_VALIDATED" for event in recovery_events)
    assert all("private chain" not in json.dumps(event) for event in review["diagnostics"])
    # REV-026: per-request effective output budget survives persistence and
    # cannot be conflated with the historical role snapshot.
    assert all(event.get("effective_max_tokens") == 1_600 for event in recovery_events if event["state"] in {"REQUEST_PREPARED", "COMPLETED", "STRUCTURED_VALIDATED"})
    scheduled = [event for event in review["diagnostics"] if event["state"] == "FINALIZATION_RECOVERY_SCHEDULED"]
    assert len(scheduled) == 1
    assert scheduled[0]["effective_max_tokens"] == 1_600
    assert scheduled[0]["retry_suppressed"] is False
    investigator_payloads = [payload for payload in captured if "Investigator" in payload["messages"][0]["content"]]
    recovery_payloads = [payload for payload in investigator_payloads if "FINALIZATION RETRY" in payload["messages"][0]["content"]]
    assert recovery_payloads
    assert recovery_payloads[0]["reasoning_effort"] == "none"
    assert recovery_payloads[0]["max_tokens"] == 1_600
    assert "FINALIZATION RETRY" not in investigator_payloads[0]["messages"][0]["content"]
