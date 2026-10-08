from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .i18n import message as _msg
from .i18n import normalize_locale
from .knowledge_schemas import RetrievalMatch, RetrievalQuery, RetrievalResult
from .repository import MemoryRepository


class KnowledgeRetriever:
    """Project-scoped hybrid candidate collection followed by deterministic reranking."""

    def __init__(self, repository: MemoryRepository, platform: Any | None = None, language_resolver: Callable[[], str] | None = None) -> None:
        self.repository = repository
        self.platform = platform
        self.language_resolver = language_resolver

    def _t(self, key: str, /, **params: object) -> str:
        try:
            locale = normalize_locale(self.language_resolver() if self.language_resolver else None)
        except Exception:  # noqa: BLE001 - retrieval reporting must never raise
            locale = "en"
        return _msg(key, locale, **params)

    def _state(self, chunk: dict[str, Any], record_cache: dict[str, dict[str, Any] | None] | None = None) -> str:
        """Use current DB lifecycle state when the chunk has a memory owner."""
        memory_id = chunk.get("intelligence_id")
        if memory_id:
            key = str(memory_id)
            if record_cache is not None and key not in record_cache:
                record_cache[key] = self.repository.get_memory(key)
            record = record_cache[key] if record_cache is not None else self.repository.get_memory(key)
            if record and record.get("project_id") == chunk.get("project_id"):
                return str(record.get("state") or "").upper()
        return str((chunk.get("metadata") or {}).get("state") or "").upper()

    def _allowed(self, chunk: dict[str, Any], record_cache: dict[str, dict[str, Any] | None] | None = None) -> bool:
        state = self._state(chunk, record_cache)
        if chunk.get("intelligence_id"):
            key = str(chunk["intelligence_id"])
            if record_cache is not None and key not in record_cache:
                record_cache[key] = self.repository.get_memory(key)
            record = record_cache[key] if record_cache is not None else self.repository.get_memory(key)
            if record and record.get("project_id") != chunk.get("project_id"):
                return False
        return state not in {"SUPERSEDED", "DISABLED", "CONFLICTED"}

    def _topology(self, request: RetrievalQuery) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
        if self.platform is None:
            return set(), set(), set(), set(), set()
        symbols = list(self.platform.list_indexed_symbols(request.project_id))
        wanted = {value.casefold() for value in request.symbols}
        query_terms = {value.casefold() for value in re.findall(r"[A-Za-z_]\w*", request.query)}
        selected = {
            item["id"] for item in symbols
            if str(item.get("name", "")).casefold() in wanted
            or (not wanted and str(item.get("name", "")).casefold() in query_terms)
            or str(item.get("file", "")).casefold() in {value.casefold() for value in request.files}
        }
        by_id = {item["id"]: item for item in symbols}
        topology_names: set[str] = set()
        topology_files: set[str] = set()
        allocation_ids: set[str] = set()
        allocation_names: set[str] = set()
        relations = self.platform.list_source_relations(request.project_id)
        for relation in relations:
            if relation.get("source_symbol_id") in selected or relation.get("target_symbol_id") in selected:
                for key in ("source_symbol_id", "target_symbol_id"):
                    item = by_id.get(relation.get(key))
                    if item:
                        topology_names.add(str(item["name"]).casefold())
                        topology_files.add(str(item["file"]).casefold())
                if relation.get("target_name"):
                    topology_names.add(str(relation["target_name"]).casefold())
        for allocation in self.platform.list_allocation_events(request.project_id):
            if allocation.get("symbol_id") in selected:
                allocation_ids.add(str(allocation["symbol_id"]))
                item = by_id.get(allocation["symbol_id"])
                if item:
                    allocation_names.add(str(item["name"]).casefold())
                    topology_names.add(str(item["name"]).casefold())
                    topology_files.add(str(item["file"]).casefold())
        return selected, topology_names, topology_files, allocation_ids, allocation_names

    def search(self, request: RetrievalQuery) -> RetrievalResult:
        all_chunks = self.repository.list_knowledge_chunks(request.project_id)
        documents = {item["id"]: item for item in self.repository.list_knowledge_documents(request.project_id)}
        requested_symbols = {value.casefold() for value in request.symbols}
        requested_files = {value.casefold() for value in request.files}
        query_terms = {term.casefold() for term in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{2,}", request.query)}
        _, topology_names, topology_files, allocation_ids, allocation_names = self._topology(request)
        candidates: dict[str, dict[str, Any]] = {}
        record_cache: dict[str, dict[str, Any] | None] = {}
        links_cache: dict[str, list[dict[str, Any]]] = {}

        def add(chunk: dict[str, Any], signal: str) -> None:
            if chunk.get("project_id") != request.project_id or not self._allowed(chunk, record_cache):
                return
            entry = candidates.setdefault(chunk["id"], {"chunk": chunk, "signals": set()})
            entry["signals"].add(signal)

        for chunk in all_chunks:
            metadata = chunk.get("metadata") or {}
            symbols = {str(value).casefold() for value in metadata.get("symbols", [])}
            files = {str(value).casefold() for value in metadata.get("files", [])}
            if requested_symbols & symbols:
                add(chunk, "exact-symbol")
            if requested_files & files:
                add(chunk, "exact-file")
            if topology_names & symbols or topology_files & files:
                add(chunk, "topology")
            if allocation_names & symbols:
                add(chunk, "allocation")
            body = str(chunk.get("body") or "")
            if request.component and request.component.casefold() in body.casefold():
                add(chunk, "component")

        fts_chunks: list[dict[str, Any]] = []
        fts_used = False
        if request.query.strip():
            fts_queries = [request.query, *sorted(query_terms)]
            seen_fts: set[str] = set()
            for fts_query in fts_queries:
                lookup = getattr(self.repository, "knowledge_fts_search_with_status", None)
                if callable(lookup):
                    found, used = lookup(request.project_id, fts_query, limit=max(request.cap * 4, 12))
                    fts_used = fts_used or used
                else:
                    found = self.repository.knowledge_fts_search(request.project_id, fts_query, limit=max(request.cap * 4, 12))
                    fts_used = fts_used or bool(getattr(self.repository, "fts_available", False))
                for chunk in found:
                    if chunk["id"] in seen_fts:
                        continue
                    seen_fts.add(chunk["id"])
                    fts_chunks.append(chunk)
                    add(chunk, "fts")

        target_ids = set(re.findall(r"MEM-[A-Z0-9]+", request.query.upper()))
        target_ids.update(value for value in request.symbols if value.upper().startswith("MEM-"))
        target_paths = {value for value in request.files if "/" in value or "." in value}
        target_paths.update(term for term in query_terms if "/" in term or "." in term)
        linked_documents = self.repository.knowledge_wikilinks_for(request.project_id, sorted(target_ids), sorted(target_paths))
        linked_ids = {item["id"] for item in linked_documents}
        for chunk in all_chunks:
            if chunk.get("document_id") in linked_ids:
                add(chunk, "wikilink")

        # Lexical fallback remains deterministic and is also useful for FTS
        # results that contain only a partial token match.
        for chunk in all_chunks:
            if not self._allowed(chunk, record_cache):
                continue
            body = str(chunk.get("body") or "").casefold()
            if query_terms and any(term in body for term in query_terms):
                add(chunk, "lexical")

        warnings: list[str] = []
        matches: list[RetrievalMatch] = []
        for entry in candidates.values():
            chunk = entry["chunk"]
            metadata = chunk.get("metadata") or {}
            state = self._state(chunk, record_cache)
            signals = sorted(entry["signals"])
            score = sum({
                "exact-symbol": 10.0, "exact-file": 8.0, "topology": 6.0,
                "allocation": 5.0, "fts": 4.0, "wikilink": 3.0,
                "component": 3.0, "lexical": 2.0,
            }.get(signal, 0.0) for signal in signals)
            body = str(chunk.get("body") or "")
            score += sum(1.0 for term in query_terms if term in body.casefold())
            if state == "VERIFIED":
                score += 3.0
            elif state == "REINFORCED":
                score += 2.0
            elif state == "PROVISIONAL":
                warnings.append(f"{chunk.get('intelligence_id')} {self._t('retrieval.provisional_warning')}")
            elif state == "NEEDS_REVALIDATION":
                score -= 3.0
                warnings.append(f"{chunk.get('intelligence_id')} {self._t('retrieval.revalidation_warning')}")
            if metadata.get("relationships"):
                score += 0.5
            evidence_count = int(metadata.get("evidence_count") or 0)
            source_files = metadata.get("files") or []
            score += min(2.0, evidence_count * 0.5 + len(source_files) * 0.25)
            document = documents.get(chunk.get("document_id"), {})
            try:
                age_days = max(0.0, (datetime.now(UTC) - datetime.fromisoformat(str(document.get("updated_at"))).astimezone(UTC)).total_seconds() / 86400)
                score += max(0.0, 1.0 - age_days / 365.0)
            except (TypeError, ValueError):
                pass
            document_id = chunk.get("document_id")
            if document_id not in links_cache:
                links_cache[document_id] = self.repository.list_knowledge_wikilinks(request.project_id, document_id)
            links = links_cache[document_id]
            matches.append(RetrievalMatch(
                intelligence_id=chunk.get("intelligence_id") or "", document_id=chunk["document_id"],
                chunk_id=chunk["id"], heading_path=chunk.get("heading_path"), statement=body[:1200],
                type=str(metadata.get("type") or ""), state=state, confidence=1.0 if state == "VERIFIED" else 0.75,
                score=score, match_signals=signals,
                lifecycle_warning=self._t("retrieval.revalidation_lifecycle") if state == "NEEDS_REVALIDATION" else None,
                related_ids=[link["target_id"] for link in links if link.get("validation_status") == "VALID" and link.get("target_id")],
                relative_path=document.get("relative_path"),
            ))
        matches.sort(key=lambda item: (-item.score, item.intelligence_id, item.chunk_id or ""))
        return RetrievalResult(matches=matches[:request.cap], used_semantic=False, used_fts=fts_used, warnings=list(dict.fromkeys(warnings))[:12])
