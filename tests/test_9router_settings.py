import pytest

from app.ai_provider import OpenAICompatibleProvider
from app.platform_repository import PlatformRepository
from app.platform_schemas import AISettingsUpdate, ReasoningEffort, StructuredOutputMode
from app.settings_service import SettingsService


def _service(tmp_path) -> SettingsService:
    return SettingsService(PlatformRepository(str(tmp_path / "settings.db")))


def test_9router_uses_named_key_and_local_default_endpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.delenv("FIRMSIGHT_AI_ENDPOINT", raising=False)
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "named-router-secret")
    monkeypatch.setenv("FIRMSIGHT_AI_API_KEY", "generic-secret")

    service = _service(tmp_path)
    config = service.config()
    provider = service.provider("chat")
    settings = service.read()

    assert config.endpoint == "http://127.0.0.1:20128/v1/chat/completions"
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.name == "9router"
    assert provider.api_key == "named-router-secret"
    assert settings.api_key_environment == "FIRMSIGHT_9ROUTER_API_KEY (or FIRMSIGHT_AI_API_KEY)"
    assert settings.api_key_masked == "••••cret"
    assert "named-router-secret" not in settings.model_dump_json()


def test_9router_falls_back_to_generic_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.delenv("FIRMSIGHT_9ROUTER_API_KEY", raising=False)
    monkeypatch.setenv("FIRMSIGHT_AI_API_KEY", "generic-secret")

    provider = _service(tmp_path).provider("chat")

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_key == "generic-secret"


def test_9router_saved_settings_survive_reload_without_exposing_key(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("FIRMSIGHT_AI_PROVIDER", raising=False)
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "named-router-secret")
    service = _service(tmp_path)
    service.update(AISettingsUpdate(
        provider="9router",
        endpoint="http://127.0.0.1:20128/v1/chat/completions",
        models={"chat": "glm/glm-4-flash"},
    ))

    reloaded = _service(tmp_path)
    assert reloaded.config().provider == "9router"
    assert reloaded.config().models["chat"] == "glm/glm-4-flash"
    assert reloaded.read().api_key_masked == "••••cret"


def test_9router_keeps_role_specific_models(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    service = _service(tmp_path)
    service.update(AISettingsUpdate(
        provider="9router",
        endpoint="http://127.0.0.1:20128/v1/chat/completions",
        models={"investigator": "glm/investigator", "chat": "glm/chat"},
    ))

    investigator = service.provider("investigator")
    chat = service.provider("chat")
    assert isinstance(investigator, OpenAICompatibleProvider)
    assert isinstance(chat, OpenAICompatibleProvider)
    assert investigator.model == "glm/investigator"
    assert chat.model == "glm/chat"


def test_structured_output_and_reasoning_settings_persist(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    service = _service(tmp_path)
    service.update(AISettingsUpdate(
        provider="9router",
        endpoint="http://127.0.0.1:20128/v1/chat/completions",
        models={"chat": "glm/chat"},
        structured_output_mode=StructuredOutputMode.JSON_OBJECT,
        reasoning_effort=ReasoningEffort.MEDIUM,
    ))

    config = service.config()
    provider = service.provider("chat")
    assert config.structured_output_mode is StructuredOutputMode.JSON_OBJECT
    assert config.reasoning_effort is ReasoningEffort.MEDIUM
    assert provider.structured_output_mode == "JSON_OBJECT"
    assert provider.reasoning_effort == "MEDIUM"


def test_structured_review_budgets_are_role_specific_and_persist(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    service = _service(tmp_path)
    saved = service.update(AISettingsUpdate(
        provider="9router",
        endpoint="http://127.0.0.1:20128/v1/chat/completions",
        models={"investigator": "glm/investigator", "verifier": "glm/verifier"},
        investigator_max_tokens=2400,
        verifier_max_tokens=1600,
        review_parallel_requests=3,
    ))

    assert saved.investigator_max_tokens == 2400
    assert saved.verifier_max_tokens == 1600
    investigator = service.provider("investigator")
    verifier = service.provider("verifier")
    assert isinstance(investigator, OpenAICompatibleProvider)
    assert isinstance(verifier, OpenAICompatibleProvider)
    assert investigator.max_tokens == 2400
    assert verifier.max_tokens == 1600
    assert _service(tmp_path).read().investigator_max_tokens == 2400
    assert _service(tmp_path).read().review_parallel_requests == 3


def test_provider_default_budget_is_independent_and_omits_global_cap(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    monkeypatch.setenv("FIRMSIGHT_AI_MAX_TOKENS", "3200")
    service = _service(tmp_path)
    saved = service.update(AISettingsUpdate(
        provider="9router",
        endpoint="http://127.0.0.1:20128/v1/chat/completions",
        models={"investigator": "glm/investigator", "verifier": "glm/verifier"},
        investigator_max_tokens="PROVIDER_DEFAULT",
        verifier_max_tokens=1600,
    ))

    assert saved.investigator_max_tokens == "PROVIDER_DEFAULT"
    assert service.config().investigator_max_tokens == "PROVIDER_DEFAULT"
    assert service.provider("investigator").max_tokens is None
    assert service.provider("verifier").max_tokens == 1600
    assert service.provider("verifier_fix").max_tokens == 1600
    assert service.provider("verifier_fix").model == "glm/verifier"
    assert _service(tmp_path).read().investigator_max_tokens == "PROVIDER_DEFAULT"


def test_verifier_fix_preserves_numeric_verifier_budget(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    service = _service(tmp_path)
    service.update(AISettingsUpdate(
        provider="9router",
        endpoint="http://127.0.0.1:20128/v1/chat/completions",
        models={"investigator": "glm/investigator", "verifier": "glm/verifier"},
        verifier_max_tokens=2_000,
    ))

    assert service.provider("verifier").max_tokens == 2_000
    assert service.provider("verifier_fix").max_tokens == 2_000


def test_structured_review_budgets_reject_non_allowlisted_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_PROVIDER", "9router")
    monkeypatch.setenv("FIRMSIGHT_9ROUTER_API_KEY", "router-secret")
    service = _service(tmp_path)
    with pytest.raises(Exception, match="Investigator output budget"):
        service.update(AISettingsUpdate(
            provider="9router",
            endpoint="http://127.0.0.1:20128/v1/chat/completions",
            models={"chat": "glm/chat"},
            investigator_max_tokens=1300,
        ))


def test_legacy_persisted_settings_use_migration_defaults_not_global_budget(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FIRMSIGHT_AI_MAX_TOKENS", "1200")
    repository = PlatformRepository(str(tmp_path / "settings.db"))
    repository.set_setting("ai_provider", {
        "provider": "9router",
        "endpoint": "http://127.0.0.1:20128/v1/chat/completions",
        "models": {"chat": "glm/chat"},
    }, "2026-09-13T00:00:00+00:00")

    settings = SettingsService(repository).read()

    assert settings.investigator_max_tokens == 2000
    assert settings.verifier_max_tokens == 1200
