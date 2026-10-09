"""Scenario-first review planning over deterministic indexed firmware flows."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from .flow_service import FlowService
from .platform_repository import PlatformRepository
from .platform_schemas import FlowReviewSourceSegment, FlowReviewUnit, FlowScenarioRead


MAX_FLOW_UNITS = 384
MAX_FALLBACK_UNITS = 256
MAX_TOTAL_REVIEW_UNITS = 640
MAX_SOURCE_CHARS_PER_UNIT = 30_000
MAX_SEGMENT_CHARS = 5_800
MAX_FACTS_PER_UNIT = 48
MAX_ASYNC_HOPS = 3
HARD_MAX_ASYNC_HOPS = 4
MAX_ASYNC_CONSUMERS_PER_HOP = 4
MAX_COMPOSITE_FLOW_DEPTH = 32
MAX_FILES_PER_UNIT = 16
MAX_SEGMENTS_PER_UNIT = 32


@dataclass(frozen=True)
class FlowReviewBudget:
    """Deterministic request budget shared by planning and cache identity."""

    context_chars: int
    request_overhead_chars: int
    source_chars: int
    metadata_chars: int
    max_symbols: int
    max_files: int
    max_segments: int
    overlap_symbols: int
    bucket: str


@dataclass(frozen=True)
class ReviewPlanTooLarge(Exception):
    """Safe metadata for a plan that cannot preserve full source coverage."""

    total_source_chars: int
    flow_units: int
    fallback_units_estimate: int
    max_units: int
    max_fallback_units: int


def derive_flow_review_budget(context_chars: int, investigator_output_tokens: int | str | None,
                              request_overhead_chars: int = 4_096) -> FlowReviewBudget:
    """Map configured context into stable source/symbol buckets.

    Context buckets reserve 4,096+ characters for schema/instructions and
    scenario facts, then assign the remainder according to measured target
    envelopes: 6.5k/10.5k/15.5k/22k/28.5k source characters. Output allowance
    shifts source by at most 600 chars (0.5 chars per token from the 2k default)
    because larger requested responses consume a small share of provider context.
    """
    context_buckets = (
        (12_000, 6_500, 5, 1), (18_000, 10_500, 6, 1),
        (24_000, 15_500, 8, 2), (32_000, 22_000, 10, 2),
        (42_000, 28_500, 12, 3),
    )
    normalized_context = max(12_000, min(42_000, int(context_chars or 42_000)))
    threshold, source_chars, max_symbols, overlap = context_buckets[-1]
    for candidate in context_buckets:
        if normalized_context <= candidate[0]:
            threshold, source_chars, max_symbols, overlap = candidate
            break
    output_tokens = investigator_output_tokens if isinstance(investigator_output_tokens, int) and not isinstance(investigator_output_tokens, bool) else 2_000
    output_bucket, output_adjustment = next(
        ((threshold, adjustment) for threshold, adjustment in ((1_200, 400), (1_600, 200), (2_000, 0), (2_400, -200), (3_200, -600)) if output_tokens <= threshold),
        (3_200, -600),
    )
    if output_tokens <= 1_200:
        output_bucket = 1_200
        output_adjustment = 400
    source_chars = max(5_000, min(MAX_SOURCE_CHARS_PER_UNIT, source_chars + output_adjustment))
    if output_tokens <= 1_200:
        max_symbols = max(4, max_symbols - 1)
    elif output_tokens >= 3_200:
        max_symbols = min(12, max_symbols + 1)
    request_overhead_chars = max(2_000, min(context_chars - 2_000, request_overhead_chars))
    metadata_chars = max(1_000, context_chars - request_overhead_chars - source_chars)
    return FlowReviewBudget(
        context_chars=context_chars, request_overhead_chars=request_overhead_chars,
        source_chars=source_chars, metadata_chars=metadata_chars,
        max_symbols=max_symbols, max_files=min(MAX_FILES_PER_UNIT, max(4, max_symbols + 2)),
        max_segments=min(MAX_SEGMENTS_PER_UNIT, max(12, max_symbols * 2)),
        overlap_symbols=min(overlap, max(1, max_symbols // 3)),
        bucket=f"ctx-{threshold}-out-{output_bucket}",
    )


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

    def __init__(self, repository: PlatformRepository, flow_service: FlowService,
                 *, max_async_hops: int = MAX_ASYNC_HOPS,
                 max_async_consumers_per_hop: int = MAX_ASYNC_CONSUMERS_PER_HOP) -> None:
        self.repository = repository
        self.flow_service = flow_service
        self.max_async_hops = max(0, min(HARD_MAX_ASYNC_HOPS, int(max_async_hops)))
        self.max_async_consumers_per_hop = max(1, min(8, int(max_async_consumers_per_hop)))

    def plan(self, project_id: str, raw_files: list[dict[str, str]], *, source_budget: int | None = None,
             budget: FlowReviewBudget | None = None) -> list[FlowReviewUnit]:
        budget = budget or derive_flow_review_budget(42_000, 2_000)
        if source_budget is not None:
            budget = replace(budget, source_chars=max(2_000, min(MAX_SOURCE_CHARS_PER_UNIT, source_budget)))
        source_budget = budget.source_chars
        files = {str(item["path"]): str(item["content"]) for item in raw_files if self._reviewable(item)}
        symbols = self.repository.list_indexed_symbols(project_id)
        symbols_by_id = {str(item["id"]): item for item in symbols if item.get("kind") == "function"}
        index = self.flow_service.build(project_id)
        units: list[FlowReviewUnit] = []
        covered: dict[str, list[tuple[int, int]]] = defaultdict(list)
        segments_by_symbol: dict[str, list[FlowReviewSourceSegment]] = {}
        for symbol_id, symbol in symbols_by_id.items():
            source = files.get(str(symbol.get("file") or ""))
            if source is not None:
                segments_by_symbol[symbol_id] = self._function_segments(symbol, source)

        for scenario in index.scenarios:
            path_ids = self._scenario_path_ids(scenario)
            if not path_ids:
                path_ids = [node.symbol_id for node in scenario.nodes if node.symbol_id and node.kind == "FUNCTION"]
            path_ids = [item for item in dict.fromkeys(path_ids) if item in symbols_by_id]
            windows = self._scenario_windows(path_ids, segments_by_symbol, budget)
            for window in windows:
                selected_edges = self._edges_for_symbols(scenario, window)
                selected_symbols = set(window)
                resource_edges = [edge for resource in index.resources for edge in resource.operations if edge.source_id in selected_symbols][:48]
                state_edges = [edge for state in index.shared_state for edge in state.accesses if edge.source_id in selected_symbols][:48]
                selected_edges = (selected_edges[0], selected_edges[1], selected_edges[2], [*resource_edges, *state_edges], selected_edges[4])
                emitted = self._build_units(
                    project_id, scenario, window, selected_edges, symbols_by_id, files,
                    source_budget, unit_kind="FLOW" if len(windows) == 1 and len(path_ids) <= budget.max_symbols else "FLOW_WINDOW",
                    title=scenario.title, budget=budget, segments_by_symbol=segments_by_symbol,
                )
                if len(emitted) > 1:
                    emitted = [item.model_copy(update={"unit_kind": "FLOW_WINDOW"}) for item in emitted]
                available = MAX_FLOW_UNITS - len(units)
                accepted = emitted[:available]
                units.extend(accepted)
                self._record_coverage(accepted, covered)
                if len(accepted) < len(emitted) or len(units) >= MAX_FLOW_UNITS:
                    break
            if len(units) >= MAX_FLOW_UNITS:
                break

        flow_unit_count = len(units)

        # Continue only exact, observed queue identities. This may add at most
        # three independently bounded queue hops; static source remains the
        # only authority for each transition.
        if len(units) < MAX_FLOW_UNITS:
            composites = self._async_units(
                project_id, index.scenarios, index.resources, symbols_by_id, files,
                source_budget, MAX_FLOW_UNITS - len(units), budget, segments_by_symbol,
            )
            units.extend(composites)
            self._record_coverage(composites, covered)
        flow_unit_count = len(units)

        # Fallback compaction happens before the hard plan limit is enforced.
        # If compact units still exceed the cap, raise instead of dropping the
        # tail and falsely reporting complete review coverage.
        fallback_limit = min(MAX_FALLBACK_UNITS, MAX_TOTAL_REVIEW_UNITS - min(MAX_FLOW_UNITS, len(units)))
        orphan_units = self._orphan_units(
            project_id, files, covered, index.source_snapshot_hash,
            index.topology_fingerprint, source_budget, budget.max_files,
            budget.max_segments, fallback_limit, budget.bucket, flow_unit_count,
        )
        if len(units) + len(orphan_units) > MAX_TOTAL_REVIEW_UNITS:
            total_source_chars = sum(len(content) for content in files.values())
            raise ReviewPlanTooLarge(
                total_source_chars=total_source_chars, flow_units=flow_unit_count,
                fallback_units_estimate=len(orphan_units), max_units=MAX_TOTAL_REVIEW_UNITS,
                max_fallback_units=MAX_FALLBACK_UNITS,
            )
        units.extend(orphan_units)
        return units

    @staticmethod
    def _record_coverage(units: list[FlowReviewUnit], covered: dict[str, list[tuple[int, int]]]) -> None:
        for unit in units:
            for segment in unit.source_segments:
                covered[segment.file].append((segment.start_offset, segment.end_offset))

    @staticmethod
    def _scenario_windows(path_ids: list[str], segments_by_symbol: dict[str, list[FlowReviewSourceSegment]], budget: FlowReviewBudget) -> list[list[str]]:
        if not path_ids:
            return []
        windows: list[list[str]] = []
        start = 0
        while start < len(path_ids):
            chosen: list[str] = []
            source_chars = 0
            for symbol_id in path_ids[start:start + budget.max_symbols]:
                symbol_chars = sum(len(segment.content) for segment in segments_by_symbol.get(symbol_id, []))
                if chosen and source_chars + symbol_chars > budget.source_chars:
                    break
                chosen.append(symbol_id)
                source_chars += symbol_chars
            if not chosen:
                chosen = [path_ids[start]]
            windows.append(chosen)
            if chosen[-1] == path_ids[-1]:
                break
            end = start + len(chosen)
            overlap = min(budget.overlap_symbols, max(0, len(chosen) - 1))
            start = max(start + 1, end - overlap)
        return windows

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

    def _build_units(self, project_id: str, scenario: FlowScenarioRead, symbol_ids: list[str], edge_groups: tuple, symbols_by_id: dict[str, dict[str, Any]], files: dict[str, str], source_budget: int, *, unit_kind: str, title: str, budget: FlowReviewBudget, segments_by_symbol: dict[str, list[FlowReviewSourceSegment]] | None = None, cycle_bounded: bool = False, cycle_resource: str | None = None, async_hops: int = 0, omitted_async_consumers: int = 0, truncated_override: bool = False) -> list[FlowReviewUnit]:
        execution, async_edges, data_edges, resource_edges, unresolved = edge_groups
        chunks: list[FlowReviewSourceSegment] = []
        for symbol_id in symbol_ids:
            symbol = symbols_by_id.get(symbol_id)
            if not symbol:
                continue
            content = files.get(str(symbol.get("file") or ""))
            if content is None:
                continue
            chunks.extend((segments_by_symbol or {}).get(symbol_id) or self._function_segments(symbol, content))

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
                "budget": budget.bucket,
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
                flow_fingerprint=fingerprint, truncated=scenario.max_depth_reached or bool(unresolved) or truncated_override or cycle_bounded or omitted_async_consumers > 0,
                confidence=scenario.confidence, covered_source_segments=len(source_pack),
                cycle_bounded=cycle_bounded, cycle_resource=cycle_resource,
                async_hops=async_hops, omitted_async_consumers=omitted_async_consumers,
            ))
            source_pack, source_chars = [], 0

        for segment in chunks:
            source_files = {item.file for item in source_pack}
            if source_pack and (
                source_chars + len(segment.content) > source_budget
                or len(source_pack) >= budget.max_segments
                or (segment.file not in source_files and len(source_files) >= budget.max_files)
            ):
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

    def _async_units(self, project_id: str, scenarios: list[FlowScenarioRead], resources: list,
                     symbols: dict[str, dict[str, Any]], files: dict[str, str], source_budget: int,
                     remaining: int, budget: FlowReviewBudget,
                     segments_by_symbol: dict[str, list[FlowReviewSourceSegment]]) -> list[FlowReviewUnit]:
        if self.max_async_hops <= 0 or remaining <= 0:
            return []
        scenario_paths = {scenario.id: self._scenario_path_ids(scenario) for scenario in scenarios}
        scenarios_by_symbol: dict[str, list[FlowScenarioRead]] = defaultdict(list)
        for scenario in scenarios:
            for symbol_id in scenario_paths[scenario.id]:
                scenarios_by_symbol[symbol_id].append(scenario)
        resource_ops: dict[str, dict[str, list[FlowEdgeRead]]] = defaultdict(lambda: {"send": [], "receive": []})
        for resource in resources:
            if getattr(resource, "kind", None) != "QUEUE":
                continue
            for edge in resource.operations:
                if edge.relation_state != "OBSERVED":
                    continue
                operation = edge.metadata.get("operation")
                resource_name = resource.identity
                if operation == "QUEUE_SEND":
                    queue_edge = edge.model_copy(update={
                        "id": _stable_id(project_id, "queue-send", edge.id),
                        "kind": "PUBLISHES_TO_QUEUE", "metadata": {**edge.metadata, "resource": resource_name},
                    })
                    resource_ops[resource_name]["send"].append(queue_edge)
                elif operation == "QUEUE_RECEIVE":
                    queue_edge = edge.model_copy(update={
                        "id": _stable_id(project_id, "queue-receive", edge.id),
                        "source_id": edge.target_id, "target_id": edge.source_id,
                        "kind": "RECEIVES_FROM_QUEUE", "metadata": {**edge.metadata, "resource": resource_name},
                    })
                    resource_ops[resource_name]["receive"].append(queue_edge)
        for pair in resource_ops.values():
            pair["send"].sort(key=lambda edge: (edge.file or "", edge.line or 0, edge.source_id))
            pair["receive"].sort(key=lambda edge: (edge.file or "", edge.line or 0, edge.target_id))

        def call_edges_for(scenario: FlowScenarioRead, segment: list[str]) -> list[FlowEdgeRead]:
            allowed = set(segment)
            return [edge for edge in scenario.edges if edge.source_id in allowed and edge.target_id in allowed]

        def emit(state: dict[str, Any]) -> list[FlowReviewUnit]:
            if not state["async_edges"]:
                return []
            title = f"Async flow: {state['root'].entry_name}"
            scenario_id = _stable_id(project_id, "async", *state["scenario_ids"], *state["transition_ids"])
            composite = state["root"].model_copy(update={
                "id": scenario_id,
                "title": title,
                "edges": state["execution_edges"],
                "async_edges": state["async_edges"],
                "data_edges": state["data_edges"],
                "unresolved_edges": state["unresolved_edges"],
                "path": [symbols.get(item, {}).get("name", item) for item in state["symbol_ids"]],
                "max_depth_reached": bool(state["truncated"] or state["cycle_bounded"]),
                "confidence": min([state["root"].confidence, *state["confidences"]]),
            })
            selected = set(state["symbol_ids"])
            resource_edges = [
                edge for resource in resources for edge in resource.operations
                if edge.source_id in selected
            ][:MAX_FACTS_PER_UNIT]
            groups = (
                state["execution_edges"], state["async_edges"], state["data_edges"],
                resource_edges, state["unresolved_edges"],
            )
            return self._build_units(
                project_id, composite, state["symbol_ids"], groups, symbols, files,
                source_budget, unit_kind="FLOW_WINDOW", title=title, budget=budget,
                segments_by_symbol=segments_by_symbol,
                cycle_bounded=state["cycle_bounded"], cycle_resource=state["cycle_resource"],
                async_hops=state["async_hops"], omitted_async_consumers=state["omitted_consumers"],
                truncated_override=state["truncated"],
            )

        composites: dict[str, FlowReviewUnit] = {}

        def append_emit(state: dict[str, Any]) -> None:
            for unit in emit(state):
                previous = composites.get(unit.id)
                if previous is None:
                    composites[unit.id] = unit
                else:
                    composites[unit.id] = previous.model_copy(update={
                        "truncated": previous.truncated or unit.truncated,
                        "cycle_bounded": previous.cycle_bounded or unit.cycle_bounded,
                        "cycle_resource": previous.cycle_resource or unit.cycle_resource,
                        "omitted_async_consumers": max(previous.omitted_async_consumers, unit.omitted_async_consumers),
                    })

        for root in scenarios:
            root_path = scenario_paths[root.id]
            initial = {
                "root": root, "current_scenario": root, "current_segment": root_path,
                "symbol_ids": list(root_path), "visited_symbols": set(root_path),
                "visited_scenarios": {root.id}, "visited_transitions": set(),
                "scenario_ids": [root.id], "transition_ids": [],
                "execution_edges": list(root.edges), "data_edges": list(root.data_edges),
                "unresolved_edges": list(root.unresolved_edges), "async_edges": [],
                "confidences": [root.confidence], "async_hops": 0,
                "cycle_bounded": False, "cycle_resource": None,
                "omitted_consumers": 0, "truncated": bool(root.max_depth_reached),
            }
            stack = [initial]
            while stack and len(composites) < remaining:
                state = stack.pop()
                current_path = set(state["current_segment"])
                candidates: list[tuple[str, FlowEdgeRead, FlowEdgeRead, FlowScenarioRead, list[str]]] = []
                omitted_for_state = 0
                for resource_name, pair in sorted(resource_ops.items()):
                    sends = [edge for edge in pair["send"] if edge.source_id in current_path]
                    if not sends:
                        continue
                    receiver_by_symbol: dict[str, FlowEdgeRead] = {}
                    for receive_edge in pair["receive"]:
                        receiver_by_symbol.setdefault(receive_edge.target_id, receive_edge)
                    receivers = [receiver_by_symbol[key] for key in sorted(receiver_by_symbol)]
                    for send in sends:
                        matching_receivers = [edge for edge in receivers if edge.relation_state == "OBSERVED"]
                        if len(matching_receivers) > self.max_async_consumers_per_hop:
                            omitted_for_state += len(matching_receivers) - self.max_async_consumers_per_hop
                        for receive in matching_receivers[:self.max_async_consumers_per_hop]:
                            receiver_id = receive.target_id
                            for consumer in scenarios_by_symbol.get(receiver_id, []):
                                consumer_path = scenario_paths[consumer.id]
                                if receiver_id not in consumer_path:
                                    continue
                                tail = consumer_path[consumer_path.index(receiver_id):]
                                transition = (resource_name, send.source_id, receiver_id)
                                candidates.append((resource_name, send, receive, consumer, tail))
                candidates.sort(key=lambda item: (item[0], item[1].file or "", item[1].line or 0, item[3].id, item[2].file or "", item[2].line or 0))
                state["omitted_consumers"] += omitted_for_state
                expandable: list[dict[str, Any]] = []
                for resource_name, send, receive, consumer, tail in candidates:
                    transition = (resource_name, send.source_id, receive.target_id)
                    transition_id = "|".join(transition)
                    new_state = {key: (set(value) if isinstance(value, set) else list(value) if isinstance(value, list) else value) for key, value in state.items()}
                    depth = state["async_hops"] + 1
                    if depth > min(self.max_async_hops, HARD_MAX_ASYNC_HOPS):
                        state["truncated"] = True
                        append_emit(state)
                        continue
                    if transition in state["visited_transitions"]:
                        new_state["cycle_bounded"] = True
                        new_state["cycle_resource"] = resource_name
                        new_state["async_edges"] = [*state["async_edges"], send, receive]
                        new_state["transition_ids"] = [*state["transition_ids"], transition_id]
                        new_state["async_hops"] = state["async_hops"] + 1
                        append_emit(new_state)
                        continue
                    repeated = next((symbol_id for symbol_id in tail if symbol_id in state["visited_symbols"]), None)
                    if repeated is not None or consumer.id in state["visited_scenarios"]:
                        new_state["cycle_bounded"] = True
                        new_state["cycle_resource"] = resource_name
                        new_state["async_edges"] = [*state["async_edges"], send, receive]
                        new_state["transition_ids"] = [*state["transition_ids"], transition_id]
                        new_state["visited_transitions"].add(transition)
                        new_state["async_hops"] = state["async_hops"] + 1
                        append_emit(new_state)
                        continue
                    if len(state["symbol_ids"]) + len(tail) > MAX_COMPOSITE_FLOW_DEPTH:
                        new_state["truncated"] = True
                        append_emit(new_state)
                        continue
                    new_state["async_edges"] = [*state["async_edges"], send, receive]
                    new_state["symbol_ids"] = [*state["symbol_ids"], *tail]
                    new_state["visited_symbols"].update(tail)
                    new_state["visited_scenarios"].add(consumer.id)
                    new_state["visited_transitions"].add(transition)
                    new_state["scenario_ids"].append(consumer.id)
                    new_state["transition_ids"].append(transition_id)
                    new_state["execution_edges"] = [*state["execution_edges"], *call_edges_for(consumer, tail)]
                    selected = set(tail)
                    new_state["data_edges"] = [*state["data_edges"], *(edge for edge in consumer.data_edges if edge.source_id in selected)]
                    new_state["unresolved_edges"] = [*state["unresolved_edges"], *(edge for edge in consumer.unresolved_edges if edge.source_id in selected)]
                    new_state["confidences"].append(consumer.confidence)
                    new_state["current_scenario"] = consumer
                    new_state["current_segment"] = tail
                    new_state["async_hops"] = depth
                    new_state["truncated"] = state["truncated"] or consumer.max_depth_reached
                    expandable.append(new_state)
                if omitted_for_state:
                    state["truncated"] = True
                if expandable:
                    if len(expandable) > self.max_async_consumers_per_hop:
                        overflow_state = dict(state)
                        overflow_state["truncated"] = True
                        overflow_state["omitted_consumers"] += len(expandable) - self.max_async_consumers_per_hop
                        append_emit(overflow_state)
                    stack.extend(reversed(expandable[:self.max_async_consumers_per_hop]))
                elif state["async_hops"] > 0:
                    append_emit(state)
                if state["omitted_consumers"] and state["async_hops"] > 0:
                    overflow_state = dict(state)
                    overflow_state["truncated"] = True
                    append_emit(overflow_state)
            if len(composites) >= remaining:
                break
        return list(composites.values())[:remaining]

    def _orphan_units(self, project_id: str, files: dict[str, str], covered: dict[str, list[tuple[int, int]]], snapshot_hash: str, topology_fingerprint: str, source_budget: int, max_files: int, max_segments: int, remaining: int, budget_bucket: str, flow_units: int) -> list[FlowReviewUnit]:
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
        pack_count = 0
        count_chars = 0
        count_segments = 0
        count_files: set[str] = set()
        for segment in source_segments:
            if count_segments and (
                count_chars + len(segment.content) > source_budget
                or count_segments >= max_segments
                or (segment.file not in count_files and len(count_files) >= max_files)
            ):
                pack_count += 1
                count_chars, count_segments, count_files = 0, 0, set()
            count_chars += len(segment.content)
            count_segments += 1
            count_files.add(segment.file)
        if count_segments:
            pack_count += 1
        if pack_count > remaining:
            raise ReviewPlanTooLarge(
                total_source_chars=sum(len(content) for content in files.values()),
                flow_units=flow_units, fallback_units_estimate=pack_count,
                max_units=MAX_TOTAL_REVIEW_UNITS, max_fallback_units=MAX_FALLBACK_UNITS,
            )

        result: list[FlowReviewUnit] = []
        pack: list[FlowReviewSourceSegment] = []
        pack_chars = 0
        pack_files: set[str] = set()

        def flush() -> None:
            nonlocal pack, pack_chars, pack_files
            if not pack:
                return
            path = pack[0].file
            files_in_pack = list(dict.fromkeys(item.file for item in pack))
            material = [(item.file, item.start_offset, item.end_offset, item.content_hash) for item in pack]
            fingerprint = _digest(json.dumps([project_id, material, topology_fingerprint, budget_bucket], separators=(",", ":")))
            result.append(FlowReviewUnit(
                id=_stable_id(project_id, "ORPHAN", fingerprint), project_id=project_id,
                unit_kind="ORPHAN_SOURCE", title=f"Source outside observed entry flows: {path}",
                files=files_in_pack, source_segments=pack, source_snapshot_hash=snapshot_hash,
                topology_fingerprint=topology_fingerprint, flow_fingerprint=fingerprint,
                confidence=0.0, covered_source_segments=len(pack),
            ))
            pack, pack_chars, pack_files = [], 0, set()

        for segment in source_segments:
            if pack and (
                pack_chars + len(segment.content) > source_budget
                or len(pack) >= max_segments
                or (segment.file not in pack_files and len(pack_files) >= max_files)
            ):
                flush()
            pack.append(segment)
            pack_chars += len(segment.content)
            pack_files.add(segment.file)
        flush()
        if len(result) != pack_count:
            raise RuntimeError("fallback review coverage preflight disagreed with compacted unit count")
        return result
