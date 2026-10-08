from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any, Callable

SOURCE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ino", ".txt", ".md", ".yml", ".yaml", ".ini", ".csv"}
CODE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".ino"}
ALLOCATORS = {"malloc", "calloc", "realloc", "new", "heap_caps_malloc", "heap_caps_realloc", "ps_malloc", "ps_realloc"}
RELEASERS = {"free", "delete", "delete[]", "heap_caps_free"}
CONTROL_NAMES = {"if", "for", "while", "switch", "catch", "return", "sizeof", "alignof"}
TASK_CREATORS = {"xTaskCreate", "xTaskCreatePinnedToCore"}
ISR_CREATORS = {"gpio_isr_handler_add", "esp_intr_alloc", "esp_intr_alloc_intrstatus"}
RESOURCE_CREATORS = {
    "xQueueCreate": "queue", "xQueueCreateStatic": "queue", "xSemaphoreCreateMutex": "mutex",
    "xSemaphoreCreateBinary": "semaphore", "xSemaphoreCreateCounting": "semaphore",
    "xEventGroupCreate": "event_group", "xEventGroupCreateStatic": "event_group",
}
RESOURCE_OPERATIONS = {
    "xQueueSend": "PUBLISHES_TO_QUEUE", "xQueueSendToBack": "PUBLISHES_TO_QUEUE", "xQueueSendFromISR": "PUBLISHES_TO_QUEUE",
    "xQueueReceive": "RECEIVES_FROM_QUEUE", "xQueueReceiveFromISR": "RECEIVES_FROM_QUEUE",
    "xSemaphoreTake": "USES_RESOURCE", "xSemaphoreGive": "USES_RESOURCE", "xSemaphoreTakeFromISR": "USES_RESOURCE",
    "xEventGroupWaitBits": "USES_RESOURCE", "xEventGroupSetBits": "USES_RESOURCE",
}


def safe_project_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    if not normalized or ".." in normalized.split("/"):
        raise ValueError("Invalid project file path")
    return normalized


def language_for_path(path: str) -> str:
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return {"c": "C", "h": "C/C++", "cc": "C++", "cpp": "C++", "cxx": "C++", "hpp": "C++", "hh": "C++", "ino": "C++", "yaml": "YAML", "yml": "YAML", "ini": "INI", "md": "Markdown", "csv": "CSV"}.get(suffix, "Text")


@dataclass(frozen=True)
class _Function:
    name: str
    file: str
    line_start: int
    line_end: int
    body_start: int
    body_end: int
    signature: str
    source_hash: str
    symbol_hash: str
    component: str | None


@dataclass(frozen=True)
class IndexResult:
    symbols: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    allocations: list[dict[str, Any]]
    relation_fingerprint: str
    language: str | None
    framework: str | None
    target: str | None
    build_system: str | None


def compute_topology_fingerprint(relations: list[dict[str, Any]], allocations: list[dict[str, Any]], digest: Callable[[str], str]) -> str:
    """Deterministic digest over the *final* persisted topology facts.

    Must be called only after every relation and allocation that will be written
    is present, so a source change that alters any persisted relation/allocation
    (including a memory-lifetime ownership escape merged after the indexer run)
    changes the stored fingerprint used for knowledge revalidation.

    The digest incorporates stable relation identity (``id``), kind, resolved
    target identity, provenance (``evidence_hash``), and the allocation/marker
    ownership state. No relation data is fabricated to reach the hash; it is a
    pure function of the persisted facts.
    """
    relation_parts = repr([(item["id"], item["relation_kind"], item.get("target_symbol_id"), item.get("target_name"), item["evidence_hash"]) for item in sorted(relations, key=lambda value: value["id"])])
    allocation_parts = repr([(item["id"], item["ownership_state"]) for item in allocations])
    return digest(relation_parts + allocation_parts)


@dataclass(frozen=True)
class SourceToken:
    """A token whose offsets refer to the original source text."""

    value: str
    start: int
    end: int


_MASK_LINE_COMMENT = re.compile(r"//[^\n]*")
_MASK_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_MASK_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')


def _mask_pattern_censored(match: re.Match[str]) -> str:
    """Replace every matched character with a space, preserving newlines."""
    return "".join(" " if char != "\n" else "\n" for char in match.group(0))


def mask_comments_and_strings(text: str) -> str:
    """Mask comments/strings while preserving source offsets and newlines."""
    out = list(text)
    state = "normal"
    quote = ""
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        current = text[i]
        following = text[i + 1] if i + 1 < n else ""
        if state == "normal":
            if current == "/" and following == "/":
                out[i] = out[i + 1] = " "
                state = "line_comment"; i += 2; continue
            if current == "/" and following == "*":
                out[i] = out[i + 1] = " "
                state = "block_comment"; i += 2; continue
            if current in {'"', "'"}:
                quote = current; state = "string"; out[i] = " "; i += 1; continue
            i += 1; continue
        if state == "line_comment":
            if current == "\n": state = "normal"
            else: out[i] = " "
            i += 1; continue
        if state == "block_comment":
            if current == "*" and following == "/":
                out[i] = out[i + 1] = " "; state = "normal"; i += 2; continue
            if current != "\n": out[i] = " "
            i += 1; continue
        if current == "\n":
            state = "normal"; escaped = False; i += 1; continue
        out[i] = " "
        if escaped: escaped = False
        elif current == "\\": escaped = True
        elif current == quote: state = "normal"
        i += 1
    return "".join(out)


def cached_mask_comments_and_strings(text: str) -> str:
    """Memoized :func:`mask_comments_and_strings` for repeated large texts.

    The lifetime analyzer masks the same symbol spans once per review batch;
    masking is a pure function of the text, so identical inputs reuse the
    first result instead of re-running the per-character loop.
    """
    return _MASK_CACHE(text)


@lru_cache(maxsize=512)
def _MASK_CACHE(text: str) -> str:
    return mask_comments_and_strings(text)


def tokenize_masked_source(masked_text: str) -> tuple[SourceToken, ...]:
    """Tokenize already-masked source without changing its offsets.

    Callers must pass the result of :func:`mask_comments_and_strings`. Keeping
    this small tokenizer beside the masker makes all non-executing source
    analysis share the same comment/string boundary.
    """

    token_pattern = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|==|!=|<=|>=|&&|\|\||->|\+\+|--|[{}()\[\];?:,=]" )
    return tuple(SourceToken(match.group(0), match.start(), match.end()) for match in token_pattern.finditer(masked_text))


# Kept as a private compatibility alias for existing indexer callers.
_mask_comments_and_strings = mask_comments_and_strings


def _matching(text: str, start: int, opening: str, closing: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening: depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0: return index
    return None


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _component_for(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if "lib" in parts:
        index = parts.index("lib")
        return parts[index + 1] if index + 1 < len(parts) else None
    return None


class FirmwareIndexer:
    """Bounded, deterministic, non-executing C/C++ topology indexer."""

    def index(self, files: list[dict[str, str]], project_id: str = "") -> IndexResult:
        functions: list[_Function] = []
        source_records: list[tuple[str, str, str]] = []
        all_content = "\n".join(file["content"] for file in files)
        paths = {file["path"].lower() for file in files}
        for file in files:
            path, content = file["path"], file["content"]
            if not any(path.lower().endswith(extension) for extension in CODE_EXTENSIONS): continue
            masked = _mask_comments_and_strings(content)
            digest = self.digest(content)
            source_records.append((path, content, masked))
            for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", masked):
                name = match.group(1)
                if name in CONTROL_NAMES: continue
                open_offset = masked.find("(", match.start())
                close = _matching(masked, open_offset, "(", ")")
                if close is None: continue
                opening = re.match(r"\s*(?:(?:const|volatile|noexcept)(?:\s*\([^)]*\))?|->\s*[^{};]+)*\s*\{", masked[close + 1:close + 220])
                if not opening: continue
                prefix_start = max(masked.rfind("\n", 0, match.start()), masked.rfind(";", 0, match.start()), masked.rfind("}", 0, match.start())) + 1
                prefix = masked[prefix_start:match.start()]
                if "=" in prefix or re.search(r"\b(if|for|while|switch|catch)\s*$", prefix): continue
                body_start = close + 1 + opening.start()
                body_end = _matching(masked, body_start, "{", "}")
                if body_end is None: continue
                line_start = _line_at(content, prefix_start)
                signature = re.sub(r"\s+", " ", content[prefix_start:close + 1]).strip()[:1200]
                symbol_source = re.sub(r"\s+", " ", content[prefix_start:body_end + 1]).strip()
                normalized_signature = re.sub(r"\s+", " ", signature).strip()
                symbol_hash = self.digest(f"{normalized_signature}\n{symbol_source}")
                functions.append(_Function(name, path, line_start, _line_at(content, body_end), body_start, body_end, signature, digest, symbol_hash, _component_for(path)))

        function_symbols: list[dict[str, Any]] = []
        by_name: dict[str, list[dict[str, Any]]] = {}
        for function in functions:
            symbol_id = self._stable_id(project_id, "symbol", function.file, function.name, str(function.line_start), function.signature)
            item = {"id": symbol_id, "name": function.name, "kind": "function", "file": function.file, "line_start": function.line_start, "line_end": function.line_end, "signature": function.signature, "component": function.component, "source_hash": function.source_hash, "file_hash": function.source_hash, "symbol_hash": function.symbol_hash, "confidence": 1.0, "_body_start": function.body_start, "_body_end": function.body_end}
            function_symbols.append(item); by_name.setdefault(function.name, []).append(item)
        symbols = [{key: value for key, value in item.items() if not key.startswith("_")} for item in function_symbols]
        relations: list[dict[str, Any]] = []
        allocations: list[dict[str, Any]] = []
        for path, content, masked in source_records:
            for function in [item for item in function_symbols if item["file"] == path]:
                body = masked[function["_body_start"]:function["_body_end"]]
                self._add_calls(project_id, path, content, body, function, by_name, relations)
                self._add_tasks_and_isrs(project_id, path, content, body, function, by_name, relations)
                self._add_resources(project_id, path, content, body, function, relations)
                self._add_allocations(project_id, path, content, body, function, allocations, relations)
        for path, content, masked in source_records:
            for match in re.finditer(r"\b(?:xTaskCreate(?:PinnedToCore)?|xQueueCreate|xSemaphoreCreateMutex|xSemaphoreCreateBinary|xEventGroupCreate)\s*\(", masked):
                line = _line_at(content, match.start())
                if match.group(0).lstrip().startswith("xTask"):
                    name_match = re.search(r"\b([A-Za-z_]\w*)\s*,", masked[match.end():])
                    if name_match:
                        name = name_match.group(1)
                        symbols.append({"id": self._stable_id(project_id, "marker", path, name, str(line)), "name": name, "kind": "freertos_task", "file": path, "line_start": line, "line_end": line, "signature": "", "component": _component_for(path), "source_hash": self.digest(content), "confidence": 1.0})
                else:
                    token = re.search(r"\b(x\w+Create\w*)", match.group(0))
                    if token:
                        api = token.group(1)
                        symbols.append({"id": self._stable_id(project_id, "resource", path, api, str(line)), "name": api, "kind": RESOURCE_CREATORS.get(api, "resource"), "file": path, "line_start": line, "line_end": line, "signature": api, "component": _component_for(path), "source_hash": self.digest(content), "confidence": 1.0})
        relation_fingerprint = compute_topology_fingerprint(relations, allocations, self.digest)
        language = "C++" if any(path.endswith((".cpp", ".hpp", ".cc", ".cxx")) for path in paths) else "C" if any(path.endswith((".c", ".h")) for path in paths) else None
        framework = "ESP-IDF" if any("idf_component.yml" in path or "sdkconfig" in path for path in paths) or "esp_err.h" in all_content else None
        target_match = re.search(r"CONFIG_IDF_TARGET_([A-Z0-9_]+)=y", all_content)
        target = target_match.group(1).replace("_", "-") if target_match else ("ESP32" if "esp_" in all_content else None)
        build_system = "CMake" if "cmakelists.txt" in paths else "PlatformIO" if "platformio.ini" in paths else None
        return IndexResult(symbols=symbols, relations=relations, allocations=allocations, relation_fingerprint=relation_fingerprint, language=language, framework=framework, target=target, build_system=build_system)

    def _add_calls(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], by_name: dict[str, list[dict[str, Any]]], relations: list[dict[str, Any]]) -> None:
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
            name = match.group(1)
            if name in CONTROL_NAMES or name in TASK_CREATORS or name in ISR_CREATORS or name in RESOURCE_CREATORS or name in RESOURCE_OPERATIONS: continue
            candidates = by_name.get(name, [])
            target = candidates[0] if len(candidates) == 1 else None
            self._relation(relations, project_id, "CALLS", source["id"], target["id"] if target else None, None if target else name, path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED" if target else "INFERRED", 1.0 if target else 0.35)

    def _add_tasks_and_isrs(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], by_name: dict[str, list[dict[str, Any]]], relations: list[dict[str, Any]]) -> None:
        for api in (*TASK_CREATORS, *ISR_CREATORS):
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(([^;]*?)\)", body, re.S):
                args = [item.strip() for item in match.group(1).split(",")]
                raw = args[0] if api in TASK_CREATORS else (args[1] if len(args) > 1 else "")
                entry = re.match(r"(?:&\s*)?([A-Za-z_]\w*)$", raw)
                if not entry: continue
                name = entry.group(1); candidates = by_name.get(name, []); target = candidates[0] if len(candidates) == 1 else None
                kind = "TASK_ENTRY" if api in TASK_CREATORS else "ISR_ENTRY"
                self._relation(relations, project_id, kind, source["id"], target["id"] if target else None, None if target else name, path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED" if target else "INFERRED", 1.0 if target else 0.35, {"creator": api})

    def _add_resources(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], relations: list[dict[str, Any]]) -> None:
        for api, relation_kind in RESOURCE_OPERATIONS.items():
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                self._relation(relations, project_id, relation_kind, source["id"], None, api, path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 1.0)

    def _add_allocations(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], allocations: list[dict[str, Any]], relations: list[dict[str, Any]]) -> None:
        for match in re.finditer(r"\b([A-Za-z_]\w*(?:\[\])?)\s*\(", body):
            api = match.group(1)
            if api not in ALLOCATORS and api not in RELEASERS: continue
            line = _line_at(content, source["_body_start"] + match.start())
            before = body[max(0, match.start() - 180):match.start()]
            variable_match = re.search(r"(?:\*|\s)([A-Za-z_]\w*)\s*=\s*$", before)
            variable = variable_match.group(1) if variable_match else None
            event_kind = "ALLOCATE" if api in ALLOCATORS and api not in {"realloc", "heap_caps_realloc", "ps_realloc"} else "RELEASE" if api in RELEASERS else "REALLOC"
            ownership_state = "RELEASED" if event_kind == "RELEASE" else "REALLOC_UNCERTAIN" if event_kind == "REALLOC" else "UNKNOWN"
            item = {"id": self._stable_id(project_id, "allocation", path, str(line), api, variable or ""), "project_id": project_id, "symbol_id": source["id"], "variable": variable, "event_kind": event_kind, "allocator_or_releaser": api, "file": path, "line": line, "evidence_hash": self.digest(f"{path}:{line}:{api}:{variable or ''}"), "ownership_state": ownership_state, "confidence": 1.0, "metadata": {}}
            allocations.append(item)
            self._relation(relations, project_id, "ALLOCATES" if event_kind == "ALLOCATE" else "RELEASES" if event_kind == "RELEASE" else "PASSES_TO_UNKNOWN", source["id"], None, api, path, line, "OBSERVED", 1.0, {"variable": variable or ""})
        for match in re.finditer(r"\bnew\s+(?:[A-Za-z_:][\w:<>]*)(?:\s*\[[^\]]*\])?", body):
            line = _line_at(content, source["_body_start"] + match.start())
            before = body[max(0, match.start() - 180):match.start()]
            variable_match = re.search(r"(?:\*|\s)([A-Za-z_]\w*)\s*=\s*$", before)
            variable = variable_match.group(1) if variable_match else None
            item = {"id": self._stable_id(project_id, "allocation", path, str(line), "new", variable or ""), "project_id": project_id, "symbol_id": source["id"], "variable": variable, "event_kind": "ALLOCATE", "allocator_or_releaser": "new", "file": path, "line": line, "evidence_hash": self.digest(f"{path}:{line}:new:{variable or ''}"), "ownership_state": "UNKNOWN", "confidence": 1.0, "metadata": {}}
            allocations.append(item)
            self._relation(relations, project_id, "ALLOCATES", source["id"], None, "new", path, line, "OBSERVED", 1.0, {"variable": variable or ""})
        for match in re.finditer(r"\bdelete(?:\[\])?\s+([A-Za-z_]\w*)", body):
            line = _line_at(content, source["_body_start"] + match.start())
            variable = match.group(1)
            item = {"id": self._stable_id(project_id, "allocation", path, str(line), "delete", variable), "project_id": project_id, "symbol_id": source["id"], "variable": variable, "event_kind": "RELEASE", "allocator_or_releaser": "delete[]" if "delete[]" in match.group(0) else "delete", "file": path, "line": line, "evidence_hash": self.digest(f"{path}:{line}:delete:{variable}"), "ownership_state": "RELEASED", "confidence": 1.0, "metadata": {}}
            allocations.append(item)
            self._relation(relations, project_id, "RELEASES", source["id"], None, item["allocator_or_releaser"], path, line, "OBSERVED", 1.0, {"variable": variable})

    @classmethod
    def _relation(cls, relations: list[dict[str, Any]], project_id: str, kind: str, source_id: str | None, target_id: str | None, target_name: str | None, file: str, line: int, state: str, confidence: float, metadata: dict[str, str] | None = None) -> None:
        evidence_hash = cls.digest(f"{project_id}:{kind}:{source_id}:{target_id or target_name}:{file}:{line}:{metadata or {}}")
        relation = {"id": cls._stable_id(project_id, "relation", evidence_hash), "project_id": project_id, "relation_kind": kind, "source_symbol_id": source_id, "target_symbol_id": target_id, "target_name": target_name, "file": file, "line": line, "evidence_hash": evidence_hash, "confidence": confidence, "relation_state": state, "metadata": metadata or {}}
        if not any(item["id"] == relation["id"] for item in relations): relations.append(relation)

    @staticmethod
    def _stable_id(project_id: str, *parts: str) -> str:
        return "IDX-" + hashlib.sha256("\0".join((project_id, *parts)).encode("utf-8")).hexdigest()[:24].upper()

    @staticmethod
    def digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
