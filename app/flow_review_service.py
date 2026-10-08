"""Scenario-first review planning over deterministic indexed firmware flows."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any

from .flow_service import FlowService
from .platform_repository import PlatformRepository
from .platform_schemas import FlowReviewSourceSegment, FlowReviewUnit, FlowScenarioRead


MAX_FLOW_UNITS = 384
MAX_SYMBOLS_PER_UNIT = 5
MAX_SOURCE_CHARS_PER_UNIT = 10_000
MAX_SEGMENT_CHARS = 5_800
MAX_FACTS_PER_UNIT = 48
MAX_ASYNC_HOPS = 1
MAX_ASYNC_CONSUMERS = 4


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(*parts: str) -> str:
    return "FRU-" + _digest("\0".join(parts))[:24].upper()


class FlowReviewPlanner:
    """Build bounded FLOW-first units and fallback units for uncovered source.

    The planner consumes FlowService scenarios and indexed symbol locations; it
    does not resolve calls or resource identities itself. Request-time source is
    attached only to the returned units and is not persisted in scenario data.
    """

    def __init__(self, repository: PlatformRepository, flow_service: FlowService) -> None:
        self.repository = repository
        self.flow_service = flow_service

    def plan(self, project_id: str, raw_files: list[dict[str, str]], *, source_budget: int = MAX_SOURCE_CHARS_PER_UNIT) -> list[FlowReviewUnit]:
        source_budget = max(2_000, min(MAX_SOURCE_CHARS_PER_UNIT, source_budget))
        files = {str(item["path"]): str(item["content"]) for item in raw_files if self._reviewable(item)}
        symbols = self.repository.list_indexed_symbols(project_id)
        symbols_by_id = {str(item["id"]): item for item in symbols if item.get("kind") == "function"}
        index = self.flow_service.build(project_id)
        units: list[FlowReviewUnit] = []
        covered: dict[str, list[tuple[int, int]]] = defaultdict(list)

        for scenario in index.scenarios:
            path_ids = self._scenario_path_ids(scenario)
            if not path_ids:
                path_ids = [node.symbol_id for node in scenario.nodes if node.symbol_id and node.kind == "FUNCTION"]
            path_ids = [item for item in dict.fromkeys(path_ids) if item in symbols_by_id]
            for window_start in range(0, len(path_ids), max(1, MAX_SYMBOLS_PER_UNIT - 1)):
                window = path_ids[window_start:window_start + MAX_SYMBOLS_PER_UNIT]
                if not window:
                    continue
                selected_edges = self._edges_for_symbols(scenario, window)
                selected_symbols = set(window)
                resource_edges = [edge for resource in index.resources for edge in resource.operations if edge.source_id in selected_symbols][:48]
                state_edges = [edge for state in index.shared_state for edge in state.accesses if edge.source_id in selected_symbols][:48]
                selected_edges = (selected_edges[0], selected_edges[1], selected_edges[2], [*resource_edges, *state_edges], selected_edges[4])
                emitted = self._build_units(
                    project_id, scenario, window, selected_edges, symbols_by_id, files,
                    source_budget, covered, unit_kind="FLOW" if len(path_ids) <= MAX_SYMBOLS_PER_UNIT else "FLOW_WINDOW",
                    title=scenario.title,
                )
                units.extend(emitted)
                if window_start + MAX_SYMBOLS_PER_UNIT >= len(path_ids):
                    break
                if len(units) >= MAX_FLOW_UNITS:
                    break
            if len(units) >= MAX_FLOW_UNITS:
                break

        # Continue only proven, exact-identity queue pairs. The two synchronous
        # portions remain separate CALLS edge chains around explicit async edges.
        if len(units) < MAX_FLOW_UNITS:
            units.extend(self._async_units(project_id, index.scenarios, symbols_by_id, files, source_budget, covered, MAX_FLOW_UNITS - len(units)))

        # Preserve complete source coverage. Only line ranges absent from all
        # observed flow packages go to explicitly unreachable/orphan units.
        units.extend(self._orphan_units(project_id, files, covered, index.source_snapshot_hash, index.topology_fingerprint, 8192))
        return units

    @staticmethod
    def _reviewable(file: dict[str, str]) -> bool:
        path, content = str(file["path"]).casefold(), str(file["content"])
        return not ("font" in path and len(content) > 32_000) and not (len(content) > 64_000 and content.count("0x") > 1_000)

    @staticmethod
    def _scenario_path_ids(scenario: FlowScenarioRead) -> list[str]:
        if not scenario.edges:
            return [scenario.entry_symbol_id]
        ids = [scenario.edges[0].source_id]
        for edge in scenario.edges:
            if not ids or edge.source_id == ids[-1]:
                ids.append(edge.target_id)
        if scenario.entry_symbol_id not in ids:
            ids.insert(0, scenario.entry_symbol_id)
        return list(dict.fromkeys(ids))

    @staticmethod
    def _edges_for_symbols(scenario: FlowScenarioRead, ids: list[str]) -> tuple[list, list, list, list, list]:
        selected = set(ids)
        execution = [edge for edge in scenario.edges if edge.source_id in selected and edge.target_id in selected][:64]
        data = [edge for edge in scenario.data_edges if edge.source_id in selected][:MAX_FACTS_PER_UNIT]
        resources = [edge for edge in scenario.data_edges if edge.source_id in selected and edge.kind in {"USES_RESOURCE", "CREATES_RESOURCE", "ALLOCATES", "RELEASES", "PUBLISHES_TO_QUEUE", "RECEIVES_FROM_QUEUE"}][:MAX_FACTS_PER_UNIT]
        async_edges = [edge for edge in scenario.async_edges if edge.source_id in selected or edge.target_id in selected][:32]
        unresolved = [edge for edge in scenario.unresolved_edges if edge.source_id in selected][:32]
        return execution, async_edges, data, resources, unresolved

    def _build_units(self, project_id: str, scenario: FlowScenarioRead, symbol_ids: list[str], edge_groups: tuple, symbols_by_id: dict[str, dict[str, Any]], files: dict[str, str], source_budget: int, covered: dict[str, list[tuple[int, int]]], *, unit_kind: str, title: str) -> list[FlowReviewUnit]:
        execution, async_edges, data_edges, resource_edges, unresolved = edge_groups
        chunks: list[FlowReviewSourceSegment] = []
        for symbol_id in symbol_ids:
            symbol = symbols_by_id.get(symbol_id)
            if not symbol:
                continue
            content = files.get(str(symbol.get("file") or ""))
            if content is None:
                continue
            chunks.extend(self._function_segments(symbol, content))

        result: list[FlowReviewUnit] = []
        source_pack: list[FlowReviewSourceSegment] = []
        source_chars = 0

        def flush() -> None:
            nonlocal source_pack, source_chars
            if not source_pack:
                return
            pack_ids = list(dict.fromkeys(item.symbol_id for item in source_pack if item.symbol_id))
            pack_files = list(dict.fromkeys(item.file for item in source_pack))
            material = {
                "project": project_id, "scenario": scenario.id, "symbols": pack_ids,
                "segments": [(item.file, item.line_start, item.line_end, item.content_hash) for item in source_pack],
                "async": [edge.id for edge in async_edges], "topology": scenario.topology_fingerprint,
            }
            fingerprint = _digest(json.dumps(material, sort_keys=True))
            unit_id = _stable_id(project_id, scenario.id, fingerprint)
            chosen = set(pack_ids)
            result.append(FlowReviewUnit(
                id=unit_id, project_id=project_id, unit_kind=unit_kind, scenario_id=scenario.id,
                title=title[:480], entry_kind=scenario.entry_kind,
                entry_symbol_id=scenario.entry_symbol_id, entry_name=scenario.entry_name,
                symbol_ids=pack_ids, files=pack_files, source_segments=source_pack,
                execution_edges=[edge for edge in execution if edge.source_id in chosen and edge.target_id in chosen][:64],
                async_edges=async_edges[:32], data_edges=[edge for edge in data_edges if edge.source_id in chosen][:64],
                resource_edges=[edge for edge in resource_edges if edge.source_id in chosen][:64],
                unresolved_edges=[edge for edge in unresolved if edge.source_id in chosen][:32],
                source_snapshot_hash=scenario.source_snapshot_hash, topology_fingerprint=scenario.topology_fingerprint,
                flow_fingerprint=fingerprint, truncated=scenario.max_depth_reached or bool(unresolved),
                confidence=scenario.confidence, covered_source_segments=len(source_pack),
            ))
            for segment in source_pack:
                covered[segment.file].append((segment.start_offset, segment.end_offset))
            source_pack, source_chars = [], 0

        for segment in chunks:
            if source_pack and source_chars + len(segment.content) > source_budget:
                flush()
            source_pack.append(segment)
            source_chars += len(segment.content)
        flush()
        return result

    @staticmethod
    def _function_segments(symbol: dict[str, Any], content: str) -> list[FlowReviewSourceSegment]:
        name = re.escape(str(symbol.get("name") or ""))
        masked = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*', lambda match: " " * len(match.group(0)), content, flags=re.S)
        span = None
        for match in re.finditer(rf"\b{name}\s*\(", masked):
            opening = masked.find("(", match.start())
            close = FlowReviewPlanner._matching(masked, opening, "(", ")")
            if close is None:
                continue
            tail = masked[close + 1:close + 221]
            opening = re.match(r"\s*(?:(?:const|volatile|noexcept)(?:\s*\([^)]*\))?|->\s*[^{};]+)*\s*\{", tail)
            if not opening:
                continue
            body_open = close + 1 + opening.start() + opening.group(0).rfind("{")
            body_close = FlowReviewPlanner._matching(masked, body_open, "{", "}")
            if body_close is None:
                continue
            prefix_start = max(masked.rfind("\n", 0, match.start()), masked.rfind(";", 0, match.start()), masked.rfind("}", 0, match.start())) + 1
            prefix = masked[prefix_start:match.start()]
            if "=" in prefix:
                continue
            span = (prefix_start, body_close + 1)
            break
        if span is None:
            # If the source parser recorded a declaration shape that cannot be
            # reconstructed, retain only its indexed line range conservatively.
            lines = content.splitlines(keepends=True)
            line_start = max(1, int(symbol.get("line_start") or 1))
            line_end = min(len(lines), int(symbol.get("line_end") or line_start))
            start_offset = sum(len(line) for line in lines[:line_start - 1])
            end_offset = sum(len(line) for line in lines[:line_end])
            span = (start_offset, end_offset)
        start_offset, end_offset = span
        result = []
        for offset in range(start_offset, end_offset, MAX_SEGMENT_CHARS):
            end = min(end_offset, offset + MAX_SEGMENT_CHARS)
            text = content[offset:end]
            result.append(FlowReviewSourceSegment(
                file=str(symbol["file"]), line_start=content.count("\n", 0, offset) + 1,
                line_end=max(1, content.count("\n", 0, end) + (1 if end == len(content) or content[end - 1:end] != "\n" else 0)),
                start_offset=offset, end_offset=end, content_hash=_digest(text), content=text,
                symbol_id=str(symbol["id"]), symbol=str(symbol["name"]),
            ))
        return result

    @staticmethod
    def _matching(text: str, start: int, opening: str, closing: str) -> int | None:
        depth = 0
        for index in range(start, len(text)):
            if text[index] == opening:
                depth += 1
            elif text[index] == closing:
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _async_units(self, project_id: str, scenarios: list[FlowScenarioRead], symbols: dict[str, dict[str, Any]], files: dict[str, str], source_budget: int, covered: dict[str, list[tuple[int, int]]], remaining: int) -> list[FlowReviewUnit]:
        result: list[FlowReviewUnit] = []
        if MAX_ASYNC_HOPS < 1:
            return result
        for producer in scenarios:
            sends = [edge for edge in producer.async_edges if edge.kind == "PUBLISHES_TO_QUEUE" and edge.relation_state == "OBSERVED" and edge.metadata.get("resource")]
            for send in sends:
                matching = [(scenario, edge) for scenario in scenarios for edge in scenario.async_edges if edge.kind == "RECEIVES_FROM_QUEUE" and edge.relation_state == "OBSERVED" and edge.metadata.get("resource") == send.metadata.get("resource")]
                for consumer_scenario, receive in matching[:MAX_ASYNC_CONSUMERS]:
                    if send.source_id == receive.target_id or send.metadata.get("resource") != receive.metadata.get("resource"):
                        continue
                    consumer_path = self._scenario_path_ids(consumer_scenario)
                    if receive.target_id not in consumer_path:
                        continue
                    producer_path = self._scenario_path_ids(producer)
                    consumer_tail = consumer_path[consumer_path.index(receive.target_id):]
                    combined = list(dict.fromkeys([*producer_path, *consumer_tail]))
                    if len(combined) == len(producer_path) and not consumer_tail:
                        continue
                    # This v1 planner follows one queue hop. If the consumer's
                    # indexed path publishes to another queue that returns to a
                    # visited function, expose that a loop was bounded here.
                    async_limit_reached = False
                    consumer_ids = set(consumer_tail)
                    for next_send in consumer_scenario.async_edges:
                        if next_send.kind != "PUBLISHES_TO_QUEUE" or next_send.source_id not in consumer_ids:
                            continue
                        for future in scenarios:
                            future_receivers = [edge for edge in future.async_edges if edge.kind == "RECEIVES_FROM_QUEUE" and edge.relation_state == "OBSERVED" and edge.metadata.get("resource") == next_send.metadata.get("resource")]
                            if future_receivers:
                                async_limit_reached = True
                                if any(edge.target_id in set(combined) for edge in future_receivers):
                                    break
                        if async_limit_reached:
                            break
                    # Composite units retain each synchronous edge chain and the
                    # exact queue boundary as separate typed edges.
                    composite = producer.model_copy(update={
                        "id": _stable_id(project_id, producer.id, consumer_scenario.id, str(send.metadata.get("resource"))),
                        "title": f"Async flow: {producer.entry_name} to {consumer_scenario.entry_name}",
                        "edges": [*producer.edges, *[edge for edge in consumer_scenario.edges if edge.source_id in set(consumer_tail) and edge.target_id in set(consumer_tail)]],
                        "async_edges": [send, receive], "data_edges": [*producer.data_edges, *consumer_scenario.data_edges],
                        "unresolved_edges": [*producer.unresolved_edges, *consumer_scenario.unresolved_edges],
                        "path": [symbols.get(item, {}).get("name", item) for item in combined],
                        "max_depth_reached": producer.max_depth_reached or consumer_scenario.max_depth_reached or async_limit_reached,
                        "confidence": min(producer.confidence, consumer_scenario.confidence),
                    })
                    execution = [*producer.edges, *[edge for edge in consumer_scenario.edges if edge.source_id in set(consumer_tail) and edge.target_id in set(consumer_tail)]]
                    groups = (execution, [send, receive], [*producer.data_edges, *consumer_scenario.data_edges], [], [*producer.unresolved_edges, *consumer_scenario.unresolved_edges])
                    result.extend(self._build_units(project_id, composite, combined, groups, symbols, files, source_budget, covered, unit_kind="FLOW_WINDOW", title=composite.title))
                    remaining -= 1
                    if remaining <= 0:
                        return result
        return result

    def _orphan_units(self, project_id: str, files: dict[str, str], covered: dict[str, list[tuple[int, int]]], snapshot_hash: str, topology_fingerprint: str, remaining: int) -> list[FlowReviewUnit]:
        source_segments: list[FlowReviewSourceSegment] = []
        for path, content in sorted(files.items()):
            ranges = sorted(covered.get(path, []))
            merged: list[tuple[int, int]] = []
            for start, end in ranges:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            gaps = []
            cursor = 0
            for start, end in merged:
                if start > cursor:
                    gaps.append((cursor, start))
                cursor = max(cursor, end)
            if cursor < len(content):
                gaps.append((cursor, len(content)))
            for gap_start, gap_end in gaps:
                for offset in range(gap_start, gap_end, MAX_SEGMENT_CHARS):
                    end_offset = min(gap_end, offset + MAX_SEGMENT_CHARS)
                    excerpt = content[offset:end_offset]
                    start_line = content.count("\n", 0, offset) + 1
                    end_line = max(start_line, content.count("\n", 0, end_offset) + (1 if end_offset == len(content) or content[end_offset - 1:end_offset] != "\n" else 0))
                    source_segments.append(FlowReviewSourceSegment(
                        file=path, line_start=start_line, line_end=end_line,
                        start_offset=offset, end_offset=end_offset,
                        content_hash=_digest(excerpt), content=excerpt,
                    ))
        result: list[FlowReviewUnit] = []
        pack: list[FlowReviewSourceSegment] = []
        pack_chars = 0

        def flush() -> None:
            nonlocal pack, pack_chars
            if not pack:
                return
            path = pack[0].file
            files_in_pack = list(dict.fromkeys(item.file for item in pack))
            material = [(item.file, item.start_offset, item.end_offset, item.content_hash) for item in pack]
            fingerprint = _digest(json.dumps([project_id, material, topology_fingerprint], separators=(",", ":")))
            result.append(FlowReviewUnit(
                id=_stable_id(project_id, "ORPHAN", fingerprint), project_id=project_id,
                unit_kind="ORPHAN_SOURCE", title=f"Source outside observed entry flows: {path}",
                files=files_in_pack, source_segments=pack, source_snapshot_hash=snapshot_hash,
                topology_fingerprint=topology_fingerprint, flow_fingerprint=fingerprint,
                confidence=0.0, covered_source_segments=len(pack),
            ))
            pack, pack_chars = [], 0

        for segment in source_segments:
            if pack and (pack[0].file != segment.file or pack_chars + len(segment.content) > MAX_SOURCE_CHARS_PER_UNIT):
                flush()
            pack.append(segment)
            pack_chars += len(segment.content)
        flush()
        return result[:remaining]
