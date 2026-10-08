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
CALLBACK_APIS = {
    # API name: (callback argument index, relation kind)
    "esp_event_handler_register": (2, "EVENT_HANDLER_ENTRY"),
    "esp_event_handler_instance_register": (2, "EVENT_HANDLER_ENTRY"),
    "esp_mqtt_client_register_event": (2, "CALLBACK_ENTRY"),
    "xTimerCreate": (4, "TIMER_ENTRY"),
    "gpio_isr_handler_add": (1, "ISR_ENTRY"),
    "esp_intr_alloc": (2, "ISR_ENTRY"),
    "esp_intr_alloc_intrstatus": (2, "ISR_ENTRY"),
}
CALLBACK_STRUCT_APIS = {
    # API name: (configuration argument index, field, struct type, entry relation)
    "httpd_register_uri_handler": (1, "handler", "httpd_uri_t", "REGISTERED_HANDLER"),
    "esp_timer_create": (0, "callback", "esp_timer_create_args_t", "TIMER_ENTRY"),
}
RESOURCE_CREATORS = {
    "xQueueCreate": "queue", "xQueueCreateStatic": "queue", "xSemaphoreCreateMutex": "mutex",
    "xSemaphoreCreateBinary": "semaphore", "xSemaphoreCreateCounting": "semaphore",
    "xEventGroupCreate": "event_group", "xEventGroupCreateStatic": "event_group",
}
RESOURCE_OPERATIONS = {
    "xQueueSend": "PUBLISHES_TO_QUEUE", "xQueueSendToBack": "PUBLISHES_TO_QUEUE", "xQueueSendFromISR": "PUBLISHES_TO_QUEUE",
    "xQueueReceive": "RECEIVES_FROM_QUEUE", "xQueueReceiveFromISR": "RECEIVES_FROM_QUEUE",
    "xSemaphoreTake": "USES_RESOURCE", "xSemaphoreTakeRecursive": "USES_RESOURCE",
    "xSemaphoreGive": "USES_RESOURCE", "xSemaphoreGiveRecursive": "USES_RESOURCE",
    "xSemaphoreTakeFromISR": "USES_RESOURCE", "xSemaphoreGiveFromISR": "USES_RESOURCE",
    "xEventGroupWaitBits": "USES_RESOURCE", "xEventGroupSetBits": "USES_RESOURCE",
    "portENTER_CRITICAL": "USES_RESOURCE", "portEXIT_CRITICAL": "USES_RESOURCE",
    "taskENTER_CRITICAL": "USES_RESOURCE", "taskEXIT_CRITICAL": "USES_RESOURCE",
}
RESOURCE_OPERATION_TYPES = {
    "xQueueSend": "QUEUE_SEND", "xQueueSendToBack": "QUEUE_SEND", "xQueueSendFromISR": "QUEUE_SEND",
    "xQueueReceive": "QUEUE_RECEIVE", "xQueueReceiveFromISR": "QUEUE_RECEIVE",
    "xSemaphoreTake": "LOCK", "xSemaphoreTakeRecursive": "LOCK", "xSemaphoreTakeFromISR": "LOCK",
    "xSemaphoreGive": "UNLOCK", "xSemaphoreGiveRecursive": "UNLOCK", "xSemaphoreGiveFromISR": "UNLOCK",
    "xEventGroupWaitBits": "WAIT", "xEventGroupSetBits": "SIGNAL",
    "portENTER_CRITICAL": "LOCK", "taskENTER_CRITICAL": "LOCK",
    "portEXIT_CRITICAL": "UNLOCK", "taskEXIT_CRITICAL": "UNLOCK",
}
UNTRUSTED_INPUT_APIS = {
    "recv": "SOCKET", "recvfrom": "SOCKET", "uart_read_bytes": "UART",
    "httpd_req_get_url_query_str": "HTTP", "httpd_query_key_value": "HTTP",
    "esp_http_client_read": "HTTP",
}
DATA_SINK_APIS = {"memcpy", "memmove", "strcpy", "strncpy", "sprintf", "snprintf", "atoi", "strtol", "nvs_set_blob", "nvs_set_str"}


def _call_arguments(text: str, opening: int) -> list[str] | None:
    """Split one C/C++ call argument list while respecting nested delimiters."""
    closing = _matching(text, opening, "(", ")")
    if closing is None:
        return None
    result: list[str] = []
    start = opening + 1
    parens = brackets = braces = 0
    for index in range(start, closing):
        char = text[index]
        if char == "(": parens += 1
        elif char == ")": parens = max(0, parens - 1)
        elif char == "[": brackets += 1
        elif char == "]": brackets = max(0, brackets - 1)
        elif char == "{": braces += 1
        elif char == "}": braces = max(0, braces - 1)
        elif char == "," and not (parens or brackets or braces):
            result.append(text[start:index].strip())
            start = index + 1
    final = text[start:closing].strip()
    if final or result:
        result.append(final)
    return result


def _simple_argument_name(value: str) -> str | None:
    value = re.sub(r"\s+", "", value)
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    match = re.fullmatch(r"&?([A-Za-z_]\w*)", value)
    return match.group(1) if match else None


def _parameter_names(signature: str) -> list[str | None]:
    """Extract only plain C/C++ parameter identifiers from a function signature."""
    opening = signature.find("(")
    closing = signature.rfind(")")
    if opening < 0 or closing <= opening:
        return []
    arguments = _call_arguments(signature, opening)
    if arguments is None:
        return []
    result: list[str] = []
    for argument in arguments:
        if not argument or argument == "void" or "..." in argument:
            continue
        # Parameter declarations are intentionally handled conservatively: the
        # final simple identifier is the name only for non-template declarators.
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", argument)
        if len(identifiers) < 2:
            # An unnamed typedef parameter is ambiguous with a lone parameter
            # name, so it is deliberately not used for argument mapping.
            result.append(None)
            continue
        match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*$", argument)
        result.append(match.group(1) if match else None)
    return result


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
        masked_content = "\n".join(mask_comments_and_strings(file["content"]) for file in files if any(file["path"].lower().endswith(extension) for extension in CODE_EXTENSIONS))
        paths = {file["path"].lower() for file in files}
        esp_idf_evidence = (
            any("idf_component.yml" in path or "sdkconfig" in path for path in paths)
            or any(re.search(r"#\s*include\s*[<\"]esp_err\.h[>\"]", str(file.get("content", ""))) for file in files)
            or bool(re.search(r"\b(?:esp_err\.h|esp_event_handler_register|esp_mqtt_client_register_event|esp_timer_create|esp_ota_[A-Za-z_]\w*)\b", masked_content))
        )
        framework = "ESP-IDF" if esp_idf_evidence else None
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
                self._add_registered_callbacks(project_id, path, content, masked, body, function, by_name, relations)
                self._add_resources(project_id, path, content, body, function, relations)
                self._add_allocations(project_id, path, content, body, function, allocations, relations)
                self._add_data_facts(project_id, path, content, body, function, relations)
            self._add_global_accesses(project_id, path, content, masked, function_symbols, relations)
        if framework == "ESP-IDF":
            for function in function_symbols:
                if function["name"] == "app_main":
                    self._relation(
                        relations, project_id, "APP_ENTRY", function["id"], function["id"],
                        function["name"], function["file"], int(function["line_start"]),
                        "OBSERVED", 1.0, {"framework": "ESP-IDF"},
                    )
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
        framework = framework or ("ESP-IDF" if "esp_err.h" in masked_content else None)
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
            if target is None:
                continue
            opening = body.find("(", match.start())
            arguments = _call_arguments(body, opening)
            parameters = _parameter_names(str(target.get("signature") or ""))
            if arguments is None:
                continue
            for index, (parameter, argument) in enumerate(zip(parameters, arguments)):
                if not parameter:
                    continue
                identifier = _simple_argument_name(argument)
                if not identifier:
                    continue
                self._relation(
                    relations, project_id, "PROPAGATES_ARGUMENT", source["id"], target["id"], parameter,
                    path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 0.9,
                    {"call": name, "argument_index": str(index), "source_identifier": identifier, "target_parameter": parameter},
                )
            result_match = re.search(r"([A-Za-z_]\w*)\s*=\s*$", body[max(0, match.start() - 100):match.start()])
            if result_match:
                self._relation(
                    relations, project_id, "CAPTURES_RETURN", source["id"], target["id"], result_match.group(1),
                    path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 0.8,
                    {"call": name, "result_identifier": result_match.group(1)},
                )

    def _add_tasks_and_isrs(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], by_name: dict[str, list[dict[str, Any]]], relations: list[dict[str, Any]]) -> None:
        for api in (*TASK_CREATORS, *ISR_CREATORS):
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                args = _call_arguments(body, body.find("(", match.start()))
                if args is None: continue
                callback_index = 0 if api in TASK_CREATORS else 1 if api == "gpio_isr_handler_add" else 2
                name = _simple_argument_name(args[callback_index]) if len(args) > callback_index else None
                if not name: continue
                candidates = by_name.get(name, []); target = candidates[0] if len(candidates) == 1 else None
                kind = "TASK_ENTRY" if api in TASK_CREATORS else "ISR_ENTRY"
                self._relation(relations, project_id, kind, source["id"], target["id"] if target else None, None if target else name, path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED" if target else "INFERRED", 1.0 if target else 0.35, {"creator": api})

    def _add_registered_callbacks(self, project_id: str, path: str, content: str, file_masked: str, body: str, source: dict[str, Any], by_name: dict[str, list[dict[str, Any]]], relations: list[dict[str, Any]]) -> None:
        """Index only callback registrations whose function pointer is explicit."""
        for api, (callback_index, relation_kind) in CALLBACK_APIS.items():
            if api in TASK_CREATORS or api in ISR_CREATORS:
                continue
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                args = _call_arguments(body, body.find("(", match.start()))
                if args is None or len(args) <= callback_index:
                    continue
                callback_name = _simple_argument_name(args[callback_index])
                self._add_callback_relation(project_id, path, content, source, by_name, relations, callback_name, relation_kind, api, match.start())

        for api, (config_index, field, struct_type, relation_kind) in CALLBACK_STRUCT_APIS.items():
            declarations: dict[str, list[tuple[str, str | None]]] = {}
            for declaration in re.finditer(rf"\b{re.escape(struct_type)}\s+([A-Za-z_]\w*)\s*=\s*\{{", file_masked):
                opening = file_masked.find("{", declaration.start())
                closing = _matching(file_masked, opening, "{", "}")
                if closing is None:
                    continue
                initializer = file_masked[opening + 1:closing]
                callback = re.search(rf"\.\s*{re.escape(field)}\s*=\s*&?\s*([A-Za-z_]\w*)\b", initializer)
                if callback:
                    owner = next((
                        function for functions in by_name.values() for function in functions
                        if function.get("kind") == "function"
                        and int(function.get("_body_start") or 0) <= declaration.start() < int(function.get("_body_end") or 0)
                    ), None)
                    owner_id = str(owner.get("id")) if owner else None
                    declarations.setdefault(declaration.group(1), []).append((callback.group(1), owner_id))
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                args = _call_arguments(body, body.find("(", match.start()))
                if args is None or len(args) <= config_index:
                    continue
                config_name = _simple_argument_name(args[config_index])
                candidates = [
                    callback for callback, owner_id in declarations.get(config_name or "", [])
                    if owner_id is None or owner_id == str(source.get("id"))
                ]
                local_candidates = [
                    callback for callback, owner_id in declarations.get(config_name or "", [])
                    if owner_id == str(source.get("id"))
                ]
                if local_candidates:
                    candidates = local_candidates
                callback_name = candidates[0] if len(set(candidates)) == 1 else None
                self._add_callback_relation(project_id, path, content, source, by_name, relations, callback_name, relation_kind, api, match.start(), config=config_name)

    def _add_callback_relation(self, project_id: str, path: str, content: str, source: dict[str, Any], by_name: dict[str, list[dict[str, Any]]], relations: list[dict[str, Any]], callback_name: str | None, relation_kind: str, api: str, offset: int, *, config: str | None = None) -> None:
        if not callback_name:
            return
        candidates = by_name.get(callback_name, [])
        target = candidates[0] if len(candidates) == 1 else None
        metadata = {"api": api, "registration": "STATIC_ARGUMENT" if config is None else "STATIC_INITIALIZER"}
        if config:
            metadata["config"] = config
        self._relation(
            relations, project_id, relation_kind, source["id"], target["id"] if target else None,
            None if target else callback_name, path,
            _line_at(content, source["_body_start"] + offset),
            "OBSERVED" if target else "INFERRED", 1.0 if target else 0.35, metadata,
        )
        if api == "esp_mqtt_client_register_event" and target is not None:
            self._relation(
                relations, project_id, "UNTRUSTED_INPUT", target["id"], None, "mqtt_event.payload",
                path, _line_at(content, source["_body_start"] + offset), "OBSERVED", 0.8,
                {"api": api, "source_kind": "MQTT_EVENT", "registration": "STATIC_ARGUMENT"},
            )

    def _add_resources(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], relations: list[dict[str, Any]]) -> None:
        for api, resource_kind in RESOURCE_CREATORS.items():
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                before = body[max(0, match.start() - 160):match.start()]
                owner_match = re.search(r"(?:\b[A-Za-z_]\w*\s+)?([A-Za-z_]\w*)\s*=\s*$", before)
                resource = owner_match.group(1) if owner_match else None
                if resource:
                    self._relation(
                        relations, project_id, "CREATES_RESOURCE", source["id"], None,
                        api, path, _line_at(content, source["_body_start"] + match.start()),
                        "OBSERVED", 1.0,
                        {"api": api, "operation": "CREATE", "resource": resource, "resource_kind": resource_kind},
                    )
        for api, relation_kind in RESOURCE_OPERATIONS.items():
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                arguments = _call_arguments(body, body.find("(", match.start()))
                if arguments is None:
                    continue
                resource_index = 0
                resource = _simple_argument_name(arguments[resource_index]) if arguments else None
                metadata = {"api": api, "operation": RESOURCE_OPERATION_TYPES.get(api, "RESOURCE")}
                if resource:
                    metadata["resource"] = resource
                if api in {"xQueueSend", "xQueueSendToBack", "xQueueSendFromISR", "xQueueReceive", "xQueueReceiveFromISR"} and len(arguments) > 1:
                    payload = _simple_argument_name(arguments[1])
                    if payload:
                        metadata["payload"] = payload
                        metadata["copy_semantics"] = "FREERTOS_VALUE_COPY"
                self._relation(relations, project_id, relation_kind, source["id"], None, api, path, _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 1.0, metadata)

    def _add_data_facts(self, project_id: str, path: str, content: str, body: str, source: dict[str, Any], relations: list[dict[str, Any]]) -> None:
        """Index a small, explicit source/validation/sink vocabulary.

        These are source observations, not taint proofs: the flow service keeps
        them as evidence labels and never assumes an untracked value alias.
        """
        for parameter in _parameter_names(str(source.get("signature") or "")):
            if parameter:
                self._relation(relations, project_id, "PARAMETER", source["id"], None, parameter, path,
                               int(source.get("line_start") or 1), "OBSERVED", 0.85,
                               {"identifier": parameter})
        for api, source_kind in UNTRUSTED_INPUT_APIS.items():
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                arguments = _call_arguments(body, body.find("(", match.start()))
                output_index = {"httpd_req_get_url_query_str": 1, "httpd_query_key_value": 2}.get(api, 1)
                output = _simple_argument_name(arguments[output_index]) if arguments and len(arguments) > output_index else None
                assignment = re.search(r"(?:\b[A-Za-z_]\w*(?:\s*\*)?\s+)?([A-Za-z_]\w*)\s*=\s*$", body[max(0, match.start() - 100):match.start()])
                returned = assignment.group(1) if assignment else None
                self._relation(relations, project_id, "UNTRUSTED_INPUT", source["id"], None, api, path,
                               _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 1.0,
                               {"api": api, "source_kind": source_kind, **({"output_identifier": output} if output else {}), **({"return_identifier": returned} if returned else {})})
        for api in DATA_SINK_APIS:
            for match in re.finditer(rf"\b{re.escape(api)}\s*\(", body):
                arguments = _call_arguments(body, body.find("(", match.start()))
                argument_names = [_simple_argument_name(argument) for argument in (arguments or [])[:6]]
                self._relation(relations, project_id, "DATA_SINK", source["id"], None, api, path,
                               _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 1.0,
                               {"api": api, "sink_kind": "MEMORY_OR_EXTERNAL_WRITE", "arguments": ",".join(name or "?" for name in argument_names)})
        for match in re.finditer(r"\bif\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", body):
            predicate = re.sub(r"\s+", " ", match.group(1)).strip()[:180]
            if not re.search(r"(?:<=|>=|<|>|==|!=|\.\.\.)", predicate):
                continue
            identifiers = sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", predicate)) - CONTROL_NAMES)
            self._relation(relations, project_id, "VALIDATES", source["id"], None, None, path,
                           _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 0.85,
                           {"predicate": predicate, "identifiers": ",".join(identifiers[:8])})
        for match in re.finditer(r"\b([A-Za-z_]\w*)\s*=\s*&?([A-Za-z_]\w*)\s*;", body):
            self._relation(relations, project_id, "ASSIGNS", source["id"], None, match.group(1), path,
                           _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 0.85,
                           {"target_identifier": match.group(1), "source_identifier": match.group(2)})
        for match in re.finditer(r"\breturn\s+([A-Za-z_]\w*)\s*;", body):
            self._relation(relations, project_id, "RETURNS_VALUE", source["id"], None, match.group(1), path,
                           _line_at(content, source["_body_start"] + match.start()), "OBSERVED", 0.8,
                           {"source_identifier": match.group(1)})

    def _add_global_accesses(self, project_id: str, path: str, content: str, masked: str,
                             functions: list[dict[str, Any]], relations: list[dict[str, Any]]) -> None:
        """Index simple file-scope scalar declarations and direct identifier access."""
        function_spans = [(int(item["_body_start"]), int(item["_body_end"])) for item in functions if item["file"] == path]
        globals_found: set[str] = set()
        for line_match in re.finditer(r"(?m)^\s*(?:static\s+)?(?:const\s+|volatile\s+|unsigned\s+|signed\s+|long\s+|short\s+)*[A-Za-z_]\w*(?:\s*\*)?\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:=[^;]*)?;", masked):
            offset = line_match.start()
            if any(start <= offset <= end for start, end in function_spans):
                continue
            if "(" in line_match.group(0) or "typedef" in line_match.group(0):
                continue
            globals_found.add(line_match.group(1))
        if not globals_found:
            return
        for function in functions:
            if function["file"] != path:
                continue
            body = masked[int(function["_body_start"]):int(function["_body_end"])]
            for variable in sorted(globals_found):
                for match in re.finditer(rf"\b{re.escape(variable)}\b", body):
                    tail = body[match.end():match.end() + 8]
                    head = body[max(0, match.start() - 4):match.start()]
                    write = bool(re.match(r"\s*(?:\+\+|--|(?:[+*/%&|^-]?=))", tail)) or bool(re.search(r"(?:\+\+|--)\s*$", head))
                    kind = "WRITES" if write else "READS"
                    line = _line_at(content, int(function["_body_start"]) + match.start())
                    metadata = {"variable": variable, "access": kind}
                    if write:
                        assignment = re.match(r"\s*(?:=)\s*([A-Za-z_]\w*)", tail)
                        if assignment:
                            metadata["new_state"] = assignment.group(1)
                            metadata["state_value_kind"] = "IDENTIFIER"
                    self._relation(relations, project_id, kind, function["id"], None, variable, path, line, "OBSERVED", 0.9, metadata)
                    if write and metadata.get("new_state"):
                        self._relation(relations, project_id, "CHANGES_STATE", function["id"], None, variable, path, line,
                                       "OBSERVED", 0.85, {"variable": variable, "new_state": metadata["new_state"]})

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
