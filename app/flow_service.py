"""Deterministic, bounded firmware flow projections over indexed source facts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any

from .platform_schemas import (
    FlowEdgeRead, FlowIndexRead, FlowNodeRead, FlowResourceRead,
    FlowScenarioRead, SharedStateRead,
)
from .platform_repository import PlatformRepository


ENTRY_KINDS = {
    "APP_ENTRY", "TASK_ENTRY", "ISR_ENTRY", "CALLBACK_ENTRY",
    "EVENT_HANDLER_ENTRY", "TIMER_ENTRY", "REGISTERED_HANDLER",
}
CALL_KINDS = {"CALL", "CALLS"}
MAX_DEPTH = 10
MAX_PATHS_PER_ENTRY = 8
MAX_TOTAL_SCENARIOS = 128
MAX_EDGES = 1200
MAX_FANOUT = 24


def _stable_id(*parts: str) -> str:
    return "FLOW-" + hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:24].upper()


def _state(value: Any) -> str:
    state = str(value or "UNKNOWN").upper()
    return state if state in {"OBSERVED", "INFERRED", "UNKNOWN"} else "UNKNOWN"


class FlowService:
    """Build project scoped execution, asynchronous, resource and state flows.

    The service consumes indexed facts only. It never asks a model to resolve a
    call, resource identity, or entry point. Scenarios are inexpensive derived
    projections and are rebuilt against the current topology snapshot.
    """

    def __init__(self, repository: PlatformRepository) -> None:
        self.repository = repository
        self._cache: dict[tuple[str, str, str], FlowIndexRead] = {}
        self._source_cache: dict[tuple[str, str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}

    @staticmethod
    def _node(symbol: dict[str, Any], *, kind: str = "FUNCTION") -> FlowNodeRead:
        return FlowNodeRead(
            id=str(symbol["id"]), kind=kind, name=str(symbol.get("name") or symbol["id"]),
            symbol_id=str(symbol["id"]), file=symbol.get("file"),
            line=int(symbol.get("line_start") or 1), relation_state="OBSERVED",
        )

    @staticmethod
    def _edge(relation: dict[str, Any], source_id: str, target_id: str, kind: str | None = None) -> FlowEdgeRead:
        return FlowEdgeRead(
            id=str(relation.get("id") or _stable_id(source_id, target_id, str(kind or relation.get("relation_kind")))),
            source_id=source_id, target_id=target_id,
            kind=str(kind or relation.get("relation_kind") or "UNKNOWN"),
            relation_state=_state(relation.get("relation_state")),
            confidence=float(relation.get("confidence") or 0),
            file=relation.get("file"), line=relation.get("line"),
            metadata={str(k): str(v)[:240] for k, v in (relation.get("metadata") or {}).items() if v is not None},
        )

    def build(self, project_id: str, *, symbols: list[dict[str, Any]] | None = None,
              relations: list[dict[str, Any]] | None = None, snapshot: dict[str, Any] | None = None) -> FlowIndexRead:
        supplied_facts = symbols is not None or relations is not None or snapshot is not None
        snapshot = snapshot if snapshot is not None else (self.repository.topology_snapshot(project_id) or {})
        cache_key = (project_id, str(snapshot.get("source_snapshot_hash") or ""), str(snapshot.get("relation_fingerprint") or ""))
        if not supplied_facts and cache_key in self._cache:
            return self._cache[cache_key]
        symbols = symbols if symbols is not None else self.repository.list_indexed_symbols(project_id)
        relations = relations if relations is not None else self.repository.list_source_relations(project_id)
        by_id = {str(item["id"]): item for item in symbols}
        calls: dict[str, list[dict[str, Any]]] = defaultdict(list)
        data_facts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        queue_operations: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"send": [], "receive": []})
        entries: list[dict[str, Any]] = []
        unresolved: list[FlowEdgeRead] = []
        unresolved_truncated = False
        call_overflow = False
        for relation in relations:
            kind = str(relation.get("relation_kind") or "").upper()
            source_id = str(relation.get("source_symbol_id") or "")
            target_id = str(relation.get("target_symbol_id") or "")
            state = _state(relation.get("relation_state"))
            if kind in ENTRY_KINDS and state == "OBSERVED" and target_id in by_id:
                entries.append(relation)
            elif kind in CALL_KINDS and state == "OBSERVED" and source_id in by_id and target_id in by_id:
                if len(calls[source_id]) < MAX_FANOUT + 1:
                    calls[source_id].append(relation)
                else:
                    call_overflow = True
            elif (kind in CALL_KINDS or state != "OBSERVED") and source_id in by_id:
                if len(unresolved) < 128:
                    unknown_id = _stable_id(project_id, "unresolved", str(relation.get("id")))
                    edge = self._edge(relation, source_id, unknown_id, "UNKNOWN_TARGET")
                    unresolved.append(edge.model_copy(update={"metadata": {**edge.metadata, "unresolved_target": str(relation.get("target_name") or "dynamic target")[:160]}}))
                else:
                    unresolved_truncated = True
            if source_id and kind in {"UNTRUSTED_INPUT", "VALIDATES", "DATA_SINK", "PARAMETER", "ASSIGNS", "PROPAGATES_ARGUMENT", "RETURNS_VALUE", "CAPTURES_RETURN", "READS", "WRITES", "CHANGES_STATE", "ALLOCATES", "RELEASES"} and len(data_facts[source_id]) < 64:
                data_facts[source_id].append(relation)
            metadata = relation.get("metadata") or {}
            resource = str(metadata.get("resource") or "")
            operation = str(metadata.get("operation") or "")
            if resource and state == "OBSERVED" and operation in {"QUEUE_SEND", "QUEUE_RECEIVE"}:
                bucket = "send" if operation == "QUEUE_SEND" else "receive"
                if len(queue_operations[resource][bucket]) < 64:
                    queue_operations[resource][bucket].append(relation)
        for values in calls.values():
            values.sort(key=lambda rel: (str(rel.get("file") or ""), int(rel.get("line") or 0), str(rel.get("target_symbol_id") or "")))
        entries.sort(key=lambda rel: (str(rel.get("relation_kind") or ""), str(rel.get("file") or ""), int(rel.get("line") or 0), str(rel.get("target_symbol_id") or "")))

        scenarios: list[FlowScenarioRead] = []
        truncated = len(relations) > MAX_EDGES or len(entries) > MAX_TOTAL_SCENARIOS or call_overflow or unresolved_truncated
        remaining_edge_budget = MAX_EDGES
        reachable_ids: set[str] = set()
        bounded_entries = entries[:MAX_TOTAL_SCENARIOS]
        reach_queue = [(str(entry.get("target_symbol_id")), 0) for entry in bounded_entries]
        queued_ids = {symbol_id for symbol_id, _ in reach_queue}
        reach_visits = 0
        while reach_queue and reach_visits < 4096:
            current_id, depth = reach_queue.pop(0)
            queued_ids.discard(current_id)
            if current_id in reachable_ids or current_id not in by_id:
                continue
            reachable_ids.add(current_id)
            reach_visits += 1
            if depth < MAX_DEPTH:
                for rel in calls.get(current_id, [])[:MAX_FANOUT]:
                    target_id = str(rel.get("target_symbol_id") or "")
                    if target_id in by_id and target_id not in reachable_ids and target_id not in queued_ids:
                        reach_queue.append((target_id, depth + 1))
                        queued_ids.add(target_id)
        if reach_queue:
            truncated = True
        for entry in bounded_entries:
            if len(scenarios) >= MAX_TOTAL_SCENARIOS or remaining_edge_budget <= 0:
                truncated = True
                break
            root_id = str(entry.get("target_symbol_id"))
            root = by_id[root_id]
            entry_kind = str(entry.get("relation_kind"))
            entry_node_id = _stable_id(project_id, "entry", entry_kind, root_id, str(entry.get("id")))
            entry_node = FlowNodeRead(id=entry_node_id, kind=entry_kind, name=str(root.get("name") or root_id), symbol_id=root_id,
                                      file=entry.get("file"), line=entry.get("line"), relation_state="OBSERVED")
            first = self._edge(entry, entry_node_id, root_id)
            queue: list[tuple[str, list[str], list[FlowEdgeRead], frozenset[str], int]] = [(root_id, [root_id], [first], frozenset({root_id}), 0)]
            emitted = 0
            while queue and emitted < MAX_PATHS_PER_ENTRY and len(scenarios) < MAX_TOTAL_SCENARIOS and remaining_edge_budget > 0:
                current_id, path_ids, path_edges, seen, depth = queue.pop(0)
                if len(path_edges) > remaining_edge_budget:
                    truncated = True
                    break
                next_edges = calls.get(current_id, [])[:MAX_FANOUT]
                if len(calls.get(current_id, [])) > MAX_FANOUT:
                    truncated = True
                extended = False
                if depth < MAX_DEPTH:
                    for relation in next_edges:
                        target_id = str(relation.get("target_symbol_id") or "")
                        if target_id not in by_id or target_id in seen:
                            continue
                        extended = True
                        edge = self._edge(relation, current_id, target_id)
                        queue.append((target_id, path_ids + [target_id], path_edges + [edge], seen | {target_id}, depth + 1))
                        if len(queue) + emitted >= MAX_PATHS_PER_ENTRY:
                            truncated = True
                            break
                else:
                    truncated = truncated or bool(next_edges)
                if extended:
                    continue
                scenario_unresolved = [edge for edge in unresolved if edge.source_id in path_ids][:16]
                nodes = [entry_node] + [self._node(by_id[node_id]) for node_id in path_ids]
                nodes.extend(FlowNodeRead(
                    id=edge.target_id, kind="UNKNOWN_TARGET",
                    name=edge.metadata.get("unresolved_target") or "dynamic target",
                    relation_state=edge.relation_state, file=edge.file, line=edge.line,
                ) for edge in scenario_unresolved)
                async_edges = self._async_edges(project_id, path_ids, queue_operations, reachable_ids)
                known_node_ids = {node.id for node in nodes}
                for edge in async_edges:
                    function_id = edge.source_id if edge.kind == "PUBLISHES_TO_QUEUE" else edge.target_id
                    async_symbol = by_id.get(function_id)
                    if async_symbol and function_id not in known_node_ids:
                        async_kind = "QUEUE_PRODUCER" if edge.kind == "PUBLISHES_TO_QUEUE" else "QUEUE_CONSUMER"
                        nodes.append(self._node(async_symbol, kind=async_kind))
                        known_node_ids.add(function_id)
                    resource_node_id = edge.target_id if edge.kind == "PUBLISHES_TO_QUEUE" else edge.source_id
                    resource_name = edge.metadata.get("resource")
                    if resource_node_id not in known_node_ids and resource_name:
                        nodes.append(FlowNodeRead(id=resource_node_id, kind="QUEUE", name=resource_name, relation_state="OBSERVED"))
                        known_node_ids.add(resource_node_id)
                data_edges = self._data_edges(project_id, path_ids, data_facts)
                scenario_edges = path_edges
                available = max(0, remaining_edge_budget - len(scenario_edges))
                if len(data_edges) > available:
                    data_edges = data_edges[:available]
                    truncated = True
                available -= len(data_edges)
                if len(async_edges) > available:
                    async_edges = async_edges[:available]
                    truncated = True
                remaining_edge_budget -= len(scenario_edges) + len(data_edges) + len(async_edges)
                names = [str(by_id[node_id].get("name") or node_id) for node_id in path_ids]
                path_key = "/".join(path_ids)
                scenario_id = _stable_id(project_id, entry_kind, entry_node_id, path_key)
                scenarios.append(FlowScenarioRead(
                    id=scenario_id, project_id=project_id, entry_kind=entry_kind,
                    entry_symbol_id=root_id, entry_name=str(root.get("name") or root_id),
                    title=f"Flow from {root.get('name') or root_id}", nodes=nodes,
                    edges=scenario_edges, data_edges=data_edges, async_edges=async_edges,
                    unresolved_edges=scenario_unresolved,
                    path=names, max_depth_reached=depth >= MAX_DEPTH,
                    confidence=min([edge.confidence for edge in scenario_edges] or [1.0]),
                    source_snapshot_hash=str(snapshot.get("source_snapshot_hash") or ""),
                    topology_fingerprint=str(snapshot.get("relation_fingerprint") or ""),
                ))
                emitted += 1
            if queue:
                truncated = True

        resources = self._resources(project_id, relations, by_id, edge_budget=remaining_edge_budget)
        remaining_edge_budget -= sum(len(item.operations) for item in resources)
        shared_state = self._shared_state(project_id, relations, by_id, edge_budget=remaining_edge_budget)
        resource_fact_count = sum(
            1 for relation in relations
            if str((relation.get("metadata") or {}).get("resource") or "")
            and str(relation.get("relation_kind") or "") in {"CREATES_RESOURCE", "USES_RESOURCE", "PUBLISHES_TO_QUEUE", "RECEIVES_FROM_QUEUE"}
        )
        state_fact_count = sum(1 for relation in relations if relation.get("relation_kind") in {"READS", "WRITES"})
        if sum(len(item.operations) for item in resources) < resource_fact_count or sum(len(item.accesses) for item in shared_state) < state_fact_count:
            truncated = True
        result = FlowIndexRead(
            project_id=project_id, scenarios=scenarios,
            resources=resources, shared_state=shared_state,
            unresolved_edges=unresolved[:128], truncated=truncated,
            source_snapshot_hash=str(snapshot.get("source_snapshot_hash") or ""),
            topology_fingerprint=str(snapshot.get("relation_fingerprint") or ""),
        )
        if not supplied_facts:
            self._cache = {cache_key: result}
            self._source_cache = {cache_key: (symbols, relations)}
        return result

    def _async_edges(self, project_id: str, path_ids: list[str], resources: dict[str, dict[str, list[dict[str, Any]]]], reachable_ids: set[str]) -> list[FlowEdgeRead]:
        path_set = set(path_ids)
        result: list[FlowEdgeRead] = []
        for identity, pair in sorted(resources.items()):
            connected = [item for item in (*pair["send"], *pair["receive"]) if str(item.get("source_symbol_id") or "") in path_set]
            if not connected:
                continue
            queue_id = _stable_id(project_id, "queue", identity)
            # Once an entry path reaches either side, expose the statically
            # matched counterpart(s) so the scenario bridges the asynchronous
            # boundary. Identity matching is exact; ambiguous handles never
            # reach this point because they have no resource metadata.
            for relation in [item for item in pair["send"] if str(item.get("source_symbol_id") or "") in reachable_ids][:4]:
                result.append(self._edge(relation, str(relation["source_symbol_id"]), queue_id, "PUBLISHES_TO_QUEUE"))
            for relation in [item for item in pair["receive"] if str(item.get("source_symbol_id") or "") in reachable_ids][:4]:
                result.append(self._edge(relation, queue_id, str(relation["source_symbol_id"]), "RECEIVES_FROM_QUEUE"))
        return result[:32]

    def _data_edges(self, project_id: str, path_ids: list[str], relations_by_source: dict[str, list[dict[str, Any]]]) -> list[FlowEdgeRead]:
        result = []
        for source_id in path_ids:
            for relation in relations_by_source.get(source_id, []):
                kind = str(relation.get("relation_kind") or "")
                target_id = str(relation.get("target_symbol_id") or "")
                if not target_id:
                    target = str(relation.get("target_name") or (relation.get("metadata") or {}).get("variable") or kind)
                    target_id = _stable_id(project_id, "fact", kind, target, str(relation.get("file") or ""), str(relation.get("line") or ""))
                result.append(self._edge(relation, source_id, target_id))
                if len(result) >= 64:
                    return result
        return result

    def _resources(self, project_id: str, relations: list[dict[str, Any]], by_id: dict[str, dict[str, Any]], *, edge_budget: int) -> list[FlowResourceRead]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relation in relations:
            metadata = relation.get("metadata") or {}
            identity = str(metadata.get("resource") or "")
            if identity and str(relation.get("relation_kind")) in {"CREATES_RESOURCE", "USES_RESOURCE", "PUBLISHES_TO_QUEUE", "RECEIVES_FROM_QUEUE"}:
                grouped[identity].append(relation)
        result = []
        remaining = max(0, edge_budget)
        for identity, items in sorted(grouped.items())[:128]:
            if remaining <= 0:
                break
            selected = items[:min(64, remaining)]
            operations = [self._edge(item, str(item.get("source_symbol_id") or "unknown"), _stable_id(project_id, "resource", identity)) for item in selected]
            remaining -= len(operations)
            functions = sorted({str(by_id.get(str(item.get("source_symbol_id") or ""), {}).get("name") or "unknown") for item in items})[:64]
            apis = {str((item.get("metadata") or {}).get("api") or "") for item in items}
            kind = (
                "QUEUE" if any(api.startswith("xQueue") for api in apis)
                else "EVENT_GROUP" if any(api.startswith("xEventGroup") for api in apis)
                else "MUTEX" if any("Mutex" in api for api in apis)
                else "SEMAPHORE" if any("Semaphore" in api for api in apis)
                else "RESOURCE"
            )
            result.append(FlowResourceRead(identity=identity, kind=kind, operations=operations, functions=functions))
        return result

    def _shared_state(self, project_id: str, relations: list[dict[str, Any]], by_id: dict[str, dict[str, Any]], *, edge_budget: int) -> list[SharedStateRead]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relation in relations:
            if relation.get("relation_kind") in {"READS", "WRITES"}:
                name = str((relation.get("metadata") or {}).get("variable") or relation.get("target_name") or "")
                if name: grouped[name].append(relation)
        result = []
        remaining = max(0, edge_budget)
        for name, items in sorted(grouped.items())[:128]:
            if remaining <= 0:
                break
            reads = sorted({str(by_id.get(str(item.get("source_symbol_id") or ""), {}).get("name") or "unknown") for item in items if item.get("relation_kind") == "READS"})
            writes = sorted({str(by_id.get(str(item.get("source_symbol_id") or ""), {}).get("name") or "unknown") for item in items if item.get("relation_kind") == "WRITES"})
            selected = items[:min(64, remaining)]
            accesses = [self._edge(item, str(item.get("source_symbol_id") or "unknown"), _stable_id(project_id, "state", name)) for item in selected]
            remaining -= len(accesses)
            result.append(SharedStateRead(name=name, readers=reads[:64], writers=writes[:64], accesses=accesses))
        return result

    def context_for_symbols(self, project_id: str, names: list[str], *, cap: int = 16) -> str:
        """Return concise scenarios and source-derived relations near symbols."""
        if not names:
            return "No selected function symbol for flow retrieval."
        index = self.build(project_id)
        selected = set(names[:24])
        lines: list[str] = []
        relevant_ids: set[str] = set()
        for scenario in index.scenarios:
            if not selected.intersection(scenario.path):
                continue
            path = " -> ".join(scenario.path)
            lines.append(f"{scenario.entry_kind} {scenario.entry_name}: {path} [confidence={scenario.confidence:.2f}]")
            relevant_ids.update(node.symbol_id for node in scenario.nodes if node.symbol_id)
            for edge in scenario.async_edges[:4]:
                lines.append(f"ASYNC {edge.kind} {edge.file or ''}:{edge.line or ''} {edge.metadata.get('resource', '')} [{edge.relation_state}]")
            for edge in scenario.unresolved_edges[:4]:
                lines.append(f"UNRESOLVED {edge.kind} at {edge.file or ''}:{edge.line or ''} [{edge.relation_state}]")
        cache_key = (project_id, index.source_snapshot_hash, index.topology_fingerprint)
        cached_source = self._source_cache.get(cache_key)
        symbols, relations = cached_source if cached_source is not None else (
            self.repository.list_indexed_symbols(project_id), self.repository.list_source_relations(project_id),
        )
        by_id = {str(item["id"]): item for item in symbols}
        relevant_ids.update(str(item["id"]) for item in symbols if item.get("name") in selected)
        fact_count = 0
        for relation in relations:
            if str(relation.get("source_symbol_id") or "") not in relevant_ids:
                continue
            kind = str(relation.get("relation_kind") or "")
            if kind not in {"READS", "WRITES", "CHANGES_STATE", "VALIDATES", "PARAMETER", "ASSIGNS", "PROPAGATES_ARGUMENT", "RETURNS_VALUE", "CAPTURES_RETURN", "DATA_SINK", "UNTRUSTED_INPUT", "CREATES_RESOURCE", "USES_RESOURCE", "PUBLISHES_TO_QUEUE", "RECEIVES_FROM_QUEUE", "ALLOCATES", "RELEASES"}:
                continue
            source_name = str(by_id.get(str(relation.get("source_symbol_id") or ""), {}).get("name") or "unknown")
            metadata = relation.get("metadata") or {}
            detail = ", ".join(f"{key}={str(value)[:80]}" for key, value in sorted(metadata.items()) if value)
            lines.append(f"FACT {source_name} {kind} {relation.get('target_name') or ''} {detail} @ {relation.get('file')}:{relation.get('line')} [{_state(relation.get('relation_state'))}]")
            fact_count += 1
            if fact_count >= 48:
                break
        if not lines:
            lines.append("No bounded observed entry-rooted scenario reaches the selected symbols.")
        return "\n".join(lines[:cap])[:5000]

    def paths_to_symbol(self, project_id: str, symbol_name: str, cap: int = 32, file: str | None = None) -> list[FlowScenarioRead]:
        index = self.build(project_id)
        return [
            scenario for scenario in index.scenarios
            if any(
                node.name == symbol_name and node.kind in {"FUNCTION", "QUEUE_PRODUCER", "QUEUE_CONSUMER"}
                and (file is None or node.file == file)
                for node in scenario.nodes
            )
        ][:max(1, min(cap, 64))]
