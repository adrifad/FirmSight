"""Obsidian-compatible Markdown projection and validation boundary (FS-KB-015).

The vault uses exactly five project-scoped top-level areas:

    Projects/<stable-project-slug>/
    ├── 00-Project/       Project.md
    ├── 01-Architecture/  Topology.md
    ├── 02-Reviews/       Review-<review-id>.md
    ├── 03-Findings/      FS-<finding-id>.md
    └── 04-Knowledge/     MEM-<intelligence-id>.md

The database remains authoritative. Every generated document is a validated
projection with stable IDs and schema-checked frontmatter; current source
evidence always outranks Markdown. Legacy layout documents from earlier
versions are migration inputs only: valid generated documents are re-projected
into the five-area layout, and unknown/user files are never deleted.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .knowledge_schemas import (
    DocumentKind,
    KnowledgeDocStatus,
    KnowledgeFrontmatter,
    VaultDocumentFrontmatter,
    VaultSyncReport,
)
from .i18n import message as _i18n_message
from .i18n import normalize_locale
from .platform_repository import PlatformRepository
from .repository import MemoryRepository
from .settings_service import SettingsService


def KB_MSG(key: str, locale: str | None = None) -> str:
    """Localized fixed vault message (FS-I18N-016). Falls back to English."""
    try:
        return _i18n_message(key, normalize_locale(locale))
    except Exception:  # noqa: BLE001 - vault reporting must never raise
        return _i18n_message(key, "en")


LAYOUT = ("00-Project", "01-Architecture", "02-Reviews", "03-Findings", "04-Knowledge")
LEGACY_LAYOUT = ("00-Project", "01-Architecture", "02-Components", "03-Reviews", "04-Knowledge", "05-Findings", "06-Resolutions", "07-Releases", "08-Index", "09-Journal")
TYPE_DIR = {
    "PROJECT_FACT": "Facts", "DESIGN_INTENT": "Design-Intent", "ARCHITECTURE_KNOWLEDGE": "Architecture",
    "FALSE_POSITIVE_KNOWLEDGE": "False-Positives", "CONFIRMED_BUG_PATTERN": "Bug-Patterns",
    "RESOLUTION_PATTERN": "Resolution-Patterns", "RECURRING_PATTERN": "Recurring-Patterns",
}
# Knowledge states eligible for projection: current lifecycle/retrieval policy
# permits active, source-validated knowledge only. PROVISIONAL is intentionally
# excluded; DISABLED/SUPERSEDED/CONFLICTED/NEEDS_REVALIDATION never project.
KNOWLEDGE_ELIGIBLE_STATES = {"VERIFIED", "REINFORCED"}
# Finding documents project every persisted final finding; the document kind
# records the classification so PROBABLE_BUG/DESIGN_RISK/SUGGESTION are never
# presented as CONFIRMED_BUG.
FINDING_DECISION_LABELS = {
    "UNREVIEWED": "Awaiting engineer review",
    "ACCEPTED": "Accepted by engineer",
    "REJECTED": "Rejected by engineer",
    "INTENTIONAL": "Marked intentional by engineer",
    "NEEDS_MORE_EVIDENCE": "Needs more evidence",
}
REVIEW_DOCUMENT = ("00-Project", "Project.md")
TOPOLOGY_DOCUMENT = ("01-Architecture", "Topology.md")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class KnowledgeBaseService:
    def __init__(self, repository: MemoryRepository, platform: PlatformRepository, settings: SettingsService) -> None:
        self.repository, self.platform, self.settings = repository, platform, settings

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return slug[:80] or "project"

    def project_root(self, project_id: str) -> Path | None:
        project = self.platform.get_project(project_id)
        if not project:
            return None
        status = self.settings.vault_status()
        if status.status.value != "READY" or not status.root:
            return None
        return Path(status.root) / "Projects" / self._slug(project["name"])

    # --- Path helpers -----------------------------------------------------

    @staticmethod
    def _knowledge_path(root: Path, record: dict[str, Any]) -> Path:
        type_dir = TYPE_DIR.get(record.get("type"), "Facts")
        return root / "04-Knowledge" / type_dir / f"{record['id']}.md"

    def _document_path(self, root: Path, record: dict[str, Any]) -> Path:
        """Canonical five-area write path for an intelligence projection (REV-028).

        This is a pure function of the record identity/type: legacy layouts never
        influence where new documents are written. Existing legacy files remain
        migration inputs only and are handled by the scan/import + reconciliation
        phases.
        """
        return self._knowledge_path(root, record)

    @staticmethod
    def _legacy_document_candidates(root: Path, record: dict[str, Any]) -> tuple[Path, ...]:
        """Legacy read locations a valid knowledge document may still occupy."""
        return (
            root / "04-Knowledge" / f"{record['id']}.md",
            root / "05-Findings" / f"{record['id']}.md",
        )

    @staticmethod
    def _project_document_path(root: Path) -> Path:
        return root / "00-Project" / "Project.md"

    @staticmethod
    def _topology_document_path(root: Path) -> Path:
        return root / "01-Architecture" / "Topology.md"

    @staticmethod
    def _review_document_path(root: Path, review_id: str) -> Path:
        return root / "02-Reviews" / f"Review-{review_id}.md"

    @staticmethod
    def _finding_document_path(root: Path, finding_id: str) -> Path:
        # Finding IDs already carry the FS- prefix (AGENTS.md finding schema),
        # so the canonical file name is the stable finding id itself.
        return root / "03-Findings" / f"{finding_id}.md"

    # --- Frontmatter ------------------------------------------------------

    @staticmethod
    def _knowledge_frontmatter(record: dict[str, Any], links: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        scope = record.get("scope") or {}
        relationships: dict[str, list[str]] = {"reviews": [], "findings": [], "resolutions": []}
        for link in links:
            kind = link.get("link_kind")
            value = link.get("link_value")
            if kind == "REVIEW": relationships["reviews"].append(value)
            elif kind == "FINDING": relationships["findings"].append(value)
        title = str(record.get("statement") or "Knowledge").split(".", 1)[0][:300]
        return {
            "id": record["id"], "type": record["type"], "project_id": record["project_id"], "status": record.get("state") or record.get("status"),
            "confidence": float(record.get("confidence") or 0), "observation_count": max(1, int(record.get("observation_count") or 1)),
            "evidence_count": len(evidence),
            "title": title, "statement": record["statement"],
            "scope": {key: value for key, value in {"component": scope.get("component"), "symbols": [scope["symbol"]] if scope.get("symbol") else [], "files": [item.get("file") for item in evidence if item.get("file")][:24], "functions": []}.items() if value},
            "relationships": relationships,
            "provenance": {"origin": record.get("origin", "SYNTHESIZER"), "commits": [record["last_validated_commit"]] if record.get("last_validated_commit") else [], "first_observed_at": record.get("first_observed_at"), "last_validated_at": record.get("last_validated_at"), "last_validated_commit": record.get("last_validated_commit")},
            "tags": ["firmsight", "project-intelligence", str(record["type"]).casefold()], "schema_version": 1,
        }

    @staticmethod
    def _vault_frontmatter(
        document_id: str,
        kind: DocumentKind,
        project_id: str,
        title: str,
        status: str,
        source: dict[str, Any],
        scope: dict[str, Any] | None = None,
        relationships: dict[str, Any] | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": document_id, "kind": kind.value, "project_id": project_id,
            "title": title[:300], "status": status[:40], "generated_at": generated_at or _now(),
            "schema_version": 1, "source": source,
            "scope": scope or {}, "relationships": relationships or {},
            "tags": ["firmsight", kind.value.casefold()],
        }

    def _stable_generated_at(self, project_id: str, relative: str) -> str | None:
        """Reuse the first generation timestamp so re-renders stay byte-identical.

        Without this, a changing `generated_at` value would rewrite every
        document on every sync and break idempotency, including RAG churn.
        """
        existing = self.repository.get_knowledge_document_by_path(project_id, relative)
        if not existing:
            return None
        stored = existing.get("frontmatter") or {}
        value = stored.get("generated_at")
        return str(value) if value else None

    # --- Rendering --------------------------------------------------------

    @staticmethod
    def _render(
        frontmatter: dict[str, Any],
        record: dict[str, Any],
        evidence: list[dict[str, Any]],
        body: str | None = None,
        related: list[tuple[str, str]] | None = None,
    ) -> str:
        yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        evidence_lines = [f"- `{item.get('file') or 'unknown'}:{item.get('line') or 1}` — {item.get('description') or 'source evidence'}" for item in evidence[:12]] or ["- No source evidence recorded."]
        notes = (body or "").strip()
        # FS-KB-022 / REQ-2: never emit an empty Related section. A knowledge
        # record without recorded review/finding relationships still links to its
        # project baseline so the vault stays a connected graph.
        related_links = list(related or [])
        if not any(target for target, _ in related_links):
            related_links = [("PRJ-PROJECT", "Project baseline for this knowledge")]
        related_lines = KnowledgeBaseService._related_section(related_links)[0][1]
        related_block = "\n".join(related_lines)
        generated = (
            f"## Statement\n\n{record['statement']}\n\n## Evidence\n\n"
            + "\n".join(evidence_lines)
            + "\n\n## Engineer Notes\n\n"
        )
        # FS-KB-022 / AGENTS.md 32B.1: the Related graph section is generated
        # (not editable), so it sits after the editable Engineer Notes block and
        # is excluded when the notes are re-imported.
        return (
            f"---\n{yaml_text}\n---\n\n# {frontmatter['title']}\n\n"
            f"{generated}{notes}\n\n## Related\n\n{related_block}\n"
        )

    @staticmethod
    def _related_section(links: list[tuple[str, str]] | None) -> list[tuple[str, list[str]]]:
        """Build the ``## Related`` section from ``(wikilink_target, description)`` pairs (FS-KB-022 / AGENTS.md 32B.1).

        Every generated document must carry at least one outgoing ``[[wikilink]]``
        so the vault forms a connected knowledge graph rather than isolated files.
        Targets are stable document ids (``MEM-``, ``REV-``/``Review-``, ``FS-``,
        ``PRJ-``, ``TOP-``), never display titles. Returns the
        ``[("Related", ["- [[target]] — description", ...])]`` shape consumed by
        :meth:`_render_document` / :meth:`_render`.
        """
        lines = [f"- [[{target}]] — {description}" for target, description in (links or []) if target]
        return [("Related", lines)]

    @staticmethod
    def _knowledge_related_links(relationships: dict[str, Any]) -> list[tuple[str, str]]:
        """Map a knowledge document's ``relationships`` to ``## Related`` links (FS-KB-022).

        Review and finding relationship values are already stable ids
        (``REV-...`` / ``FS-...``). An empty result still yields a Related section
        via fallback links supplied by the caller when available.
        """
        related: list[tuple[str, str]] = []
        for review_id in relationships.get("reviews") or []:
            if review_id:
                related.append((str(review_id), "Review that produced this knowledge"))
        for finding_id in relationships.get("findings") or []:
            if finding_id:
                related.append((str(finding_id), "Related finding"))
        return related

    @staticmethod
    def _render_document(frontmatter: dict[str, Any], title: str, sections: list[tuple[str, list[str]]]) -> str:
        """Render a generated projection document with no editable section."""
        yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        body = [f"---\n{yaml_text}\n---\n\n# {title}\n"]
        for heading, lines in sections:
            body.append(f"\n## {heading}\n")
            body.extend(line for line in lines if line is not None)
        return "\n".join(body).rstrip() + "\n"

    @staticmethod
    def _bullets(values: list[str], empty: str) -> list[str]:
        return [f"- {value}" for value in values] or [f"- {empty}"]

    @staticmethod
    def _field_lines(fields: list[tuple[str, Any]]) -> list[str]:
        lines = [f"- **{label}:** {value}" for label, value in fields if value not in (None, "", [])]
        return lines or ["- Not available in the indexed project data."]

    # --- Project baseline (TASK-002) ---------------------------------------

    def _render_project_document(self, project_id: str, generated_at: str | None = None) -> tuple[str, dict[str, Any]] | None:
        project = self.platform.get_project(project_id)
        if not project:
            return None
        counts = {
            "indexed_files": int(project.get("file_count") or 0),
            "indexed_symbols": int(project.get("symbol_count") or 0),
        }
        source_lines = self._field_lines([
            ("Source type", project.get("source_type")),
            ("Language", project.get("language")),
            ("Framework", project.get("framework")),
            ("Target", project.get("target")),
            ("Build system", project.get("build_system")),
        ])
        # FS-KB-022 / REQ-6: the project baseline links to its topology document
        # and any projected knowledge documents, so the baseline is never an
        # isolated file.
        related: list[tuple[str, str]] = [("TOP-TOPOLOGY", "Source topology for this project")]
        for record in self._eligible_knowledge_records(project_id):
            related.append((str(record["id"]), "Project knowledge"))
        sections: list[tuple[str, list[str]]] = [
            ("Overview", self._bullets([project["description"]], "No description recorded.") if (project.get("description") or "").strip() else ["- No description recorded."]),
            ("Platform", source_lines),
            ("Index", self._field_lines([
                ("Indexed files", counts["indexed_files"] or None),
                ("Indexed symbols", counts["indexed_symbols"] or None),
                ("Project created", project.get("created_at")),
                ("Last index/source update", project.get("updated_at")),
            ])),
            *self._related_section(related),
        ]
        frontmatter = self._vault_frontmatter(
            "PRJ-PROJECT", DocumentKind.PROJECT, project_id,
            str(project.get("name") or "Project"), "ACTIVE",
            source={
                "authority": "DATABASE",
                "source_type": project.get("source_type"),
                "language": project.get("language"),
                "framework": project.get("framework"),
                "target": project.get("target"),
                "build_system": project.get("build_system"),
                "indexed_files": counts["indexed_files"],
                "indexed_symbols": counts["indexed_symbols"],
            },
            scope={"project": project.get("name")},
            generated_at=generated_at,
        )
        return self._render_document(frontmatter, f"{project.get('name') or 'Project'}", sections), frontmatter

    def _render_topology_document(self, project_id: str, generated_at: str | None = None) -> tuple[str, dict[str, Any]] | None:
        """Compact source-backed topology; honestly skipped without index evidence."""
        symbols = self.platform.list_indexed_symbols(project_id)
        relations = self.platform.list_source_relations(project_id)
        observed = [item for item in relations if item.get("relation_state") == "OBSERVED"]
        tasks = [item for item in symbols if item.get("kind") == "task"]
        entries = [item for item in observed if item.get("relation_kind") in {"TASK_ENTRY", "ISR_ENTRY"}]
        if not symbols or not observed:
            return None
        task_lines = [f"- `{item['name']}` — {item['file']}:{item['line_start']}" for item in tasks[:24]]
        if not task_lines and entries:
            task_lines = [f"- `{item.get('target_name') or item.get('target_symbol_id')}` — {item['file']}:{item['line']}" for item in entries[:24]]
        call_lines = [
            f"- `{(self._symbol_name(symbols, item.get('source_symbol_id')) or 'unknown')} → {self._symbol_name(symbols, item.get('target_symbol_id')) or item.get('target_name') or 'unknown'}` — {item['file']}:{item['line']}"
            for item in observed if item.get("relation_kind") == "CALLS"
        ][:24]
        resource_lines = [
            f"- `{self._symbol_name(symbols, item.get('source_symbol_id')) or 'unknown'} {item.get('relation_kind', '').replace('_', ' ').lower()} {item.get('target_name') or 'resource'}` — {item['file']}:{item['line']}"
            for item in observed if item.get("relation_kind") not in {"CALLS", "TASK_ENTRY", "ISR_ENTRY"}
        ][:24]
        sections = [
            ("Tasks and ISRs", self._bullets(task_lines, "No FreeRTOS task or ISR entry points were observed in the indexed source.")),
            ("Call relations", self._bullets(call_lines, "No resolved call relations were observed in the indexed source.")),
            ("Resource relations", self._bullets(resource_lines, "No queue/mutex/semaphore/event-group relations were observed in the indexed source.")),
            # FS-KB-022 / REQ-7: link back to the project baseline (bidirectional).
            *self._related_section([("PRJ-PROJECT", "Project overview")]),
        ]
        snapshot = self.platform.topology_snapshot(project_id)
        frontmatter = self._vault_frontmatter(
            "TOP-TOPOLOGY", DocumentKind.ARCHITECTURE, project_id,
            "Source topology", "ACTIVE",
            source={
                "authority": "SOURCE_INDEX",
                "relation_fingerprint": (snapshot or {}).get("relation_fingerprint"),
                "source_snapshot_hash": (snapshot or {}).get("source_snapshot_hash"),
                "indexed_symbols": len(symbols),
                "observed_relations": len(observed),
            },
            scope={"symbols": [item["name"] for item in tasks[:24]]},
            generated_at=generated_at,
        )
        return self._render_document(frontmatter, "Source topology", sections), frontmatter

    def _eligible_knowledge_records(self, project_id: str) -> list[dict[str, Any]]:
        """Knowledge records that are projected to the vault (FS-KB-022).

        Used to interconnect the project baseline with its knowledge documents.
        Bounded and best-effort: a listing failure must not break projection.
        """
        try:
            records = self.repository.list_intelligence(project_id)
        except Exception:  # noqa: BLE001 - interconnection links are best-effort
            return []
        return [
            record for record in records
            if record.get("state") not in {"DISABLED", "SUPERSEDED"}
            and str(record.get("state") or "").upper() in KNOWLEDGE_ELIGIBLE_STATES
        ]

    @staticmethod
    def _symbol_name(symbols: list[dict[str, Any]], symbol_id: str | None) -> str | None:
        if not symbol_id:
            return None
        for item in symbols:
            if item.get("id") == symbol_id:
                return str(item.get("name"))
        return None

    # --- Review / finding projections (TASK-003) ---------------------------

    def _render_review_document(self, project_id: str, review: dict[str, Any], findings: list[dict[str, Any]], generated_at: str | None = None) -> tuple[str, dict[str, Any]]:
        review_id = review["id"]
        focus = [str(item) for item in (review.get("focus") or [])]
        # FS-KB-022 / REQ-8: link with the finding's own stable id (already
        # ``FS-...``); do not double the prefix. ``_bullets`` adds the marker.
        finding_refs = [f"[[{item['id']}]] — {item.get('classification')} · {item.get('severity')} · {item.get('decision')}" for item in findings]
        coverage = self._field_lines([
            ("Status", review.get("status")),
            ("Scope", review.get("scope")),
            ("Total batches", review.get("total_batches") or None),
            ("Validated batches", review.get("validated_batches") or None),
            ("Unavailable batches", review.get("unavailable_batches") or None),
            ("Findings", len(findings) or None),
            ("Source snapshot", review.get("source_snapshot_hash")),
            ("Started", review.get("created_at")),
            ("Completed", review.get("completed_at")),
        ])
        related = [(item["id"], f"{item.get('classification')} · {item.get('severity')}") for item in findings]
        sections = [
            ("Scope and focus", self._bullets([f"Scope: {review.get('scope')}"] + ([f"Focus: {', '.join(focus)}"] if focus else []), "Scope unavailable.")),
            ("Coverage", coverage),
            ("Findings", self._bullets(finding_refs, "No findings were persisted for this review.")),
            *self._related_section(related),
        ]
        frontmatter = self._vault_frontmatter(
            review_id, DocumentKind.REVIEW, project_id,
            f"Review {review_id}", str(review.get("status") or "UNKNOWN"),
            source={
                "authority": "DATABASE",
                "review_id": review_id,
                "scope": review.get("scope"),
                "focus": focus,
                "source_snapshot_hash": review.get("source_snapshot_hash"),
                "total_batches": review.get("total_batches"),
                "validated_batches": review.get("validated_batches"),
                "unavailable_batches": review.get("unavailable_batches"),
                "finding_count": len(findings),
            },
            relationships={"findings": [item["id"] for item in findings][:24]},
            generated_at=generated_at,
        )
        return self._render_document(frontmatter, f"Review {review_id}", sections), frontmatter

    def _render_finding_document(self, finding: dict[str, Any], generated_at: str | None = None) -> tuple[str, dict[str, Any]]:
        finding_id = finding["id"]
        evidence_lines = [
            f"- `{item.get('file') or 'unknown'}:{item.get('line') or 1}` — {item.get('description') or 'source evidence'}"
            for item in (finding.get("evidence") or [])[:12]
        ] or ["- No source evidence recorded."]
        path_lines = [f"- {step}" for step in (finding.get("execution_path") or [])] or ["- No execution path was recorded."]
        assumption_lines = [
            f"- {item.get('statement')} ({item.get('status') or 'UNVERIFIED'})"
            for item in (finding.get("assumptions") or [])
        ] or ["- None recorded."]
        decision = str(finding.get("decision") or "UNREVIEWED")
        resolution = str(finding.get("resolution") or "OPEN")
        remediation = finding.get("remediation") or {}
        status = "SOLVED" if resolution == "SOLVED" else decision
        sections = [
            ("Summary", [finding.get("summary") or ""]),
            ("Evidence", evidence_lines),
            ("Execution path", path_lines),
            ("Runtime impact", [finding.get("impact") or "Not recorded.", ""]),
            ("Assumptions", assumption_lines),
            ("Recommendation", [finding.get("recommendation") or "Not recorded."]),
            ("Engineer decision", self._bullets([
                f"Decision: {decision} — {FINDING_DECISION_LABELS.get(decision, decision)}",
                f"Resolution: {resolution}",
                f"Fix verification: {remediation.get('status') or 'UNVERIFIED'}",
            ] + ([f"Decision reason: {finding['decision_reason']}"] if finding.get("decision_reason") else []), "Decision unavailable.")),
        ]
        # FS-KB-022 / REQ-4: link back to the review that produced this finding.
        parent_review = finding.get("review_id")
        sections.extend(self._related_section([(parent_review, "Parent review of this finding")] if parent_review else []))
        frontmatter = self._vault_frontmatter(
            finding_id, DocumentKind.FINDING, finding["project_id"],
            str(finding.get("title") or finding_id), status,
            source={
                "authority": "DATABASE",
                "review_id": finding.get("review_id"),
                "classification": finding.get("classification"),
                "severity": finding.get("severity"),
                "category": finding.get("category"),
                "confidence": float(finding.get("confidence") or 0),
                "location": {
                    "file": (finding.get("location") or {}).get("file"),
                    "line_start": (finding.get("location") or {}).get("line_start"),
                    "line_end": (finding.get("location") or {}).get("line_end"),
                },
            },
            scope={"file": (finding.get("location") or {}).get("file"), "function": (finding.get("location") or {}).get("function")},
            relationships={"review": finding.get("review_id")},
            generated_at=generated_at,
        )
        return self._render_document(frontmatter, str(finding.get("title") or finding_id), sections), frontmatter

    # --- Markdown safety ---------------------------------------------------

    @staticmethod
    def _rendered_body(rendered: str) -> str:
        """Return the Markdown body without YAML frontmatter for indexing."""
        separator = "\n---\n"
        _frontmatter, body = rendered.split(separator, 1)
        return body.lstrip("\n")

    @staticmethod
    def _engineer_notes(body: str) -> str:
        """Extract only the editable section from a parsed Markdown body.

        Statement, evidence, and frontmatter are generated projections. External
        edits to those sections are intentionally not carried into the database;
        only the explicitly editable notes section is imported.
        """
        marker = re.search(r"(?m)^##[ \t]+Engineer Notes[ \t]*\r?\n", body)
        if marker is None:
            return ""
        notes = body[marker.end():]
        next_heading = re.search(r"(?m)^##[ \t]+", notes)
        if next_heading is not None:
            notes = notes[:next_heading.start()]
        return notes.strip()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        # Never let mkdir/mkstemp/os.replace traverse a symlinked parent.  A
        # symlink can be introduced below an otherwise valid vault after the
        # scan phase, so validate every component immediately before writing.
        current = Path(path.anchor) if path.is_absolute() else Path()
        components = path.parts[1:] if path.is_absolute() else path.parts
        for component in components[:-1]:
            current = current / component
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                current.mkdir()
                mode = current.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError("refusing to write below a symlink or non-directory vault parent")
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISREG(mode)):
            raise ValueError("refusing to replace a symlink or non-regular Markdown file")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _parse_markdown(path: Path, max_bytes: int = 1_000_000) -> tuple[KnowledgeFrontmatter | VaultDocumentFrontmatter, str, str]:
        try:
            before_open = path.lstat()
        except OSError as error:
            raise ValueError("Markdown file cannot be inspected safely") from error
        if stat.S_ISLNK(before_open.st_mode) or not stat.S_ISREG(before_open.st_mode) or before_open.st_size > max_bytes:
            raise ValueError("symlink or oversized Markdown document")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                text = handle.read(max_bytes + 1)
        except (OSError, UnicodeError) as error:
            raise ValueError("Markdown file cannot be read safely") from error
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError("symlink or oversized Markdown document")
        if not text.startswith("---\n"):
            raise ValueError("missing YAML frontmatter")
        closing = text.find("\n---\n", 4)
        if closing < 0:
            raise ValueError("unterminated YAML frontmatter")
        try:
            data = yaml.safe_load(text[4:closing])
        except yaml.YAMLError as error:
            raise ValueError("malformed YAML frontmatter") from error
        if not isinstance(data, dict):
            raise ValueError("frontmatter must be a mapping")
        if isinstance(data.get("kind"), str) and data.get("kind") in {item.value for item in DocumentKind}:
            frontmatter: KnowledgeFrontmatter | VaultDocumentFrontmatter = VaultDocumentFrontmatter.model_validate(data)
        else:
            frontmatter = KnowledgeFrontmatter.model_validate(data)
        body = text[closing + 6:]
        return frontmatter, body, hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _markdown_files(root: Path) -> list[Path]:
        """List Markdown files without traversing symlinked directories."""
        files: list[Path] = []
        if not root.exists() or root.is_symlink():
            return files
        for directory, directories, names in os.walk(root, followlinks=False):
            current = Path(directory)
            directories[:] = [name for name in directories if not (current / name).is_symlink()]
            files.extend(current / name for name in names if name.casefold().endswith(".md"))
        return sorted(files)

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        return path.relative_to(root).as_posix()

    def _quarantine(self, report: VaultSyncReport, root: Path, path: Path, error: str) -> None:
        report.quarantined += 1
        try:
            relative = self._relative(root, path)
        except ValueError:
            relative = path.name
        if relative not in report.quarantined_paths:
            report.quarantined_paths.append(relative)
        report.errors.append(f"{relative}: {error}")

    def _record_external_edit(
        self,
        project_id: str,
        record: dict[str, Any],
        path: Path,
        relative: str,
        frontmatter: KnowledgeFrontmatter,
        body: str,
        content_hash: str,
        existing: dict[str, Any],
    ) -> str:
        notes = self._engineer_notes(body)
        now = _now()
        self.repository.upsert_knowledge_document({
            "id": existing["id"],
            "project_id": project_id,
            "intelligence_id": record["id"],
            "relative_path": relative,
            "content_hash": content_hash,
            "frontmatter": frontmatter.model_dump(mode="json"),
            "body": body,
            "sync_status": KnowledgeDocStatus.EDITED.value,
            "source_status": "ENGINEER_EDITED",
            "external_modified_at": now,
            "schema_version": 1,
            "last_indexed_at": existing.get("last_indexed_at"),
            "created_at": existing["created_at"],
            "updated_at": now,
        })
        self.repository.add_intelligence_observation({
            "memory_id": record["id"],
            "project_id": project_id,
            "kind": "ENGINEER_MARKDOWN_EDIT",
            "from_state": record.get("state"),
            "to_state": record.get("state"),
            "fingerprint": content_hash,
            "detail": f"Imported editable Engineer Notes from {relative}; generated sections remain DB-authoritative.",
            "created_at": now,
        })
        return notes

    # --- Document persistence ---------------------------------------------

    def _persist_document(
        self,
        project_id: str,
        document_id: str,
        intelligence_id: str,
        relative: str,
        frontmatter: dict[str, Any],
        rendered: str,
    ) -> None:
        existing = self.repository.get_knowledge_document_by_path(project_id, relative)
        self.repository.upsert_knowledge_document({
            "id": existing["id"] if existing else document_id,
            "project_id": project_id,
            "intelligence_id": intelligence_id,
            "relative_path": relative,
            "content_hash": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "frontmatter": frontmatter,
            "body": self._rendered_body(rendered),
            "sync_status": KnowledgeDocStatus.SYNCED.value,
            "source_status": "GENERATED",
            "external_modified_at": None,
            "schema_version": 1,
            "last_indexed_at": existing.get("last_indexed_at") if existing else None,
            "created_at": existing["created_at"] if existing else _now(),
            "updated_at": _now(),
        })

    def _write_if_changed(
        self,
        root: Path,
        path: Path,
        rendered: str,
        relative: str,
        blocked: set[str],
        report: VaultSyncReport,
        stored_hash: str | None,
        imported: bool,
        locale: str | None = None,
    ) -> None:
        """Idempotent write: skip when identical, refuse silent overwrite of edits.

        The database row is authoritative. When the on-disk bytes match neither
        the content we are about to write nor the last persisted generated content
        (``stored_hash``), the file was changed outside this projection and is
        reported as an unimported external edit instead of being overwritten.
        Same-project projections are serialized by the shared project workflow
        lock (REV-046), so a concurrent projection can no longer leave a file/row
        mismatch that this check would misread as an external edit.
        """
        if relative in blocked or path.is_symlink():
            if relative not in blocked:
                blocked.add(relative)
                self._quarantine(report, root, path, KB_MSG("error.vault_blocked_path", locale))
            return
        actual_hash: str | None = None
        if path.exists():
            try:
                _frontmatter, _body, actual_hash = self._parse_markdown(path)
            except (OSError, UnicodeError, ValueError, ValidationError) as error:
                blocked.add(relative)
                self._quarantine(report, root, path, str(error))
                return
        rendered_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        if actual_hash == rendered_hash:
            report.unchanged += 1
            return
        if actual_hash is not None and stored_hash != actual_hash and not imported:
            report.stale += 1
            blocked.add(relative)
            report.errors.append(f"{relative}: {KB_MSG('error.vault_external_edit', locale)}")
            return
        try:
            self._atomic_write(path, rendered)
        except (OSError, ValueError) as error:
            blocked.add(relative)
            self._quarantine(report, root, path, str(error))
            return
        if imported:
            report.regenerated += 1
        elif stored_hash == actual_hash or actual_hash is None:
            report.updated += 1
        else:
            report.regenerated += 1

    # --- Sync --------------------------------------------------------------

    def sync_project(self, project_id: str, *, regenerate: bool = False, persist_state: bool = True) -> VaultSyncReport:
        """Synchronize one project without destroying unimported Obsidian edits.

        The operation is deliberately scan/import/write ordered. A normal sync
        imports a validated ``## Engineer Notes`` edit before considering any
        write. ``regenerate=True`` is the explicit user-confirmed path for
        rewriting generated sections; malformed, duplicate, wrong-project, and
        symlink files remain quarantined even in that mode. Unknown files and
        legacy/user artifacts are never deleted.

        ``persist_state=False`` (REV-036) suppresses every ``knowledge_sync_state``
        write performed by this method — including the not-configured/SKIPPED
        early return and the hard symlink/layout ``FAILED`` early returns — so a
        caller that owns the whole projection-plus-index workflow can persist
        exactly one final durable state while holding the project lock. Manual
        ``sync_project()`` keeps the default ``persist_state=True`` behavior.
        """
        root = self.project_root(project_id)
        locale = None
        try:
            locale = self.settings.config().language
        except Exception:  # noqa: BLE001 - vault reporting must never raise
            locale = None
        if root is None:
            report = VaultSyncReport(errors=[self.settings.vault_status().message])
            if persist_state:
                self._record_sync_state(project_id, report, "SKIPPED")
            return report
        records = [item for item in self.repository.list_intelligence(project_id) if item.get("state") not in {"DISABLED", "SUPERSEDED"}]
        records_by_id = {record["id"]: record for record in records}
        report = VaultSyncReport()
        blocked: set[str] = set()
        parsed: dict[str, list[tuple[Path, KnowledgeFrontmatter, str, str]]] = {}
        if root.exists() and root.is_symlink():
            self._quarantine(report, root.parent, root, "refusing to scan a symlinked project vault")
            if persist_state:
                self._record_sync_state(project_id, report, "FAILED")
            return report
        for path in self._markdown_files(root):
            report.scanned += 1
            relative = self._relative(root, path)
            try:
                frontmatter, body, content_hash = self._parse_markdown(path)
                if isinstance(frontmatter, VaultDocumentFrontmatter):
                    if frontmatter.project_id != project_id:
                        raise ValueError(KB_MSG("error.vault_wrong_project_doc", locale))
                    continue  # generated projection documents are re-rendered below
                if frontmatter.project_id != project_id or frontmatter.id not in records_by_id:
                    raise ValueError(KB_MSG("error.vault_wrong_project_id", locale))
                parsed.setdefault(frontmatter.id, []).append((path, frontmatter, body, content_hash))
            except (OSError, UnicodeError, ValueError, ValidationError) as error:
                blocked.add(relative)
                self._quarantine(report, root, path, str(error))

        imported_paths: set[str] = set()
        imported_notes: dict[str, str] = {}
        legacy_migrated: set[str] = set()
        for intelligence_id, candidates in parsed.items():
            if len(candidates) > 1:
                for path, _frontmatter, _body, _content_hash in candidates:
                    blocked.add(self._relative(root, path))
                    self._quarantine(report, root, path, KB_MSG("error.vault_duplicate_id", locale))
                continue
            path, frontmatter, body, content_hash = candidates[0]
            relative = self._relative(root, path)
            canonical = self._document_path(root, records_by_id[intelligence_id])
            if path == canonical:
                existing = self.repository.get_knowledge_document_by_path(project_id, relative)
                if existing is None:
                    blocked.add(relative)
                    self._quarantine(report, root, path, KB_MSG("error.vault_no_projection", locale))
                    continue
                if existing.get("content_hash") != content_hash:
                    imported_paths.add(relative)
                    imported_notes[intelligence_id] = self._record_external_edit(
                        project_id, records_by_id[intelligence_id], path, relative, frontmatter, body, content_hash, existing
                    )
                continue
            # Valid knowledge on a legacy path (old ungrouped `04-Knowledge/` or
            # `05-Findings/`) is a migration input (REV-028): validated Engineer
            # Notes are imported, the file stays in place, and the canonical
            # projection below is written fresh at the stable five-area path.
            notes = self._engineer_notes(body)
            legacy_migrated.add(intelligence_id)
            if notes:
                imported_notes[intelligence_id] = notes

        try:
            root.mkdir(parents=True, exist_ok=True)
            for directory in LAYOUT:
                directory_path = root / directory
                if directory_path.is_symlink():
                    self._quarantine(report, root, directory_path, KB_MSG("error.vault_symlink_dir", locale))
                    continue
                directory_path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            report.errors.append(f"{KB_MSG('error.vault_layout', locale)}: {error}")
            if persist_state:
                self._record_sync_state(project_id, report, "FAILED")
            return report

        # Baseline projections (TASK-002): only when source-backed inputs exist.
        # REV-046: the baseline document ids are project-scoped. A constant id
        # (for example "DOC-PRJ-PROJECT") collides in ``knowledge_documents``
        # across projects because ``upsert_knowledge_document`` conflicts on the
        # primary key ``id``; a second project's baseline would then OVERWRITE the
        # first project's row, leaving that row's ``content_hash`` belonging to a
        # different project. That produced a false "external edit" hard error on
        # the next sync. Scoping by ``project_id`` keeps each project's baseline
        # row distinct and authoritative.
        for document_id, intelligence_id, target, render in (
            (f"DOC-PRJ-PROJECT-{project_id}", "PRJ-PROJECT", self._project_document_path(root),
             lambda at: self._render_project_document(project_id, at)),
            (f"DOC-TOP-TOPOLOGY-{project_id}", "TOP-TOPOLOGY", self._topology_document_path(root),
             lambda at: self._render_topology_document(project_id, at)),
        ):
            relative = self._relative(root, target)
            rendered = render(self._stable_generated_at(project_id, relative))
            if rendered is None:
                report.skipped.append(f"{relative}: skipped — no source-backed inputs")
                continue
            content, frontmatter = rendered
            existing = self.repository.get_knowledge_document_by_path(project_id, relative)
            self._write_if_changed(root, target, content, relative, blocked, report, existing.get("content_hash") if existing else None, False, locale)
            self._persist_document(project_id, document_id, intelligence_id, relative, frontmatter, content)

        # Review summaries (TASK-003): one compact document per completed review.
        for review in (self.platform.get_review(item["id"]) for item in self._completed_reviews(project_id)):
            if not review:
                continue
            findings = [item for item in self.platform.findings(project_id) if item.get("review_id") == review["id"]]
            target = self._review_document_path(root, review["id"])
            relative = self._relative(root, target)
            document_id = f"DOC-REV-{review['id']}"
            generated_at = self._stable_generated_at(project_id, relative)
            content, frontmatter = self._render_review_document(project_id, review, findings, generated_at)
            existing = self.repository.get_knowledge_document_by_path(project_id, relative)
            self._write_if_changed(root, target, content, relative, blocked, report, existing.get("content_hash") if existing else None, False, locale)
            self._persist_document(project_id, document_id, review["id"], relative, frontmatter, content)

        # Finding projections (TASK-003): only persisted final findings, updated in place.
        for finding in self.platform.findings(project_id):
            target = self._finding_document_path(root, finding["id"])
            relative = self._relative(root, target)
            document_id = f"DOC-FS-{finding['id']}"
            generated_at = self._stable_generated_at(project_id, relative)
            content, frontmatter = self._render_finding_document(finding, generated_at)
            existing = self.repository.get_knowledge_document_by_path(project_id, relative)
            self._write_if_changed(root, target, content, relative, blocked, report, existing.get("content_hash") if existing else None, False, locale)
            self._persist_document(project_id, document_id, finding["id"], relative, frontmatter, content)

        # Knowledge projections (TASK-004): eligible lifecycle states only.
        # REV-028: writes always go to the canonical five-area path; legacy
        # files found during the scan remain untouched migration inputs.
        for record in records:
            if str(record.get("state") or "").upper() not in KNOWLEDGE_ELIGIBLE_STATES:
                report.skipped.append(f"{record['id']}: skipped — state {str(record.get('state') or 'UNKNOWN')} is not eligible for vault projection")
                continue
            path = self._document_path(root, record)
            relative = self._relative(root, path)
            migrated_from_legacy = record["id"] in legacy_migrated
            if relative in blocked or path.is_symlink():
                if relative not in blocked:
                    blocked.add(relative)
                    self._quarantine(report, root, path, "refusing to overwrite a symlink")
                continue
            links = self.repository.list_intelligence_links(record["id"])
            evidence = self.repository.list_intelligence_evidence(record["id"], limit=12)
            frontmatter = self._knowledge_frontmatter(record, links, evidence)
            existing = self.repository.get_knowledge_document_by_intelligence(record["id"])
            stored_body = existing.get("body") if existing and existing.get("source_status") == "ENGINEER_EDITED" else None
            if stored_body and "## Engineer Notes" in stored_body:
                stored_body = self._engineer_notes(stored_body)
            notes = imported_notes.get(record["id"], stored_body)
            related = self._knowledge_related_links(frontmatter.get("relationships") or {})
            rendered = self._render(frontmatter, record, evidence, notes, related)
            rendered_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            actual_hash: str | None = None
            if path.exists():
                try:
                    _frontmatter, _body, actual_hash = self._parse_markdown(path)
                except (OSError, UnicodeError, ValueError, ValidationError) as error:
                    blocked.add(relative)
                    self._quarantine(report, root, path, str(error))
                    continue
            stored_hash = existing.get("content_hash") if existing else None
            if actual_hash is not None and stored_hash != actual_hash and relative not in imported_paths:
                if not regenerate:
                    report.stale += 1
                    blocked.add(relative)
                    report.errors.append(f"{relative}: {KB_MSG('error.vault_external_edit_regen', locale)}")
                    continue
            if actual_hash == rendered_hash and not migrated_from_legacy:
                report.unchanged += 1
                continue
            try:
                self._atomic_write(path, rendered)
            except (OSError, ValueError) as error:
                blocked.add(relative)
                self._quarantine(report, root, path, str(error))
                continue
            rendered_body = self._rendered_body(rendered)
            imported = relative in imported_paths
            if migrated_from_legacy:
                # Canonical re-projection of a valid legacy document: count the
                # migration explicitly, keep stable document identity, and never
                # treat this as a duplicate record.
                report.migrated += 1
                report.unchanged += 0
            elif imported:
                report.regenerated += 1
            elif existing:
                report.regenerated += 1 if regenerate or actual_hash == stored_hash else 0
                report.updated += 0 if regenerate or actual_hash == stored_hash else 1
            else:
                report.updated += 1
            engineer_edited = bool(existing and existing.get("source_status") == "ENGINEER_EDITED") or imported
            if migrated_from_legacy:
                engineer_edited = engineer_edited or bool(imported_notes.get(record["id"]))
            self.repository.upsert_knowledge_document({
                # Stable identity: one KnowledgeDocument per intelligence record,
                # converging on the canonical relative path (REV-028).
                "id": existing["id"] if existing else f"DOC-{record['id']}",
                "project_id": project_id,
                "intelligence_id": record["id"],
                "relative_path": relative,
                "content_hash": rendered_hash,
                "frontmatter": frontmatter,
                "body": rendered_body,
                "sync_status": KnowledgeDocStatus.EDITED.value if engineer_edited else KnowledgeDocStatus.SYNCED.value,
                "source_status": "ENGINEER_EDITED" if engineer_edited else "GENERATED",
                "schema_version": 1,
                "last_indexed_at": existing.get("last_indexed_at") if existing else None,
                "created_at": existing["created_at"] if existing else _now(),
                "updated_at": _now(),
            })
        self._reconcile_legacy_documents(project_id, root, report, blocked, records_by_id)
        if persist_state:
            self._record_sync_state(project_id, report, "FAILED" if report.errors else "SYNCED")
        return report

    def record_sync_state(self, project_id: str, report: VaultSyncReport, status: str, error_summary: str | None = None) -> bool:
        """Public entry point for the single durable sync-state write (REV-036).

        The atomic projection-plus-index workflow owns exactly one final durable
        state; it calls this while holding the project lock after projection and
        indexing have both resolved. ``error_summary`` lets that workflow store
        its own fixed safe message (for example the ``INDEX_FAILED`` summary that
        is not derived from the projection report). Only bounded counts and a
        bounded error summary are stored — never raw source, provider responses,
        or full file contents.

        Returns ``True`` only when the durable upsert actually landed and
        ``False`` otherwise (REV-038), so the workflow can fall back to its typed
        outcome instead of a stale persisted row.
        """
        return self._record_sync_state(project_id, report, status, error_summary)

    def _record_sync_state(
        self,
        project_id: str,
        report: VaultSyncReport,
        status: str,
        error_summary: str | None = None,
    ) -> bool:
        """Persist a compact, safe last-sync outcome for the project UI (TASK-005).

        Only bounded counts and a bounded error summary are stored — never raw
        source, provider responses, or full file contents. Returns ``True`` when
        the durable upsert landed, ``False`` when bookkeeping failed (REV-038).
        """
        counts = {
            "scanned": report.scanned,
            "updated": report.updated,
            "regenerated": report.regenerated,
            "unchanged": report.unchanged,
            "quarantined": report.quarantined,
            "stale": report.stale,
            "migrated": report.migrated,
            "legacy_left_in_place": report.legacy_left_in_place,
            "warnings": len(report.warnings),
            "skipped": len(report.skipped),
            "documents": len(self.repository.list_knowledge_documents(project_id)),
        }
        # Non-fatal migration warnings are surfaced alongside any hard errors,
        # but only errors flip the durable status (REV-029 acceptance). An explicit
        # caller summary (the atomic workflow) always wins.
        if error_summary is None:
            error_summary = "; ".join(report.errors[:3] + report.warnings[:2]) if report.errors or report.warnings else None
        try:
            self.repository.upsert_knowledge_sync_state({
                "project_id": project_id,
                "status": status,
                "counts": counts,
                "error_summary": " ".join(error_summary.split())[:400] if error_summary else None,
                "updated_at": _now(),
            })
            return True
        except Exception:  # noqa: BLE001 - sync-state bookkeeping must never break a sync
            return False

    # --- Review / finding helpers -----------------------------------------

    def _completed_reviews(self, project_id: str) -> list[dict[str, Any]]:
        """Completed review ids for this project only (compact summaries)."""
        rows = self.platform.list_reviews(project_id)
        return [row for row in rows if str(row.get("status")) in {"COMPLETED", "PARTIAL"}][:24]

    # --- Legacy reconciliation (TASK-001) ----------------------------------

    def _reconcile_legacy_documents(
        self,
        project_id: str,
        root: Path,
        report: VaultSyncReport,
        blocked: set[str],
        records_by_id: dict[str, dict[str, Any]],
    ) -> None:
        """Report legacy layout artifacts without deleting anything.

        Valid legacy generated documents were already re-projected through the
        normal knowledge write path above (their intelligence id resolved and
        the canonical path is written fresh). Here we only count what remains
        on disk so the migration status is visible and auditable. User-owned
        and unknown files are untouched, always.
        """
        legacy_dirs = [root / name for name in LEGACY_LAYOUT if name not in LAYOUT]
        for directory in legacy_dirs:
            if not directory.exists() or directory.is_symlink():
                continue
            for path in self._markdown_files(directory):
                report.scanned += 1
                try:
                    frontmatter, _body, _hash = self._parse_markdown(path)
                except (OSError, UnicodeError, ValueError, ValidationError):
                    continue  # quarantined during the main scan if relevant
                if isinstance(frontmatter, KnowledgeFrontmatter) and frontmatter.project_id == project_id:
                    report.legacy_left_in_place += 1
                    detail = f"{self._relative(root, path)}: legacy layout document left in place (non-destructive migration)"
                    if detail not in report.warnings:
                        report.warnings.append(detail)
