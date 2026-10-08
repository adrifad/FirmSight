from fastapi.testclient import TestClient
import hashlib
from types import SimpleNamespace

from app.indexer import FirmwareIndexer
from app.main import create_app
from app.platform_repository import PlatformRepository
from app.platform_schemas import ProjectCreate, ReviewCreate
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.service import MemoryService


def test_review_context_setting_defaults_persists_and_rejects_invalid_values(tmp_path) -> None:
    database = str(tmp_path / "settings.db")
    api = TestClient(create_app(database))

    initial = api.get("/api/settings")
    assert initial.status_code == 200
    assert initial.json()["review_context_chars"] == 42_000

    update = {
        "provider": "openai-compatible",
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "models": {"chat": "example/chat"},
        "review_context_chars": 12_000,
        "investigator_max_tokens": "PROVIDER_DEFAULT",
        "verifier_max_tokens": 2_000,
    }
    saved = api.put("/api/settings", json=update)
    assert saved.status_code == 200, saved.text
    assert saved.json()["review_context_chars"] == 12_000
    assert saved.json()["investigator_max_tokens"] == "PROVIDER_DEFAULT"
    assert saved.json()["verifier_max_tokens"] == 2_000

    reloaded = TestClient(create_app(database)).get("/api/settings")
    assert reloaded.json()["review_context_chars"] == 12_000
    assert reloaded.json()["investigator_max_tokens"] == "PROVIDER_DEFAULT"
    assert reloaded.json()["verifier_max_tokens"] == 2_000

    invalid = api.put("/api/settings", json={**update, "review_context_chars": 13_000})
    assert invalid.status_code == 422
    assert "12,000" in invalid.json()["detail"]


def test_compact_review_context_keeps_every_source_file_and_stays_bounded(tmp_path) -> None:
    database = str(tmp_path / "review.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Compact context"))
    files = {
        f"src/task_{index}.c": (f"void task_{index}(void) {{\n" + "  queue_receive();\n" * 500 + "}\n")
        for index in range(6)
    }
    projects.add_files(project.id, files)
    projects.index(project.id)
    review = ReviewService(
        repository,
        projects,
        lambda _role: None,  # type: ignore[arg-type]
        MemoryService(MemoryRepository(database)),
        review_context_resolver=lambda: 12_000,
    )

    batches = review._context_batches(project.id, project.name, ["concurrency"], repository.raw_files(project.id))

    assert batches
    assert all(len(batch.context) <= 12_000 for batch in batches)
    covered = {path for batch in batches for path in batch.files}
    assert covered == set(files)
    segments = [segment for batch in batches for segment in batch.segments]
    assert all(segment.content_hash and len(segment.content_hash) == 64 for segment in segments)
    assert len({batch.unit_id for batch in batches}) == len(batches)
    assert all(batch.label for batch in batches)
    assert sum(segment.line_end - segment.line_start + 1 for segment in segments) >= 6 * 500


def test_review_unit_cache_key_changes_for_all_review_inputs(tmp_path, monkeypatch) -> None:
    database = str(tmp_path / "cache-key.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Cache key"))
    projects.add_files(project.id, {"src/main.c": "void main_task(void) { queue_receive(); }\n"})
    projects.index(project.id)
    service = ReviewService(
        repository,
        projects,
        lambda _role: None,  # type: ignore[arg-type]
        MemoryService(MemoryRepository(database)),
    )
    investigator = SimpleNamespace(model="investigator-a", max_tokens=2000, structured_output_mode="PROMPT_ONLY", reasoning_effort="LOW")
    verifier = SimpleNamespace(model="verifier-a", max_tokens=1200, structured_output_mode="PROMPT_ONLY", reasoning_effort="LOW")
    review = ReviewCreate(scope="Full Project", focus=["concurrency"])
    batch = service._context_batches(project.id, project.name, review.focus, repository.raw_files(project.id))[0]
    original = service._unit_cache_key(project.id, review, batch, investigator, verifier)

    assert original != service._unit_cache_key(project.id, ReviewCreate(scope="Selected File", focus=review.focus), batch, investigator, verifier)
    assert original != service._unit_cache_key(project.id, review, batch, SimpleNamespace(**{**investigator.__dict__, "model": "investigator-b"}), verifier)
    assert original != service._unit_cache_key(project.id, ReviewCreate(scope=review.scope, focus=["mqtt"]), batch, investigator, verifier)
    assert original != service._unit_cache_key(project.id, review, batch, SimpleNamespace(**{**investigator.__dict__, "max_tokens": 3200}), verifier)
    assert original != service._unit_cache_key(project.id, review, batch, SimpleNamespace(**{**investigator.__dict__, "structured_output_mode": "JSON_SCHEMA"}), verifier)
    assert original != service._unit_cache_key(project.id, review, batch, SimpleNamespace(**{**investigator.__dict__, "reasoning_effort": "HIGH"}), verifier)
    assert original != service._unit_cache_key(project.id, review, batch, investigator, SimpleNamespace(**{**verifier.__dict__, "model": "verifier-b"}))
    assert original != service._unit_cache_key(project.id, review, batch, investigator, SimpleNamespace(**{**verifier.__dict__, "max_tokens": 2000}))

    repository.add_yaml({"id": "yaml-1", "project_id": project.id, "content": "version: 1\n", "valid": True, "errors": [], "generated_at": "2026-09-13T00:00:00+00:00"})
    assert original != service._unit_cache_key(project.id, review, batch, investigator, verifier)
    monkeypatch.setattr(service, "_memory_context_fingerprint", lambda _project_id, _files: "changed-memory")
    assert original != service._unit_cache_key(project.id, review, batch, investigator, verifier)
    monkeypatch.setattr("app.review_service.INVESTIGATOR_PROMPT_VERSION", "investigator-test-version")
    assert original != service._unit_cache_key(project.id, review, batch, investigator, verifier)
    monkeypatch.setattr(ReviewService, "REVIEW_ENGINE_VERSION", "review-engine-test-version")
    assert original != service._unit_cache_key(project.id, review, batch, investigator, verifier)

    projects.add_files(project.id, {"src/main.c": "void main_task(void) { queue_receive(); queue_send(); }\n"})
    projects.index(project.id)
    changed_batch = service._context_batches(project.id, project.name, review.focus, repository.raw_files(project.id))[0]
    assert original != service._unit_cache_key(project.id, review, changed_batch, investigator, verifier)


def test_focus_profiles_prioritize_matching_indexed_symbols(tmp_path) -> None:
    database = str(tmp_path / "focus-planner.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Focus planner"))
    projects.add_files(project.id, {
        "platformio.ini": "[env:esp32]\nplatform = espressif32\n",
        "src/generic.c": "void helper(void) { return; }\n",
        "src/mqtt_client.c": "void mqtt_publish_task(void) { mqtt_publish(); }\n",
        "src/ota_manager.c": "void ota_install(void) { esp_ota_begin(); }\n",
        "src/freertos_tasks.c": "void measurement_task(void) { xQueueReceive(queue, 0, 0); }\n",
    })
    projects.index(project.id)
    raw_files = repository.raw_files(project.id)
    service = ReviewService(repository, projects, lambda _role: None, MemoryService(MemoryRepository(database)))

    def source_order(focus: list[str]) -> list[str]:
        return [path for batch in service._context_batches(project.id, project.name, focus, raw_files) for path in batch.files if path != "platformio.ini"]

    assert source_order(["mqtt"])[0] == "src/mqtt_client.c"
    assert source_order(["ota"])[0] == "src/ota_manager.c"
    assert source_order(["freertos"])[0] == "src/freertos_tasks.c"


def test_context_caps_cover_each_source_segment_exactly_once(tmp_path) -> None:
    database = str(tmp_path / "coverage-caps.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Coverage caps"))
    files = {
        f"src/module_{index}.c": "void module_task(void) {\n" + "  xQueueReceive(queue, &item, portMAX_DELAY);\n" * 520 + "}\n"
        for index in range(4)
    }
    files["src/generated_font.c"] = "0x00, " * 30_000
    projects.add_files(project.id, files)
    projects.index(project.id)
    raw_files = repository.raw_files(project.id)
    service = ReviewService(repository, projects, lambda _role: None, MemoryService(MemoryRepository(database)))
    expected: set[tuple[str, int, int, str]] = set()
    for file in raw_files:
        if "generated_font" in file["path"]:
            continue
        for start, end, excerpt in service._source_segments(file):
            expected.add((file["path"], start, end, hashlib.sha256(excerpt.encode()).hexdigest()))

    for cap in (12_000, 24_000, 32_000):
        batches = service._context_batches(project.id, project.name, ["concurrency"], raw_files, context_limit_chars=cap)
        actual = {
            (segment.file, segment.line_start, segment.line_end, segment.content_hash)
            for batch in batches
            for segment in batch.segments
        }
        assert all(len(batch.context) <= cap for batch in batches)
        assert actual == expected
        assert len(actual) == sum(len(batch.segments) for batch in batches)
        assert all("generated_font" not in path for batch in batches for path in batch.files)


def test_source_envelope_scales_with_investigator_budget(tmp_path) -> None:
    database = str(tmp_path / "budget-envelope.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Budget envelope"))
    files = {
        f"src/task_{index}.c": (f"void task_{index}(void) {{\n" + "  queue_receive();\n" * 400 + "}\n")
        for index in range(5)
    }
    projects.add_files(project.id, files)
    projects.index(project.id)
    service = ReviewService(
        repository,
        projects,
        lambda _role: None,  # type: ignore[arg-type]
        MemoryService(MemoryRepository(database)),
    )
    raw_files = repository.raw_files(project.id)

    def segment_chars(budget: int | str | None) -> int:
        batches = service._context_batches(project.id, project.name, ["concurrency"], raw_files, investigator_budget=budget)
        assert batches
        return max(len(batch.context) for batch in batches)

    compact = segment_chars(1_200)
    default = segment_chars(2_000)
    generous = segment_chars(3_200)
    provider_default = segment_chars("PROVIDER_DEFAULT")
    assert compact < default <= generous
    assert provider_default == segment_chars(None)
    # Compact budget still covers every source file across its extra units.
    for budget in (1_200, 3_200, "PROVIDER_DEFAULT"):
        batches = service._context_batches(project.id, project.name, ["concurrency"], raw_files, investigator_budget=budget)
        covered = {path for batch in batches for path in batch.files}
        assert covered == set(files)


def test_source_chars_for_budget_buckets_are_deterministic() -> None:
    from app.review_service import ReviewService

    assert ReviewService.source_chars_for_budget(1_200) == 12_000
    assert ReviewService.source_chars_for_budget(1_600) == 16_000
    assert ReviewService.source_chars_for_budget(2_000) == 20_000
    assert ReviewService.source_chars_for_budget(2_400) == 24_000
    assert ReviewService.source_chars_for_budget(3_200) == 30_000
    assert ReviewService.source_chars_for_budget(2_100) == 24_000
    assert ReviewService.source_chars_for_budget("PROVIDER_DEFAULT") == ReviewService.DEFAULT_SOURCE_CHARS_FOR_BUDGET
    assert ReviewService.source_chars_for_budget(None) == ReviewService.DEFAULT_SOURCE_CHARS_FOR_BUDGET
