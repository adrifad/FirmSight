from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from fastapi import HTTPException, status

from .ai_provider import AIProvider, OpenAICompatibleProvider, OpenRouterProvider, UnavailableProvider
from .i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, normalize_locale
from .i18n import message as _msg
from .platform_repository import PlatformRepository
from .platform_schemas import AISettingsRead, AISettingsUpdate, ReasoningEffort, StructuredFinalizationPolicy, StructuredOutputMode
from .knowledge_schemas import VaultSettingsRead, VaultStatus, VaultSettingsUpdate


ROLE_DEFAULTS = {
    "investigator": "z-ai/glm-5.3-flash",
    "verifier": "z-ai/glm-5.3-flash",
    "chat": "z-ai/glm-5.3-flash",
    "yaml_generator": "z-ai/glm-5.3-flash",
    "memory_synthesizer": "z-ai/glm-5.3-flash",
    "memory_verifier": "z-ai/glm-5.3-flash",
}

ROLE_MAX_TOKENS = {
    "investigator": 1_200,
    "verifier": 700,
    "chat": 1_200,
    "yaml_generator": 3_000,
    "memory_synthesizer": 1_400,
    "memory_verifier": 900,
}

INVESTIGATOR_MAX_TOKEN_OPTIONS = (1_200, 1_600, 2_000, 2_400, 3_200)
VERIFIER_MAX_TOKEN_OPTIONS = (800, 1_200, 1_600, 2_000)
PROVIDER_DEFAULT = "PROVIDER_DEFAULT"
DEFAULT_INVESTIGATOR_MAX_TOKENS = 2_000
DEFAULT_VERIFIER_MAX_TOKENS = 1_200
FIX_VERIFIER_ROLE = "verifier_fix"

# Safe bounded output budget used only by the one-shot structured
# finalization recovery request. It differs from the normal role budget so
# the recovery contract is materially smaller than the exhausted attempt.
FINALIZATION_RECOVERY_MAX_TOKENS = 1_600
REVIEW_PARALLEL_REQUEST_OPTIONS = (1, 2, 3)
DEFAULT_REVIEW_PARALLEL_REQUESTS = 2

REVIEW_CONTEXT_CHAR_OPTIONS = (12_000, 18_000, 24_000, 32_000, 42_000)
DEFAULT_REVIEW_CONTEXT_CHARS = 42_000

DEFAULT_STRUCTURED_FINALIZATION_POLICY = StructuredFinalizationPolicy.AUTO

# Bounded final-output budget for the one-shot structured finalization
# recovery request. It deliberately differs from the normal role budget so
# the recovery contract is materially smaller than the exhausted attempt.
FINALIZATION_RECOVERY_MAX_TOKENS = 1_600
DEFAULT_STRUCTURED_FINALIZATION_POLICY = StructuredFinalizationPolicy.AUTO


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    endpoint: str
    models: dict[str, str]
    review_context_chars: int
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.PROMPT_ONLY
    reasoning_effort: ReasoningEffort = ReasoningEffort.UNSPECIFIED
    structured_finalization_policy: StructuredFinalizationPolicy = DEFAULT_STRUCTURED_FINALIZATION_POLICY
    investigator_max_tokens: int | str = DEFAULT_INVESTIGATOR_MAX_TOKENS
    verifier_max_tokens: int | str = DEFAULT_VERIFIER_MAX_TOKENS
    review_parallel_requests: int = DEFAULT_REVIEW_PARALLEL_REQUESTS
    language: str = DEFAULT_LOCALE


class SettingsService:
    def __init__(self, repository: PlatformRepository) -> None:
        self.repository = repository

    def _error(self, status_code: int, key: str, /, **params: object) -> HTTPException:
        # Settings errors render in English: the language setting may itself be
        # invalid at this point (e.g. the request that sets it is being validated).
        return HTTPException(status_code, _msg(key, "en", **params))

    # ------------------------------------------------------------------
    # Server-side Obsidian vault configuration
    # ------------------------------------------------------------------

    @staticmethod
    def _allowed_vault_roots() -> list[Path]:
        raw = os.getenv("FIRMSIGHT_OBSIDIAN_ALLOWED_ROOTS", "")
        roots: list[Path] = []
        for value in raw.split(os.pathsep):
            if not value.strip():
                continue
            try:
                roots.append(Path(value).expanduser().resolve(strict=False))
            except (OSError, RuntimeError):
                continue
        return roots

    @classmethod
    def _validate_vault_path(cls, value: str | None, *, create: bool) -> tuple[Path | None, VaultStatus, str]:
        if value is None or not value.strip():
            return None, VaultStatus.NOT_CONFIGURED, "No Obsidian vault root is configured."
        if "\x00" in value:
            return None, VaultStatus.INVALID, "Vault root cannot contain NUL bytes."
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None, VaultStatus.INVALID, "Vault root must be an absolute server-side path."
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None, VaultStatus.INVALID, "Vault root could not be resolved."
        for allowed in cls._allowed_vault_roots():
            if not (resolved == allowed or allowed in resolved.parents):
                continue
            break
        else:
            if cls._allowed_vault_roots():
                return None, VaultStatus.INVALID, "Vault root is outside FIRMSIGHT_OBSIDIAN_ALLOWED_ROOTS."
        parent = resolved.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK):
            return resolved, VaultStatus.NOT_WRITABLE, "Vault root parent is not writable."
        if create:
            try:
                resolved.mkdir(parents=False, exist_ok=True)
            except OSError:
                return resolved, VaultStatus.NOT_WRITABLE, "Vault root could not be created or is not writable."
        if not resolved.is_dir() or not os.access(resolved, os.W_OK):
            return resolved, VaultStatus.NOT_WRITABLE, "Vault root is not writable."
        return resolved, VaultStatus.READY, "Vault root is ready for portable Markdown projections."

    def vault_status(self) -> VaultSettingsRead:
        stored = self.repository.get_setting("obsidian_vault_root")
        configured = (stored or {}).get("root") or os.getenv("FIRMSIGHT_OBSIDIAN_VAULT_ROOT")
        root, state, message = self._validate_vault_path(configured, create=False)
        return VaultSettingsRead(root=str(root) if root else configured, status=state, message=message, allowed_roots=[str(item) for item in self._allowed_vault_roots()], configured=bool(configured))

    def update_vault(self, update: VaultSettingsUpdate) -> VaultSettingsRead:
        if update.root is None or not update.root.strip():
            self.repository.set_setting("obsidian_vault_root", {"root": None}, datetime.now(UTC).isoformat())
            return self.vault_status()
        root, state, message = self._validate_vault_path(update.root, create=True)
        if state is not VaultStatus.READY or root is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)
        self.repository.set_setting("obsidian_vault_root", {"root": str(root)}, datetime.now(UTC).isoformat())
        return self.vault_status()

    @staticmethod
    def _environment_defaults() -> ProviderConfig:
        provider = os.getenv("FIRMSIGHT_AI_PROVIDER") or ("openrouter" if os.getenv("OPENROUTER_API_KEY") else "unconfigured")
        if provider == "openrouter":
            default_endpoint = "https://openrouter.ai/api/v1/chat/completions"
        elif provider == "9router":
            default_endpoint = "http://127.0.0.1:20128/v1/chat/completions"
        else:
            default_endpoint = "https://api.openai.com/v1/chat/completions"
        models = {role: os.getenv(f"FIRMSIGHT_MODEL_{role.upper()}", os.getenv(f"FIRMSIGHT_{role.upper()}_MODEL", default)) for role, default in ROLE_DEFAULTS.items()}
        try:
            structured_output_mode = StructuredOutputMode(os.getenv("FIRMSIGHT_AI_STRUCTURED_OUTPUT_MODE", StructuredOutputMode.PROMPT_ONLY))
        except (TypeError, ValueError):
            structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        try:
            reasoning_effort = ReasoningEffort(os.getenv("FIRMSIGHT_AI_REASONING_EFFORT", ReasoningEffort.UNSPECIFIED))
        except (TypeError, ValueError):
            reasoning_effort = ReasoningEffort.UNSPECIFIED
        try:
            structured_finalization_policy = StructuredFinalizationPolicy(os.getenv("FIRMSIGHT_AI_STRUCTURED_FINALIZATION_POLICY", DEFAULT_STRUCTURED_FINALIZATION_POLICY))
        except (TypeError, ValueError):
            structured_finalization_policy = DEFAULT_STRUCTURED_FINALIZATION_POLICY
        investigator_max_tokens = SettingsService._env_token_budget("FIRMSIGHT_AI_INVESTIGATOR_MAX_TOKENS", DEFAULT_INVESTIGATOR_MAX_TOKENS, INVESTIGATOR_MAX_TOKEN_OPTIONS)
        verifier_max_tokens = SettingsService._env_token_budget("FIRMSIGHT_AI_VERIFIER_MAX_TOKENS", DEFAULT_VERIFIER_MAX_TOKENS, VERIFIER_MAX_TOKEN_OPTIONS)
        try:
            parallel = int(os.getenv("FIRMSIGHT_REVIEW_PARALLEL_REQUESTS", str(DEFAULT_REVIEW_PARALLEL_REQUESTS)))
        except ValueError:
            parallel = DEFAULT_REVIEW_PARALLEL_REQUESTS
        if parallel not in REVIEW_PARALLEL_REQUEST_OPTIONS:
            parallel = DEFAULT_REVIEW_PARALLEL_REQUESTS
        return ProviderConfig(provider=provider, endpoint=os.getenv("FIRMSIGHT_AI_ENDPOINT", default_endpoint), models=models, review_context_chars=DEFAULT_REVIEW_CONTEXT_CHARS, structured_output_mode=structured_output_mode, reasoning_effort=reasoning_effort, structured_finalization_policy=structured_finalization_policy, investigator_max_tokens=investigator_max_tokens, verifier_max_tokens=verifier_max_tokens, review_parallel_requests=parallel, language=normalize_locale(os.getenv("FIRMSIGHT_UI_LANGUAGE", DEFAULT_LOCALE)))

    def config(self) -> ProviderConfig:
        stored = self.repository.get_setting("ai_provider")
        # Older development builds persisted ``local-demo``.  It is no longer a
        # valid runtime provider: use the real environment configuration instead.
        if not stored or stored["provider"] == "local-demo":
            return self._environment_defaults()
        # Settings stored before a role existed (e.g. memory_synthesizer) keep
        # working: missing roles fall back to their documented defaults.
        models = {**{role: os.getenv(f"FIRMSIGHT_MODEL_{role.upper()}", default) for role, default in ROLE_DEFAULTS.items()}, **stored["models"]}
        stored_context_chars = stored.get("review_context_chars", DEFAULT_REVIEW_CONTEXT_CHARS)
        if stored_context_chars not in REVIEW_CONTEXT_CHAR_OPTIONS:
            stored_context_chars = DEFAULT_REVIEW_CONTEXT_CHARS
        try:
            structured_output_mode = StructuredOutputMode(stored.get("structured_output_mode", StructuredOutputMode.PROMPT_ONLY))
        except (TypeError, ValueError):
            structured_output_mode = StructuredOutputMode.PROMPT_ONLY
        try:
            reasoning_effort = ReasoningEffort(stored.get("reasoning_effort", ReasoningEffort.UNSPECIFIED))
        except (TypeError, ValueError):
            reasoning_effort = ReasoningEffort.UNSPECIFIED
        try:
            structured_finalization_policy = StructuredFinalizationPolicy(stored.get("structured_finalization_policy", DEFAULT_STRUCTURED_FINALIZATION_POLICY))
        except (TypeError, ValueError):
            structured_finalization_policy = DEFAULT_STRUCTURED_FINALIZATION_POLICY
        # A persisted provider record is authoritative.  Older records that do
        # not contain the role budgets migrate to the fixed review defaults;
        # the legacy global environment value is only a fresh-database fallback.
        investigator_max_tokens = stored.get("investigator_max_tokens", DEFAULT_INVESTIGATOR_MAX_TOKENS)
        verifier_max_tokens = stored.get("verifier_max_tokens", DEFAULT_VERIFIER_MAX_TOKENS)
        if investigator_max_tokens not in (*INVESTIGATOR_MAX_TOKEN_OPTIONS, PROVIDER_DEFAULT):
            investigator_max_tokens = DEFAULT_INVESTIGATOR_MAX_TOKENS
        if verifier_max_tokens not in (*VERIFIER_MAX_TOKEN_OPTIONS, PROVIDER_DEFAULT):
            verifier_max_tokens = DEFAULT_VERIFIER_MAX_TOKENS
        parallel = stored.get("review_parallel_requests", DEFAULT_REVIEW_PARALLEL_REQUESTS)
        if parallel not in REVIEW_PARALLEL_REQUEST_OPTIONS:
            parallel = DEFAULT_REVIEW_PARALLEL_REQUESTS
        return ProviderConfig(provider=stored["provider"], endpoint=stored["endpoint"], models=models, review_context_chars=stored_context_chars, structured_output_mode=structured_output_mode, reasoning_effort=reasoning_effort, structured_finalization_policy=structured_finalization_policy, investigator_max_tokens=investigator_max_tokens, verifier_max_tokens=verifier_max_tokens, review_parallel_requests=parallel, language=normalize_locale(stored.get("language")))

    @staticmethod
    def _key_for(provider: str) -> tuple[str | None, str]:
        if provider == "openrouter":
            return os.getenv("OPENROUTER_API_KEY") or os.getenv("FIRMSIGHT_AI_API_KEY"), "OPENROUTER_API_KEY (or FIRMSIGHT_AI_API_KEY)"
        if provider == "9router":
            return os.getenv("FIRMSIGHT_9ROUTER_API_KEY") or os.getenv("FIRMSIGHT_AI_API_KEY"), "FIRMSIGHT_9ROUTER_API_KEY (or FIRMSIGHT_AI_API_KEY)"
        return os.getenv("FIRMSIGHT_AI_API_KEY"), "FIRMSIGHT_AI_API_KEY"

    def read(self) -> AISettingsRead:
        config = self.config(); api_key, environment = self._key_for(config.provider)
        return AISettingsRead(provider=config.provider, endpoint=config.endpoint, api_key_configured=bool(api_key), api_key_masked=f"••••{api_key[-4:]}" if api_key else None, api_key_environment=environment, models=config.models, language=config.language, review_context_chars=config.review_context_chars, structured_output_mode=config.structured_output_mode, reasoning_effort=config.reasoning_effort, structured_finalization_policy=config.structured_finalization_policy, investigator_max_tokens=config.investigator_max_tokens, verifier_max_tokens=config.verifier_max_tokens, review_parallel_requests=config.review_parallel_requests)

    def update(self, update: AISettingsUpdate) -> AISettingsRead:
        parsed = urlparse(update.endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.endpoint_invalid")
        # Validate the optional language first so an invalid value cannot
        # partially persist other fields.
        if update.language is not None:
            normalized = update.language.strip().casefold()
            if normalized not in SUPPORTED_LOCALES:
                raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.language_invalid")
        # Backward compatibility: clients saved before the memory roles existed
        # may submit only the original four roles. Unknown roles are rejected;
        # missing roles keep their current (or default) model instead of being
        # silently emptied.
        submitted = dict(update.models)
        unknown_roles = set(submitted) - set(ROLE_DEFAULTS)
        if unknown_roles or any(not model.strip() for model in submitted.values()):
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.models_invalid")
        current = self.config().models
        current_mode = self.config().structured_output_mode
        current_reasoning = self.config().reasoning_effort
        current_finalization_policy = self.config().structured_finalization_policy
        current_investigator_budget = self.config().investigator_max_tokens
        current_verifier_budget = self.config().verifier_max_tokens
        review_context_chars = update.review_context_chars if update.review_context_chars is not None else self.config().review_context_chars
        if review_context_chars not in REVIEW_CONTEXT_CHAR_OPTIONS:
            allowed = ", ".join(f"{value:,}" for value in REVIEW_CONTEXT_CHAR_OPTIONS)
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.review_context_invalid", allowed=allowed)
        investigator_max_tokens = update.investigator_max_tokens if update.investigator_max_tokens is not None else current_investigator_budget
        verifier_max_tokens = update.verifier_max_tokens if update.verifier_max_tokens is not None else current_verifier_budget
        review_parallel_requests = update.review_parallel_requests if update.review_parallel_requests is not None else self.config().review_parallel_requests
        if review_parallel_requests not in REVIEW_PARALLEL_REQUEST_OPTIONS:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.parallel_invalid")
        language = normalize_locale(update.language) if update.language is not None else self.config().language
        if investigator_max_tokens not in (*INVESTIGATOR_MAX_TOKEN_OPTIONS, PROVIDER_DEFAULT):
            allowed = ", ".join(str(value) for value in INVESTIGATOR_MAX_TOKEN_OPTIONS)
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.investigator_budget_invalid", allowed=allowed, default=PROVIDER_DEFAULT)
        if verifier_max_tokens not in (*VERIFIER_MAX_TOKEN_OPTIONS, PROVIDER_DEFAULT):
            allowed = ", ".join(str(value) for value in VERIFIER_MAX_TOKEN_OPTIONS)
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.verifier_budget_invalid", allowed=allowed, default=PROVIDER_DEFAULT)
        merged = {role: submitted.get(role) or current.get(role) or default for role, default in ROLE_DEFAULTS.items()}
        if any(not model.strip() for model in merged.values()):
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.models_incomplete")
        self.repository.set_setting("ai_provider", {"provider": update.provider, "endpoint": update.endpoint, "models": merged, "language": language, "review_context_chars": review_context_chars, "structured_output_mode": (update.structured_output_mode or current_mode).value, "reasoning_effort": (update.reasoning_effort or current_reasoning).value, "structured_finalization_policy": (update.structured_finalization_policy or current_finalization_policy).value, "investigator_max_tokens": investigator_max_tokens, "verifier_max_tokens": verifier_max_tokens, "review_parallel_requests": review_parallel_requests}, datetime.now(UTC).isoformat())
        return self.read()

    def provider(self, role: str = "chat") -> AIProvider:
        config = self.config(); api_key, _ = self._key_for(config.provider)
        if not api_key:
            return UnavailableProvider()
        model_role = "verifier" if role == FIX_VERIFIER_ROLE else role
        model = config.models.get(model_role)
        if not model:
            return UnavailableProvider()
        max_tokens = self._max_tokens(role, config)
        finalization_policy = config.structured_finalization_policy.value
        if config.provider == "openrouter":
            return OpenRouterProvider(api_key=api_key, endpoint=config.endpoint, model=model, user_agent=os.getenv("FIRMSIGHT_AI_USER_AGENT") or None, max_tokens=max_tokens, structured_output_mode=config.structured_output_mode.value, reasoning_effort=config.reasoning_effort.value, structured_finalization_policy=finalization_policy)
        return OpenAICompatibleProvider(api_key=api_key, endpoint=config.endpoint, model=model, name=config.provider, user_agent=os.getenv("FIRMSIGHT_AI_USER_AGENT") or None, max_tokens=max_tokens, structured_output_mode=config.structured_output_mode.value, reasoning_effort=config.reasoning_effort.value, structured_finalization_policy=finalization_policy)

    @staticmethod
    def _max_tokens(role: str, config: ProviderConfig | None = None) -> int | None:
        if role == FIX_VERIFIER_ROLE:
            if config is not None and config.verifier_max_tokens != PROVIDER_DEFAULT:
                return config.verifier_max_tokens
            return DEFAULT_VERIFIER_MAX_TOKENS
        if config is not None and role == "investigator":
            return None if config.investigator_max_tokens == PROVIDER_DEFAULT else config.investigator_max_tokens
        if config is not None and role == "verifier":
            return None if config.verifier_max_tokens == PROVIDER_DEFAULT else config.verifier_max_tokens
        configured = os.getenv("FIRMSIGHT_AI_MAX_TOKENS")
        if configured:
            try:
                value = int(configured)
                if 128 <= value <= 16_000:
                    return value
            except ValueError:
                pass
        return ROLE_MAX_TOKENS.get(role, 1_200)

    @staticmethod
    def _env_token_budget(name: str, default: int, allowed: tuple[int, ...]) -> int:
        value = os.getenv(name) or os.getenv("FIRMSIGHT_AI_MAX_TOKENS")
        try:
            parsed = int(value) if value else default
        except ValueError:
            parsed = default
        return parsed if parsed in allowed else default
