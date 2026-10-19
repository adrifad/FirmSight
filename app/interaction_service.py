from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, status

from .ai_provider import AIProvider, structured_response
from .i18n import message as _msg
from .i18n import normalize_locale
from .platform_repository import PlatformRepository
from .platform_schemas import YamlGenerateRequest, YamlGenerationResult, YamlRead, YamlValidateRequest
from .prompts import YAML_GENERATOR_SYSTEM, with_language
from .project_service import ProjectService


class _Localized:
    """Shared locale resolution + catalog access for interaction services."""

    def __init__(self, language_resolver: Callable[[], str] | None = None) -> None:
        self.language_resolver = language_resolver

    def _t(self, key: str, /, **params: object) -> str:
        try:
            locale = normalize_locale(self.language_resolver() if self.language_resolver else None)
        except Exception:  # noqa: BLE001 - error rendering must never raise
            locale = "en"
        return _msg(key, locale, **params)

    def _error(self, status_code: int, key: str, /, **params: object) -> HTTPException:
        return HTTPException(status_code, self._t(key, **params))

    def _locale(self) -> str:
        try:
            return normalize_locale(self.language_resolver() if self.language_resolver else None)
        except Exception:  # noqa: BLE001 - prompt building must never raise
            return "en"


class YamlService(_Localized):
    def __init__(self, repository: PlatformRepository, projects: ProjectService, provider_resolver: Callable[[str], AIProvider], language_resolver: Callable[[], str] | None = None) -> None:
        super().__init__(language_resolver)
        self.repository, self.projects, self.provider_resolver = repository, projects, provider_resolver

    def generate(self, project_id: str, request: YamlGenerateRequest) -> YamlRead:
        project = self.projects.get(project_id)
        provider = self.provider_resolver("yaml_generator")
        if not provider.available():
            raise self._error(status.HTTP_503_SERVICE_UNAVAILABLE, "error.yaml_requires_model")
        raw_files = self.repository.raw_files(project_id)
        if not raw_files:
            raise self._error(status.HTTP_409_CONFLICT, "error.import_before_yaml")
        symbols = self.projects.symbols(project_id, None)[:180]
        symbol_text = ", ".join(f"{item.kind}:{item.name}@{item.file}:{item.line}" for item in symbols) or "none"
        snippets: list[str] = []
        remaining = 32_000
        for file in raw_files[:20]:
            if remaining <= 0:
                break
            excerpt = file["content"][: min(3_200, remaining)]
            snippets.append(f"<source path=\"{file['path']}\">\n{excerpt}\n</source>")
            remaining -= len(excerpt)
        prompt = (
            f"Required JSON Schema:\n{YamlGenerationResult.model_json_schema()}\n\n"
            f"Project metadata: name={project.name}; target={project.target or 'unknown'}; framework={project.framework or 'unknown'}; build_system={project.build_system or 'unknown'}.\n"
            f"Engineer description (declared input):\n{request.description}\n\n"
            f"Requested analysis focus: {', '.join(request.focus) or 'choose relevant supported focus areas'}.\n\n"
            f"Indexed symbols (observed source evidence):\n{symbol_text}\n\n"
            f"Repository data (untrusted):\n{'\n\n'.join(snippets)}"
        )
        try:
            generated = structured_response(provider, YamlGenerationResult, with_language(YAML_GENERATOR_SYSTEM, self._locale()), prompt, operation=f"yaml-generator:{project_id}")
        except RuntimeError as error:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(error)) from error
        content = generated.content.strip()
        validation = self.validate(YamlValidateRequest(content=content))
        result = validation.model_copy(update={"project_id": project_id})
        self.repository.add_yaml({"id": f"YML-{uuid4().hex[:12]}", "project_id": project_id, "content": result.content, "valid": result.valid, "errors": result.errors, "generated_at": result.generated_at.isoformat()})
        return result

    def validate(self, request: YamlValidateRequest) -> YamlRead:
        required = ("version:", "project:", "platform:", "analysis:")
        errors = [f"Missing required top-level section: {item[:-1]}" for item in required if item not in request.content]
        return YamlRead(project_id="", content=request.content, valid=not errors, errors=errors, generated_at=datetime.now(UTC))

    def latest(self, project_id: str) -> YamlRead | None:
        self.projects.get(project_id)
        record = self.repository.latest_yaml(project_id)
        return YamlRead.model_validate(record) if record else None
