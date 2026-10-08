from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import Callable
from typing import Literal, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import BaseModel, ValidationError


logger = logging.getLogger("firmsight.ai")


ContentState = Literal["PRESENT", "EMPTY", "REASONING_ONLY", "MISSING_MESSAGE", "UNSUPPORTED_CONTENT_SHAPE"]


@dataclass(frozen=True)
class CompletionSummary:
    content_state: ContentState
    finish_reason: str | None = None
    reasoning_present: bool = False


@dataclass(frozen=True)
class ValidationSummary:
    category: str
    fields: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> tuple[str, tuple[str, ...]]:
        return self.category, self.fields


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None

    @classmethod
    def from_payload(cls, payload: object) -> "TokenUsage | None":
        if not isinstance(payload, dict):
            return None

        def integer(value: object) -> int | None:
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        prompt = integer(payload.get("prompt_tokens"))
        completion = integer(payload.get("completion_tokens"))
        total = integer(payload.get("total_tokens"))
        reasoning = integer(payload.get("reasoning_tokens"))
        completion_details = payload.get("completion_tokens_details")
        if reasoning is None and isinstance(completion_details, dict):
            reasoning = integer(completion_details.get("reasoning_tokens"))
        if prompt is None and completion is None and total is None and reasoning is None:
            return None
        return cls(prompt, completion, total, reasoning)

    def as_dict(self) -> dict[str, int]:
        return {
            key: value
            for key, value in {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "reasoning_tokens": self.reasoning_tokens,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class ProviderEvent:
    state: str
    request_id: str
    operation: str
    provider: str
    model: str
    endpoint: str
    created_at: str
    elapsed_ms: int | None = None
    http_status: int | None = None
    content_type: str | None = None
    request_chars: int | None = None
    response_chars: int | None = None
    usage: TokenUsage | None = None
    error_kind: str | None = None
    error_message: str | None = None
    attempt: int = 1
    repair_attempted: bool = False
    content_state: ContentState | None = None
    finish_reason: str | None = None
    structured_mode: str | None = None
    finalization_policy: str | None = None
    finalization_recovery: bool = False
    effective_max_tokens: int | None = None
    validation_category: str | None = None
    validation_fields: tuple[str, ...] = ()
    retry_suppressed: bool = False


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    request_id: str
    provider: str
    model: str
    endpoint: str
    elapsed_ms: int
    http_status: int | None
    content_type: str
    request_chars: int
    response_chars: int
    usage: TokenUsage | None = None
    content_state: ContentState = "PRESENT"
    finish_reason: str | None = None
    reasoning_present: bool = False


ProviderEventSink = Callable[[ProviderEvent], None]


class AIProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def chat(self, system_prompt: str, user_prompt: str) -> str: ...


@dataclass(frozen=True)
class UnavailableProvider:
    """Explicitly represents a missing server-side AI configuration."""

    name: str = "unconfigured"

    def available(self) -> bool:
        return False

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("No server AI provider is configured")


@dataclass(frozen=True)
class OpenRouterProvider:
    """OpenAI-compatible provider boundary; credentials never leave this process."""

    api_key: str | None
    endpoint: str
    model: str
    name: str = "openrouter"
    user_agent: str | None = None
    max_tokens: int | None = None
    structured_output_mode: str = "PROMPT_ONLY"
    reasoning_effort: str = "UNSPECIFIED"
    structured_finalization_policy: str = "AUTO"

    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        return self.chat_with_metadata(system_prompt, user_prompt).content

    def chat_with_metadata(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        operation: str = "chat",
        request_id: str | None = None,
        attempt: int = 1,
        on_event: ProviderEventSink | None = None,
        structured_schema: dict[str, object] | None = None,
        structured_output_mode: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens_override: int | None = None,
    ) -> ProviderResponse:
        if not self.api_key:
            raise RuntimeError("No server AI API key is configured")
        request_id = request_id or f"AI-{uuid4().hex[:12].upper()}"
        started = time.monotonic()
        request_chars = len(system_prompt) + len(user_prompt)
        safe_endpoint = _safe_endpoint(self.endpoint)

        def emit(
            state: str,
            *,
            elapsed_ms: int | None = None,
            http_status: int | None = None,
            content_type: str | None = None,
            response_chars: int | None = None,
            usage: TokenUsage | None = None,
            error_kind: str | None = None,
            error_message: str | None = None,
            content_state: ContentState | None = None,
            finish_reason: str | None = None,
            validation_category: str | None = None,
            validation_fields: tuple[str, ...] = (),
            retry_suppressed: bool = False,
            effective_max_tokens: int | None = None,
        ) -> None:
            event = ProviderEvent(
                state=state,
                request_id=request_id,
                operation=operation,
                provider=self.name,
                model=self.model,
                endpoint=safe_endpoint,
                created_at=datetime.now(UTC).isoformat(),
                elapsed_ms=elapsed_ms,
                http_status=http_status,
                content_type=content_type,
                request_chars=request_chars,
                response_chars=response_chars,
                usage=usage,
                error_kind=error_kind,
                error_message=error_message,
                attempt=attempt,
                content_state=content_state,
                finish_reason=finish_reason,
                structured_mode=output_mode if structured_call else None,
                finalization_policy=self.structured_finalization_policy,
                finalization_recovery=operation.endswith(":finalization-retry"),
                effective_max_tokens=effective_max_tokens if effective_max_tokens is not None else (configured_max_tokens if isinstance(configured_max_tokens, int) else None),
                validation_category=validation_category,
                validation_fields=validation_fields,
                retry_suppressed=retry_suppressed,
            )
            _publish_event(event, on_event)

        payload_data: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
        }
        configured_max_tokens = max_tokens_override if max_tokens_override is not None else self.max_tokens
        if configured_max_tokens:
            payload_data["max_tokens"] = configured_max_tokens
        output_mode = structured_output_mode or self.structured_output_mode
        if structured_schema is not None and output_mode == "JSON_OBJECT":
            payload_data["response_format"] = {"type": "json_object"}
        elif structured_schema is not None and output_mode == "JSON_SCHEMA":
            payload_data["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "firmsight_structured_result", "strict": True, "schema": structured_schema},
            }
        effort = reasoning_effort or self.reasoning_effort
        model_id = self.model.casefold().rsplit("/", 1)[-1]
        is_deepseek = model_id.startswith("deepseek")
        # DeepSeek enables thinking by default. For structured FirmSight calls,
        # an unspecified effort must not silently consume the whole completion
        # budget before the required JSON object is emitted. Users can still
        # explicitly select LOW/MEDIUM/HIGH when they want thinking enabled.
        if structured_schema is not None and is_deepseek and (not effort or effort == "UNSPECIFIED"):
            effort = "NONE"
        if effort and effort != "UNSPECIFIED":
            normalized_effort = effort.casefold()
            payload_data["reasoning_effort"] = normalized_effort
            # DeepSeek V4/V4.1 exposes thinking as an additional OpenAI-style
            # body field.  KiosAPI forwards this field, while silently ignoring
            # an effort hint alone can leave the model in its default thinking
            # mode and consume the entire completion budget before JSON.
            if is_deepseek:
                payload_data["thinking"] = {"type": "disabled" if normalized_effort == "none" else "enabled"}
        structured_call = structured_schema is not None
        structured_request = structured_call and output_mode != "PROMPT_ONLY"
        payload = json.dumps(payload_data).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": self.user_agent or _default_user_agent(self.name),
            },
            method="POST",
        )
        try:
            emit("REQUEST_SENT")
            emit("WAITING_FOR_PROVIDER")
            with urlopen(request, timeout=90) as response:  # nosec B310: endpoint is validated in settings
                body = response.read().decode("utf-8", errors="replace")
                status_code = _response_status(response)
                content_type = _response_content_type(response)
                emit("RESPONSE_RECEIVED", elapsed_ms=_elapsed_ms(started), http_status=status_code, content_type=content_type, response_chars=len(body))
                decoded = _decode_completion_response(body, content_type)
        except HTTPError as error:
            structured_unsupported = (
                structured_request
                and error.code in {400, 422}
                and _http_error_indicates_structured_mode_rejection(error)
            )
            error_kind = "STRUCTURED_MODE_UNSUPPORTED" if structured_unsupported else "http_error"
            if error_kind == "STRUCTURED_MODE_UNSUPPORTED":
                message = f"{self.name} does not support the configured {output_mode} structured-output mode"
            else:
                message = f"{self.name} upstream returned HTTP {error.code}"
            emit("FAILED", elapsed_ms=_elapsed_ms(started), http_status=error.code, error_kind=error_kind, error_message=message, retry_suppressed=error_kind == "STRUCTURED_MODE_UNSUPPORTED")
            raise RuntimeError(
                f"{self.name} request failed (HTTP {error.code}): {message}"
            ) from error
        except URLError as error:
            message = "provider endpoint could not be reached"
            emit("FAILED", elapsed_ms=_elapsed_ms(started), error_kind="network_error", error_message=message)
            raise RuntimeError(f"{self.name} endpoint could not be reached: {message}") from error
        except TimeoutError as error:
            message = "request timed out after 90 seconds"
            emit("FAILED", elapsed_ms=_elapsed_ms(started), error_kind="timeout", error_message=message)
            raise RuntimeError(f"{self.name} request timed out after 90 seconds") from error
        except OSError as error:
            message = "provider network request failed"
            emit("FAILED", elapsed_ms=_elapsed_ms(started), error_kind="network_error", error_message=message)
            raise RuntimeError(f"{self.name} network request failed: {message}") from error
        except json.JSONDecodeError as error:
            message = "provider response was not valid JSON or SSE"
            error_kind = "INVALID_STRUCTURED_JSON" if structured_call else "malformed_response"
            emit("FAILED", elapsed_ms=_elapsed_ms(started), error_kind=error_kind, error_message=message)
            raise RuntimeError(f"{self.name} returned an invalid JSON response") from error

        usage = _usage_from_response(decoded)
        summary = _completion_summary(decoded)
        try:
            message = decoded["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as error:
            kind = "OUTPUT_LIMIT_BEFORE_FINAL" if _is_output_limit_before_final(summary) else "PROVIDER_FORMAT_INCOMPATIBLE"
            message = {
                "OUTPUT_LIMIT_BEFORE_FINAL": f"{self.name} output limit reached before final structured response",
                "PROVIDER_FORMAT_INCOMPATIBLE": f"{self.name} provider format incompatible: no assistant message",
            }[kind]
            emit("FAILED", elapsed_ms=_elapsed_ms(started), response_chars=len(body), usage=usage, error_kind=kind, error_message=message, content_state=summary.content_state, finish_reason=summary.finish_reason, retry_suppressed=kind in {"OUTPUT_LIMIT_BEFORE_FINAL", "PROVIDER_FORMAT_INCOMPATIBLE"})
            raise RuntimeError(message) from error
        if not isinstance(message, dict):
            kind = "OUTPUT_LIMIT_BEFORE_FINAL" if _is_output_limit_before_final(summary) else "PROVIDER_FORMAT_INCOMPATIBLE"
            message = {
                "OUTPUT_LIMIT_BEFORE_FINAL": f"{self.name} output limit reached before final structured response",
                "PROVIDER_FORMAT_INCOMPATIBLE": f"{self.name} provider format incompatible: no assistant message",
            }[kind]
            emit("FAILED", elapsed_ms=_elapsed_ms(started), response_chars=len(body), usage=usage, error_kind=kind, error_message=message, content_state=summary.content_state, finish_reason=summary.finish_reason, retry_suppressed=kind in {"OUTPUT_LIMIT_BEFORE_FINAL", "PROVIDER_FORMAT_INCOMPATIBLE"})
            raise RuntimeError(message)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            kind = "OUTPUT_LIMIT_BEFORE_FINAL" if _is_output_limit_before_final(summary) else "REASONING_ONLY_COMPLETION" if summary.content_state == "REASONING_ONLY" else "PROVIDER_FORMAT_INCOMPATIBLE"
            message = {
                "OUTPUT_LIMIT_BEFORE_FINAL": f"{self.name} output limit reached before final structured response",
                "REASONING_ONLY_COMPLETION": f"{self.name} returned reasoning without final message content",
                "PROVIDER_FORMAT_INCOMPATIBLE": f"{self.name} provider format incompatible: unsupported or empty assistant content",
            }[kind]
            emit("FAILED", elapsed_ms=_elapsed_ms(started), response_chars=len(body), usage=usage, error_kind=kind, error_message=message, content_state=summary.content_state, finish_reason=summary.finish_reason, retry_suppressed=kind in {"OUTPUT_LIMIT_BEFORE_FINAL", "PROVIDER_FORMAT_INCOMPATIBLE"})
            raise RuntimeError(message)
        result = ProviderResponse(
            content=content,
            request_id=request_id,
            provider=self.name,
            model=self.model,
            endpoint=safe_endpoint,
            elapsed_ms=_elapsed_ms(started),
            http_status=status_code,
            content_type=content_type,
            request_chars=request_chars,
            response_chars=len(body),
            usage=usage,
            content_state=summary.content_state,
            finish_reason=summary.finish_reason,
            reasoning_present=summary.reasoning_present,
        )
        emit("COMPLETED", elapsed_ms=result.elapsed_ms, http_status=status_code, content_type=content_type, response_chars=len(body), usage=usage, content_state=summary.content_state, finish_reason=summary.finish_reason)
        return result


@dataclass(frozen=True)
class OpenAICompatibleProvider(OpenRouterProvider):
    name: str = "openai-compatible"


ModelT = TypeVar("ModelT", bound=BaseModel)
MAX_EMPTY_CONTENT_ATTEMPTS = 3
STRUCTURED_REPAIR_MIN_TOKENS = 2_000
# Bounded final-output budget for the one-shot structured finalization
# recovery. It is intentionally different from the normal role budget so the
# recovery request contract differs deterministically from the failed one.
FINALIZATION_RECOVERY_MAX_TOKENS = 1_600
# Conditions that make a structured request eligible for exactly one
# finalization recovery: the provider hit its output limit before emitting
# any final assistant content (finish_reason "length" with reasoning-only,
# empty, or missing message content).
FINALIZATION_RECOVERY_CONTENT_STATES = frozenset({"REASONING_ONLY", "EMPTY", "MISSING_MESSAGE"})


def _is_finalization_recovery_trigger(summary: CompletionSummary) -> bool:
    """Recognize the bounded recovery trigger without reading raw output."""
    return summary.finish_reason == "length" and summary.content_state in FINALIZATION_RECOVERY_CONTENT_STATES


def _finalization_policy_allows_recovery(provider: AIProvider) -> bool:
    policy = str(getattr(provider, "structured_finalization_policy", "AUTO") or "AUTO").upper()
    if policy == "NEVER":
        return False
    if not callable(getattr(provider, "chat_with_metadata", None)):
        return False
    if policy == "ALWAYS":
        return True
    # AUTO: only reasoning-capable OpenAI-compatible providers whose current
    # reasoning policy can consume the shared completion budget.
    if not _is_deepseek_provider(provider):
        return False
    return str(getattr(provider, "reasoning_effort", "UNSPECIFIED") or "UNSPECIFIED").upper() in {"", "UNSPECIFIED", "LOW", "MEDIUM", "HIGH"}


def _completion_summary(response: object) -> CompletionSummary:
    if not isinstance(response, dict):
        return CompletionSummary("MISSING_MESSAGE")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return CompletionSummary("MISSING_MESSAGE")
    choice = choices[0]
    finish_reason = choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None
    message = choice.get("message")
    if not isinstance(message, dict):
        return CompletionSummary("MISSING_MESSAGE", finish_reason)
    reasoning_present = bool(message.get("reasoning_content"))
    if "content" not in message:
        return CompletionSummary("REASONING_ONLY" if reasoning_present else "MISSING_MESSAGE", finish_reason, reasoning_present)
    content = message.get("content")
    if not isinstance(content, str):
        return CompletionSummary("UNSUPPORTED_CONTENT_SHAPE", finish_reason, reasoning_present)
    if content.strip():
        return CompletionSummary("PRESENT", finish_reason, reasoning_present)
    return CompletionSummary("REASONING_ONLY" if reasoning_present else "EMPTY", finish_reason, reasoning_present)


def _is_output_limit_before_final(summary: CompletionSummary) -> bool:
    """Recognize truncation before final assistant content without reading raw output."""
    return _is_finalization_recovery_trigger(summary)


def _validation_summary(error: ValidationError | ValueError | json.JSONDecodeError) -> ValidationSummary:
    if isinstance(error, json.JSONDecodeError):
        return ValidationSummary("INVALID_JSON")
    if not isinstance(error, ValidationError):
        return ValidationSummary("OTHER_SCHEMA")
    errors = error.errors()
    if not errors:
        return ValidationSummary("OTHER_SCHEMA")
    categories: list[str] = []
    fields: list[str] = []
    for item in errors[:8]:
        error_type = str(item.get("type", "")).casefold()
        if error_type == "missing":
            category = "MISSING_REQUIRED_FIELD"
        elif "enum" in error_type or error_type == "literal_error":
            category = "INVALID_ENUM"
        elif "type" in error_type or error_type.endswith("_parsing"):
            category = "TYPE_CONSTRAINT"
        elif "too_short" in error_type or "too_long" in error_type or "greater_than" in error_type or "less_than" in error_type:
            category = "VALUE_CONSTRAINT"
        else:
            category = "OTHER_SCHEMA"
        location = item.get("loc", ())
        if not isinstance(location, (list, tuple)) or not location:
            if error_type.endswith("_type") or error_type in {"model_type", "list_type", "dict_type"}:
                category = "ROOT_TYPE"
        categories.append(category)
        if isinstance(location, (list, tuple)):
            safe_parts = [str(part) if isinstance(part, int) else part if isinstance(part, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) else "<field>" for part in location if isinstance(part, (str, int))]
            path = ".".join(safe_parts)
            if path:
                fields.append(path[:160])
    priority = ("MISSING_REQUIRED_FIELD", "INVALID_ENUM", "TYPE_CONSTRAINT", "VALUE_CONSTRAINT", "ROOT_TYPE", "OTHER_SCHEMA")
    category = next((candidate for candidate in priority if candidate in categories), "OTHER_SCHEMA")
    return ValidationSummary(category, tuple(dict.fromkeys(fields))[:8])


def _validation_error_message(summary: ValidationSummary, prefix: str) -> str:
    fields = ", ".join(summary.fields)
    return f"{prefix}: {summary.category.lower().replace('_', ' ')}" + (f" ({fields})" if fields else "")


def _structured_repair_budget(provider: AIProvider) -> int:
    """Give a repair enough room to emit the full schema, without changing normal budgets."""
    configured = getattr(provider, "max_tokens", None)
    if isinstance(configured, int) and not isinstance(configured, bool):
        return max(configured, STRUCTURED_REPAIR_MIN_TOKENS)
    return STRUCTURED_REPAIR_MIN_TOKENS


def _finalization_recovery_budget(provider: AIProvider) -> int:
    """Safe bounded output budget for the finalization recovery request.

    The recovery asks for one concise JSON object with thinking disabled, so a
    smaller fixed budget than the exhausted attempt is sufficient and keeps the
    request contract visibly bounded.
    """
    return FINALIZATION_RECOVERY_MAX_TOKENS


def _is_deepseek_provider(provider: AIProvider) -> bool:
    model = str(getattr(provider, "model", "")).casefold().rsplit("/", 1)[-1]
    return model.startswith("deepseek") and callable(getattr(provider, "chat_with_metadata", None))


def structured_response(
    provider: AIProvider,
    model: type[ModelT],
    system_prompt: str,
    user_prompt: str,
    *,
    operation: str = "structured",
    on_event: ProviderEventSink | None = None,
) -> ModelT:
    """Accept JSON only, retry empty completions, repair malformed output once, then validate it."""
    if not provider.available():
        raise RuntimeError("No server AI provider or API key is configured. Configure it in Settings and restart the backend if needed.")
    raw: str | None = None
    empty_completion_error: RuntimeError | None = None
    active_request_id = ""
    provider_emits_lifecycle = callable(getattr(provider, "chat_with_metadata", None))
    for attempt in range(1, MAX_EMPTY_CONTENT_ATTEMPTS + 1):
        retry_instruction = "" if attempt == 1 else (
            "\n\nIMPORTANT: Put the complete required JSON object in message.content. "
            "Do not return an empty completion. Return only the requested JSON."
        )
        try:
            attempt_system = f"{system_prompt}{retry_instruction}"
            active_request_id = f"AI-{uuid4().hex[:12].upper()}"
            _publish_event(
                ProviderEvent(
                    state="REQUEST_PREPARED",
                    request_id=active_request_id,
                    operation=operation,
                    provider=getattr(provider, "name", "unknown"),
                    model=getattr(provider, "model", "unknown"),
                    endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                    created_at=datetime.now(UTC).isoformat(),
                    request_chars=len(attempt_system) + len(user_prompt),
                    attempt=attempt,
                    structured_mode=str(getattr(provider, "structured_output_mode", None) or None),
                    finalization_policy=str(getattr(provider, "structured_finalization_policy", None) or None),
                    finalization_recovery=False,
                    effective_max_tokens=getattr(provider, "max_tokens", None) if isinstance(getattr(provider, "max_tokens", None), int) and not isinstance(getattr(provider, "max_tokens", None), bool) else None,
                ),
                on_event,
            )
            if (chat_with_metadata := getattr(provider, "chat_with_metadata", None)) and callable(chat_with_metadata):
                response = chat_with_metadata(
                    attempt_system,
                    user_prompt,
                    operation=operation,
                    request_id=active_request_id,
                    attempt=attempt,
                    on_event=on_event,
                    structured_schema=model.model_json_schema(),
                    structured_output_mode=getattr(provider, "structured_output_mode", "PROMPT_ONLY"),
                    reasoning_effort=getattr(provider, "reasoning_effort", "UNSPECIFIED"),
                )
                raw = response.content
            else:
                raw = provider.chat(attempt_system, user_prompt)
            break
        except RuntimeError as error:
            message = str(error)
            format_marker = any(marker in message for marker in ("output limit reached before final", "returned reasoning without final", "provider format incompatible"))
            legacy_empty = "did not contain usable chat content" in message.casefold()
            if not format_marker and not legacy_empty:
                # Concrete providers already emit the terminal event with the
                # most specific category (HTTP status, timeout, malformed
                # response, and so on). A second generic wrapper event would
                # overwrite that useful metadata in the UI. Legacy test
                # doubles that only implement ``chat`` still receive a safe
                # wrapper event for observability.
                if not provider_emits_lifecycle:
                    _publish_event(
                        ProviderEvent(
                            state="FAILED",
                            request_id=active_request_id,
                            operation=operation,
                            provider=getattr(provider, "name", "unknown"),
                            model=getattr(provider, "model", "unknown"),
                            endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                            created_at=datetime.now(UTC).isoformat(),
                            error_kind=_error_kind(error),
                            error_message=_safe_detail(error),
                            attempt=attempt,
                        ),
                        on_event,
                    )
                raise
            # Some reasoning-capable OpenAI-compatible models intermittently finish
            # without content. It is safe to retry because no response has been used.
            empty_completion_error = error
            if not provider_emits_lifecycle:
                _publish_event(
                    ProviderEvent(
                        state="RETRY_SUPPRESSED" if "provider format incompatible" in message or "output limit reached before final" in message else "FAILED",
                        request_id=active_request_id,
                        operation=operation,
                        provider=getattr(provider, "name", "unknown"),
                        model=getattr(provider, "model", "unknown"),
                        endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                        created_at=datetime.now(UTC).isoformat(),
                        error_kind="PROVIDER_FORMAT_INCOMPATIBLE" if "provider format incompatible" in message else "OUTPUT_LIMIT_BEFORE_FINAL" if "output limit reached before final" in message else "REASONING_ONLY_COMPLETION",
                        error_message=message,
                        retry_suppressed="provider format incompatible" in message or "output limit reached before final" in message,
                    ),
                    on_event,
                )
            elif "returned reasoning without final" in message and attempt >= 2:
                _publish_event(
                    ProviderEvent(
                        state="FAILED",
                        request_id=active_request_id,
                        operation=operation,
                        provider=getattr(provider, "name", "unknown"),
                        model=getattr(provider, "model", "unknown"),
                        endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                        created_at=datetime.now(UTC).isoformat(),
                        error_kind="REASONING_ONLY_COMPLETION",
                        error_message="model returned reasoning without final structured response",
                        retry_suppressed=True,
                    ),
                    on_event,
                )
                raise error
            if "provider format incompatible" in message or "output limit reached before final" in message:
                # An output-limit-before-final failure is eligible for exactly
                # one materially different finalization recovery: thinking
                # forced off, one concise JSON object required, and the safe
                # bounded final-output budget. The recovery is attempted only
                # when the provider exposes the structured metadata boundary
                # and the persisted finalization policy allows it. The initial
                # request's terminal state is marked as
                # FINALIZATION_RECOVERY_SCHEDULED so diagnostics never present
                # the flow as "retry suppressed" when a changed recovery is
                # actually being sent.
                recovery_allowed = (
                    "output limit reached before final" in message
                    and _finalization_policy_allows_recovery(provider)
                )
                if recovery_allowed and provider_emits_lifecycle:
                    _publish_event(
                        ProviderEvent(
                            state="FINALIZATION_RECOVERY_SCHEDULED",
                            request_id=active_request_id,
                            operation=operation,
                            provider=getattr(provider, "name", "unknown"),
                            model=getattr(provider, "model", "unknown"),
                            endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                            created_at=datetime.now(UTC).isoformat(),
                            attempt=attempt,
                            structured_mode=str(getattr(provider, "structured_output_mode", None) or None),
                            finalization_policy=str(getattr(provider, "structured_finalization_policy", None) or None),
                            finalization_recovery=False,
                            effective_max_tokens=FINALIZATION_RECOVERY_MAX_TOKENS,
                            retry_suppressed=False,
                        ),
                        on_event,
                    )
                    recovered = _finalization_recovery(
                        provider, model, system_prompt, user_prompt,
                        operation=operation, on_event=on_event,
                    )
                    if recovered is not None:
                        return recovered
                raise error

    if raw is None:
        raise empty_completion_error or RuntimeError("AI response did not contain usable chat content")
    try:
        result = model.model_validate(_extract_json(raw))
        _publish_event(
            ProviderEvent(
                state="STRUCTURED_VALIDATED",
                request_id=active_request_id,
                operation=operation,
                provider=getattr(provider, "name", "unknown"),
                model=getattr(provider, "model", "unknown"),
                endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                created_at=datetime.now(UTC).isoformat(),
                attempt=attempt,
                effective_max_tokens=getattr(provider, "max_tokens", None) if isinstance(getattr(provider, "max_tokens", None), int) and not isinstance(getattr(provider, "max_tokens", None), bool) else None,
            ),
            on_event,
        )
        return result
    except (ValidationError, ValueError, json.JSONDecodeError) as first_error:
        first_summary = _validation_summary(first_error)
        # DeepSeek/KiosAPI can occasionally emit a nearly complete object on
        # the first structured call. Give it one strict, concise retry before
        # asking the repair prompt to reconstruct the original response. This
        # is bounded to one extra request and only applies to concrete
        # OpenAI-compatible DeepSeek providers.
        if _is_deepseek_provider(provider):
            retry_request_id = f"AI-{uuid4().hex[:12].upper()}"
            retry_system = (
                f"{system_prompt}\n\nSTRICT STRUCTURED RETRY: return exactly one valid JSON object matching the schema. "
                "Do not emit reasoning, Markdown, comments, or a second object. Keep all strings concise."
            )
            retry_user = f"{user_prompt}\n\nThe previous object failed validation ({_validation_error_message(first_summary, 'invalid fields')}). Return a corrected JSON object only."
            _publish_event(
                ProviderEvent(
                    state="REQUEST_PREPARED",
                    request_id=retry_request_id,
                    operation=f"{operation}:schema-retry",
                    provider=getattr(provider, "name", "unknown"),
                    model=getattr(provider, "model", "unknown"),
                    endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                    created_at=datetime.now(UTC).isoformat(),
                    request_chars=len(retry_system) + len(retry_user),
                    attempt=2,
                    repair_attempted=True,
                ),
                on_event,
            )
            try:
                retry_response = provider.chat_with_metadata(  # type: ignore[attr-defined]
                    retry_system,
                    retry_user,
                    operation=f"{operation}:schema-retry",
                    request_id=retry_request_id,
                    attempt=2,
                    on_event=on_event,
                    structured_schema=model.model_json_schema(),
                    structured_output_mode=getattr(provider, "structured_output_mode", "PROMPT_ONLY"),
                    reasoning_effort=getattr(provider, "reasoning_effort", "UNSPECIFIED"),
                    max_tokens_override=_structured_repair_budget(provider),
                )
                raw = retry_response.content
                result = model.model_validate(_extract_json(raw))
                _publish_event(
                    ProviderEvent(
                        state="STRUCTURED_VALIDATED",
                        request_id=retry_request_id,
                        operation=f"{operation}:schema-retry",
                        provider=getattr(provider, "name", "unknown"),
                        model=getattr(provider, "model", "unknown"),
                        endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                        created_at=datetime.now(UTC).isoformat(),
                        attempt=2,
                        repair_attempted=True,
                    ),
                    on_event,
                )
                return result
            except (RuntimeError, ValidationError, ValueError, json.JSONDecodeError) as retry_error:
                # Preserve the strict retry's response for the following
                # repair attempt when one was received; otherwise retain the
                # original response and its validation context.
                if isinstance(retry_error, RuntimeError) and raw is None:
                    raw = None
        _publish_event(
            ProviderEvent(
                state="REPAIR_STARTED",
                request_id=active_request_id,
                operation=operation,
                provider=getattr(provider, "name", "unknown"),
                model=getattr(provider, "model", "unknown"),
                endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                created_at=datetime.now(UTC).isoformat(),
                error_kind="schema_validation",
                error_message=_validation_error_message(first_summary, "structured response requires repair"),
                repair_attempted=True,
                validation_category=first_summary.category,
                validation_fields=first_summary.fields,
            ),
            on_event,
        )
        repair_system = "You repair structured FirmSight responses. Return only one valid JSON object that matches the requested schema. Do not add Markdown, explanation, reasoning, or code fences. Keep strings concise and preserve only evidence present in the invalid response."
        repair_user = (
            f"Validation issue: {_validation_error_message(first_summary, 'the previous response failed')}\n\n"
            f"Required JSON Schema:\n{json.dumps(model.model_json_schema(), ensure_ascii=False)}\n\n"
            f"Invalid response:\n{raw[:24000]}"
        )
        repair_request_id = f"AI-{uuid4().hex[:12].upper()}"
        _publish_event(
            ProviderEvent(
                state="REQUEST_PREPARED",
                request_id=repair_request_id,
                operation=f"{operation}:repair",
                provider=getattr(provider, "name", "unknown"),
                model=getattr(provider, "model", "unknown"),
                endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                created_at=datetime.now(UTC).isoformat(),
                request_chars=len(repair_system) + len(repair_user),
                repair_attempted=True,
            ),
            on_event,
        )
        if (chat_with_metadata := getattr(provider, "chat_with_metadata", None)) and callable(chat_with_metadata):
            repaired = chat_with_metadata(
                repair_system,
                repair_user,
                operation=f"{operation}:repair",
                request_id=repair_request_id,
                on_event=on_event,
                structured_schema=model.model_json_schema(),
                structured_output_mode=getattr(provider, "structured_output_mode", "PROMPT_ONLY"),
                reasoning_effort=getattr(provider, "reasoning_effort", "UNSPECIFIED"),
                max_tokens_override=_structured_repair_budget(provider),
            ).content
        else:
            repaired = provider.chat(repair_system, repair_user)
        try:
            result = model.model_validate(_extract_json(repaired))
            _publish_event(
                ProviderEvent(
                    state="STRUCTURED_VALIDATED",
                    request_id=repair_request_id,
                    operation=f"{operation}:repair",
                    provider=getattr(provider, "name", "unknown"),
                    model=getattr(provider, "model", "unknown"),
                    endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                    created_at=datetime.now(UTC).isoformat(),
                    repair_attempted=True,
                ),
                on_event,
            )
            return result
        except (ValidationError, ValueError, json.JSONDecodeError) as error:
            repair_summary = _validation_summary(error)
            same_failure = repair_summary.fingerprint == first_summary.fingerprint
            _publish_event(
                ProviderEvent(
                    state="FAILED",
                    request_id=repair_request_id,
                    operation=f"{operation}:repair",
                    provider=getattr(provider, "name", "unknown"),
                    model=getattr(provider, "model", "unknown"),
                    endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                    created_at=datetime.now(UTC).isoformat(),
                    error_kind="SCHEMA_VALIDATION_FAILED",
                    error_message=_validation_error_message(repair_summary, "AI response could not be validated after repair"),
                    repair_attempted=True,
                    validation_category=repair_summary.category,
                    validation_fields=repair_summary.fields,
                    retry_suppressed=same_failure,
                ),
                on_event,
            )
            raise RuntimeError("AI response could not be validated after one repair attempt") from error


def _finalization_recovery(
    provider: AIProvider,
    model: type[ModelT],
    system_prompt: str,
    user_prompt: str,
    *,
    operation: str,
    on_event: ProviderEventSink | None,
) -> ModelT | None:
    """Attempt one bounded, materially different structured finalization request.

    Triggered only after the provider exhausted its completion budget before any
    final assistant content (finish_reason "length" with reasoning-only, empty,
    or missing content). The recovery contract differs deterministically:

    - a new request id and a ``:finalization-retry`` operation suffix,
    - structured reasoning/thinking forced off,
    - an explicit concise-JSON-only instruction,
    - the safe bounded final-output budget instead of the exhausted role budget.

    Hidden reasoning from the failed response is never reused. Returns a
    validated result, or ``None`` when recovery is exhausted (after emitting a
    terminal ``OUTPUT_FINALIZATION_EXHAUSTED`` event with both attempts' safe
    metadata and no response content).
    """
    recovery_operation = f"{operation}:finalization-retry"
    recovery_request_id = f"AI-{uuid4().hex[:12].upper()}"
    recovery_system = (
        f"{system_prompt}\n\nFINALIZATION RETRY: The previous structured request exhausted its completion "
        "budget before producing the required JSON object. Respond with exactly one valid JSON object that "
        "matches the schema. Do not emit reasoning, Markdown, comments, or code fences. Keep all strings "
        "concise and return the complete object in message.content."
    )
    _publish_event(
        ProviderEvent(
            state="REQUEST_PREPARED",
            request_id=recovery_request_id,
            operation=recovery_operation,
            provider=getattr(provider, "name", "unknown"),
            model=getattr(provider, "model", "unknown"),
            endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
            created_at=datetime.now(UTC).isoformat(),
            request_chars=len(recovery_system) + len(user_prompt),
            attempt=2,
            repair_attempted=True,
            structured_mode=str(getattr(provider, "structured_output_mode", None) or None),
            finalization_policy=str(getattr(provider, "structured_finalization_policy", None) or None),
            finalization_recovery=True,
            effective_max_tokens=FINALIZATION_RECOVERY_MAX_TOKENS,
        ),
        on_event,
    )
    try:
        recovery_response = provider.chat_with_metadata(  # type: ignore[attr-defined]
            recovery_system,
            user_prompt,
            operation=recovery_operation,
            request_id=recovery_request_id,
            attempt=2,
            on_event=on_event,
            structured_schema=model.model_json_schema(),
            structured_output_mode=getattr(provider, "structured_output_mode", "PROMPT_ONLY"),
            reasoning_effort="NONE",
            max_tokens_override=_finalization_recovery_budget(provider),
        )
        result = model.model_validate(_extract_json(recovery_response.content))
    except (RuntimeError, ValidationError, ValueError, json.JSONDecodeError) as recovery_error:
        summary = _validation_summary(recovery_error) if isinstance(recovery_error, (ValidationError, ValueError, json.JSONDecodeError)) else None
        _publish_event(
            ProviderEvent(
                state="FAILED",
                request_id=recovery_request_id,
                operation=recovery_operation,
                provider=getattr(provider, "name", "unknown"),
                model=getattr(provider, "model", "unknown"),
                endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
                created_at=datetime.now(UTC).isoformat(),
                error_kind="OUTPUT_FINALIZATION_EXHAUSTED",
                error_message=(
                    "Initial structured request exhausted its output budget on reasoning and the one bounded "
                    "finalization retry did not produce a validated JSON response"
                    if summary is None
                    else "Initial structured request exhausted its output budget on reasoning and the one bounded "
                    "finalization retry failed schema validation"
                ),
                attempt=2,
                repair_attempted=True,
                finalization_recovery=True,
                effective_max_tokens=FINALIZATION_RECOVERY_MAX_TOKENS,
                validation_category=summary.category if summary else None,
                validation_fields=summary.fields if summary else (),
            ),
            on_event,
        )
        return None
    _publish_event(
        ProviderEvent(
            state="STRUCTURED_VALIDATED",
            request_id=recovery_request_id,
            operation=recovery_operation,
            provider=getattr(provider, "name", "unknown"),
            model=getattr(provider, "model", "unknown"),
            endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
            created_at=datetime.now(UTC).isoformat(),
            attempt=2,
            repair_attempted=True,
            structured_mode=str(getattr(provider, "structured_output_mode", None) or None),
            finalization_policy=str(getattr(provider, "structured_finalization_policy", None) or None),
            finalization_recovery=True,
            effective_max_tokens=FINALIZATION_RECOVERY_MAX_TOKENS,
        ),
        on_event,
    )
    return result


def chat_response(
    provider: AIProvider,
    system_prompt: str,
    user_prompt: str,
    *,
    operation: str = "chat",
    on_event: ProviderEventSink | None = None,
) -> ProviderResponse:
    """Run a provider request while keeping legacy test doubles compatible."""
    request_id = f"AI-{uuid4().hex[:12].upper()}"
    _publish_event(
        ProviderEvent(
            state="REQUEST_PREPARED",
            request_id=request_id,
            operation=operation,
            provider=getattr(provider, "name", "unknown"),
            model=getattr(provider, "model", "unknown"),
            endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
            created_at=datetime.now(UTC).isoformat(),
            request_chars=len(system_prompt) + len(user_prompt),
        ),
        on_event,
    )
    chat_with_metadata = getattr(provider, "chat_with_metadata", None)
    if callable(chat_with_metadata):
        return chat_with_metadata(system_prompt, user_prompt, operation=operation, request_id=request_id, attempt=1, on_event=on_event)
    started = time.monotonic()
    content = provider.chat(system_prompt, user_prompt)
    result = ProviderResponse(
        content=content,
        request_id=request_id,
        provider=getattr(provider, "name", "unknown"),
        model=getattr(provider, "model", "unknown"),
        endpoint=_safe_endpoint(getattr(provider, "endpoint", "")),
        elapsed_ms=_elapsed_ms(started),
        http_status=None,
        content_type="",
        request_chars=len(system_prompt) + len(user_prompt),
        response_chars=len(content),
    )
    _publish_event(
        ProviderEvent(
            state="COMPLETED",
            request_id=request_id,
            operation=operation,
            provider=result.provider,
            model=result.model,
            endpoint=result.endpoint,
            created_at=datetime.now(UTC).isoformat(),
            elapsed_ms=result.elapsed_ms,
            request_chars=result.request_chars,
            response_chars=result.response_chars,
        ),
        on_event,
    )
    return result


def _extract_json(raw: str, *, _depth: int = 0) -> object:
    if _depth > 1:
        raise json.JSONDecodeError("nested JSON string is too deep", raw, 0)
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    text = text.strip()
    try:
        value = json.loads(text)
        return _extract_json(value, _depth=_depth + 1) if isinstance(value, str) else value
    except json.JSONDecodeError:
        # Gateways occasionally preserve a trailing comma from a model's JSON
        # draft. This is a narrow normalization; arbitrary prose or Python
        # literals are still rejected and never reach schema validation.
        normalized = re.sub(r",\s*([}\]])", r"\1", text)
        if normalized != text:
            try:
                return json.loads(normalized)
            except json.JSONDecodeError:
                pass
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
                return _extract_json(value, _depth=_depth + 1) if isinstance(value, str) else value
            except json.JSONDecodeError:
                continue
        raise


def _response_content_type(response: object) -> str:
    """Read a response content type without requiring test doubles to expose headers."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        get_content_type = getattr(headers, "get_content_type", None)
        if callable(get_content_type):
            return str(get_content_type())
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter("Content-Type", ""))
    except (AttributeError, TypeError):
        return ""
    return ""


def _decode_completion_response(body: str, content_type: str = "") -> object:
    """Decode ordinary JSON and OpenAI-compatible buffered SSE responses.

    Some gateways (including 9router) return a completion frame followed by
    ``data: [DONE]`` even when the caller did not request streaming.  We accept
    both that form and standard delta frames while only using assistant message
    content; reasoning fields are never substituted for a response.
    """
    text = body.strip()
    if not text:
        raise json.JSONDecodeError("empty response", body, 0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as ordinary_error:
        if "text/event-stream" not in content_type.casefold() and "data:" not in text:
            raise ordinary_error

    # Be tolerant of a gateway concatenating a JSON completion and its DONE
    # marker without a line break.
    text = re.sub(r"}(?=data:\s*)", "}\n", text)
    frames: list[dict[str, object]] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line.startswith((":", "event:", "id:", "retry:")):
            continue
        payload = line[5:].strip() if line.startswith("data:") else line
        if not payload or payload == "[DONE]":
            continue
        try:
            frame = json.loads(payload)
        except json.JSONDecodeError as error:
            raise json.JSONDecodeError("invalid SSE completion frame", body, 0) from error
        if isinstance(frame, dict):
            frames.append(frame)
    if not frames:
        raise json.JSONDecodeError("no completion frames", body, 0)

    # Preserve a complete completion frame when present. Otherwise combine
    # delta.content fragments into the shape consumed by the normal extractor.
    for frame in frames:
        content = _frame_message_content(frame)
        if content is not None:
            usage = next((_frame_usage(item) for item in frames if _frame_usage(item) is not None), None)
            if usage is not None and "usage" not in frame:
                return {**frame, "usage": usage}
            return frame
    deltas = [part for frame in frames if (part := _frame_delta_content(frame)) is not None]
    if deltas:
        usage = next((_frame_usage(item) for item in frames if _frame_usage(item) is not None), None)
        result: dict[str, object] = {"choices": [{"message": {"role": "assistant", "content": "".join(deltas)}}]}
        finish_reason = next((value for item in frames if (value := _frame_finish_reason(item)) is not None), None)
        if finish_reason is not None:
            result["choices"][0]["finish_reason"] = finish_reason
        if usage is not None:
            result["usage"] = usage
        return result
    reasoning = next((value for item in frames if (value := _frame_reasoning_content(item)) is not None), None)
    if reasoning is not None:
        finish_reason = next((value for item in frames if (value := _frame_finish_reason(item)) is not None), None)
        choice: dict[str, object] = {"message": {"role": "assistant", "reasoning_content": reasoning}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return {"choices": [choice]}
    return frames[0]


def _frame_message_content(frame: dict[str, object]) -> str | None:
    try:
        choices = frame.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return content if isinstance(content, str) else None
    except (AttributeError, IndexError, TypeError):
        return None


def _frame_delta_content(frame: dict[str, object]) -> str | None:
    try:
        choices = frame.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        delta = first.get("delta") if isinstance(first, dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        return content if isinstance(content, str) else None
    except (AttributeError, IndexError, TypeError):
        return None


def _frame_finish_reason(frame: dict[str, object]) -> str | None:
    try:
        choices = frame.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        value = first.get("finish_reason") if isinstance(first, dict) else None
        return value if isinstance(value, str) else None
    except (AttributeError, IndexError, TypeError):
        return None


def _frame_reasoning_content(frame: dict[str, object]) -> str | None:
    try:
        choices = frame.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        delta = first.get("delta") if isinstance(first, dict) else None
        message = first.get("message") if isinstance(first, dict) else None
        for candidate in (delta, message):
            value = candidate.get("reasoning_content") if isinstance(candidate, dict) else None
            if isinstance(value, str) and value:
                return value
    except (AttributeError, IndexError, TypeError):
        return None
    return None


def _frame_usage(frame: dict[str, object]) -> dict[str, object] | None:
    usage = frame.get("usage")
    return usage if isinstance(usage, dict) else None


def _usage_from_response(response: object) -> TokenUsage | None:
    return TokenUsage.from_payload(response.get("usage")) if isinstance(response, dict) else None


def _response_status(response: object) -> int | None:
    getter = getattr(response, "getcode", None)
    try:
        status = getter() if callable(getter) else getattr(response, "status", None)
    except (AttributeError, OSError):
        return None
    return status if isinstance(status, int) else None


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _safe_endpoint(endpoint: object) -> str:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(endpoint))
        return f"{parsed.scheme}://{parsed.hostname or parsed.netloc}" if parsed.scheme and (parsed.hostname or parsed.netloc) else "configured-endpoint"
    except ValueError:
        return "configured-endpoint"


def _error_kind(error: RuntimeError) -> str:
    message = str(error).casefold()
    if "timed out" in message:
        return "timeout"
    if "invalid json" in message:
        return "malformed_response"
    if "validated" in message:
        return "schema_validation"
    if "usable chat content" in message:
        return "empty_content"
    return "provider_error"


def _publish_event(event: ProviderEvent, sink: ProviderEventSink | None) -> None:
    fields = {
        "request_id": event.request_id,
        "operation": event.operation,
        "provider": event.provider,
        "model": event.model,
        "endpoint": event.endpoint,
        "state": event.state,
        "elapsed_ms": event.elapsed_ms,
        "http_status": event.http_status,
        "request_chars": event.request_chars,
        "response_chars": event.response_chars,
        "error_kind": event.error_kind,
        "content_state": event.content_state,
        "finish_reason": event.finish_reason,
        "validation_category": event.validation_category,
        "validation_fields": ",".join(event.validation_fields),
        "retry_suppressed": event.retry_suppressed,
        "attempt": event.attempt,
        "repair_attempted": event.repair_attempted,
    }
    if event.usage:
        fields.update({f"usage_{key}": value for key, value in event.usage.as_dict().items()})
    logger.info("ai_provider_event", extra={key: value for key, value in fields.items() if value is not None})
    if sink is not None:
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - telemetry must never break an AI call
            logger.exception("ai_provider_event_sink_failed", extra={"request_id": event.request_id, "operation": event.operation})


def _http_error_indicates_structured_mode_rejection(error: HTTPError) -> bool:
    """Inspect an HTTP body only in memory to classify documented JSON-mode rejection.

    The body is deliberately never returned, logged, or persisted.  404/405 are
    never passed here because they identify endpoint/method failures, not an
    unsupported response format.
    """
    try:
        payload = json.loads(error.read(8_192).decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False

    def rejected(value: object) -> bool:
        if isinstance(value, dict):
            for key in ("code", "type", "param"):
                token = value.get(key)
                if isinstance(token, str) and token.casefold() in {
                    "response_format_not_supported",
                    "unsupported_response_format",
                    "invalid_response_format",
                    "json_mode_not_supported",
                    "structured_output_not_supported",
                }:
                    return True
            return any(rejected(item) for item in value.values())
        if isinstance(value, list):
            return any(rejected(item) for item in value)
        if isinstance(value, str):
            text = value.casefold()
            return (
                ("response_format" in text and any(term in text for term in ("unsupported", "not support", "invalid")))
                or ("structured output" in text and any(term in text for term in ("unsupported", "not support", "invalid")))
                or ("json mode" in text and any(term in text for term in ("unsupported", "not support", "invalid")))
            )
        return False

    return rejected(payload)


def _http_error_detail(error: HTTPError) -> str:
    """Return a fixed, value-free HTTP detail for compatibility callers."""
    return f"upstream returned HTTP {error.code}"


def _safe_detail(value: object) -> str:
    """Return a fixed detail; arbitrary exception values are untrusted input."""
    del value
    return "provider request failed"


def _default_user_agent(provider_name: str) -> str:
    """Use the transport identity accepted by gateways that block urllib clients."""
    # FreGateway currently rejects Python's default ``Python-urllib`` user agent
    # with HTTP 403 but accepts the same OpenAI-compatible request from curl.
    if provider_name.casefold() == "fregateway":
        return "curl/8.14.1"
    return "FirmSight/0.1"


def configured_provider() -> AIProvider:
    provider_name = os.getenv("FIRMSIGHT_AI_PROVIDER") or ("openrouter" if os.getenv("OPENROUTER_API_KEY") else "unconfigured")
    if provider_name == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY") or os.getenv("FIRMSIGHT_AI_API_KEY")
        default_endpoint = "https://openrouter.ai/api/v1/chat/completions"
    elif provider_name == "9router":
        key = os.getenv("FIRMSIGHT_9ROUTER_API_KEY") or os.getenv("FIRMSIGHT_AI_API_KEY")
        default_endpoint = "http://127.0.0.1:20128/v1/chat/completions"
    else:
        key = os.getenv("FIRMSIGHT_AI_API_KEY")
        default_endpoint = "https://api.openai.com/v1/chat/completions"
    if not key:
        return UnavailableProvider()
    endpoint = os.getenv("FIRMSIGHT_AI_ENDPOINT", default_endpoint)
    model = os.getenv("FIRMSIGHT_CHAT_MODEL", "z-ai/glm-5.3-flash")
    if provider_name == "openrouter":
        return OpenRouterProvider(api_key=key, endpoint=endpoint, model=model)
    return OpenAICompatibleProvider(api_key=key, endpoint=endpoint, model=model, name=provider_name)
