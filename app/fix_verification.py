"""Bounded, source-indexed context and deterministic validation for fix checks."""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any

from .platform_schemas import FindingRead, FixVerificationResult


MAX_FIX_CONTEXT_CHARS = 16_000
MAX_SIDE_SOURCE_CHARS = 2_400
MAX_DIFF_CHARS = 2_600
WINDOW_LINES = 28


def _file_hash(item: dict[str, Any]) -> str:
    value = item.get("content_hash")
    if value:
        return str(value)
    return hashlib.sha256(str(item.get("content", "")).encode("utf-8")).hexdigest()


def _symbol_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("file") or ""), str(item.get("name") or "")


def _connected_symbols(
    finding: FindingRead,
    symbols: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    depth: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id = {str(item.get("id")): item for item in symbols}
    roots = {
        str(item["id"])
        for item in symbols
        if item.get("kind") == "function"
        and item.get("name") == finding.location.function
        and item.get("file") == finding.location.file
    }
    if not roots and finding.location.function:
        roots = {str(item["id"]) for item in symbols if item.get("kind") == "function" and item.get("name") == finding.location.function}
    seen = set(roots)
    frontier = set(roots)
    selected_relations: list[dict[str, Any]] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for relation in relations:
            source = relation.get("source_symbol_id")
            target = relation.get("target_symbol_id")
            if source in frontier or target in frontier:
                if relation not in selected_relations:
                    selected_relations.append(relation)
                for symbol_id in (source, target):
                    if symbol_id and symbol_id in by_id and symbol_id not in seen:
                        seen.add(symbol_id)
                        next_frontier.add(symbol_id)
        frontier = next_frontier
        if not frontier:
            break
    selected_symbols = [by_id[item] for item in seen if item in by_id]
    selected_symbols.sort(key=lambda item: (str(item.get("file") or ""), int(item.get("line_start") or 0), str(item.get("name") or "")))
    selected_relations.sort(key=lambda item: (
        str(item.get("file") or ""), int(item.get("line") or 0),
        str(item.get("relation_kind") or ""), str(item.get("source_symbol_id") or ""),
        str(item.get("target_symbol_id") or ""),
    ))
    return selected_symbols, selected_relations[:48]


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _ranges_for_side(
    finding: FindingRead,
    files: dict[str, dict[str, Any]],
    symbols: list[dict[str, Any]],
    related_symbols: list[dict[str, Any]],
    changed_keys: set[tuple[str, str]],
) -> dict[str, list[tuple[int, int]]]:
    priority: list[tuple[str, int, int]] = []
    symbol_lookup = {(str(s.get("file")), str(s.get("name"))): s for s in symbols if s.get("kind") == "function"}

    def add_symbol(item: dict[str, Any], center: int | None = None) -> None:
        start, end = int(item.get("line_start") or 1), int(item.get("line_end") or item.get("line_start") or 1)
        content = str(files.get(str(item.get("file")), {}).get("content", ""))
        lines = content.splitlines()
        start, end = max(1, start), min(max(1, len(lines)), max(start, end))
        if end - start > 150 or sum(len(line) + 1 for line in lines[start - 1:end]) > 2_200:
            pivot = center or (start + end) // 2
            start, end = max(start, pivot - WINDOW_LINES), min(end, pivot + WINDOW_LINES)
        priority.append((str(item.get("file") or ""), start, end))

    original = symbol_lookup.get((finding.location.file, finding.location.function or ""))
    if original:
        add_symbol(original, (finding.location.line_start + finding.location.line_end) // 2)
    else:
        priority.append((finding.location.file, max(1, finding.location.line_start - WINDOW_LINES), finding.location.line_end + WINDOW_LINES))

    for evidence in finding.evidence[:8]:
        file_item = files.get(evidence.file)
        if not file_item:
            continue
        candidate = next((s for s in symbols if s.get("file") == evidence.file and int(s.get("line_start") or 0) <= evidence.line <= int(s.get("line_end") or 0)), None)
        if candidate:
            add_symbol(candidate, evidence.line)
        else:
            priority.append((evidence.file, max(1, evidence.line - WINDOW_LINES), evidence.line + WINDOW_LINES))

    for key in sorted(changed_keys):
        candidate = symbol_lookup.get(key)
        if candidate:
            start, end = int(candidate.get("line_start") or 1), int(candidate.get("line_end") or 1)
            overlaps = key[0] == finding.location.file and start <= finding.location.line_end and end >= finding.location.line_start
            original_symbol_missing = (finding.location.file, finding.location.function or "") not in symbol_lookup
            name_similarity = difflib.SequenceMatcher(None, (finding.location.function or "").casefold(), key[1].casefold()).ratio()
            if overlaps or key[1] == finding.location.function or (original_symbol_missing and name_similarity >= 0.45):
                add_symbol(candidate, (finding.location.line_start + finding.location.line_end) // 2)

    for item in related_symbols:
        add_symbol(item)

    by_file: dict[str, list[tuple[int, int]]] = {}
    for path, start, end in priority:
        if path in files:
            line_count = max(1, len(str(files[path].get("content", "")).splitlines()))
            by_file.setdefault(path, []).append((max(1, start), min(line_count, max(start, end))))
    return {path: _merge_ranges(ranges) for path, ranges in by_file.items()}


def _extract(files: dict[str, dict[str, Any]], ranges: dict[str, list[tuple[int, int]]], limit: int) -> tuple[str, dict[str, str]]:
    blocks: list[str] = []
    selected: dict[str, str] = {}
    remaining = limit
    for path, spans in ranges.items():
        if remaining < 80:
            break
        lines = str(files[path].get("content", "")).splitlines()
        chunks = [f"[{start}-{end}]\n" + "\n".join(lines[start - 1:end]) for start, end in spans]
        excerpt = "\n...\n".join(chunks)
        allowance = min(remaining, 2_200)
        if len(excerpt) > allowance:
            excerpt = excerpt[:allowance] + "\n[excerpt bounded]"
        block = f"<source path=\"{path}\">\n{excerpt}\n</source>"
        blocks.append(block)
        selected[path] = excerpt
        remaining -= len(block)
    return "\n\n".join(blocks), selected


def build_differential_context(
    finding: FindingRead,
    before_files: list[dict[str, Any]],
    current_files: list[dict[str, Any]],
    *,
    before_symbols: list[dict[str, Any]] | None = None,
    current_symbols: list[dict[str, Any]] | None = None,
    before_relations: list[dict[str, Any]] | None = None,
    current_relations: list[dict[str, Any]] | None = None,
    before_allocations: list[dict[str, Any]] | None = None,
    current_allocations: list[dict[str, Any]] | None = None,
    project_intelligence: list[dict[str, str]] | None = None,
    lifetime_facts: list[dict[str, Any]] | None = None,
    max_chars: int = MAX_FIX_CONTEXT_CHARS,
) -> str:
    """Select bounded source around the issue and its indexed topology, then diff it."""
    before_files_by_path = {str(item["path"]): item for item in before_files}
    current_files_by_path = {str(item["path"]): item for item in current_files}
    before_symbols, current_symbols = before_symbols or [], current_symbols or []
    before_relations, current_relations = before_relations or [], current_relations or []
    before_nodes, before_edges = _connected_symbols(finding, before_symbols, before_relations)
    current_nodes, current_edges = _connected_symbols(finding, current_symbols, current_relations)

    before_symbol_map = {_symbol_key(item): item for item in before_symbols if item.get("kind") == "function"}
    current_symbol_map = {_symbol_key(item): item for item in current_symbols if item.get("kind") == "function"}
    changed_keys = {
        key for key in before_symbol_map.keys() | current_symbol_map.keys()
        if key not in before_symbol_map or key not in current_symbol_map
        or before_symbol_map[key].get("source_hash") != current_symbol_map[key].get("source_hash")
    }
    before_ranges = _ranges_for_side(finding, before_files_by_path, before_symbols, before_nodes, changed_keys)
    current_ranges = _ranges_for_side(finding, current_files_by_path, current_symbols, current_nodes, changed_keys)
    before_text, before_selected = _extract(before_files_by_path, before_ranges, MAX_SIDE_SOURCE_CHARS)
    current_text, current_selected = _extract(current_files_by_path, current_ranges, MAX_SIDE_SOURCE_CHARS)

    diff_lines: list[str] = []
    for path in dict.fromkeys([*before_selected, *current_selected]):
        old = before_selected.get(path, "").splitlines()
        new = current_selected.get(path, "").splitlines()
        if old == new:
            continue
        diff_lines.extend(difflib.unified_diff(old, new, fromfile=f"BEFORE/{path}", tofile=f"AFTER/{path}", lineterm=""))
    diff_text = "\n".join(diff_lines)[:MAX_DIFF_CHARS] or "No selected-source text diff; source hashes and topology may still differ."

    def snapshot_id(files: dict[str, dict[str, Any]]) -> str:
        digest = hashlib.sha256()
        for path, item in sorted(files.items()):
            digest.update(path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(_file_hash(item).encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    before_snapshot = snapshot_id(before_files_by_path)
    current_snapshot = snapshot_id(current_files_by_path)
    changed_files = sorted(path for path in before_files_by_path.keys() | current_files_by_path.keys() if path not in before_files_by_path or path not in current_files_by_path or _file_hash(before_files_by_path[path]) != _file_hash(current_files_by_path[path]))
    changed_symbols = sorted(f"{name} ({path})" for path, name in changed_keys)
    topology = {
        "baseline_relations": before_edges[:48], "current_relations": current_edges[:48],
        "baseline_allocations": (before_allocations or [])[:24], "current_allocations": (current_allocations or [])[:24],
        "baseline_symbols": [f"{item.get('name')} ({item.get('file')}:{item.get('line_start')})" for item in before_nodes[:32]],
        "current_symbols": [f"{item.get('name')} ({item.get('file')}:{item.get('line_start')})" for item in current_nodes[:32]],
    }
    header = "\n\n".join([
        "ORIGINAL FINDING (historical evidence, not proof of current status):\n" + json.dumps(finding.model_dump(mode="json"), ensure_ascii=False)[:2_200],
        f"SOURCE SNAPSHOTS: baseline={before_snapshot}; current={current_snapshot}",
        "CHANGED FILES: " + json.dumps(changed_files[:80], ensure_ascii=False),
        "CHANGED FUNCTIONS: " + json.dumps(changed_symbols[:60], ensure_ascii=False),
        "BASELINE SOURCE:\n" + (before_text or "No baseline excerpt could be resolved."),
        "CURRENT SOURCE:\n" + (current_text or "No current excerpt could be resolved."),
        "SOURCE DIFF (BEFORE -> AFTER):\n" + diff_text,
        "CURRENT TOPOLOGY AND BASELINE COMPARISON (untrusted indexed facts):\n" + json.dumps(topology, ensure_ascii=False, default=str)[:1_800],
        "RELEVANT PROJECT INTELLIGENCE (current source has authority):\n" + json.dumps((project_intelligence or [])[:8], ensure_ascii=False)[:900],
        "CURRENT STATIC LIFETIME FACTS (where applicable):\n" + json.dumps((lifetime_facts or [])[:16], ensure_ascii=False, default=str)[:1_200],
    ])
    return header[:max_chars]


def validate_fix_verification(
    result: FixVerificationResult,
    current_files: list[dict[str, Any]],
    current_symbols: list[dict[str, Any]],
    finding: FindingRead | None = None,
) -> FixVerificationResult:
    """Reject model-invented locations and downgrade claims lacking current proof."""
    file_lines = {str(item["path"]): str(item.get("content", "")).splitlines() for item in current_files}
    files = {path: len(lines) for path, lines in file_lines.items()}
    symbols = {(str(item.get("name")), str(item.get("file"))) for item in current_symbols}

    def valid_evidence(item) -> bool:
        line_count = files.get(item.file)
        if line_count is None or item.line > line_count:
            return False
        source_line = file_lines[item.file][item.line - 1]
        if not item.evidence_snippet.strip() or item.evidence_snippet not in source_line:
            return False
        return item.symbol is None or (item.symbol, item.file) in symbols

    valid_mitigations = [item for item in result.mitigations_found if valid_evidence(item)]
    valid_remaining = [item for item in result.remaining_failure_evidence if valid_evidence(item)]
    valid_inspected_files = [path for path in result.inspected_files if path in files]
    valid_inspected_symbols = [name for name in result.inspected_symbols if any(symbol == name for symbol, _ in symbols)]
    verdict = result.verdict
    downgrade_reason: str | None = None
    if verdict == "STILL_PRESENT" and (not valid_remaining or not result.current_execution_path or result.missing_context):
        downgrade_reason = "The current failure path lacked valid current-source evidence or had unresolved context."
        verdict = "INCONCLUSIVE"
    elif verdict == "FIXED" and (not valid_mitigations or not result.original_failure_condition.strip() or not result.current_execution_path or not result.reasoning_summary.strip() or result.missing_context):
        downgrade_reason = "The fix claim lacked valid current mitigation evidence or a supported explanation that the original path is unreachable."
        verdict = "INCONCLUSIVE"
    missing_context = list(result.missing_context)
    summary = result.reasoning_summary
    if downgrade_reason:
        if len(missing_context) < 12:
            missing_context.append(downgrade_reason)
        if downgrade_reason not in summary:
            summary = f"{summary} Verdict downgraded to INCONCLUSIVE: {downgrade_reason}".strip()[:1600]
    return result.model_copy(update={
        "verdict": verdict,
        "reasoning_summary": summary,
        "mitigations_found": valid_mitigations,
        "remaining_failure_evidence": valid_remaining,
        "inspected_files": valid_inspected_files,
        "inspected_symbols": valid_inspected_symbols,
        "missing_context": missing_context,
        **({"original_execution_path": finding.execution_path} if finding is not None else {}),
    })
