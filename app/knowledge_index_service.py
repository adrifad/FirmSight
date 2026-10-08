from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from .knowledge_schemas import KnowledgeIndexReport
from .repository import MemoryRepository

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _hash_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class KnowledgeIndexService:
    """Incremental, heading-aware Markdown chunk and wikilink index."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    @staticmethod
    def _chunks(document: dict[str, Any]) -> list[dict[str, Any]]:
        body = document.get("body") or ""
        sections: list[tuple[str, list[str]]] = []
        heading: list[str] = []
        lines: list[str] = []
        for line in body.splitlines():
            match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if match and lines:
                sections.append((" / ".join(heading), lines))
                lines = []
            if match:
                level = len(match.group(1))
                heading = heading[: level - 1] + [match.group(2).strip()]
            elif line.strip():
                lines.append(line)
        if lines:
            sections.append((" / ".join(heading), lines))
        if not sections:
            sections = [("", [body[:12000]])]
        frontmatter = document.get("frontmatter") or {}
        scope = frontmatter.get("scope") or {}
        metadata = {
            "state": frontmatter.get("status"),
            "type": frontmatter.get("type"),
            "symbols": scope.get("symbols", []),
            "files": scope.get("files", []),
            "relationships": frontmatter.get("relationships") or {},
            "source_status": document.get("source_status"),
            "evidence_count": int(frontmatter.get("evidence_count") or 0),
        }
        chunks: list[dict[str, Any]] = []
        for ordinal, (heading_path, values) in enumerate(sections):
            text = "\n".join(values).strip()[:12000]
            if not text:
                continue
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            chunks.append({
                "id": "CHK-" + hashlib.sha256(f"{document['id']}:{ordinal}:{content_hash}".encode()).hexdigest()[:24].upper(),
                "document_id": document["id"], "project_id": document["project_id"],
                "intelligence_id": document.get("intelligence_id"), "ordinal": ordinal,
                "heading_path": heading_path, "body": text, "content_hash": content_hash,
                "metadata": metadata, "source_span_start": None, "source_span_end": None,
                "embedding": None, "embedding_provider": None, "embedding_version": None,
                "created_at": datetime.now(UTC).isoformat(),
            })
        return chunks

    @staticmethod
    def _links(document: dict[str, Any], by_id: dict[str, dict[str, Any]], by_path: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        links = []
        for raw in WIKILINK_RE.findall(str(document.get("body") or "")):
            target = raw.strip()
            target_id = target if target in by_id else None
            resolved_path = target if target in by_path else None
            # Preserve an unresolved target token so a later index can resolve
            # it when the target document is added or its path changes, while
            # keeping validation false until it is actually resolved.
            target_path = resolved_path or (target if target_id is None else None)
            links.append({
                "project_id": document["project_id"], "document_id": document["id"], "chunk_id": None,
                "target_id": target_id, "target_path": target_path, "link_kind": "WIKILINK",
                "validation_status": "VALID" if target_id or resolved_path else "INVALID",
                "created_at": datetime.now(UTC).isoformat(),
            })
        return links

    @staticmethod
    def _versions(state: dict[str, Any] | None) -> dict[str, dict[str, str]]:
        if not state or not state.get("document_version"):
            return {}
        try:
            value = json.loads(state["document_version"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def index_project(self, project_id: str) -> KnowledgeIndexReport:
        documents = self.repository.list_knowledge_documents(project_id)
        previous = self._versions(self.repository.get_knowledge_index_state(project_id))
        current = {
            document["id"]: {
                "content_hash": str(document.get("content_hash") or ""),
                "frontmatter_hash": _hash_json(document.get("frontmatter") or {}),
            }
            for document in documents
        }
        by_id = {document["id"]: document for document in documents}
        by_path = {document["relative_path"]: document for document in documents}
        indexed_ids = {chunk["document_id"] for chunk in self.repository.list_knowledge_chunks(project_id)}
        removed = 0
        removed_document_ids = (set(previous) | indexed_ids) - set(current)
        for document_id in removed_document_ids:
            removed += self.repository.remove_knowledge_index(document_id, project_id)
        link_revalidation_ids: set[str] = set()
        for link in self.repository.list_knowledge_wikilinks(project_id):
            target_id = link.get("target_id")
            target_path = link.get("target_path")
            if target_id in removed_document_ids or (target_id and target_id not in by_id):
                link_revalidation_ids.add(link["document_id"])
            elif target_id is None and (target_path in by_id or target_path in by_path):
                link_revalidation_ids.add(link["document_id"])
            elif target_id and target_path and target_path not in by_path:
                link_revalidation_ids.add(link["document_id"])
        links_changed = False

        written = 0
        changed = 0
        now = datetime.now(UTC).isoformat()
        for document in documents:
            document_id = document["id"]
            version = current[document_id]
            # The persisted document version is authoritative for incremental
            # decisions, including a valid document whose body currently yields
            # zero chunks. Do not churn that document on every reindex.
            unchanged = previous.get(document_id) == version
            if unchanged:
                if document_id in link_revalidation_ids:
                    self.repository.replace_knowledge_wikilinks(document_id, project_id, self._links(document, by_id, by_path))
                    links_changed = True
                continue
            removed += self.repository.count_knowledge_chunks(document_id, project_id)
            written += self.repository.replace_knowledge_chunks(document_id, project_id, self._chunks(document))
            self.repository.replace_knowledge_wikilinks(document_id, project_id, self._links(document, by_id, by_path))
            self.repository.mark_knowledge_document_indexed(document_id, now)
            changed += 1

        self.repository.set_knowledge_index_state({
            "project_id": project_id,
            "document_version": json.dumps(current, sort_keys=True),
            "status": "READY" if documents else "EMPTY", "index_version": 2,
            "last_success_at": now,
            "content_hash": _hash_json({key: value["content_hash"] for key, value in current.items()}) if documents else None,
            "frontmatter_hash": _hash_json({key: value["frontmatter_hash"] for key, value in current.items()}) if documents else None,
        })
        return KnowledgeIndexReport(documents_indexed=changed, chunks_written=written, chunks_removed=removed, noop=not changed and not written and not removed and not links_changed, index_version=2)
