"""Bounded, source-indexed context and deterministic validation for fix checks."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from typing import Any

from .platform_schemas import (
    FindingBaselineFile,
    FindingBaselineSymbol,
    FindingRead,
    FindingVerificationBaseline,
    FixVerificationPathEdge,
    FixVerificationResult,
)


MAX_FIX_CONTEXT_CHARS = 16_000
MAX_SIDE_SOURCE_CHARS = 2_400
MAX_DIFF_CHARS = 2_600
WINDOW_LINES = 28
MAX_FINDING_BASELINE_CHARS = 18_000
MITIGATION_RELATIONS = {
    "USES_RESOURCE", "PUBLISHES_TO_QUEUE", "RECEIVES_FROM_QUEUE",
    "OWNERSHIP_TRANSFER", "PASSES_TO_UNKNOWN", "VALIDATES_INPUT",
}


def _file_hash(item: dict[str, Any]) -> str:
    value = item.get("content_hash")
    if value:
        return str(value)
    return hashlib.sha256(str(item.get("content", "")).encode("utf-8")).hexdigest()


def _symbol_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("file") or ""), str(item.get("name") or "")


def _symbol_hash(item: dict[str, Any]) -> str | None:
    # Old persisted rows expose an empty symbol_hash; their source_hash is a
    # whole-file digest and must not be interpreted as function change proof.
    return str(item.get("symbol_hash") or "") or None


def _edge_from_relation(relation: dict[str, Any], symbols_by_id: dict[str, dict[str, Any]]) -> FixVerificationPathEdge | None:
    source_id = str(relation.get("source_symbol_id") or "")
    target_id = str(relation.get("target_symbol_id") or "")
    source_symbol = symbols_by_id.get(source_id)
    target_symbol = symbols_by_id.get(target_id)
    source = str((source_symbol or {}).get("name") or source_id)
    target = str((target_symbol or {}).get("name") or relation.get("target_name") or target_id)
    if not source or not target or not relation.get("relation_kind"):
        return None
    return FixVerificationPathEdge(
        source=source,
        relation=str(relation["relation_kind"]),
        target=target,
        file=relation.get("file"),
        line=relation.get("line"),
        relation_state=relation.get("relation_state"),
        source_id=source_id or None,
        target_id=target_id or None,
    )


def capture_finding_baseline(
    finding: FindingRead,
    files: list[dict[str, Any]],
    symbols: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    *,
    source_snapshot_hash: str,
    topology_fingerprint: str | None = None,
) -> FindingVerificationBaseline:
    """Persist bounded evidence from the source/index state that created a finding."""
    files_by_path = {str(item["path"]): item for item in files}
    symbol_by_id = {str(item.get("id")): item for item in symbols}
    nodes, edges = _connected_symbols(finding, symbols, relations, depth=3)
    stored_files: list[FindingBaselineFile] = []
    remaining = MAX_FINDING_BASELINE_CHARS
    paths = list(dict.fromkeys([finding.location.file, *(item.file for item in finding.evidence[:8])]))
    for path in paths[:8]:
        item = files_by_path.get(path)
        if not item or remaining < 200:
            continue
        content = str(item.get("content", ""))
        lines = content.splitlines()
        target_line = finding.location.line_start if path == finding.location.file else next(
            (entry.line for entry in finding.evidence if entry.file == path), 1
        )
        candidates = [
            symbol for symbol in symbols
            if symbol.get("kind") == "function" and symbol.get("file") == path
            and int(symbol.get("line_start") or 0) <= target_line <= int(symbol.get("line_end") or 0)
        ]
        if path == finding.location.file and finding.location.function:
            preferred = next((symbol for symbol in candidates if symbol.get("name") == finding.location.function), None)
            if preferred:
                candidates = [preferred]
        symbol = candidates[0] if candidates else None
        start = max(1, target_line - 24)
        end = min(len(lines), target_line + 24)
        if symbol:
            symbol_start = max(1, int(symbol.get("line_start") or 1))
            symbol_end = min(len(lines), int(symbol.get("line_end") or symbol_start))
            full = "\n".join(lines[symbol_start - 1:symbol_end])
            if len(full) <= min(6_000, remaining):
                start, end = symbol_start, symbol_end
            else:
                start, end = max(symbol_start, target_line - 24), min(symbol_end, target_line + 24)
        excerpt = "\n".join(lines[start - 1:end])
        if len(excerpt) > min(8_000, remaining):
            excerpt = excerpt[:min(8_000, remaining)]
        if not excerpt:
            continue
        stored_files.append(FindingBaselineFile(
            file=path,
            content_hash=_file_hash(item),
            line_start=start,
            excerpt=excerpt,
            symbol_name=str(symbol.get("name")) if symbol else None,
            signature=str(symbol.get("signature") or "") if symbol else None,
            symbol_hash=_symbol_hash(symbol) if symbol else None,
        ))
        remaining -= len(excerpt)

    baseline_symbols = [
        FindingBaselineSymbol(
            id=str(item.get("id") or ""), name=str(item.get("name") or ""), file=str(item.get("file") or ""),
            signature=str(item.get("signature") or ""), symbol_hash=_symbol_hash(item),
            component=item.get("component"), line_start=max(1, int(item.get("line_start") or 1)),
            line_end=max(1, int(item.get("line_end") or item.get("line_start") or 1)),
        )
        for item in nodes if item.get("kind") == "function" and item.get("name") and item.get("file")
    ][:24]
    edge_models = [
        edge for relation in edges
        if (edge := _edge_from_relation(relation, symbol_by_id)) is not None
    ][:48]
    fingerprint_payload = {
        "symbols": [item.model_dump(mode="json") for item in baseline_symbols],
        "edges": [item.model_dump(mode="json") for item in edge_models],
    }
    fingerprint = topology_fingerprint or hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return FindingVerificationBaseline(
        source_snapshot_hash=source_snapshot_hash,
        topology_fingerprint=fingerprint,
        files=stored_files,
        symbols=baseline_symbols,
        topology_edges=edge_models,
    )


def _moved_function_candidates(
    finding: FindingRead,
    baseline: FindingVerificationBaseline | None,
    current_symbols: list[dict[str, Any]],
    current_relations: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve a moved function only when indexed callers corroborate identity."""
    if baseline is None or not finding.location.function:
        return "UNKNOWN", []
    old = next((item for item in baseline.symbols if item.file == finding.location.file and item.name == finding.location.function), None)
    if old is None:
        return "UNKNOWN", []
    old_callers = [
        edge for edge in baseline.topology_edges
        if edge.target_id == old.id and edge.relation.upper() in {"CALL", "CALLS", "TASK_ENTRY", "ISR_ENTRY"}
    ]
    if not old_callers:
        return "UNKNOWN", []
    by_id = {str(item.get("id")): item for item in current_symbols}
    redirected: dict[str, dict[str, Any]] = {}
    for relation in current_relations:
        if str(relation.get("relation_kind") or "").upper() not in {"CALL", "CALLS", "TASK_ENTRY", "ISR_ENTRY"}:
            continue
        source = by_id.get(str(relation.get("source_symbol_id") or ""))
        target = by_id.get(str(relation.get("target_symbol_id") or ""))
        if not source or not target or target.get("kind") != "function":
            continue
        if any(
            source.get("name") == caller.source
            and relation.get("relation_state") == "OBSERVED"
            for caller in old_callers
        ):
            redirected[str(target["id"])] = target
    candidates = []
    for item in redirected.values():
        signature_similarity = difflib.SequenceMatcher(
            None, " ".join(old.signature.casefold().split()), " ".join(str(item.get("signature") or "").casefold().split())
        ).ratio()
        name_similarity = difflib.SequenceMatcher(None, old.name.casefold(), str(item.get("name") or "").casefold()).ratio()
        same_component = bool(old.component and old.component == item.get("component"))
        if signature_similarity >= 0.62 or (name_similarity >= 0.72 and same_component):
            candidates.append({**item, "_signature_similarity": signature_similarity, "_name_similarity": name_similarity})
    candidates.sort(key=lambda item: (str(item.get("file") or ""), int(item.get("line_start") or 0), str(item.get("name") or "")))
    if len(candidates) == 1:
        return "RESOLVED", candidates
    if len(candidates) > 1:
        return "AMBIGUOUS", candidates[:8]
    return "UNKNOWN", []


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
    finding_baseline: FindingVerificationBaseline | None = None,
    max_chars: int = MAX_FIX_CONTEXT_CHARS,
) -> str:
    """Select bounded source around the issue and its indexed topology, then diff it."""
    before_files_by_path = {str(item["path"]): item for item in before_files}
    current_files_by_path = {str(item["path"]): item for item in current_files}
    before_symbols, current_symbols = before_symbols or [], current_symbols or []
    before_relations, current_relations = before_relations or [], current_relations or []
    before_nodes, before_edges = _connected_symbols(finding, before_symbols, before_relations)
    current_nodes, current_edges = _connected_symbols(finding, current_symbols, current_relations)
    moved_status, moved_candidates = _moved_function_candidates(
        finding, finding_baseline, current_symbols, current_relations
    )
    current_nodes = list({str(item.get("id")): item for item in [*current_nodes, *moved_candidates]}.values())
    current_nodes.sort(key=lambda item: (str(item.get("file") or ""), int(item.get("line_start") or 0), str(item.get("name") or "")))

    before_symbol_map = {_symbol_key(item): item for item in before_symbols if item.get("kind") == "function"}
    current_symbol_map = {_symbol_key(item): item for item in current_symbols if item.get("kind") == "function"}
    changed_keys = {
        key for key in before_symbol_map.keys() | current_symbol_map.keys()
        if key not in before_symbol_map or key not in current_symbol_map
        or (
            _symbol_hash(before_symbol_map[key]) is not None
            and _symbol_hash(current_symbol_map[key]) is not None
            and _symbol_hash(before_symbol_map[key]) != _symbol_hash(current_symbol_map[key])
        )
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

    original_baseline_text = "Finding-time baseline unavailable. Historical finding evidence is not a source snapshot."
    original_diff_lines: list[str] = []
    if finding_baseline is not None:
        baseline_blocks = []
        for item in finding_baseline.files[:8]:
            baseline_blocks.append(
                f"<source path=\"{item.file}\" original-lines=\"{item.line_start}-\">\n{item.excerpt}\n</source>"
            )
            current_excerpt = current_selected.get(item.file)
            if current_excerpt is None and item.file in current_files_by_path:
                current_lines = str(current_files_by_path[item.file].get("content", "")).splitlines()
                start = max(1, item.line_start - 24)
                end = min(len(current_lines), item.line_start + max(24, len(item.excerpt.splitlines())))
                current_excerpt = "\n".join(current_lines[start - 1:end])
            before_excerpt = item.excerpt.splitlines()
            after_excerpt = (current_excerpt or "<source removed>").splitlines()
            original_diff_lines.extend(difflib.unified_diff(
                before_excerpt, after_excerpt,
                fromfile=f"ORIGINAL/{item.file}", tofile=f"CURRENT/{item.file}", lineterm="",
            ))
        original_baseline_text = "\n\n".join(baseline_blocks) or "Finding-time baseline metadata exists, but no source excerpt was captured."
    original_diff = "\n".join(original_diff_lines)[:MAX_DIFF_CHARS] or "No finding-baseline excerpt diff could be resolved."

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
        "SOURCE SNAPSHOTS: " + json.dumps({
            "finding_baseline": finding_baseline.source_snapshot_hash if finding_baseline else None,
            "finding_topology_fingerprint": finding_baseline.topology_fingerprint if finding_baseline else None,
            "pre_refresh_index": before_snapshot,
            "current_refreshed_source": current_snapshot,
            "finding_baseline_is_known": finding_baseline is not None,
        }, ensure_ascii=False),
        "CHANGED FILES: " + json.dumps(changed_files[:80], ensure_ascii=False),
        "CHANGED FUNCTIONS: " + json.dumps(changed_symbols[:60], ensure_ascii=False),
        "ORIGINAL FINDING BASELINE SOURCE:\n" + original_baseline_text,
        "ORIGINAL FINDING -> CURRENT SOURCE DIFF:\n" + original_diff,
        "PRE-REFRESH INDEX (comparison snapshot only; not necessarily the finding-time source):\n" + (before_text or "No pre-refresh excerpt could be resolved."),
        "CURRENT SOURCE:\n" + (current_text or "No current excerpt could be resolved."),
        "SOURCE DIFF (BEFORE -> AFTER)\nComparison snapshots: PRE-REFRESH -> CURRENT\n" + diff_text,
        "CURRENT TOPOLOGY AND SNAPSHOT COMPARISON (untrusted indexed facts):\n" + json.dumps({
            **topology,
            "finding_time_topology_edges": [item.model_dump(mode="json") for item in finding_baseline.topology_edges[:48]] if finding_baseline else [],
            "moved_function_resolution": moved_status,
            "moved_function_candidates": [
                {key: item.get(key) for key in ("name", "file", "line_start", "line_end", "signature", "component", "symbol_hash", "_signature_similarity", "_name_similarity")}
                for item in moved_candidates[:8]
            ],
        }, ensure_ascii=False, default=str)[:3_000],
        "RELEVANT PROJECT INTELLIGENCE (current source has authority):\n" + json.dumps((project_intelligence or [])[:8], ensure_ascii=False)[:900],
        "CURRENT STATIC LIFETIME FACTS (where applicable):\n" + json.dumps((lifetime_facts or [])[:16], ensure_ascii=False, default=str)[:1_200],
    ])
    return header[:max_chars]


def validate_fix_verification(
    result: FixVerificationResult,
    current_files: list[dict[str, Any]],
    current_symbols: list[dict[str, Any]],
    finding: FindingRead | None = None,
    *,
    current_relations: list[dict[str, Any]] | None = None,
) -> FixVerificationResult:
    """Ground fix verdicts in current source and current indexed relations."""
    file_lines = {str(item["path"]): str(item.get("content", "")).splitlines() for item in current_files}
    files = {path: len(lines) for path, lines in file_lines.items()}
    current_relations = current_relations or []
    symbols_by_id = {str(item.get("id")): item for item in current_symbols}
    symbols_by_name_file = {
        (str(item.get("name") or ""), str(item.get("file") or "")): item
        for item in current_symbols if item.get("kind") == "function"
    }
    related_nodes, _ = _connected_symbols(finding, current_symbols, current_relations, depth=3) if finding else ([], [])
    allowed_symbols = {str(item.get("id")) for item in related_nodes}
    baseline = finding.verification_baseline if finding else None
    baseline_old_symbol = None
    baseline_callers: list[FindingBaselineSymbol] = []
    if finding and baseline is not None:
        baseline_old_symbol = next((item for item in baseline.symbols if item.file == finding.location.file and item.name == finding.location.function), None)
        if baseline_old_symbol is not None:
            caller_names = {
                edge.source for edge in baseline.topology_edges
                if edge.target_id == baseline_old_symbol.id and edge.relation.upper() in {"CALL", "CALLS", "TASK_ENTRY", "ISR_ENTRY"}
            }
            baseline_callers = [item for item in baseline.symbols if item.name in caller_names]
            for prior in baseline_callers:
                current = symbols_by_name_file.get((prior.name, prior.file))
                if current:
                    allowed_symbols.add(str(current.get("id")))
    moved_status, moved_candidates = _moved_function_candidates(finding, baseline, current_symbols, current_relations) if finding else ("UNKNOWN", [])
    for item in moved_candidates:
        allowed_symbols.add(str(item.get("id")))
    path_relation_kinds = {"CALL", "CALLS", "TASK_ENTRY", "ISR_ENTRY"}

    def relation_matches(edge: FixVerificationPathEdge, relation: dict[str, Any]) -> bool:
        relation_kind = str(relation.get("relation_kind") or "").upper()
        requested_kind = edge.relation.upper()
        if requested_kind == "CALL":
            requested_kind = "CALLS"
        if relation_kind == "CALL":
            relation_kind = "CALLS"
        if relation_kind != requested_kind:
            return False
        source_id = str(relation.get("source_symbol_id") or "")
        target_id = str(relation.get("target_symbol_id") or "")
        source_symbol = symbols_by_id.get(source_id)
        target_symbol = symbols_by_id.get(target_id)
        actual_source = str((source_symbol or {}).get("name") or source_id)
        actual_target = str((target_symbol or {}).get("name") or relation.get("target_name") or target_id)
        if edge.source not in {actual_source, source_id} or edge.target not in {actual_target, target_id}:
            return False
        if edge.source_id and edge.source_id != source_id:
            return False
        if edge.target_id and edge.target_id != target_id:
            return False
        # A path claim must point to an observed current relation with an
        # indexed source location. Optional schema fields keep old payloads
        # readable, but they cannot establish a current path.
        if not edge.file or edge.file != relation.get("file"):
            return False
        if edge.line is None or edge.line != relation.get("line"):
            return False
        if edge.relation_state != relation.get("relation_state"):
            return False
        if relation_kind in path_relation_kinds:
            # An inferred/ambiguous target cannot establish reachability.
            if relation.get("relation_state") != "OBSERVED" or not source_id or not target_id:
                return False
        return True

    validated_edge_pairs: list[tuple[FixVerificationPathEdge, dict[str, Any]]] = []
    invalid_edge_count = 0
    for edge in result.current_path_edges:
        matches = [relation for relation in current_relations if relation_matches(edge, relation)]
        if len(matches) != 1:
            invalid_edge_count += 1
            continue
        validated_edge_pairs.append((edge, matches[0]))
    edges_chain = bool(validated_edge_pairs) and invalid_edge_count == 0
    for (previous_edge, _), (next_edge, _) in zip(validated_edge_pairs, validated_edge_pairs[1:]):
        if previous_edge.target_id and next_edge.source_id:
            if previous_edge.target_id != next_edge.source_id:
                edges_chain = False
                break
        elif previous_edge.target != next_edge.source:
            edges_chain = False
            break
    validated_edges = [edge for edge, _ in validated_edge_pairs]
    path_symbol_ids = {
        str(relation.get(key) or "")
        for _, relation in validated_edge_pairs for key in ("source_symbol_id", "target_symbol_id")
        if relation.get(key)
    }
    current_entry_path = any(
        edge.relation.upper() in {"TASK_ENTRY", "ISR_ENTRY"}
        and edge.relation_state == "OBSERVED"
        for edge in validated_edges
    )

    def current_evidence(item, *, require_path_sink: bool = False):
        if item.file not in file_lines or item.line > files[item.file]:
            return None
        line_text = file_lines[item.file][item.line - 1]
        stripped = line_text.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "*/")):
            return None
        symbol = symbols_by_name_file.get((item.symbol or "", item.file)) if item.symbol else None
        if item.symbol and symbol is None:
            return None
        if symbol and not (int(symbol.get("line_start") or 0) <= item.line <= int(symbol.get("line_end") or 0)):
            return None
        if not symbol:
            symbol = next((candidate for candidate in current_symbols if candidate.get("kind") == "function" and candidate.get("file") == item.file and int(candidate.get("line_start") or 0) <= item.line <= int(candidate.get("line_end") or 0)), None)
        if symbol is None:
            return None
        symbol_id = str(symbol.get("id") or "")
        # Evidence must belong to the finding's original source/evidence area,
        # an indexed neighbor, or an endpoint of the claimed current path.
        original_paths = {finding.location.file, *(e.file for e in finding.evidence)} if finding else set()
        if item.file not in original_paths and symbol_id not in allowed_symbols and symbol_id not in path_symbol_ids:
            return None
        if symbol_id not in allowed_symbols and symbol_id not in path_symbol_ids and item.file in original_paths:
            original_name = finding.location.function if finding and item.file == finding.location.file else None
            if original_name and symbol.get("name") != original_name:
                return None
        if require_path_sink:
            if not validated_edge_pairs:
                return None
            last_relation = validated_edge_pairs[-1][1]
            target_id = str(last_relation.get("target_symbol_id") or "")
            if target_id != symbol_id:
                return None
            if last_relation.get("relation_kind") not in path_relation_kinds:
                return None
            if finding is not None:
                original_text = " ".join([
                    finding.title, finding.summary, finding.runtime_scenario,
                    *(entry.description for entry in finding.evidence),
                ]).casefold()
                original_terms = _evidence_terms(original_text)
                line_terms = _evidence_terms(stripped)
                if original_terms and not original_terms.intersection(line_terms):
                    return None
        else:
            # A valid line in a related function is not automatically support
            # for a mitigation claim. Ground specific claim terms in source.
            mitigation_terms = _evidence_terms(item.description + " " + (finding.recommendation if finding else ""))
            line_terms = _evidence_terms(stripped)
            symbol_terms = _evidence_terms(str(symbol.get("name") or ""))
            if mitigation_terms and not mitigation_terms.intersection(line_terms | symbol_terms):
                return None
        # The copied model snippet is a hint only. Persist the canonical current
        # source line after validating its file, line, symbol, and relationship.
        return item.model_copy(update={
            "symbol": str(symbol.get("name")),
            "evidence_snippet": stripped[:300],
        })

    valid_mitigations = [
        grounded for item in result.mitigations_found
        if (grounded := current_evidence(item)) is not None
    ]
    valid_remaining = [
        grounded for item in result.remaining_failure_evidence
        if (grounded := current_evidence(item, require_path_sink=True)) is not None
    ]
    valid_inspected_files = [path for path in result.inspected_files if path in files]
    valid_inspected_symbols = [name for name in result.inspected_symbols if any(item.get("name") == name for item in current_symbols)]
    # The AI response may contain compatibility/extra fields despite the
    # prompt; only the submitted verdict is treated as its proposal. Validation
    # metadata is always recomputed and overwritten below.
    requested_verdict = result.verdict
    verdict = result.verdict
    # Never trust model-authored validation metadata. The validator derives its
    # reasons afresh from current source and topology on every invocation.
    reasons: list[str] = []
    path_sink_symbols = {
        str((symbols_by_id.get(symbol_id) or {}).get("name") or "")
        for symbol_id in path_symbol_ids
    }
    path_has_mitigation_relation = any(
        str(relation.get("relation_kind") or "").upper() in MITIGATION_RELATIONS
        and bool(path_symbol_ids.intersection({str(relation.get("source_symbol_id") or ""), str(relation.get("target_symbol_id") or "")}))
        for relation in current_relations
    )
    mitigation_intersects_path = any(item.symbol in path_sink_symbols for item in valid_mitigations)
    source_guard_on_path = any(
        str(relation.get("relation_kind") or "").upper() in path_relation_kinds
        and relation.get("file") in file_lines
        and relation.get("line") is not None
        and bool(re.search(
            r"\b(if|switch|assert|validate|check|bounds|length|size|mutex|semaphore|lock|critical|queue)\b|xSemaphoreTake|portENTER_CRITICAL",
            "\n".join(file_lines[str(relation["file"])][max(0, int(relation["line"]) - 3):min(len(file_lines[str(relation["file"])]), int(relation["line"]) + 2)]),
            re.IGNORECASE,
        ))
        for _, relation in validated_edge_pairs
    )

    if verdict == "STILL_PRESENT":
        if not valid_remaining:
            reasons.append("No current-source failure evidence was validated against the indexed finding path.")
        if not result.current_path_edges or not edges_chain:
            reasons.append("Current path edges were missing, fabricated, ambiguous, or disconnected from current indexed topology.")
        if not current_entry_path:
            reasons.append("The verified current path does not include an observed task or ISR entry relation.")
        if not result.current_execution_path:
            reasons.append("The engineer-readable current path summary is missing.")
        if result.missing_context:
            reasons.append("The verifier reported unresolved current context.")
        if path_has_mitigation_relation or mitigation_intersects_path:
            reasons.append("Current resource or mitigation evidence intersects the claimed path, but coverage or bypass was not established.")
        if source_guard_on_path:
            reasons.append("Current source shows a guard or synchronization operation on the path, but its coverage was not established.")
        # A moved implementation is accepted only when the original indexed
        # caller redirects to one uniquely supported current implementation.
        if finding and valid_remaining and valid_remaining[0].symbol != finding.location.function and moved_status != "RESOLVED":
            reasons.append("The moved implementation could not be uniquely linked to the original caller topology.")
        if reasons and any(reason for reason in reasons):
            verdict = "INCONCLUSIVE"
    elif verdict == "FIXED":
        if not valid_mitigations:
            reasons.append("No current-source mitigation or removal evidence was validated.")
        if not result.original_failure_condition.strip() or not result.current_execution_path or not result.reasoning_summary.strip():
            reasons.append("The original failure condition or evidence-based fix explanation is incomplete.")
        if result.missing_context:
            reasons.append("The verifier reported unresolved context, so the fix cannot be established.")
        if valid_remaining:
            reasons.append("The response also supplied current evidence for a remaining failure path.")
        if result.current_path_edges and invalid_edge_count:
            reasons.append("One or more proposed current path edges do not match current indexed topology.")
        # If the baseline/current graph still has the same reachable sink and
        # no path-local mitigation was grounded, the fix claim is unsupported.
        if validated_edge_pairs and valid_remaining:
            reasons.append("A validated current path still reaches the reported unsafe sink.")
        # A disappeared function is not automatically a fix. For a finding
        # with a saved baseline, require the original indexed callers to remain
        # resolvable and the mitigation evidence to point into those callers.
        if baseline_old_symbol is not None and (baseline_old_symbol.name, baseline_old_symbol.file) not in symbols_by_name_file:
            current_caller_ids = {
                str(symbols_by_name_file[(prior.name, prior.file)].get("id"))
                for prior in baseline_callers if (prior.name, prior.file) in symbols_by_name_file
            }
            all_callers_resolved = bool(baseline_callers) and len(current_caller_ids) == len(baseline_callers)
            reachable_ids = {
                str(relation.get("target_symbol_id") or "")
                for relation in current_relations
                if str(relation.get("relation_kind") or "").upper() in {"TASK_ENTRY", "ISR_ENTRY"}
                and relation.get("relation_state") == "OBSERVED"
                and relation.get("target_symbol_id")
            }
            changed = True
            while changed:
                changed = False
                for relation in current_relations:
                    if (
                        str(relation.get("relation_kind") or "").upper() in {"CALL", "CALLS"}
                        and relation.get("relation_state") == "OBSERVED"
                        and str(relation.get("source_symbol_id") or "") in reachable_ids
                        and relation.get("target_symbol_id")
                    ):
                        target_id = str(relation["target_symbol_id"])
                        if target_id not in reachable_ids:
                            reachable_ids.add(target_id)
                            changed = True
            all_callers_reachable = bool(current_caller_ids) and current_caller_ids.issubset(reachable_ids)
            caller_evidence = any(
                (symbol := symbols_by_name_file.get((item.symbol or "", item.file))) is not None
                and str(symbol.get("id")) in current_caller_ids
                for item in valid_mitigations
            )
            caller_redirects = any(
                str(relation.get("source_symbol_id") or "") in current_caller_ids
                and str(relation.get("relation_kind") or "").upper() in {"CALL", "CALLS", "TASK_ENTRY", "ISR_ENTRY"}
                for relation in current_relations
            )
            if not all_callers_resolved or not all_callers_reachable or not caller_evidence or caller_redirects:
                reasons.append("The removed function's original callers do not provide complete current evidence that the path was removed.")
        if reasons:
            verdict = "INCONCLUSIVE"
    if verdict != requested_verdict and not reasons:
        reasons.append("The requested verdict failed deterministic current-source validation.")
    summary = result.reasoning_summary
    if verdict != requested_verdict:
        summary = f"{summary} FirmSight validated {verdict} instead of {requested_verdict}: {reasons[-1]}".strip()[:1600]
    missing_context = list(result.missing_context)
    for reason in reasons:
        if reason not in missing_context and len(missing_context) < 12:
            missing_context.append(reason)
    return result.model_copy(update={
        "verdict": verdict,
        "reasoning_summary": summary,
        "mitigations_found": valid_mitigations,
        "remaining_failure_evidence": valid_remaining,
        "current_path_edges": validated_edges,
        "inspected_files": valid_inspected_files,
        "inspected_symbols": valid_inspected_symbols,
        "missing_context": missing_context,
        "model_verdict": requested_verdict,
        "validation_status": "DOWNGRADED" if verdict != requested_verdict else "VALIDATED",
        "validation_reasons": reasons[:12],
        **({"original_execution_path": finding.execution_path} if finding is not None else {}),
    })


def _evidence_terms(value: str) -> set[str]:
    """Normalize identifiers and prose into bounded lexical evidence tokens."""
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    terms = {
        term for token in re.findall(r"[a-z][a-z0-9_]{2,}", expanded.casefold())
        for term in token.split("_") if len(term) >= 4
    }
    return terms - {
        "this", "that", "with", "from", "when", "after", "before", "still", "current", "source",
        "function", "without", "could", "would", "their", "there", "same", "path", "evidence",
        "operation", "original", "failure", "condition", "proof", "safe", "unsafe", "remains",
        "remain", "added", "found", "line", "code", "entry", "point", "feature", "vulnerable",
        "acquired", "concrete", "result", "value", "valid", "actual", "shows", "only", "than",
        "then", "being", "because", "does", "from", "with", "after", "before", "current",
    }
