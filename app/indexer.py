from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


SOURCE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ino", ".txt", ".md", ".yml", ".yaml", ".ini", ".csv"}
CODE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ino"}


def safe_project_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    if not normalized or ".." in normalized.split("/"):
        raise ValueError("Invalid project file path")
    return normalized


def language_for_path(path: str) -> str:
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return {"c": "C", "h": "C/C++", "cc": "C++", "cpp": "C++", "cxx": "C++", "hpp": "C++", "hh": "C++", "ino": "C++", "yaml": "YAML", "yml": "YAML", "ini": "INI", "md": "Markdown", "csv": "CSV"}.get(suffix, "Text")


@dataclass(frozen=True)
class IndexResult:
    symbols: list[dict[str, str | int]]
    language: str | None
    framework: str | None
    target: str | None
    build_system: str | None


class FirmwareIndexer:
    """Deterministic, non-executing first-pass indexer for firmware source."""

    def index(self, files: list[dict[str, str]]) -> IndexResult:
        symbols: list[dict[str, str | int]] = []
        all_content = "\n".join(file["content"] for file in files)
        paths = {file["path"].lower() for file in files}
        for file in files:
            path, content = file["path"], file["content"]
            if not any(path.lower().endswith(extension) for extension in CODE_EXTENSIONS):
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                function = re.match(r"^\s*(?:static\s+)?(?:[\w:*<>]+\s+)+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{", line)
                if function:
                    symbols.append({"project_id": "", "name": function.group(1), "kind": "function", "file": path, "line": line_number})
                task = re.search(r"xTaskCreate(?:PinnedToCore)?\s*\(\s*([A-Za-z_]\w*)", line)
                if task:
                    symbols.append({"project_id": "", "name": task.group(1), "kind": "freertos_task", "file": path, "line": line_number})
                for token, kind in (("xQueueCreate", "queue"), ("xSemaphoreCreateMutex", "mutex"), ("xSemaphoreCreateBinary", "semaphore"), ("xEventGroupCreate", "event_group")):
                    if token in line:
                        symbols.append({"project_id": "", "name": token, "kind": kind, "file": path, "line": line_number})
        language = "C++" if any(path.endswith((".cpp", ".hpp", ".cc")) for path in paths) else "C" if any(path.endswith((".c", ".h")) for path in paths) else None
        framework = "ESP-IDF" if any("idf_component.yml" in path or "sdkconfig" in path for path in paths) or "esp_err.h" in all_content else None
        target_match = re.search(r"CONFIG_IDF_TARGET_([A-Z0-9_]+)=y", all_content)
        target = target_match.group(1).replace("_", "-") if target_match else ("ESP32" if "esp_" in all_content else None)
        build_system = "CMake" if "cmakelists.txt" in paths else "PlatformIO" if "platformio.ini" in paths else None
        return IndexResult(symbols=symbols, language=language, framework=framework, target=target, build_system=build_system)

    @staticmethod
    def digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
