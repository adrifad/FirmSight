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
