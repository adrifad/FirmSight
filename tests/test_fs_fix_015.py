"""FS-FIX-015 regression tests: review worker liveness, terminal states, races.

Covers the Touchsense stuck-review incident fixes:

- a hung provider is marked INTERRUPTED by the stale path and a retry resumes;
- a worker that finishes after the stale path marked the review terminal must
  not resurrect RUNNING (REQ-2);
- the worker heartbeat advances ``last_activity_at`` while units are in flight;
- the ``except Exception`` path writes FAILED even when the first terminal
  ``update_review`` raises (bounded retry, REQ-3).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.indexer import FirmwareIndexer
from app.platform_repository import PlatformRepository
from app.platform_schemas import ProjectCreate, ReviewCreate
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.service import MemoryService


def _investigator_payload(path: str = "src/main.c") -> str:
    return json.dumps({"findings": [{
        "title": "OTA mutex is not released when setup fails",
        "classification": "CONFIRMED_BUG",
        "severity": "high",
        "category": "CONCURRENCY",
        "confidence": 0.86,
        "location": {"file": path, "function": "ota_install", "line_start": 4, "line_end": 9},
        "summary": "A failed esp_ota_begin returns after the mutex has been acquired.",
        "evidence": [{"description": "The error return occurs before xSemaphoreGive.", "file": path, "line": 7}],
        "execution_path": ["ota_install", "xSemaphoreTake", "esp_ota_begin", "return"],
        "runtime_scenario": "A failed OTA setup leaves later OTA callers blocked on the mutex.",
        "impact": "Subsequent OTA operations may stop progressing.",
        "assumptions": [{"statement": "No cleanup wrapper releases the mutex.", "status": "UNVERIFIED"}],
        "recommendation": "Release the mutex on every error exit path.",
    }]})


class BlockingProvider:
    """Investigator blocks until released, then fails like a timed-out request.

    This models the incident: the worker sits inside a provider call that writes
    nothing and eventually errors out, leaving the review with no validated
    batches and no success diagnostics.
    """

    name = "blocking"

    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if "Investigator" in system_prompt:
            self._entered.set()
            # Simulate a provider request that never returns until released.
            assert self._release.wait(timeout=15), "test never released the provider"
            raise RuntimeError("provider request timed out after 90 seconds")
        if "Verifier" in system_prompt:
            return json.dumps({"verdict": "SURVIVES", "notes": "Confirmed against the supplied source."})
        if "Fix Verifier" in system_prompt:
            return json.dumps({"verdict": "FIXED", "notes": "n/a"})
        return json.dumps({})


class WorkingProvider:
    """Completes immediately so a retry can reach a terminal state."""

    name = "working"

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        if "Investigator" in system_prompt:
            return _investigator_payload()
        if "Verifier" in system_prompt:
            return json.dumps({"verdict": "SURVIVES", "notes": "Confirmed against the supplied source."})
        if "Fix Verifier" in system_prompt:
            return json.dumps({"verdict": "FIXED", "notes": "n/a"})
        return json.dumps({})


def _project(tmp_path: Path) -> tuple[PlatformRepository, ProjectService, str]:
    database = str(tmp_path / "firmsight.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Liveness"))
    projects.add_files(project.id, {"src/main.c": "".join(f"void task_{i}(void) {{ shared_state++; }}\n" for i in range(30))})
    projects.index(project.id)
    return repository, projects, project.id


def _service(repository: PlatformRepository, projects: ProjectService, provider) -> ReviewService:
    return ReviewService(
        repository,
        projects,
        lambda _role: provider,  # type: ignore[arg-type]
        MemoryService(MemoryRepository(repository.database_path)),
    )


def _backdate_last_activity(database: str, review_id: str, seconds: int) -> None:
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE reviews SET last_activity_at=? WHERE id=?", (stamp, review_id))


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _wait_until_in_flight(database: str, review_id: str, timeout: float = 5.0) -> None:
    """Wait until the worker has recorded an in-flight request.

    The executor can start the provider call before the dispatch loop writes its
    RUNNING heartbeat, so an ``entered`` event alone does not prove the worker
    has finished its most recent RUNNING write. Waiting for ``in_flight_requests``
    to become non-zero removes that race before the test backdates the marker.
    """
    def _in_flight() -> bool:
        record = PlatformRepository(database).get_review(review_id)
        return bool(record and (record.get("execution_progress") or {}).get("in_flight_requests"))

    assert _wait_for(_in_flight, timeout=timeout), "worker never recorded an in-flight request"


def test_hung_provider_is_interrupted_then_retry_resumes(tmp_path):
    """REQ-5.1: a hung provider -> INTERRUPTED -> retry() resumes to terminal."""
    repository, projects, project_id = _project(tmp_path)
    entered, release = threading.Event(), threading.Event()
    blocking = BlockingProvider(entered, release)
    service = _service(repository, projects, blocking)

    review = service.begin(project_id, ReviewCreate(scope="Full Project", focus=["concurrency"]))
    worker = threading.Thread(target=service.execute, args=(review.id,), daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=5), "investigator never started"
        _wait_until_in_flight(repository.database_path, review.id)
        # The worker is alive but not heartbeating yet (it is inside the provider
        # call). Backdate the marker to simulate the incident's stale window.
        _backdate_last_activity(repository.database_path, review.id, service.RUNNING_STALE_SECONDS + 5)
        interrupted = service.get(review.id)
        assert interrupted.status == "INTERRUPTED"
    finally:
        release.set()
        worker.join(timeout=10)
    assert not worker.is_alive(), "worker must exit after being released"

    # Retry with a working provider resumes the review to a terminal state.
    service.provider_resolver = lambda _role: WorkingProvider()
    resumed = service.retry(review.id)
    assert resumed.status == "RUNNING"
    service.execute(review.id)
    final = service.get(review.id)
    assert final.status in {"COMPLETED", "PARTIAL"}, final.status
    assert final.unavailable_batches == 0


def test_worker_does_not_resurrect_running_after_stale_marking(tmp_path):
    """REQ-5.2 / REQ-2: a worker finishing after stale marking stays terminal."""
    repository, projects, project_id = _project(tmp_path)
    entered, release = threading.Event(), threading.Event()
    blocker = BlockingProvider(entered, release)
    service = _service(repository, projects, blocker)

    review = service.begin(project_id, ReviewCreate(scope="Full Project", focus=["concurrency"]))
    worker = threading.Thread(target=service.execute, args=(review.id,), daemon=True)
    worker.start()
    assert entered.wait(timeout=5), "investigator never started"
    _wait_until_in_flight(repository.database_path, review.id)
    _backdate_last_activity(repository.database_path, review.id, service.RUNNING_STALE_SECONDS + 5)
    assert service.get(review.id).status == "INTERRUPTED"

    # Release the worker: it must notice the terminal state and abort, never
    # writing RUNNING again.
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert service.get(review.id).status == "INTERRUPTED"
    assert repository.review_status(review.id) == "INTERRUPTED"


def test_heartbeat_advances_last_activity_while_units_are_in_flight(tmp_path, monkeypatch):
    """REQ-5.3: the heartbeat keeps last_activity_at fresh during a slow request."""
    repository, projects, project_id = _project(tmp_path)
    entered, release = threading.Event(), threading.Event()
    service = _service(repository, projects, BlockingProvider(entered, release))
    # Make the heartbeat fire fast so the test is quick but still well below the
    # (unchanged) stale window.
    monkeypatch.setattr(service, "HEARTBEAT_SECONDS", 0.15)

    review = service.begin(project_id, ReviewCreate(scope="Full Project", focus=["concurrency"]))
    worker = threading.Thread(target=service.execute, args=(review.id,), daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=5), "investigator never started"
        _wait_until_in_flight(repository.database_path, review.id)
        before = repository.get_review(review.id)["last_activity_at"]
        advanced = _wait_for(
            lambda: repository.get_review(review.id)["last_activity_at"] != before,
            timeout=5,
        )
        assert advanced, "heartbeat did not advance last_activity_at while in flight"
        # A live worker with a fresh heartbeat is never stale-marked.
        assert service.get(review.id).status == "RUNNING"
    finally:
        release.set()
        worker.join(timeout=10)


def test_except_exception_writes_failed_when_first_update_raises(tmp_path, monkeypatch):
    """REQ-5.4 / REQ-3: terminal FAILED is written even if the first write raises."""
    repository, projects, project_id = _project(tmp_path)
    service = _service(repository, projects, WorkingProvider())

    review = service.begin(project_id, ReviewCreate(scope="Full Project", focus=["concurrency"]))

    # Force the body to raise an *unexpected* (non-RuntimeError) error partway
    # through, so the except-Exception terminal path runs.
    def _boom(*args, **kwargs):
        raise ValueError("boom inside review body")

    monkeypatch.setattr(ReviewService, "_execute_body", _boom)

    real_update = PlatformRepository.update_review
    calls = {"count": 0}

    def flaky_update(self, review_id, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1 and kwargs.get("status") == "FAILED":
            raise sqlite3.OperationalError("database is locked")
        return real_update(self, review_id, **kwargs)

    monkeypatch.setattr(PlatformRepository, "update_review", flaky_update)

    service.execute(review.id)

    stored = repository.get_review(review.id)
    assert stored["status"] == "FAILED"
    assert calls["count"] >= 2, "terminal write must be retried once"


def test_review_terminal_helper_distinguishes_running(tmp_path):
    repository, projects, project_id = _project(tmp_path)
    service = _service(repository, projects, WorkingProvider())
    review = service.begin(project_id, ReviewCreate(scope="Full Project", focus=["concurrency"]))
    assert service._is_review_terminal(review.id) is False
    repository.update_review(review.id, status="INTERRUPTED", progress=[])
    assert service._is_review_terminal(review.id) is True
