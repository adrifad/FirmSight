import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai_provider import OpenAICompatibleProvider, ProviderEvent, TokenUsage, _completion_summary, _decode_completion_response, _validation_summary, structured_response
from app.indexer import FirmwareIndexer
from app.main import create_app
from app.platform_schemas import FindingCandidate, FixVerificationResult, InvestigatorResult, ProjectCreate
from app.platform_repository import PlatformRepository
from app.project_service import ProjectService
from app.repository import MemoryRepository
from app.review_service import ReviewService
from app.service import MemoryService


def test_review_candidate_deduplicates_near_identical_issue_at_same_location() -> None:
    first = FindingCandidate.model_validate({
        "title": "OTA mutex is not released when setup fails",
        "classification": "PROBABLE_BUG",
        "severity": "high",
        "category": "CONCURRENCY",
        "confidence": 0.86,
        "location": {"file": "src/ota.cpp", "function": "ota_install", "line_start": 40, "line_end": 48},
        "summary": "A failed OTA setup returns after the mutex has been acquired.",
        "evidence": [{"description": "The error return occurs before xSemaphoreGive.", "file": "src/ota.cpp", "line": 45}],
        "execution_path": ["ota_install", "xSemaphoreTake", "return"],
        "runtime_scenario": "A failed setup leaves later OTA callers blocked on the mutex.",
        "impact": "Subsequent OTA operations may stop progressing.",
        "assumptions": [],
        "recommendation": "Release the mutex on every error exit path.",
    })
    duplicate = FindingCandidate.model_validate({**first.model_dump(mode="json"),
        "title": "OTA mutex remains locked on setup error path",
        "summary": "When OTA setup fails, the acquired mutex is not released before returning.",
        "location": {"file": "src/ota.cpp", "function": "ota_install", "line_start": 42, "line_end": 49},
    })
    distinct = FindingCandidate.model_validate({**first.model_dump(mode="json"),
        "title": "OTA error result is ignored by the caller",
        "summary": "The caller continues without handling the failed OTA setup result.",
        "location": {"file": "src/ota.cpp", "function": "ota_install", "line_start": 40, "line_end": 48},
    })

    assert ReviewService._same_finding_candidate(first, duplicate)
    assert not ReviewService._same_finding_candidate(first, distinct)


def test_usage_is_extracted_from_json_and_reasoning_details() -> None:
    decoded = _decode_completion_response(json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
    }))

    usage = TokenUsage.from_payload(decoded["usage"])  # type: ignore[index]
    assert usage is not None
    assert usage.as_dict() == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18, "reasoning_tokens": 5}


def test_completion_and_validation_summaries_are_safe() -> None:
    summary = _completion_summary({"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "SECRET_SOURCE_SENTINEL"}}]})
    assert summary.content_state == "REASONING_ONLY"
    assert summary.finish_reason == "length"
    assert "SECRET_SOURCE_SENTINEL" not in repr(summary)

    try:
        InvestigatorResult.model_validate({"findings": [{}]})
    except ValidationError as error:
        validation = _validation_summary(error)
    else:
        raise AssertionError("expected schema validation failure")
    assert validation.category == "MISSING_REQUIRED_FIELD"
    assert validation.fields
    assert all(path.startswith("findings.0.") for path in validation.fields)
    assert "SECRET_SOURCE_SENTINEL" not in repr(validation)


def test_sse_completion_keeps_usage_only_terminal_frame() -> None:
    body = '\n'.join([
        'data: {"choices":[{"delta":{"content":"ok"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":4,"total_tokens":24}}',
        'data: [DONE]',
    ])

    decoded = _decode_completion_response(body, "text/event-stream")
    assert decoded["choices"][0]["message"]["content"] == "ok"  # type: ignore[index]
    assert decoded["usage"] == {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}  # type: ignore[index]


def test_provider_emits_safe_lifecycle_events_without_response_body(monkeypatch) -> None:
    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":3}}'

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider("secret-key", "https://gateway.example/v1/chat/completions", "glm/flash", name="9router")

    result = provider.chat_with_metadata("system", "question", operation="review:REV-1:investigator:batch-1", on_event=events.append)

    assert result.content == "ok"
    assert result.usage is not None
    assert result.usage.total_tokens == 3
    assert [event.state for event in events] == ["REQUEST_SENT", "WAITING_FOR_PROVIDER", "RESPONSE_RECEIVED", "COMPLETED"]
    assert all("secret-key" not in repr(event) for event in events)
    assert all(event.response_chars is None or event.response_chars < 200 for event in events)


def test_provider_marks_timeout_as_distinct_failure(monkeypatch) -> None:
    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider("secret-key", "http://router.test/v1/chat/completions", "glm/flash", name="9router")

    try:
        provider.chat_with_metadata("system", "question", operation="review:REV-1:investigator:batch-2", on_event=events.append)
    except RuntimeError as error:
        assert "timed out after 90 seconds" in str(error)
    else:
        raise AssertionError("expected provider timeout")

    failure = events[-1]
    assert failure.state == "FAILED"
    assert failure.error_kind == "timeout"
    assert failure.elapsed_ms is not None


def test_provider_classifies_reasoning_only_output_limit_without_raw_content(monkeypatch) -> None:
    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"finish_reason":"length","message":{"reasoning_content":"private chain"}}],"usage":{"completion_tokens":10}}'

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider("secret-key", "https://gateway.example/v1/chat/completions", "glm/flash", name="9router")

    try:
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)
    except RuntimeError as error:
        assert "output limit reached before final" in str(error)
        assert "private chain" not in str(error)
    else:
        raise AssertionError("expected output-limit failure")

    failure = next(event for event in events if event.error_kind == "OUTPUT_LIMIT_BEFORE_FINAL")
    assert failure.content_state == "REASONING_ONLY"
    assert failure.finish_reason == "length"
    assert failure.retry_suppressed is True
    assert all("private chain" not in repr(event) for event in events)


@pytest.mark.parametrize(
    "message",
    [{"content": ""}, {}],
    ids=["empty-content", "missing-content"],
)
def test_provider_classifies_length_without_reasoning_as_output_limit(monkeypatch, message) -> None:
    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"choices": [{"finish_reason": "length", "message": message}]}).encode()

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider("secret-key", "https://gateway.example/v1/chat/completions", "glm/flash", name="9router")

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)

    failure = next(event for event in events if event.error_kind == "OUTPUT_LIMIT_BEFORE_FINAL")
    assert failure.finish_reason == "length"
    assert failure.content_state in {"EMPTY", "MISSING_MESSAGE"}
    assert failure.retry_suppressed is True


def test_structured_response_does_not_overwrite_provider_failure_metadata() -> None:
    class HttpFailureProvider:
        name = "openrouter"
        model = "glm/flash"
        endpoint = "https://router.example/v1/chat/completions"

        def available(self) -> bool:
            return True

        def chat(self, _system_prompt: str, _user_prompt: str) -> str:
            raise AssertionError("chat fallback should not be used")

        def chat_with_metadata(self, _system_prompt: str, _user_prompt: str, *, on_event=None, **_kwargs):
            if on_event:
                on_event(ProviderEvent(
                    state="FAILED",
                    request_id="AI-HTTP503",
                    operation="review:REV-1:investigator:batch-1",
                    provider=self.name,
                    model=self.model,
                    endpoint=self.endpoint,
                    created_at="2026-09-13T00:00:00+00:00",
                    http_status=503,
                    error_kind="http_error",
                    error_message="upstream unavailable",
                ))
            raise RuntimeError("openrouter request failed (HTTP 503): upstream unavailable")

    events: list[ProviderEvent] = []
    try:
        structured_response(HttpFailureProvider(), FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)
    except RuntimeError as error:
        assert "HTTP 503" in str(error)
    else:
        raise AssertionError("expected provider failure")

    failures = [event for event in events if event.state == "FAILED"]
    assert len(failures) == 1
    assert failures[0].error_kind == "http_error"
    assert failures[0].http_status == 503


class EmptyReviewProvider:
    name = "test-provider"
    model = "test-model"
    endpoint = "http://provider.test/v1"

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, _user_prompt: str) -> str:
        if "Investigator" in system_prompt:
            return '{"findings":[]}'
        return '{"verdict":"REJECTED","notes":"No candidate should be verified."}'


class TimeoutReviewProvider(EmptyReviewProvider):
    def chat(self, _system_prompt: str, _user_prompt: str) -> str:
        raise RuntimeError("9router request timed out after 90 seconds")


def test_empty_investigator_result_is_completed_and_diagnostic_is_scoped(tmp_path) -> None:
    provider = EmptyReviewProvider()
    api = TestClient(create_app(str(tmp_path / "telemetry.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Telemetry fixture", "source_type": "MANUAL"}).json()
    added = api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "void app_main(void) {}\n"}})
    assert added.status_code == 200

    response = api.post(f"/api/projects/{project['id']}/reviews", json={"scope": "Full Project", "focus": ["concurrency"]})
    assert response.status_code == 201, response.text
    review_id = response.json()["id"]
    review = api.get(f"/api/projects/{project['id']}/reviews/{review_id}").json()
    for _ in range(20):
        if review["status"] != "RUNNING":
            break
        review = api.get(f"/api/projects/{project['id']}/reviews/{review_id}").json()

    assert review["status"] == "COMPLETED"
    assert any("returned 0 candidate findings" in step for step in review["progress"])
    diagnostics = review["diagnostics"]
    assert diagnostics
    assert all(event["review_id"] == review["id"] for event in diagnostics)
    assert [event["state"] for event in diagnostics if event["role"] == "investigator"] == ["REQUEST_PREPARED", "STRUCTURED_VALIDATED"]
    assert all("void app_main" not in json.dumps(event) for event in diagnostics)


def test_review_timeout_is_persisted_as_timeout_diagnostic(tmp_path) -> None:
    provider = TimeoutReviewProvider()
    api = TestClient(create_app(str(tmp_path / "timeout.db"), provider_resolver=lambda _role: provider))
    project = api.post("/api/projects", json={"name": "Timeout fixture", "source_type": "MANUAL"}).json()
    api.post(f"/api/projects/{project['id']}/files", json={"files": {"src/main.c": "void app_main(void) {}\n"}})
    response = api.post(f"/api/projects/{project['id']}/reviews", json={"scope": "Full Project", "focus": ["concurrency"]})
    review_id = response.json()["id"]
    review = api.get(f"/api/projects/{project['id']}/reviews/{review_id}").json()
    assert review["status"] == "FAILED"
    failures = [event for event in review["diagnostics"] if event["state"] == "FAILED"]
    assert failures
    assert any(event["error_kind"] == "timeout" for event in failures)


def test_stale_running_review_is_marked_interrupted(tmp_path) -> None:
    database = str(tmp_path / "stale.db")
    repository = PlatformRepository(database)
    projects = ProjectService(repository, FirmwareIndexer())
    project = projects.create(ProjectCreate(name="Stale review", source_type="MANUAL"))
    old = (datetime.now(UTC) - timedelta(seconds=240)).isoformat()
    repository.create_review({
        "id": "REV-STALE",
        "project_id": project.id,
        "scope": "Full Project",
        "focus": ["concurrency"],
        "context_files": ["src/main.c"],
        "context_chars": 12000,
        "total_batches": 2,
        "validated_batches": 1,
        "unavailable_batches": 0,
        "status": "RUNNING",
        "progress": ["Investigator is analyzing source batch 2/2"],
        "created_at": old,
        "last_activity_at": old,
        "completed_at": None,
    })
    service = ReviewService(
        repository,
        projects,
        lambda _role: EmptyReviewProvider(),
        MemoryService(MemoryRepository(database)),
    )

    review = service.get("REV-STALE")

    assert review.status == "INTERRUPTED"
    assert review.validated_batches == 1
    assert review.unavailable_batches == 1
    assert "worker stopped" in (review.error or "")


def test_provider_events_carry_structured_request_contract_metadata(monkeypatch) -> None:
    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"finish_reason":"length","message":{"reasoning_content":"private chain"}}]}'

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider(
        "secret-key",
        "https://gateway.example/v1/chat/completions",
        "deepseek-v4.1-flash",
        name="9router",
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="LOW",
        structured_finalization_policy="ALWAYS",
    )

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)

    prepared = [event for event in events if event.state == "REQUEST_PREPARED"]
    assert prepared
    initial = [event for event in prepared if not event.finalization_recovery]
    assert initial
    for event in initial:
        assert event.structured_mode == "JSON_OBJECT"
        assert event.finalization_policy == "ALWAYS"
        assert event.finalization_recovery is False
    # ALWAYS policy on a DeepSeek output-limit failure attempts the one
    # bounded recovery; its contract metadata is asserted separately below.
    recovery_prepared = [event for event in prepared if event.finalization_recovery]
    assert recovery_prepared
    for event in recovery_prepared:
        assert event.structured_mode == "JSON_OBJECT"
        assert event.finalization_policy == "ALWAYS"
        assert event.operation.endswith(":finalization-retry")
    exhausted = [event for event in events if event.error_kind == "OUTPUT_FINALIZATION_EXHAUSTED"]
    assert len(exhausted) == 1
    assert exhausted[0].finalization_recovery is True


def test_finalization_recovery_events_are_marked_and_safe(monkeypatch) -> None:
    bodies = [
        json.dumps({"choices": [{"finish_reason": "length", "message": {"reasoning_content": "private chain"}}]}).encode(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Recovered final structured response."}'}}]}).encode(),
    ]

    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200
        index = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            body = bodies[min(self.index, len(bodies) - 1)]
            Response.index += 1
            return body

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider(
        "secret-key",
        "https://gateway.example/v1/chat/completions",
        "deepseek-v4.1-flash",
        name="9router",
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="LOW",
        structured_finalization_policy="AUTO",
    )

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)

    assert result.verdict == "FIXED"
    recovery = [event for event in events if event.finalization_recovery]
    assert recovery
    assert any(event.state == "STRUCTURED_VALIDATED" and event.finalization_recovery for event in events)
    assert all(event.structured_mode == "JSON_OBJECT" for event in recovery)
    assert all(event.finalization_policy == "AUTO" for event in recovery)
    assert all("private chain" not in repr(event) for event in events)


def test_settings_persist_structured_finalization_policy(tmp_path) -> None:
    database = str(tmp_path / "finalization-policy.db")
    api = TestClient(create_app(database))

    initial = api.get("/api/settings")
    assert initial.status_code == 200
    assert initial.json()["structured_finalization_policy"] == "AUTO"

    update = {
        "provider": "openai-compatible",
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "models": {"chat": "example/chat"},
        "structured_finalization_policy": "ALWAYS",
    }
    saved = api.put("/api/settings", json=update)
    assert saved.status_code == 200, saved.text
    assert saved.json()["structured_finalization_policy"] == "ALWAYS"

    reloaded = TestClient(create_app(database)).get("/api/settings")
    assert reloaded.json()["structured_finalization_policy"] == "ALWAYS"

    invalid = api.put("/api/settings", json={**update, "structured_finalization_policy": "SOMETIMES"})
    assert invalid.status_code == 422


def test_finalization_timeline_carries_effective_budget_and_scheduled_marker(monkeypatch) -> None:
    bodies = [
        json.dumps({"choices": [{"finish_reason": "length", "message": {"reasoning_content": "private chain"}}], "usage": {"completion_tokens": 3_200}}).encode(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Recovered final structured response."}'}}]}).encode(),
    ]

    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200
        index = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            body = bodies[min(self.index, len(bodies) - 1)]
            Response.index += 1
            return body

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider(
        "secret-key",
        "https://gateway.example/v1/chat/completions",
        "deepseek-v4.1-flash",
        name="9router",
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="LOW",
        structured_finalization_policy="AUTO",
        max_tokens=2_400,
    )

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)

    assert result.verdict == "FIXED"
    scheduled = [event for event in events if event.state == "FINALIZATION_RECOVERY_SCHEDULED"]
    assert len(scheduled) == 1
    assert scheduled[0].effective_max_tokens == 1_600
    # Initial attempt reports the effective role cap (the scheduled marker is
    # a forward-looking notice of the recovery cap, not the initial cap).
    assert all(event.effective_max_tokens == 2_400 for event in events if not event.finalization_recovery and event.operation == "structured" and event.state != "FINALIZATION_RECOVERY_SCHEDULED")
    # Every recovery-lifecycle event reports the bounded 1,600 cap.
    assert all(event.effective_max_tokens == 1_600 for event in events if event.finalization_recovery)
    assert all("private chain" not in repr(event) for event in events)


def test_exhausted_policy_reports_terminal_suppression_without_scheduled_marker(monkeypatch) -> None:
    bodies = [json.dumps({"choices": [{"finish_reason": "length", "message": {"reasoning_content": "private chain"}}]}).encode()]

    class Response:
        headers = {"Content-Type": "application/json"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return bodies[0]

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    events: list[ProviderEvent] = []
    provider = OpenAICompatibleProvider(
        "secret-key",
        "https://gateway.example/v1/chat/completions",
        "deepseek-v4.1-flash",
        name="9router",
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="LOW",
        structured_finalization_policy="NEVER",
        max_tokens=2_400,
    )

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)

    assert not [event for event in events if event.state == "FINALIZATION_RECOVERY_SCHEDULED"]
    failed = [event for event in events if event.error_kind == "OUTPUT_LIMIT_BEFORE_FINAL"]
    assert failed
    assert all(event.retry_suppressed for event in failed)
    assert all(event.effective_max_tokens == 2_400 for event in failed)
