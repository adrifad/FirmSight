"""FS-I18N-016: UI language setting (English / Indonesian) end-to-end behavior.

Covers the catalog, locale normalization, the settings round-trip, localized
review progress and HTTP error details, and frontend wiring contracts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES, message, normalize_locale
from tests.test_intelligence import INVESTIGATOR_RESULT, VERIFIER_RESULT


def test_catalog_has_both_locales_for_every_entry():
    from app.i18n import _MESSAGES
    for key, value in _MESSAGES.items():
        assert isinstance(value, tuple) and len(value) == 2, key
        assert all(isinstance(part, str) and part for part in value), key


def test_normalize_locale_defaults_for_unknown_values():
    assert normalize_locale(None) == "en"
    assert normalize_locale("") == "en"
    assert normalize_locale("fr") == "en"
    assert normalize_locale(" ID ") == "id"
    assert normalize_locale("EN") == "en"
    assert DEFAULT_LOCALE == "en"
    assert set(SUPPORTED_LOCALES) == {"en", "id"}


def test_message_interpolates_parameters_and_falls_back_to_english():
    assert message("error.path_not_utf8", "id", path="main.cpp") == "main.cpp bukan teks sumber UTF-8 yang valid"
    assert message("error.path_not_utf8", "en", path="main.cpp") == "main.cpp is not valid UTF-8 source text"
    assert message("error.path_not_utf8", "fr", path="x") == message("error.path_not_utf8", "en", path="x")


def test_settings_round_trip_language(tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main_module

    api = main_module.create_app(database_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(api)
    body = {
        "provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "models": {"investigator": "x", "verifier": "y", "chat": "z", "yaml_generator": "w", "memory_synthesizer": "a", "memory_verifier": "b"},
        "language": "id",
    }
    response = client.put("/api/settings", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["language"] == "id"
    assert client.get("/api/settings").json()["language"] == "id"


def test_settings_reject_unknown_language_in_english(tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main_module

    api = main_module.create_app(database_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(api)
    body = {
        "provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "models": {"investigator": "x", "verifier": "y", "chat": "z", "yaml_generator": "w", "memory_synthesizer": "a", "memory_verifier": "b"},
        "language": "fr",
    }
    response = client.put("/api/settings", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == "Language must be one of: en, id"


def test_review_progress_renders_in_indonesian(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    class ScriptedProvider:
        role = "investigator"
        model = "scripted"
        max_tokens = None

        def available(self) -> bool:
            return True

        def generate_structured(self, _system, schema, _prompt, operation=None, on_event=None):
            from app.ai_provider import structured_response

            raise RuntimeError("gateway unreachable")

        def chat(self, _request):
            raise RuntimeError("gateway unreachable")

    api = main_module.create_app(
        database_path=str(tmp_path / "db.sqlite3"),
        import_root=str(tmp_path / "imports"),
        provider_resolver=lambda _role: ScriptedProvider(),
    )
    client = TestClient(api)
    body = {
        "provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "models": {"investigator": "x", "verifier": "y", "chat": "z", "yaml_generator": "w", "memory_synthesizer": "a", "memory_verifier": "b"},
        "language": "id",
    }
    assert client.put("/api/settings", json=body).status_code == 200
    project = client.post("/api/projects", json={"name": "lang-fixture", "source_type": "MANUAL"})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    assert client.post(f"/api/projects/{project_id}/files", json={"files": {"main.c": "void loop(void) {}\n"}}).status_code == 200
    review = client.post(f"/api/projects/{project_id}/reviews", json={"scope": "FULL_PROJECT", "focus": []})
    assert review.status_code == 201, review.text
    review_id = review.json()["id"]
    detail = client.get(f"/api/projects/{project_id}/reviews/{review_id}").json()
    joined = "\n".join(detail["progress"])
    # Indonesian catalog phrases: "Antrean review" (queued review) and
    # "Mengindeks" (indexing) prove review progress rendering is localized.
    assert "Antrean review" in joined and "Mengindeks" in joined, joined
    assert "English" not in joined


def test_service_error_details_are_localized(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main_module

    api = main_module.create_app(database_path=str(tmp_path / "db.sqlite3"))
    client = TestClient(api)
    missing = client.get("/api/projects/PRJ-MISSING")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Project not found"

    body = {
        "provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "models": {"investigator": "x", "verifier": "y", "chat": "z", "yaml_generator": "w", "memory_synthesizer": "a", "memory_verifier": "b"},
        "language": "id",
    }
    assert client.put("/api/settings", json=body).status_code == 200
    localized = client.get("/api/projects/PRJ-MISSING")
    assert localized.status_code == 404
    assert localized.json()["detail"] == "Proyek tidak ditemukan"


def test_project_service_error_helper_uses_catalog(tmp_path):
    from app.indexer import FirmwareIndexer
    from app.project_service import ProjectService

    service = ProjectService.__new__(ProjectService)
    service.language_resolver = lambda: "id"
    error = service._error(404, "error.project_not_found")
    assert isinstance(error, HTTPException)
    assert error.status_code == 404
    assert error.detail == "Proyek tidak ditemukan"


def test_knowledge_vault_messages_are_localized(tmp_path):
    from app.i18n import message

    assert message("error.vault_blocked_path", "id") == "menolak menulis ke jalur yang diblokir"
    assert message("error.vault_external_edit", "id").startswith("suntingan eksternal tidak diimpor")


# ----------------------------------------------------------------------
# REV-101: AI output language (REQ-3)
# ----------------------------------------------------------------------

PROMPT_ROLES = [
    ("INVESTIGATOR_SYSTEM", "INVESTIGATOR_PROMPT_VERSION"),
    ("VERIFIER_SYSTEM", "VERIFIER_PROMPT_VERSION"),
    ("FIX_VERIFIER_SYSTEM", "FIX_VERIFIER_PROMPT_VERSION"),
    ("YAML_GENERATOR_SYSTEM", "YAML_GENERATOR_PROMPT_VERSION"),
    ("MEMORY_SYNTHESIZER_SYSTEM", "MEMORY_SYNTHESIZER_PROMPT_VERSION"),
    ("MEMORY_VERIFIER_SYSTEM", "MEMORY_VERIFIER_PROMPT_VERSION"),
]


def test_language_block_present_for_every_role_when_id():
    import app.prompts as prompts

    for system_name, _version_name in PROMPT_ROLES:
        base = getattr(prompts, system_name)
        localized = prompts.with_language(base, "id")
        assert "LANGUAGE INSTRUCTION (Bahasa Indonesia)" in localized, system_name
        assert "mutex" in localized and "ISR" in localized, system_name
        assert "NEVER translate code" in localized, system_name
        assert localized.startswith(base), system_name


def test_en_prompts_are_byte_identical_to_base():
    import app.prompts as prompts

    for system_name, _version_name in PROMPT_ROLES:
        base = getattr(prompts, system_name)
        assert prompts.with_language(base, "en") == base, system_name
        assert prompts.with_language(base, None) == base, system_name
        assert prompts.with_language(base, "fr") == base, system_name
    assert prompts.with_language(prompts.CHAT_SYSTEM_BASE, "en") == prompts.CHAT_SYSTEM_BASE


def test_prompt_versions_bumped_for_language_block():
    import app.prompts as prompts

    versions = {name: getattr(prompts, name) for _system, name in PROMPT_ROLES}
    # Every role version moved past v1/v2 baseline recorded before FS-I18N-016.
    assert versions["INVESTIGATOR_PROMPT_VERSION"] == "v5-flow-first"
    assert versions["VERIFIER_PROMPT_VERSION"] == "v4-flow-first"
    assert versions["FIX_VERIFIER_PROMPT_VERSION"] == "v6"
    for name in ("YAML_GENERATOR_PROMPT_VERSION", "MEMORY_SYNTHESIZER_PROMPT_VERSION", "MEMORY_VERIFIER_PROMPT_VERSION", "CHAT_PROMPT_VERSION"):
        assert versions.get(name, getattr(prompts, name)) == "v2", name
    assert prompts.LANGUAGE_INSTRUCTION_VERSION == "v1"


def test_chat_and_role_prompts_receive_language_block_through_services(tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main_module

    captured: dict[str, str] = {}
    mode = {"phase": "chat"}

    class CapturingProvider:
        name = "capturing"
        role = "chat"
        model = "capture-1"
        max_tokens = None

        def available(self) -> bool:
            return True

        def chat(self, system_prompt: str, _user_prompt: str) -> str:
            if mode["phase"] == "chat":
                captured["chat_system"] = system_prompt
                return "Structured analysis of the supplied source."
            captured["structured_systems"] = system_prompt
            if "Investigator" in system_prompt:
                return INVESTIGATOR_RESULT
            if "Verifier / Skeptic" in system_prompt:
                return VERIFIER_RESULT
            return "{}"

    api = main_module.create_app(
        database_path=str(tmp_path / "db.sqlite3"),
        import_root=str(tmp_path / "imports"),
        provider_resolver=lambda _role: CapturingProvider(),
    )
    client = TestClient(api)
    body = {
        "provider": "openrouter", "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "models": {"investigator": "x", "verifier": "y", "chat": "z", "yaml_generator": "w", "memory_synthesizer": "a", "memory_verifier": "b"},
        "language": "id",
    }
    assert client.put("/api/settings", json=body).status_code == 200
    project = client.post("/api/projects", json={"name": "prompt-lang", "source_type": "MANUAL"})
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    assert client.post(f"/api/projects/{project_id}/files", json={"files": {"main.c": "void loop(void) {}\n"}}).status_code == 200

    # Chat path must append the Indonesian block.
    chat = client.post(f"/api/projects/{project_id}/chat", json={"message": "Explain the OTA path"})
    assert chat.status_code == 200, chat.text
    assert "LANGUAGE INSTRUCTION (Bahasa Indonesia)" in captured["chat_system"]

    # Review path: investigator system prompt must contain the block.
    mode["phase"] = "review"
    review = client.post(f"/api/projects/{project_id}/reviews", json={"scope": "FULL_PROJECT", "focus": []})
    assert review.status_code == 201, review.text

    def _probe() -> str | None:
        job = client.get(f"/api/projects/{project_id}/reviews/{review.json()['id']}").json()
        return job.get("status")

    import time

    for _ in range(50):
        if _probe() in {"COMPLETED", "PARTIAL", "FAILED"}:
            break
        time.sleep(0.1)
    assert "LANGUAGE INSTRUCTION (Bahasa Indonesia)" in captured["structured_systems"]

    # English mode must leave the chat system prompt without the block.
    mode["phase"] = "chat"
    body["language"] = "en"
    assert client.put("/api/settings", json=body).status_code == 200
    chat_en = client.post(f"/api/projects/{project_id}/chat", json={"message": "Explain again"})
    assert chat_en.status_code == 200
    assert "LANGUAGE INSTRUCTION" not in captured["chat_system"]
    assert captured["chat_system"].startswith("You are FirmSight's project-aware firmware assistant.")


def test_frontend_has_language_selector_and_localization_module():
    web = Path(__file__).resolve().parents[1] / "apps" / "web" / "src"
    assert (web / "i18n.ts").exists()
    app_text = (web / "App.tsx").read_text(encoding="utf-8")
    assert "firmsight:locale-changed" in app_text
    assert "ui.language.indonesian" in app_text
    assert "LocaleContext.Provider" in app_text
    api_text = (web / "api.ts").read_text(encoding="utf-8")
    assert "language" in api_text


def test_ui_catalog_entries_live_in_frontend_module():
    text = (Path(__file__).resolve().parents[1] / "apps" / "web" / "src" / "i18n.ts").read_text(encoding="utf-8")
    assert "'ui.language': ['Language', 'Bahasa']" in text
    assert "'ui.language.indonesian': ['Bahasa Indonesia', 'Bahasa Indonesia']" in text
