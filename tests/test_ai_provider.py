import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from app.ai_provider import OpenAICompatibleProvider, configured_provider, structured_response
from app.intelligence_schemas import MemorySynthesisResult, MemoryVerificationResult
from app.platform_schemas import FixVerificationResult
from app.prompts import MEMORY_SYNTHESIZER_SYSTEM, MEMORY_VERIFIER_SYSTEM


def test_provider_reports_safe_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_urlopen(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            "https://example.test/v1/chat/completions",
            429,
            "Too Many Requests",
            {},
            BytesIO(b'{"error":{"message":"rate limit exceeded"}}'),
        )

    monkeypatch.setattr("app.ai_provider.urlopen", failing_urlopen)
    provider = OpenAICompatibleProvider(
        api_key="secret-api-key",
        endpoint="https://example.test/v1/chat/completions",
        model="example-model",
        name="fregateway",
    )

    with pytest.raises(RuntimeError, match=r"fregateway request failed \(HTTP 429\): fregateway upstream returned HTTP 429") as error:
        provider.chat("system", "question")

    assert "secret-api-key" not in str(error.value)


def test_http_error_body_is_not_returned_or_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_urlopen(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            "https://example.test/v1/chat/completions",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"error":{"message":"FIRMWARE_SOURCE_SENTINEL API_KEY_SENTINEL"}}'),
        )

    monkeypatch.setattr("app.ai_provider.urlopen", failing_urlopen)
    provider = OpenAICompatibleProvider("secret-api-key", "https://example.test/v1/chat/completions", "example-model", name="9router")

    with pytest.raises(RuntimeError) as caught:
        provider.chat("system", "question")
    rendered = str(caught.value)
    assert "FIRMWARE_SOURCE_SENTINEL" not in rendered
    assert "API_KEY_SENTINEL" not in rendered
    assert "secret-api-key" not in rendered
    assert "HTTP 400" in rendered


def test_structured_mode_requires_explicit_provider_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    def failing_urlopen(request: object, **_kwargs: object) -> None:
        captured.append(request)
        raise HTTPError(
            "https://example.test/v1/chat/completions",
            404,
            "Not Found",
            {},
            BytesIO(b'{"error":{"message":"response_format endpoint missing"}}'),
        )

    monkeypatch.setattr("app.ai_provider.urlopen", failing_urlopen)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "example-model", name="9router", structured_output_mode="JSON_OBJECT")
    with pytest.raises(RuntimeError, match=r"HTTP 404") as caught:
        provider.chat_with_metadata("system", "question", structured_schema={"type": "object"})
    assert "does not support" not in str(caught.value)
    assert captured


def test_structured_mode_rejection_is_classified_without_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_urlopen(*_args: object, **_kwargs: object) -> None:
        raise HTTPError(
            "https://example.test/v1/chat/completions",
            422,
            "Unprocessable Entity",
            {},
            BytesIO(b'{"error":{"code":"response_format_not_supported","message":"FIRMWARE_SOURCE_SENTINEL"}}'),
        )

    monkeypatch.setattr("app.ai_provider.urlopen", failing_urlopen)
    events = []
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "example-model", name="9router", structured_output_mode="JSON_SCHEMA")
    with pytest.raises(RuntimeError) as caught:
        provider.chat_with_metadata("system", "question", structured_schema={"type": "object"}, on_event=events.append)
    assert "does not support the configured JSON_SCHEMA" in str(caught.value)
    assert "FIRMWARE_SOURCE_SENTINEL" not in str(caught.value)
    assert events[-1].error_kind == "STRUCTURED_MODE_UNSUPPORTED"


def test_fregateway_uses_compatible_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured.update(dict(request.header_items()))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider(
        api_key="secret-api-key",
        endpoint="https://example.test/v1/chat/completions",
        model="example-model",
        name="fregateway",
    )

    assert provider.chat("system", "question") == "ok"
    assert captured["User-agent"] == "curl/8.14.1"


def test_provider_sends_configured_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured["payload"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider(
        api_key="secret-api-key",
        endpoint="https://example.test/v1/chat/completions",
        model="example-model",
        max_tokens=321,
    )

    assert provider.chat("system", "question") == "ok"
    assert captured["payload"]["max_tokens"] == 321  # type: ignore[index]


def test_provider_default_omits_output_limit_from_structured_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured["payload"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider("secret-api-key", "https://example.test/v1/chat/completions", "example-model", max_tokens=None)

    assert provider.chat_with_metadata("system", "question", structured_schema={"type": "object"}).content == "ok"
    assert "max_tokens" not in captured["payload"]  # type: ignore[operator]


@pytest.mark.parametrize(
    ("mode", "expected_format"),
    [
        ("JSON_OBJECT", {"type": "json_object"}),
        ("JSON_SCHEMA", {"type": "json_schema"}),
    ],
)
def test_provider_sends_explicit_structured_output_mode(monkeypatch: pytest.MonkeyPatch, mode: str, expected_format: dict[str, str]) -> None:
    captured: dict[str, object] = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured["payload"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "glm/flash", structured_output_mode=mode, reasoning_effort="HIGH")

    assert provider.chat_with_metadata("system", "question", structured_schema={"type": "object"}).content == "ok"
    payload = captured["payload"]  # type: ignore[assignment]
    assert payload["response_format"]["type"] == expected_format["type"]  # type: ignore[index]
    assert payload["reasoning_effort"] == "high"  # type: ignore[index]


def test_deepseek_non_thinking_mode_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured["payload"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider(
        "secret",
        "https://example.test/v1/chat/completions",
        "deepseek-v4.1-flash",
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="NONE",
    )

    assert provider.chat_with_metadata("system", "question", structured_schema={"type": "object"}).content == "ok"
    payload = captured["payload"]  # type: ignore[assignment]
    assert payload["reasoning_effort"] == "none"  # type: ignore[index]
    assert payload["thinking"] == {"type": "disabled"}  # type: ignore[index]
    assert payload["response_format"] == {"type": "json_object"}  # type: ignore[index]


def test_deepseek_structured_default_disables_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured["payload"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider(
        "secret",
        "https://example.test/v1/chat/completions",
        "deepseek-v4.1-flash",
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="UNSPECIFIED",
    )

    provider.chat_with_metadata("system", "question", structured_schema={"type": "object"})
    payload = captured["payload"]  # type: ignore[assignment]
    assert payload["reasoning_effort"] == "none"  # type: ignore[index]
    assert payload["thinking"] == {"type": "disabled"}  # type: ignore[index]


def test_structured_repair_gets_headroom_and_accepts_trailing_comma(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    responses = [
        '{"verdict":"FIXED","notes":"missing closing brace"',
        '{"verdict":"FIXED","notes":"The repaired response is complete.",}',
    ]

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            content = responses.pop(0)
            return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured.append(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider(
        "secret",
        "https://example.test/v1/chat/completions",
        "deepseek-v4.1-flash",
        max_tokens=1_000,
        structured_output_mode="JSON_OBJECT",
        reasoning_effort="NONE",
    )

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert result.verdict == "FIXED"
    assert captured[0]["max_tokens"] == 1_000
    assert captured[1]["max_tokens"] == 2_000


def test_prompt_only_does_not_send_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}]}'

    def successful_urlopen(request: object, **_kwargs: object) -> Response:
        captured["payload"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        return Response()

    monkeypatch.setattr("app.ai_provider.urlopen", successful_urlopen)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "glm/flash")
    provider.chat_with_metadata("system", "question", structured_schema={"type": "object"})

    assert "response_format" not in captured["payload"]  # type: ignore[operator]


def test_provider_accepts_buffered_sse_completion_followed_by_done(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}\ndata: [DONE]\n'

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    provider = OpenAICompatibleProvider("secret", "http://router.test/v1/chat/completions", "glm/glm-4-flash", name="9router")

    assert provider.chat("system", "question") == "ok"


def test_provider_combines_standard_sse_delta_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return (b'data: {"choices":[{"delta":{"content":"o"}}]}\n\n'
                    b'data: {"choices":[{"delta":{"content":"k"}}]}\n\n'
                    b'data: [DONE]\n')

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    provider = OpenAICompatibleProvider("secret", "http://router.test/v1/chat/completions", "glm/glm-4-flash", name="9router")

    assert provider.chat("system", "question") == "ok"


def test_provider_reports_malformed_sse_without_exposing_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'data: {not-json}\n'

    monkeypatch.setattr("app.ai_provider.urlopen", lambda *_args, **_kwargs: Response())
    provider = OpenAICompatibleProvider("secret", "http://router.test/v1/chat/completions", "glm/glm-4-flash", name="9router")

    with pytest.raises(RuntimeError, match="9router returned an invalid JSON response") as error:
        provider.chat("system", "question")
    assert "not-json" not in str(error.value)


def test_configured_provider_supports_9router_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.delenv("FIRMSIGHT_AI_ENDPOINT", raising=False)
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    monkeypatch.setenv("FIRMSIGHT_CHAT_MODEL", "glm/glm-4-flash")

    provider = configured_provider()

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "9router"
    assert provider.endpoint == "http://127.0.0.1:20128/v1/chat/completions"
    assert provider.model == "glm/glm-4-flash"


def test_structured_response_retries_empty_completions_before_succeeding() -> None:
    class EmptyThenJsonProvider:
        name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        def available(self) -> bool:
            return True

        def chat(self, _system_prompt: str, _user_prompt: str) -> str:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("openrouter response did not contain usable chat content")
            return '{"verdict":"FIXED","notes":"Current source releases the mutex before return."}'

    provider = EmptyThenJsonProvider()

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check the source.")

    assert result.verdict == "FIXED"
    assert provider.calls == 3


class QueuedResponseProvider:
    """Test-only provider answering from an ordered queue of raw completions."""

    name = "openrouter"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def chat(self, system_prompt: str, _user_prompt: str) -> str:
        self.calls += 1
        self.prompts.append(system_prompt)
        if not self.responses:
            raise RuntimeError("scripted provider exhausted")
        return self.responses.pop(0)


def _valid_synthesis_json() -> str:
    return json.dumps({"candidates": [{
        "type": "BUG_PATTERN",
        "statement": "OTA mutex acquisition lacks a release on the error path.",
        "confidence": 0.8,
        "observed": True,
        "origin_reason": "The accepted finding showed the missing mutex release.",
        "reuse_reason": "Future reviews should recheck error-path mutex release.",
        "symbols": ["ota_install"],
        "components": [],
        "evidence": [{"file": "src/main.c", "line": 6, "symbol": "ota_install", "description": "Mutex take without matching give."}],
    }]})


def _valid_verifier_json() -> str:
    return json.dumps({
        "action": "VERIFY_NEW",
        "target_id": None,
        "confidence": 0.9,
        "source_support": "SUPPORTED",
        "valid_evidence": [{"file": "src/main.c", "line": 6, "description": "Mutex take without release in current source."}],
        "conflict_target_id": None,
        "conflict_evidence": [],
        "rationale": "Current source confirms the missing release on the error path.",
    })


def test_structured_response_repairs_malformed_memory_synthesizer_output() -> None:
    provider = QueuedResponseProvider(["<broken synthesizer output not json>", _valid_synthesis_json()])

    result = structured_response(provider, MemorySynthesisResult, MEMORY_SYNTHESIZER_SYSTEM, "Synthesize candidates.")

    assert [candidate.type for candidate in result.candidates] == ["BUG_PATTERN"]
    assert result.candidates[0].observed is True
    assert provider.calls == 2
    assert "repair" in provider.prompts[1].casefold()


def test_structured_response_repairs_schema_invalid_memory_verifier_output() -> None:
    schema_invalid = json.dumps({
        "action": "MAKE_IT_SO",
        "target_id": None,
        "confidence": 0.9,
        "source_support": "SUPPORTED",
        "valid_evidence": [{"file": "src/main.c", "line": 6, "description": "Mutex take without release."}],
        "conflict_target_id": None,
        "conflict_evidence": [],
        "rationale": "An unsupported verdict action for the Memory Verifier schema.",
    })
    provider = QueuedResponseProvider([schema_invalid, _valid_verifier_json()])

    result = structured_response(provider, MemoryVerificationResult, MEMORY_VERIFIER_SYSTEM, "Disprove the candidate.")

    assert result.action == "VERIFY_NEW"
    assert result.source_support == "SUPPORTED"
    assert provider.calls == 2


def test_structured_response_rejects_after_failed_memory_verifier_repair() -> None:
    schema_invalid = json.dumps({
        "action": "MAKE_IT_SO",
        "confidence": 0.9,
        "source_support": "CONTRADICTED",
        "rationale": "Still an unsupported verdict action after repair.",
    })
    provider = QueuedResponseProvider(["no json here at all", schema_invalid])

    with pytest.raises(RuntimeError, match="could not be validated after one repair attempt"):
        structured_response(provider, MemoryVerificationResult, MEMORY_VERIFIER_SYSTEM, "Disprove the candidate.")

    assert provider.calls == 2


def test_structured_response_rejects_after_failed_memory_synthesizer_repair() -> None:
    schema_invalid = json.dumps({"candidates": [{"type": "BOGUS_TYPE", "statement": "too short", "confidence": 9}]})
    provider = QueuedResponseProvider([schema_invalid, schema_invalid])

    with pytest.raises(RuntimeError, match="could not be validated after one repair attempt"):
        structured_response(provider, MemorySynthesisResult, MEMORY_SYNTHESIZER_SYSTEM, "Synthesize candidates.")

    assert provider.calls == 2


def _reasoning_only_length_body(reasoning: str = "private chain of thought") -> bytes:
    return json.dumps({
        "choices": [{"finish_reason": "length", "message": {"reasoning_content": reasoning}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 3_200, "total_tokens": 3_300, "completion_tokens_details": {"reasoning_tokens": 3_200}},
    }).encode()


class FinalizationCapture:
    """Test-only urlopen double capturing payloads and returning scripted bodies."""

    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = list(bodies)
        self.payloads: list[dict] = []

    def __call__(self, request: object, **_kwargs: object) -> object:
        self.payloads.append(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
        body = self.bodies.pop(0) if self.bodies else self.bodies[-1]

        class Response:
            headers = {"Content-Type": "application/json"}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return body

        return Response()


_FINALIZATION_PROVIDER_KWARGS = {
    "structured_output_mode": "JSON_OBJECT",
    "reasoning_effort": "LOW",
    "structured_finalization_policy": "AUTO",
}


def test_deepseek_reasoning_only_length_triggers_one_finalization_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([
        _reasoning_only_length_body(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Recovered final JSON after reasoning was disabled."}'}}]}).encode(),
    ])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert result.verdict == "FIXED"
    assert len(capture.payloads) == 2
    initial, recovery = capture.payloads
    # Recovery contract is materially different: thinking off, bounded budget,
    # explicit final-JSON instruction in the system prompt.
    assert initial["reasoning_effort"] == "low"
    assert initial["thinking"] == {"type": "enabled"}
    assert recovery["reasoning_effort"] == "none"
    assert recovery["thinking"] == {"type": "disabled"}
    assert recovery["max_tokens"] == 1_600
    assert "FINALIZATION RETRY" in recovery["messages"][0]["content"]
    assert initial["messages"][1] == recovery["messages"][1]


def test_finalization_recovery_failure_is_terminal_and_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([_reasoning_only_length_body(), _reasoning_only_length_body()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)
    events: list[object] = []

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)  # type: ignore[arg-type]

    assert len(capture.payloads) == 2
    exhausted = [event for event in events if getattr(event, "error_kind", None) == "OUTPUT_FINALIZATION_EXHAUSTED"]  # type: ignore[attr-defined]
    assert len(exhausted) == 1
    assert getattr(exhausted[0], "finalization_recovery", False) is True  # type: ignore[attr-defined]
    assert all("private chain" not in repr(event) for event in events)  # type: ignore[attr-defined]


def test_finalization_recovery_is_not_attempted_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([_reasoning_only_length_body(), _reasoning_only_length_body()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert len(capture.payloads) == 2
    assert capture.bodies == []


@pytest.mark.parametrize("status_code", [401, 403, 429, 500])
def test_http_errors_do_not_trigger_finalization_recovery(monkeypatch: pytest.MonkeyPatch, status_code: int) -> None:
    def failing_urlopen(*_args: object, **_kwargs: object) -> None:
        raise HTTPError("https://example.test/v1/chat/completions", status_code, "error", {}, BytesIO(b"{}"))

    monkeypatch.setattr("app.ai_provider.urlopen", failing_urlopen)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)

    with pytest.raises(RuntimeError, match=f"HTTP {status_code}"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")


def test_timeout_does_not_trigger_finalization_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def hanging_urlopen(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("request timed out")

    monkeypatch.setattr("app.ai_provider.urlopen", hanging_urlopen)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)

    with pytest.raises(RuntimeError, match="timed out"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")


def test_invalid_json_content_does_not_use_finalization_recovery_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # Invalid JSON in an otherwise present message is an ordinary schema
    # failure: it must flow through the existing repair path, not the
    # output-limit finalization recovery. A non-DeepSeek model keeps the
    # DeepSeek-only strict schema retry out of the picture.
    capture = FinalizationCapture([
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "definitely not json"}}]}).encode(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Ordinary repair path produced valid JSON."}'}}]}).encode(),
    ])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "glm/flash", max_tokens=2_000, structured_output_mode="JSON_OBJECT", reasoning_effort="UNSPECIFIED", structured_finalization_policy="AUTO")

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert result.verdict == "FIXED"
    # Initial call + one repair call; no :finalization-retry contract.
    assert len(capture.payloads) == 2
    assert all("FINALIZATION RETRY" not in payload["messages"][0]["content"] for payload in capture.payloads)


def test_structured_mode_rejection_does_not_trigger_finalization_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_urlopen(*_args: object, **_kwargs: object) -> None:
        raise HTTPError("https://example.test/v1/chat/completions", 400, "Bad Request", {}, BytesIO(b'{"error":{"message":"response_format json_object is not supported"}}'))

    monkeypatch.setattr("app.ai_provider.urlopen", failing_urlopen)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)
    events: list[object] = []

    with pytest.raises(RuntimeError, match="structured-output mode"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)  # type: ignore[arg-type]

    assert not [event for event in events if getattr(event, "finalization_recovery", False)]  # type: ignore[attr-defined]


def test_finalization_policy_never_disables_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([_reasoning_only_length_body()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, structured_output_mode="JSON_OBJECT", reasoning_effort="LOW", structured_finalization_policy="NEVER")

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert len(capture.payloads) == 1


def test_finalization_policy_always_applies_to_non_deepseek(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([
        _reasoning_only_length_body(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Non DeepSeek provider recovery succeeded."}'}}]}).encode(),
    ])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "glm/flash", max_tokens=2_000, structured_output_mode="JSON_OBJECT", reasoning_effort="UNSPECIFIED", structured_finalization_policy="ALWAYS")

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert result.verdict == "FIXED"
    assert len(capture.payloads) == 2


def test_auto_policy_skips_non_deepseek_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([_reasoning_only_length_body()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "glm/flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert len(capture.payloads) == 1


def test_auto_policy_skips_deepseek_with_reasoning_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([_reasoning_only_length_body()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, structured_output_mode="JSON_OBJECT", reasoning_effort="NONE", structured_finalization_policy="AUTO")

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.")

    assert len(capture.payloads) == 1


def test_successful_chat_is_never_finalization_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "plain chat answer"}}]}).encode()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=2_000, **_FINALIZATION_PROVIDER_KWARGS)

    assert provider.chat("system", "question") == "plain chat answer"
    assert len(capture.payloads) == 1


def test_recovery_pair_exposes_distinct_effective_output_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([
        _reasoning_only_length_body(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Recovered final JSON after reasoning was disabled."}'}}]}).encode(),
    ])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=3_200, **_FINALIZATION_PROVIDER_KWARGS)
    events: list[object] = []

    result = structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)  # type: ignore[arg-type]

    assert result.verdict == "FIXED"
    by_operation = {}
    for event in events:  # type: ignore[attr-defined]
        by_operation.setdefault(getattr(event, "operation", ""), []).append(event)
    initial = by_operation["structured"]
    recovery = by_operation["structured:finalization-retry"]
    # Initial request carries the effective role cap on prepared.
    prepared = [event for event in initial if getattr(event, "state", None) == "REQUEST_PREPARED"]
    assert prepared
    assert all(getattr(event, "effective_max_tokens", None) == 3_200 for event in prepared)
    # Recovery carries the fixed bounded cap, never the role cap.
    recovery_prepared = [event for event in recovery if getattr(event, "state", None) == "REQUEST_PREPARED"]
    validated = [event for event in recovery if getattr(event, "state", None) == "STRUCTURED_VALIDATED"]
    assert recovery_prepared and validated
    assert all(getattr(event, "effective_max_tokens", None) == 1_600 for event in recovery_prepared + validated)


def test_scheduled_marker_separates_recovery_from_terminal_suppression(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([
        _reasoning_only_length_body(),
        json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": '{"verdict":"FIXED","notes":"Recovered final JSON after reasoning was disabled."}'}}]}).encode(),
    ])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=3_200, **_FINALIZATION_PROVIDER_KWARGS)
    events: list[object] = []

    structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)  # type: ignore[arg-type]

    scheduled = [event for event in events if getattr(event, "state", None) == "FINALIZATION_RECOVERY_SCHEDULED"]  # type: ignore[attr-defined]
    assert len(scheduled) == 1
    assert scheduled[0].finalization_recovery is False  # type: ignore[attr-defined]
    assert scheduled[0].retry_suppressed is False  # type: ignore[attr-defined]
    assert scheduled[0].effective_max_tokens == 1_600  # type: ignore[attr-defined]
    # The initial request id is reused so the timeline groups as one flow.
    assert scheduled[0].request_id == events[0].request_id  # type: ignore[attr-defined]


def test_no_scheduled_marker_when_policy_blocks_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = FinalizationCapture([_reasoning_only_length_body()])
    monkeypatch.setattr("app.ai_provider.urlopen", capture)
    provider = OpenAICompatibleProvider("secret", "https://example.test/v1/chat/completions", "deepseek-v4.1-flash", max_tokens=3_200, structured_output_mode="JSON_OBJECT", reasoning_effort="LOW", structured_finalization_policy="NEVER")
    events: list[object] = []

    with pytest.raises(RuntimeError, match="output limit reached before final"):
        structured_response(provider, FixVerificationResult, "Return JSON.", "Check source.", on_event=events.append)  # type: ignore[arg-type]

    assert not [event for event in events if getattr(event, "state", None) == "FINALIZATION_RECOVERY_SCHEDULED"]  # type: ignore[attr-defined]
