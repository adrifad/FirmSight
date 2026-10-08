"""Conservative, source-backed memory lifetime facts for review context.

This module intentionally does not claim runtime heap behavior. It records the
smallest facts that can be established lexically from indexed functions and
keeps ownership escapes unresolved unless a stronger relation exists.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .indexer import (
    ALLOCATORS,
    RELEASERS,
    FirmwareIndexer,
    cached_mask_comments_and_strings,
    tokenize_masked_source,
)


@dataclass(frozen=True)
class MemoryLifetimeFact:
    project_id: str
    symbol_id: str
    symbol: str
    variable: str | None
    allocator: str
    file: str
    allocation_line: int
    release_lines: tuple[int, ...]
    exit_lines: tuple[int, ...]
    ownership_state: str
    evidence_hash: str
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeakCandidateSeed:
    project_id: str
    symbol_id: str
    symbol: str
    file: str
    allocation_line: int
    exit_line: int
    variable: str | None
    ownership_state: str
    evidence_hash: str
    path: tuple[str, ...]
    assumptions: tuple[str, ...] = ()


@dataclass
class LifetimeAnalysis:
    facts: list[MemoryLifetimeFact] = field(default_factory=list)
    seeds: list[LeakCandidateSeed] = field(default_factory=list)
    ownership_relations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _SourceEvent:
    line: int
    start: int
    end: int


class MemoryLifetimeService:
    """Analyze only explicit supported allocation/release APIs."""

    def analyze(
        self,
        project_id: str,
        files: list[dict[str, Any]],
        symbols: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        allocations: list[dict[str, Any]],
    ) -> LifetimeAnalysis:
        by_path = {item["path"]: item["content"] for item in files}
        function_symbols = {item["id"]: item for item in symbols if item.get("kind") == "function"}
        result = LifetimeAnalysis()
        for allocation in allocations:
            if allocation.get("event_kind") != "ALLOCATE" or allocation.get("allocator_or_releaser") not in ALLOCATORS:
                continue
            symbol = function_symbols.get(allocation.get("symbol_id"))
            content = by_path.get(allocation.get("file"))
            if not symbol or content is None:
                continue
            lines = content.splitlines()
            start = max(1, int(symbol.get("line_start") or 1))
            end = min(len(lines), int(symbol.get("line_end") or len(lines)))
            function_text = "\n".join(lines[start - 1:end])
            masked_text = cached_mask_comments_and_strings(function_text)
            allocation_line = int(allocation["line"])
            variable = allocation.get("variable")
            release_events = self._release_events(masked_text, start, allocation_line, variable, RELEASERS)
            release_lines = tuple(event.line for event in release_events)
            conditional_release_lines = tuple(
                event.line
                for event in release_events
                if self._event_is_conditional(masked_text, event.start)
            )
            unconditional_release_lines = tuple(
                event.line for event in release_events if event.line not in conditional_release_lines
            )
            exit_lines = tuple(self._exit_lines(masked_text, start, allocation_line))
            ownership_state, assumptions, escapes = self._ownership(
                masked_text,
                start,
                allocation_line,
                variable,
                bool(conditional_release_lines),
            )
            all_exits_covered = not exit_lines or all(
                any(release < exit for release in unconditional_release_lines)
                for exit in exit_lines
            )
            if escapes:
                ownership_state = escapes[0][0]
            elif conditional_release_lines and not all_exits_covered:
                ownership_state = "UNKNOWN"
                assumption = "A conditional release was observed, but branch coverage for every direct exit cannot be proven."
                if assumption not in assumptions:
                    assumptions.append(assumption)
            elif conditional_release_lines and not unconditional_release_lines:
                ownership_state = "UNKNOWN"
            elif all_exits_covered and (unconditional_release_lines or not exit_lines):
                ownership_state = "RELEASED"
            elif exit_lines:
                ownership_state = "UNBALANCED_EXIT"
            else:
                ownership_state = "UNKNOWN"
            evidence_hash = self._hash(project_id, allocation["file"], allocation_line, variable or "", ownership_state, ",".join(map(str, release_lines)), ",".join(map(str, exit_lines)))
            fact = MemoryLifetimeFact(project_id, symbol["id"], symbol["name"], variable, allocation["allocator_or_releaser"], allocation["file"], allocation_line, release_lines, exit_lines, ownership_state, evidence_hash, tuple(assumptions))
            result.facts.append(fact)
            allocation["ownership_state"] = ownership_state
            allocation.setdefault("metadata", {})["release_lines"] = ",".join(map(str, release_lines))
            allocation["metadata"]["exit_lines"] = ",".join(map(str, exit_lines))
            if escapes:
                for state, line, callee in escapes:
                    # FS-FIX-014: the relation identity must include the stable
                    # allocation/event identity, the owning symbol, and the
                    # allocation variable. Two distinct allocations passed to the
                    # same unknown callee on one physical line share
                    # state/file/line/callee but are different ownership events;
                    # omitting allocation identity made their ids collide and
                    # surfaced as a UNIQUE constraint failure on source_relations.id.
                    # Every input is a trusted indexed fact, so ids stay
                    # deterministic, project-scoped, and stable for unchanged
                    # source (no random UUIDs).
                    evidence_hash = self._hash(
                        project_id,
                        state,
                        allocation["file"],
                        line,
                        callee,
                        allocation.get("id", ""),
                        symbol["id"],
                        variable or "",
                    )
                    result.ownership_relations.append(
                        {
                            "id": FirmwareIndexer._stable_id(project_id, "relation", evidence_hash),
                            "project_id": project_id,
                            "relation_kind": state,
                            "source_symbol_id": symbol["id"],
                            "target_symbol_id": None,
                            "target_name": callee,
                            "file": allocation["file"],
                            "line": line,
                            "evidence_hash": evidence_hash,
                            "confidence": 1.0,
                            "relation_state": "OBSERVED",
                            "metadata": {"variable": variable or ""},
                        }
                    )
            # A seed is deliberately narrower than an UNBALANCED_EXIT fact:
            # there must be a concrete variable, an exit after allocation, no
            # ownership escape, and no release before that exit.
            if variable and ownership_state == "UNBALANCED_EXIT":
                for exit_line in exit_lines:
                    if not any(release < exit_line for release in release_lines):
                        seed_hash = self._hash(project_id, allocation["file"], allocation_line, exit_line, variable)
                        result.seeds.append(LeakCandidateSeed(project_id, symbol["id"], symbol["name"], allocation["file"], allocation_line, exit_line, variable, ownership_state, seed_hash, (symbol["name"], f"allocation@{allocation_line}", f"error_exit@{exit_line}"), tuple(assumptions)))
        unique: dict[str, LeakCandidateSeed] = {seed.evidence_hash: seed for seed in result.seeds}
        result.seeds = list(unique.values())
        return result

    @classmethod
    def _release_events(
        cls,
        text: str,
        start_line: int,
        allocation_line: int,
        variable: str | None,
        releasers: set[str],
    ) -> list[_SourceEvent]:
        if not variable:
            return []
        masked = cached_mask_comments_and_strings(text)
        function_releasers = sorted(
            item for item in releasers if item not in {"delete", "delete[]"}
        )
        events: list[_SourceEvent] = []
        if function_releasers:
            pattern = (
                r"\b(?:"
                + "|".join(re.escape(item) for item in function_releasers)
                + r")\s*\(\s*"
                + re.escape(variable)
                + r"\b"
            )
            events.extend(
                _SourceEvent(
                    start_line + masked[:match.start()].count("\n"),
                    match.start(),
                    match.end(),
                )
                for match in re.finditer(pattern, masked)
                if start_line + masked[:match.start()].count("\n") > allocation_line
            )
        if "delete" in releasers or "delete[]" in releasers:
            pattern = r"\bdelete\s*(?:\[\])?\s*" + re.escape(variable) + r"\b"
            events.extend(
                _SourceEvent(
                    start_line + masked[:match.start()].count("\n"),
                    match.start(),
                    match.end(),
                )
                for match in re.finditer(pattern, masked)
                if start_line + masked[:match.start()].count("\n") > allocation_line
            )
        return sorted(events, key=lambda event: event.start)

    @classmethod
    def _matching_lines(cls, text: str, start_line: int, allocation_line: int, variable: str | None, releasers: set[str]) -> list[int]:
        """Return source-backed release lines using the shared lexer boundary."""

        return [
            event.line
            for event in cls._release_events(text, start_line, allocation_line, variable, releasers)
        ]

    @staticmethod
    def _exit_lines(text: str, start_line: int, allocation_line: int) -> list[int]:
        masked = cached_mask_comments_and_strings(text)
        return [
            start_line + masked[:token.start].count("\n")
            for token in tokenize_masked_source(masked)
            if token.value == "return"
            and start_line + masked[:token.start].count("\n") > allocation_line
        ]

    @classmethod
    def _ownership(
        cls,
        text: str,
        start_line: int,
        allocation_line: int,
        variable: str | None,
        conditional_release: bool = False,
    ) -> tuple[str | None, list[str], list[tuple[str, int, str]]]:
        text = cached_mask_comments_and_strings(text)
        if not variable:
            return None, ["The allocation result was not assigned to a local variable."], []
        assumptions: list[str] = []
        escapes: list[tuple[str, int, str]] = []
        returned = next((match for match in re.finditer(r"\breturn\b[^;]*\b" + re.escape(variable) + r"\b", text) if start_line + text[:match.start()].count("\n") > allocation_line), None)
        if returned:
            escapes.append(("RETURNS_OWNERSHIP", start_line + text[:returned.start()].count("\n"), "return"))
            assumptions.append("The caller is responsible for releasing the returned pointer.")
        stored = next((match for match in re.finditer(r"(?:\w+\s*(?:->|\.|::)\s*\w+|\b(?:g_|s_|global_|owner\w*)\b)\s*=\s*[^;\n]*\b" + re.escape(variable) + r"\b", text) if start_line + text[:match.start()].count("\n") > allocation_line), None)
        if stored:
            escapes.append(("STORES_OWNERSHIP", start_line + text[:stored.start()].count("\n"), "owner-storage"))
            assumptions.append("Ownership is stored outside the local variable scope.")
        for call in re.finditer(r"\b([A-Za-z_]\w*)\s*\([^()\n;]*\b" + re.escape(variable) + r"\b[^()\n;]*\)", text):
            if start_line + text[:call.start()].count("\n") <= allocation_line:
                continue
            callee = call.group(1)
            if callee in ALLOCATORS or callee in RELEASERS: continue
            escapes.append(("PASSES_TO_UNKNOWN", start_line + text[:call.start()].count("\n"), callee))
            assumptions.append(f"Ownership passed to {callee} is not annotated.")
        if conditional_release:
            assumptions.append(
                "At least one release is conditional; ownership coverage is unresolved without branch analysis."
            )
        return escapes[0][0] if escapes else None, assumptions, escapes

    @classmethod
    def _event_is_conditional(cls, text: str, event_start: int) -> bool:
        masked = cached_mask_comments_and_strings(text)
        return any(start <= event_start < end for start, end in cls._conditional_ranges(masked))

    @staticmethod
    def _conditional_ranges(text: str) -> tuple[tuple[int, int], ...]:
        """Return conservative source spans controlled by branch/loop syntax."""

        masked = cached_mask_comments_and_strings(text)
        tokens = tokenize_masked_source(masked)
        pairs: dict[int, int] = {}
        stacks: dict[str, list[int]] = {"(": [], "{": []}
        closing = {")": "(",
            "}": "{",
        }
        for index, token in enumerate(tokens):
            if token.value in stacks:
                stacks[token.value].append(index)
            elif token.value in closing and stacks[closing[token.value]]:
                pairs[stacks[closing[token.value]].pop()] = index

        ranges: list[tuple[int, int]] = []
        controls = {"if", "for", "while", "switch", "catch"}
        for index, token in enumerate(tokens):
            if token.value in controls and index + 1 < len(tokens) and tokens[index + 1].value == "(":
                close_paren = pairs.get(index + 1)
                if close_paren is None or close_paren + 1 >= len(tokens):
                    continue
                body_index = close_paren + 1
                if tokens[body_index].value == "{" and body_index in pairs:
                    end = tokens[pairs[body_index]].end
                else:
                    end_index = body_index
                    while end_index < len(tokens) and tokens[end_index].value != ";":
                        end_index += 1
                    if end_index >= len(tokens):
                        continue
                    end = tokens[end_index].end
                ranges.append((token.start, end))
            elif token.value == "else" and index + 1 < len(tokens):
                body_index = index + 1
                if tokens[body_index].value == "{" and body_index in pairs:
                    end = tokens[pairs[body_index]].end
                else:
                    end_index = body_index
                    while end_index < len(tokens) and tokens[end_index].value != ";":
                        end_index += 1
                    if end_index >= len(tokens):
                        continue
                    end = tokens[end_index].end
                ranges.append((token.start, end))

        # A conditional expression is also uncertain ownership control flow.
        for index, token in enumerate(tokens):
            if token.value != "?":
                continue
            end_index = index + 1
            while end_index < len(tokens) and tokens[end_index].value != ";":
                end_index += 1
            if end_index < len(tokens):
                ranges.append((token.start, tokens[end_index].end))
        return tuple(ranges)

    @staticmethod
    def _hash(*parts: object) -> str:
        return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()
