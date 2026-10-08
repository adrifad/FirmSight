from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import intelligence_core as core
from .schemas import canonical_intelligence_type, canonical_memory_type


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryRepository:
    """Owns Engineering Memory / Project Intelligence persistence.

    The memories table keeps its primary identity and legacy columns; the
    intelligence lifecycle (state, confidence, origin, observation counters)
    and normalized child rows (evidence, links, observations, conflicts,
    learning jobs, review summaries) extend it. Migration is idempotent and
    transactional: existing memories are backfilled to canonical states
    without destructive resets.
    """

    INTELLIGENCE_SCHEMA_VERSION = 2

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        self.fts_available = False
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._migrate_legacy_memories()
        self._migrate_canonical_types()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_proposals (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, type TEXT NOT NULL,
                    statement TEXT NOT NULL, scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    source_json TEXT NOT NULL, proposed_by TEXT NOT NULL, commit_sha TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, type TEXT NOT NULL,
                    statement TEXT NOT NULL, scope_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    source_json TEXT NOT NULL, status TEXT NOT NULL, proposed_by TEXT NOT NULL,
                    approved_by TEXT NOT NULL, commit_sha TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revalidation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
                    commit_sha TEXT, observation_json TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL, FOREIGN KEY(memory_id) REFERENCES memories(id)
                );
                CREATE INDEX IF NOT EXISTS idx_memories_project_status ON memories(project_id, status);
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intelligence_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    kind TEXT NOT NULL, file TEXT, line INTEGER, symbol TEXT, file_hash TEXT,
                    description TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id)
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_evidence_memory ON intelligence_evidence(memory_id);
                CREATE INDEX IF NOT EXISTS idx_intelligence_evidence_fingerprint ON intelligence_evidence(memory_id, fingerprint);
                CREATE INDEX IF NOT EXISTS idx_intelligence_evidence_file ON intelligence_evidence(project_id, file);
                CREATE TABLE IF NOT EXISTS intelligence_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    link_kind TEXT NOT NULL, link_value TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id)
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_links_lookup ON intelligence_links(project_id, link_kind, link_value);
                CREATE INDEX IF NOT EXISTS idx_intelligence_links_memory ON intelligence_links(memory_id);
                CREATE TABLE IF NOT EXISTS intelligence_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    kind TEXT NOT NULL, from_state TEXT, to_state TEXT, confidence REAL, confidence_delta REAL,
                    fingerprint TEXT, review_id TEXT, finding_id TEXT, chat_message_id TEXT, job_id TEXT,
                    detail TEXT, created_at TEXT NOT NULL,
                    FOREIGN KEY(memory_id) REFERENCES memories(id)
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_observations_memory ON intelligence_observations(memory_id, id);
                CREATE INDEX IF NOT EXISTS idx_intelligence_observations_finding ON intelligence_observations(finding_id);
                CREATE TABLE IF NOT EXISTS intelligence_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, snapshot_json TEXT NOT NULL, resolution_state TEXT NOT NULL,
                    resolved_observation_id INTEGER, detail TEXT, created_at TEXT NOT NULL, resolved_at TEXT,
                    FOREIGN KEY(memory_id) REFERENCES memories(id)
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_conflicts_memory ON intelligence_conflicts(memory_id, resolution_state);
                CREATE TABLE IF NOT EXISTS intelligence_learning_jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, trigger TEXT NOT NULL,
                    trigger_payload_json TEXT NOT NULL, status TEXT NOT NULL,
                    progress_json TEXT NOT NULL DEFAULT '[]', error TEXT, prompt_version TEXT,
                    created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_intelligence_learning_jobs_status ON intelligence_learning_jobs(project_id, status, created_at);
                CREATE TABLE IF NOT EXISTS review_learning_summaries (
                    review_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT,
                    status TEXT NOT NULL, counts_json TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_review_learning_summaries_project ON review_learning_summaries(project_id);
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, intelligence_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL, content_hash TEXT NOT NULL,
                    frontmatter_json TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
                    sync_status TEXT NOT NULL, source_status TEXT NOT NULL,
                    external_modified_at TEXT, schema_version INTEGER NOT NULL,
                    last_indexed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(project_id, relative_path),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_project ON knowledge_documents(project_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_documents_intelligence ON knowledge_documents(intelligence_id);
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    intelligence_id TEXT, ordinal INTEGER NOT NULL, heading_path TEXT NOT NULL,
                    body TEXT NOT NULL, content_hash TEXT NOT NULL,
                    source_span_start INTEGER, source_span_end INTEGER,
                    metadata_json TEXT NOT NULL, embedding BLOB, embedding_provider TEXT,
                    embedding_version TEXT, created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_project ON knowledge_chunks(project_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_document ON knowledge_chunks(document_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_intelligence ON knowledge_chunks(intelligence_id);
                CREATE TABLE IF NOT EXISTS knowledge_wikilinks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                    document_id TEXT NOT NULL, chunk_id TEXT, target_id TEXT, target_path TEXT,
                    link_kind TEXT NOT NULL, validation_status TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES knowledge_documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_wikilinks_project ON knowledge_wikilinks(project_id, target_id);
                CREATE INDEX IF NOT EXISTS idx_knowledge_wikilinks_document ON knowledge_wikilinks(document_id);
                CREATE TABLE IF NOT EXISTS knowledge_index_state (
                    project_id TEXT PRIMARY KEY, document_version TEXT, content_hash TEXT,
                    frontmatter_hash TEXT, index_version INTEGER NOT NULL, status TEXT NOT NULL,
                    last_success_at TEXT, last_error TEXT, quarantine_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS knowledge_sync_state (
                    project_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    counts_json TEXT NOT NULL, error_summary TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                """
            )
            # Project-scoped full-text search when FTS5 is available; the LIKE
            # fallback in the query helpers keeps retrieval correct otherwise.
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5("
                    "chunk_id UNINDEXED, project_id UNINDEXED, body, tokenize='unicode61')"
                )
                self.fts_available = True
            except sqlite3.OperationalError:
                self.fts_available = False
            # Idempotency for learning jobs (FS-DEV-012): identical triggers must
            # not enqueue duplicate work after retries.
            self._ensure_column(conn, "intelligence_learning_jobs", "idempotency_key", "TEXT")
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_jobs_idempotency "
                    "ON intelligence_learning_jobs(project_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
            except sqlite3.IntegrityError:
                # Preserve every legacy job for auditability, but retain the
                # first row as the canonical idempotent job. Duplicate legacy
                # rows are explicitly unkeyed rather than silently deleted.
                duplicates = conn.execute(
                    """SELECT project_id, idempotency_key, GROUP_CONCAT(id) AS ids
                       FROM intelligence_learning_jobs
                       WHERE idempotency_key IS NOT NULL
                       GROUP BY project_id, idempotency_key HAVING COUNT(*) > 1"""
                ).fetchall()
                for duplicate in duplicates:
                    ids = [value for value in str(duplicate["ids"]).split(",") if value]
                    for job_id in ids[1:]:
                        conn.execute("UPDATE intelligence_learning_jobs SET idempotency_key=NULL WHERE id=?", (job_id,))
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_jobs_idempotency "
                    "ON intelligence_learning_jobs(project_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
            # Idempotent column additions for the intelligence lifecycle.
            for column, definition in (
                ("state", "TEXT"),
                ("confidence", "REAL"),
                ("origin", "TEXT"),
                ("first_observed_at", "TEXT"),
                ("last_observed_at", "TEXT"),
                ("last_validated_at", "TEXT"),
                ("last_validated_commit", "TEXT"),
                ("last_validated_git_status", "TEXT"),
                ("observation_count", "INTEGER NOT NULL DEFAULT 1"),
                ("reinforcement_count", "INTEGER NOT NULL DEFAULT 0"),
                ("superseded_by", "TEXT"),
                ("conflict_summary", "TEXT"),
                ("fingerprint", "TEXT"),
            ):
                self._ensure_column(conn, "memories", column, definition)
            # Legacy rows never had these columns; give them explicit defaults.
            conn.execute("UPDATE memories SET observation_count = 1 WHERE observation_count IS NULL")
            conn.execute("UPDATE memories SET reinforcement_count = 0 WHERE reinforcement_count IS NULL")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_legacy_memories(self) -> None:
        """Backfill legacy memories into the intelligence lifecycle.

        ACTIVE becomes REINFORCED with LEGACY_ENGINEER_APPROVED origin and
        confidence 0.75 (below the 0.85 VERIFIED threshold): engineer-approved
        knowledge keeps its provenance but still needs current-source
        revalidation before it can claim VERIFIED. Types map to the Project
        Intelligence vocabulary. All work runs in one transaction with the
        schema-version marker; a crash rolls back and the next start retries.
        """
        with self._connect() as conn:
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 1"
            ).fetchone()
            if already:
                return
            legacy_rows = conn.execute("SELECT * FROM memories WHERE state IS NULL").fetchall()
            for row in legacy_rows:
                record = self._decode_row(row)
                state = core.LEGACY_STATE_MIGRATION.get(record["status"])
                if state is None:
                    # Unknown legacy status is never silently trusted.
                    state = core.NEEDS_REVALIDATION
                canonical_type = canonical_memory_type(record["type"])
                scope = record["scope"]
                evidence_items = record["evidence"]
                primary_links = [scope.get("symbol") or "", scope.get("component") or ""]
                evidence_locations = [f"{item.get('file') or ''}:{item.get('line') or 0}" for item in evidence_items]
                fingerprint = core.compute_fingerprint(record["project_id"], canonical_type, record["statement"], primary_links, evidence_locations)
                status = core.legacy_status_for(state)
                now = _now()
                conn.execute(
                    """UPDATE memories SET type=?, state=?, status=?, confidence=?, origin=?,
                       first_observed_at=?, last_observed_at=?, observation_count=1, reinforcement_count=0,
                       superseded_by=NULL, conflict_summary=NULL, fingerprint=? WHERE id=?""",
                    (
                        canonical_type, state, status, 0.75, "LEGACY_ENGINEER_APPROVED",
                        record["created_at"], record["updated_at"], fingerprint, record["id"],
                    ),
                )
                links: list[tuple[Any, ...]] = []
                if scope.get("symbol"):
                    links.append((record["id"], record["project_id"], "SYMBOL", scope["symbol"], "PRIMARY", now))
                if scope.get("component"):
                    links.append((record["id"], record["project_id"], "COMPONENT", scope["component"], "PRIMARY", now))
                for item in evidence_items:
                    if item.get("symbol"):
                        links.append((record["id"], record["project_id"], "SYMBOL", item["symbol"], "SUPPORTING", now))
                    if item.get("file"):
                        links.append((record["id"], record["project_id"], "FILE", item["file"], "SUPPORTING", now))
                if links:
                    conn.executemany(
                        "INSERT INTO intelligence_links(memory_id,project_id,link_kind,link_value,role,created_at) VALUES (?,?,?,?,?,?)",
                        links,
                    )
                for item in evidence_items:
                    description = item.get("description") or "Legacy evidence migrated from Engineering Memory."
                    conn.execute(
                        """INSERT INTO intelligence_evidence(memory_id,project_id,kind,file,line,symbol,file_hash,description,fingerprint,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (
                            record["id"], record["project_id"], "ENGINEER", item.get("file"), item.get("line"),
                            item.get("symbol"), None, description[:2000],
                            core.evidence_fingerprint("ENGINEER", item.get("file"), item.get("line"), item.get("symbol"), description),
                            now,
                        ),
                    )
                conn.execute(
                    """INSERT INTO intelligence_observations(memory_id,project_id,kind,from_state,to_state,confidence,fingerprint,detail,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        record["id"], record["project_id"], "MIGRATION", record["status"], state, 0.75, fingerprint,
                        f"Migrated legacy {record['type']} memory ({record['status']}) to {state} with canonical type {canonical_type}.",
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (_now(),),
            )

    def _migrate_canonical_types(self) -> None:
        """FS-DEV-012 canonical type migration (version 2 marker).

        ARCHITECTURAL_PATTERN / BEHAVIORAL_PATTERN / ENGINEERING_PATTERN map to
        ARCHITECTURE_KNOWLEDGE. BUG_PATTERN becomes CONFIRMED_BUG_PATTERN only
        when record evidence/state supports it (VERIFIED or REINFORCED with
        evidence rows); otherwise the legacy value is retained. Fingerprints
        are recomputed for retyped rows so duplicate matching stays correct.
        Idempotent and transactional.
        """
        with self._connect() as conn:
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (self.INTELLIGENCE_SCHEMA_VERSION,)
            ).fetchone()
            if already:
                return
            rows = conn.execute(
                "SELECT id, project_id, type, statement, state, scope_json, evidence_json, fingerprint FROM memories"
            ).fetchall()
            for row in rows:
                evidence = json.loads(row["evidence_json"] or "[]")
                canonical = canonical_intelligence_type(row["type"], row["state"], bool(evidence))
                if canonical == row["type"]:
                    continue
                scope = json.loads(row["scope_json"] or "{}")
                primary_links = [scope.get("symbol") or "", scope.get("component") or ""]
                evidence_locations = [f"{item.get('file') or ''}:{item.get('line') or 0}" for item in evidence]
                fingerprint = core.compute_fingerprint(row["project_id"], canonical, row["statement"], primary_links, evidence_locations)
                conn.execute(
                    "UPDATE memories SET type = ?, fingerprint = ? WHERE id = ?",
                    (canonical, fingerprint, row["id"]),
                )
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (self.INTELLIGENCE_SCHEMA_VERSION, _now()),
            )

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    @staticmethod
    def _decode_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for column in ("scope_json", "evidence_json", "source_json"):
            if column in result:
                result[column.removesuffix("_json")] = json.loads(result.pop(column))
        return result

    # --- Legacy memory surface (compatibility) ---

    def create_proposal(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memory_proposals VALUES (:id,:project_id,:type,:statement,:scope_json,:evidence_json,:source_json,:proposed_by,:commit_sha,:created_at)",
                {**record, "scope_json": self._encode(record["scope"]), "evidence_json": self._encode(record["evidence"]), "source_json": self._encode(record["source"])},
            )

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._decode_row(conn.execute("SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)).fetchone())

    def update_proposal(self, proposal_id: str, changes: dict[str, Any]) -> None:
        values: dict[str, Any] = {}
        for key, value in changes.items():
            column = f"{key}_json" if key in {"scope", "evidence", "source"} else key
            values[column] = self._encode(value) if key in {"scope", "evidence", "source"} else value
        assignments = ", ".join(f"{column} = :{column}" for column in values)
        values["id"] = proposal_id
        with self._connect() as conn:
            conn.execute(f"UPDATE memory_proposals SET {assignments} WHERE id = :id", values)

    def delete_proposal(self, proposal_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_proposals WHERE id = ?", (proposal_id,))

    def create_memory(self, record: dict[str, Any]) -> None:
        """Insert one memory, canonicalizing state/status for both callers.

        Intelligence callers pass ``state``; the legacy approval path passes
        ``status``. Whichever is supplied, both columns end up consistent
        (status is always the legacy projection of the canonical state).
        """
        record = self._normalized_memory_record(record)
        with self._connect() as conn:
            self._insert_memory(conn, record)

    @staticmethod
    def _normalized_memory_record(record: dict[str, Any]) -> dict[str, Any]:
        state = record.get("state") or core.LEGACY_STATE_MIGRATION.get(record.get("status", ""), record.get("status", ""))
        return {
            **record,
            "type": canonical_memory_type(record["type"]),
            "state": state,
            "status": core.legacy_status_for(state),
            "confidence": record.get("confidence", 0.7),
            "origin": record.get("origin", "ENGINEER_APPROVED"),
            "first_observed_at": record.get("first_observed_at", record["created_at"]),
            "last_observed_at": record.get("last_observed_at", record["created_at"]),
            "last_validated_at": record.get("last_validated_at"),
            "last_validated_commit": record.get("last_validated_commit"),
            "last_validated_git_status": record.get("last_validated_git_status"),
            "observation_count": record.get("observation_count", 1),
            "reinforcement_count": record.get("reinforcement_count", 0),
            "superseded_by": record.get("superseded_by"),
            "conflict_summary": record.get("conflict_summary"),
            "fingerprint": record.get("fingerprint"),
        }

    @staticmethod
    def _insert_memory(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO memories (
                       id, project_id, type, statement, scope_json, evidence_json, source_json,
                       status, proposed_by, approved_by, commit_sha, created_at, updated_at,
                       state, confidence, origin, first_observed_at, last_observed_at,
                       last_validated_at, last_validated_commit, last_validated_git_status,
                       observation_count, reinforcement_count, superseded_by, conflict_summary, fingerprint
                   ) VALUES (
                       :id,:project_id,:type,:statement,:scope_json,:evidence_json,:source_json,
                       :status,:proposed_by,:approved_by,:commit_sha,:created_at,:updated_at,
                       :state,:confidence,:origin,:first_observed_at,:last_observed_at,
                       :last_validated_at,:last_validated_commit,:last_validated_git_status,
                       :observation_count,:reinforcement_count,:superseded_by,:conflict_summary,:fingerprint
                   )""",
                {**record, "scope_json": json.dumps(record["scope"], sort_keys=True), "evidence_json": json.dumps(record["evidence"], sort_keys=True), "source_json": json.dumps(record["source"], sort_keys=True)},
            )

    def create_memory_if_absent(self, record: dict[str, Any]) -> tuple[str, bool]:
        """Atomically insert a fingerprint or return the existing live record."""
        normalized = self._normalized_memory_record(record)
        fingerprint = normalized.get("fingerprint")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if fingerprint:
                row = conn.execute(
                    "SELECT id FROM memories WHERE project_id=? AND fingerprint=? AND state NOT IN ('SUPERSEDED','DISABLED') LIMIT 1",
                    (normalized["project_id"], fingerprint),
                ).fetchone()
                if row:
                    return str(row["id"]), False
            self._insert_memory(conn, normalized)
            return str(normalized["id"]), True

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._decode_row(conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone())

    def list_memories(self, project_id: str, status: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM memories WHERE project_id = ?", [project_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            return [self._decode_row(row) for row in conn.execute(query, params).fetchall()]

    def update_memory(self, memory_id: str, changes: dict[str, Any]) -> None:
        """Apply partial updates, keeping state/status projections consistent."""
        changes = dict(changes)
        if "state" in changes:
            changes.pop("status", None)
            changes["status"] = core.legacy_status_for(changes["state"])
        elif "status" in changes:
            changes["state"] = core.LEGACY_STATE_MIGRATION.get(changes["status"], changes["status"])
        values: dict[str, Any] = {}
        for key, value in changes.items():
            values[f"{key}_json" if key in {"scope", "evidence", "source"} else key] = self._encode(value) if key in {"scope", "evidence", "source"} else value
        if values:
            assignments = ", ".join(f"{column} = :{column}" for column in values)
            values["id"] = memory_id
            with self._connect() as conn:
                conn.execute(f"UPDATE memories SET {assignments} WHERE id = :id", values)

    def add_revalidation_events(self, events: Iterable[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO revalidation_events(memory_id,commit_sha,observation_json,reason,created_at) VALUES (:memory_id,:commit_sha,:observation_json,:reason,:created_at)",
                [{**event, "observation_json": self._encode(event["observation"])} for event in events],
            )

    def list_revalidation_events(self, memory_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT memory_id, commit_sha, observation_json, reason, created_at FROM revalidation_events WHERE memory_id = ? ORDER BY id DESC",
                (memory_id,),
            ).fetchall()
        return [
            {
                "memory_id": row["memory_id"],
                "commit_sha": row["commit_sha"],
                "observation": json.loads(row["observation_json"]),
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # --- Project Intelligence queries ---

    def list_intelligence(self, project_id: str, state: str | None = None, memory_type: str | None = None, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM memories WHERE project_id = ?", [project_id]
        if state:
            query += " AND state = ?"
            params.append(state)
        if memory_type:
            query += " AND type = ?"
            params.append(canonical_memory_type(memory_type))
        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._connect() as conn:
            return [self._decode_row(row) for row in conn.execute(query, params).fetchall()]

    def intelligence_counts(self, project_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, type, COUNT(*) AS total FROM memories WHERE project_id = ? GROUP BY state, type",
                (project_id,),
            ).fetchall()
        by_state: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for row in rows:
            by_state[row["state"]] = by_state.get(row["state"], 0) + row["total"]
            by_type[row["type"]] = by_type.get(row["type"], 0) + row["total"]
        return {
            "total": sum(by_state.values()),
            "provisional": by_state.get("PROVISIONAL", 0),
            "reinforced": by_state.get("REINFORCED", 0),
            "verified": by_state.get("VERIFIED", 0),
            "needs_revalidation": by_state.get("NEEDS_REVALIDATION", 0),
            "conflicted": by_state.get("CONFLICTED", 0),
            "superseded": by_state.get("SUPERSEDED", 0),
            "disabled": by_state.get("DISABLED", 0),
            "by_type": by_type,
        }

    def find_memory_by_fingerprint(self, project_id: str, fingerprint: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE project_id = ? AND fingerprint = ?", (project_id, fingerprint)
            ).fetchone()
        return self._decode_row(row)

    def find_memories_by_link_values(self, project_id: str, link_values: list[str], memory_type: str | None = None, limit: int = 12) -> list[dict[str, Any]]:
        if not link_values:
            return []
        normalized = [value.strip().casefold() for value in link_values if value and value.strip()]
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        query = f"""
            SELECT m.* FROM memories m
            JOIN intelligence_links l ON l.memory_id = m.id
            WHERE m.project_id = ? AND m.state NOT IN ('SUPERSEDED', 'DISABLED')
              AND LOWER(l.link_value) IN ({placeholders})
        """
        params: list[Any] = [project_id, *normalized]
        if memory_type:
            query += " AND m.type = ?"
            params.append(canonical_memory_type(memory_type))
        query += " GROUP BY m.id ORDER BY m.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [self._decode_row(row) for row in conn.execute(query, params).fetchall()]

    def retrievable_intelligence(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE project_id = ? AND state IN ('VERIFIED','REINFORCED') ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def provisional_intelligence(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memories WHERE project_id = ? AND state = 'PROVISIONAL' ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    # --- Intelligence evidence ---

    def add_intelligence_evidence(self, rows: Iterable[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO intelligence_evidence(memory_id,project_id,kind,file,line,symbol,file_hash,description,fingerprint,created_at)
                   VALUES (:memory_id,:project_id,:kind,:file,:line,:symbol,:file_hash,:description,:fingerprint,:created_at)""",
                [self._bounded_evidence(row) for row in rows],
            )

    @staticmethod
    def _bounded_evidence(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "description": (row.get("description") or "")[:2000]}

    def list_intelligence_evidence(self, memory_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intelligence_evidence WHERE memory_id = ? ORDER BY id DESC LIMIT ?",
                (memory_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def evidence_fingerprints(self, memory_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT fingerprint FROM intelligence_evidence WHERE memory_id = ?", (memory_id,)
            ).fetchall()
        return {row["fingerprint"] for row in rows}

    def find_evidence_files(self, project_id: str) -> list[dict[str, Any]]:
        """Distinct evidence file references for source-authority revalidation."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT e.memory_id, e.file, e.file_hash, e.symbol
                   FROM intelligence_evidence e JOIN memories m ON m.id = e.memory_id
                   WHERE e.project_id = ? AND e.file IS NOT NULL AND m.state NOT IN ('SUPERSEDED','DISABLED')""",
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Intelligence links ---

    def add_intelligence_links(self, rows: Iterable[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO intelligence_links(memory_id,project_id,link_kind,link_value,role,created_at) VALUES (:memory_id,:project_id,:link_kind,:link_value,:role,:created_at)",
                list(rows),
            )

    def list_intelligence_links(self, memory_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM intelligence_links WHERE memory_id = ? ORDER BY id", (memory_id,)).fetchall()]

    def links_for_memories(self, memory_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM intelligence_links WHERE memory_id IN ({placeholders}) ORDER BY id", memory_ids
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["memory_id"], []).append(dict(row))
        return grouped

    def evidence_for_memories(self, memory_ids: list[str], limit_per_memory: int = 5) -> dict[str, list[dict[str, Any]]]:
        if not memory_ids:
            return {}
        placeholders = ",".join("?" for _ in memory_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM intelligence_evidence WHERE memory_id IN ({placeholders}) ORDER BY id DESC", memory_ids
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            items = grouped.setdefault(row["memory_id"], [])
            if len(items) < limit_per_memory:
                items.append(dict(row))
        return grouped

    # --- Intelligence observations ---

    def add_intelligence_observation(self, row: dict[str, Any]) -> None:
        row = {
            "confidence": None, "confidence_delta": None, "fingerprint": None,
            "review_id": None, "finding_id": None, "chat_message_id": None, "job_id": None,
            **row,
        }
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO intelligence_observations(memory_id,project_id,kind,from_state,to_state,confidence,confidence_delta,
                   fingerprint,review_id,finding_id,chat_message_id,job_id,detail,created_at)
                   VALUES (:memory_id,:project_id,:kind,:from_state,:to_state,:confidence,:confidence_delta,
                   :fingerprint,:review_id,:finding_id,:chat_message_id,:job_id,:detail,:created_at)""",
                {**row, "detail": (row.get("detail") or "")[:2000]},
            )

    def list_intelligence_observations(self, memory_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intelligence_observations WHERE memory_id = ? ORDER BY id DESC LIMIT ?",
                (memory_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Intelligence conflicts ---

    def add_intelligence_conflict(self, row: dict[str, Any]) -> int:
        row = {"resolved_observation_id": None, "resolved_at": None, "detail": None, **row}
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO intelligence_conflicts(memory_id,project_id,evidence_json,snapshot_json,resolution_state,resolved_observation_id,detail,created_at,resolved_at)
                   VALUES (:memory_id,:project_id,:evidence_json,:snapshot_json,:resolution_state,:resolved_observation_id,:detail,:created_at,:resolved_at)""",
                {
                    **row,
                    "evidence_json": self._encode(row.get("evidence") or {}),
                    "snapshot_json": self._encode(row.get("snapshot") or {}),
                    "detail": (row.get("detail") or "")[:2000],
                },
            )
            return int(cursor.lastrowid or 0)

    def list_intelligence_conflicts(self, memory_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM intelligence_conflicts WHERE memory_id = ? ORDER BY id DESC", (memory_id,)
            ).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            record["evidence"] = json.loads(record.pop("evidence_json"))
            record["snapshot"] = json.loads(record.pop("snapshot_json"))
            result.append(record)
        return result

    def open_conflict_count(self, project_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM intelligence_conflicts WHERE project_id = ? AND resolution_state = 'OPEN'",
                (project_id,),
            ).fetchone()
        return int(row["total"])

    def resolve_conflicts(self, memory_id: str, resolution_state: str, resolved_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE intelligence_conflicts SET resolution_state = ?, resolved_at = ? WHERE memory_id = ? AND resolution_state = 'OPEN'",
                (resolution_state, resolved_at, memory_id),
            )

    # --- Learning jobs ---

    def create_learning_job(self, record: dict[str, Any]) -> bool:
        """Insert one learning job. Returns False when an identical job
        (same idempotency key) already exists, so retries never double-process."""
        record = {"error": None, "prompt_version": None, "started_at": None, "completed_at": None, "idempotency_key": None, **record}
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO intelligence_learning_jobs(id,project_id,trigger,trigger_payload_json,status,progress_json,error,prompt_version,created_at,started_at,completed_at,idempotency_key)
                       VALUES (:id,:project_id,:trigger,:trigger_payload_json,:status,:progress_json,:error,:prompt_version,:created_at,:started_at,:completed_at,:idempotency_key)""",
                    {**record, "trigger_payload_json": self._encode(record.get("trigger_payload") or {}), "progress_json": self._encode(record.get("progress") or [])},
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def find_learning_job_idempotent(self, project_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM intelligence_learning_jobs WHERE project_id = ? AND idempotency_key = ? ORDER BY created_at DESC LIMIT 1",
                (project_id, idempotency_key),
            ).fetchone()
        return self._decode_job(row)

    def get_learning_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM intelligence_learning_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_job(row)

    @staticmethod
    def _decode_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["trigger_payload"] = json.loads(record.pop("trigger_payload_json") or "{}")
        record["progress"] = json.loads(record.pop("progress_json") or "[]")
        return record

    def update_learning_job(self, job_id: str, changes: dict[str, Any]) -> None:
        changes = dict(changes)
        values: dict[str, Any] = {}
        for key, value in changes.items():
            column = f"{key}_json" if key in {"progress", "trigger_payload"} else key
            values[column] = self._encode(value) if key in {"progress", "trigger_payload"} else value
        if values:
            assignments = ", ".join(f"{column} = :{column}" for column in values)
            values["id"] = job_id
            with self._connect() as conn:
                conn.execute(f"UPDATE intelligence_learning_jobs SET {assignments} WHERE id = :id", values)

    def claim_learning_job(self, job_id: str) -> bool:
        """Atomically move QUEUED -> RUNNING so concurrent executors never double-process."""
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE intelligence_learning_jobs SET status='RUNNING', started_at=? WHERE id=? AND status='QUEUED'",
                (_now(), job_id),
            )
            return cursor.rowcount == 1

    def list_learning_jobs(self, project_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM intelligence_learning_jobs WHERE project_id = ?", [project_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            return [self._decode_job(row) for row in conn.execute(query, params).fetchall()]

    # --- Review learning summaries ---

    def upsert_review_learning_summary(self, record: dict[str, Any]) -> None:
        record = {"job_id": None, "error": None, "counts": {}, **record}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO review_learning_summaries(review_id,project_id,job_id,status,counts_json,error,created_at,updated_at)
                   VALUES (:review_id,:project_id,:job_id,:status,:counts_json,:error,:created_at,:updated_at)
                   ON CONFLICT(review_id) DO UPDATE SET job_id=:job_id, status=:status, counts_json=:counts_json,
                     error=:error, updated_at=:updated_at""",
                {**record, "counts_json": self._encode(record.get("counts") or {})},
            )

    def get_review_learning_summary(self, review_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM review_learning_summaries WHERE review_id = ?", (review_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        record["counts"] = json.loads(record.pop("counts_json") or "{}")
        return record

    # --- Knowledge vault documents / chunks / links / index state (FS-DEV-012) ---

    def upsert_knowledge_sync_state(self, record: dict[str, Any]) -> None:
        """Persist the durable per-project vault projection outcome (FS-KB-015)."""
        record = {"error_summary": None, "counts": {}, **record}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO knowledge_sync_state(project_id,status,counts_json,error_summary,updated_at)
                   VALUES (:project_id,:status,:counts_json,:error_summary,:updated_at)
                   ON CONFLICT(project_id) DO UPDATE SET status=:status, counts_json=:counts_json,
                     error_summary=:error_summary, updated_at=:updated_at""",
                {**record, "counts_json": self._encode(record.get("counts") or {})},
            )

    def get_knowledge_sync_state(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM knowledge_sync_state WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        record["counts"] = json.loads(record.pop("counts_json") or "{}")
        return record

    def upsert_knowledge_document(self, record: dict[str, Any]) -> None:
        record = {"external_modified_at": None, "last_indexed_at": None, "body": "", **record}
        with self._connect() as conn:
            # REV-046: resolve the existing row strictly within this project. The
            # conflict target is (project_id, relative_path) — never the global
            # primary key ``id`` — so a generated document id that happens to
            # repeat across projects (for example a constant "DOC-PRJ-PROJECT")
            # can never make one project's upsert overwrite another project's row
            # and strand that row with a foreign ``content_hash``.
            existing = conn.execute(
                "SELECT id FROM knowledge_documents WHERE project_id=? AND relative_path=?",
                (record["project_id"], record["relative_path"]),
            ).fetchone()
            if existing:
                record["id"] = existing["id"]
            collision = conn.execute(
                "SELECT project_id FROM knowledge_documents WHERE id=?",
                (record["id"],),
            ).fetchone()
            if collision and collision["project_id"] != record["project_id"]:
                # The requested id belongs to another project; mint a
                # project-scoped id so we never mutate that project's row.
                record["id"] = f"{record['id']}-{record['project_id']}"
            conn.execute(
                """INSERT INTO knowledge_documents(id,project_id,intelligence_id,relative_path,content_hash,frontmatter_json,body,
                       sync_status,source_status,external_modified_at,schema_version,last_indexed_at,created_at,updated_at)
                   VALUES (:id,:project_id,:intelligence_id,:relative_path,:content_hash,:frontmatter_json,:body,
                       :sync_status,:source_status,:external_modified_at,:schema_version,:last_indexed_at,:created_at,:updated_at)
                   ON CONFLICT(project_id, relative_path) DO UPDATE SET content_hash=:content_hash,
                       intelligence_id=:intelligence_id,
                       frontmatter_json=:frontmatter_json, body=:body, sync_status=:sync_status, source_status=:source_status,
                       external_modified_at=:external_modified_at, schema_version=:schema_version,
                       last_indexed_at=:last_indexed_at, updated_at=:updated_at""",
                {**record, "frontmatter_json": self._encode(record.get("frontmatter") or {})},
            )

    def get_knowledge_document(self, document_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (document_id,)).fetchone()
        return self._decode_knowledge_document(row)

    def get_knowledge_document_by_path(self, project_id: str, relative_path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_documents WHERE project_id = ? AND relative_path = ?",
                (project_id, relative_path),
            ).fetchone()
        return self._decode_knowledge_document(row)

    def get_knowledge_document_by_intelligence(self, intelligence_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_documents WHERE intelligence_id = ? ORDER BY updated_at DESC LIMIT 1",
                (intelligence_id,),
            ).fetchone()
        return self._decode_knowledge_document(row)

    def list_knowledge_documents(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_documents WHERE project_id = ? ORDER BY relative_path", (project_id,)
            ).fetchall()
        return [self._decode_knowledge_document(row) for row in rows]

    def delete_knowledge_document(self, document_id: str) -> None:
        with self._connect() as conn:
            old_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM knowledge_chunks WHERE document_id = ?", (document_id,)
                ).fetchall()
            ]
            if self.fts_available and old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(f"DELETE FROM knowledge_fts WHERE chunk_id IN ({placeholders})", old_ids)
            conn.execute("DELETE FROM knowledge_wikilinks WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (document_id,))

    def count_knowledge_chunks(self, document_id: str, project_id: str | None = None) -> int:
        query = "SELECT COUNT(*) AS total FROM knowledge_chunks WHERE document_id = ?"
        params: list[Any] = [document_id]
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["total"]) if row else 0

    def mark_knowledge_document_indexed(self, document_id: str, indexed_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE knowledge_documents SET last_indexed_at = ?, updated_at = updated_at WHERE id = ?",
                (indexed_at, document_id),
            )

    def remove_knowledge_index(self, document_id: str, project_id: str | None = None) -> int:
        """Remove one document's derived chunks, links, and FTS rows."""
        query = "SELECT id FROM knowledge_chunks WHERE document_id = ?"
        params: list[Any] = [document_id]
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        with self._connect() as conn:
            old_ids = [row["id"] for row in conn.execute(query, params).fetchall()]
            if self.fts_available and old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(f"DELETE FROM knowledge_fts WHERE chunk_id IN ({placeholders})", old_ids)
            if project_id is None:
                conn.execute("DELETE FROM knowledge_wikilinks WHERE document_id = ?", (document_id,))
                conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            else:
                conn.execute(
                    "DELETE FROM knowledge_wikilinks WHERE document_id = ? AND project_id = ?",
                    (document_id, project_id),
                )
                conn.execute(
                    "DELETE FROM knowledge_chunks WHERE document_id = ? AND project_id = ?",
                    (document_id, project_id),
                )
        return len(old_ids)

    @staticmethod
    def _decode_knowledge_document(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["frontmatter"] = json.loads(record.pop("frontmatter_json") or "{}")
        return record

    def replace_knowledge_chunks(self, document_id: str, project_id: str, chunks: list[dict[str, Any]]) -> int:
        """Replace all chunks for one document atomically; returns rows written."""
        with self._connect() as conn:
            old_ids = [row["id"] for row in conn.execute("SELECT id FROM knowledge_chunks WHERE document_id = ?", (document_id,)).fetchall()]
            conn.execute("DELETE FROM knowledge_chunks WHERE document_id = ?", (document_id,))
            if self.fts_available and old_ids:
                placeholders = ",".join("?" for _ in old_ids)
                conn.execute(f"DELETE FROM knowledge_fts WHERE chunk_id IN ({placeholders})", old_ids)
            for chunk in chunks:
                conn.execute(
                    """INSERT INTO knowledge_chunks(id,document_id,project_id,intelligence_id,ordinal,heading_path,body,
                           content_hash,source_span_start,source_span_end,metadata_json,embedding,embedding_provider,
                           embedding_version,created_at)
                       VALUES (:id,:document_id,:project_id,:intelligence_id,:ordinal,:heading_path,:body,
                           :content_hash,:source_span_start,:source_span_end,:metadata_json,:embedding,:embedding_provider,
                           :embedding_version,:created_at)""",
                    {**chunk, "metadata_json": self._encode(chunk.get("metadata") or {})},
                )
            if self.fts_available:
                for chunk in chunks:
                    conn.execute(
                        "INSERT INTO knowledge_fts(chunk_id, project_id, body) VALUES (?,?,?)",
                        (chunk["id"], project_id, chunk["body"]),
                    )
        return len(chunks)

    def list_knowledge_chunks(self, project_id: str, document_id: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM knowledge_chunks WHERE project_id = ?", [project_id]
        if document_id:
            query += " AND document_id = ?"
            params.append(document_id)
        query += " ORDER BY document_id, ordinal"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_chunk(row) for row in rows]

    @staticmethod
    def _decode_chunk(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["metadata"] = json.loads(record.pop("metadata_json") or "{}")
        return record

    def replace_knowledge_wikilinks(self, document_id: str, project_id: str, links: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM knowledge_wikilinks WHERE document_id = ?", (document_id,))
            for link in links:
                conn.execute(
                    """INSERT INTO knowledge_wikilinks(project_id,document_id,chunk_id,target_id,target_path,link_kind,validation_status,created_at)
                       VALUES (:project_id,:document_id,:chunk_id,:target_id,:target_path,:link_kind,:validation_status,:created_at)""",
                    link,
                )

    def list_knowledge_wikilinks(self, project_id: str, document_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM knowledge_wikilinks WHERE project_id = ?"
        params: list[Any] = [project_id]
        if document_id is not None:
            query += " AND document_id = ?"
            params.append(document_id)
        query += " ORDER BY document_id, id"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def knowledge_wikilinks_for(self, project_id: str, target_ids: list[str], target_paths: list[str] | None = None) -> list[dict[str, Any]]:
        """Documents whose validated wikilinks point at target IDs or paths."""
        target_paths = target_paths or []
        if not target_ids and not target_paths:
            return []
        predicates: list[str] = []
        params: list[Any] = [project_id]
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            predicates.append(f"w.target_id IN ({placeholders})")
            params.extend(target_ids)
        if target_paths:
            placeholders = ",".join("?" for _ in target_paths)
            predicates.append(f"w.target_path IN ({placeholders})")
            params.extend(target_paths)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT d.* FROM knowledge_documents d
                    JOIN knowledge_wikilinks w ON w.document_id = d.id
                    WHERE d.project_id = ? AND w.validation_status = 'VALID' AND ({' OR '.join(predicates)})""",
                params,
            ).fetchall()
        return [self._decode_knowledge_document(row) for row in rows]

    def get_knowledge_index_state(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM knowledge_index_state WHERE project_id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    def set_knowledge_index_state(self, record: dict[str, Any]) -> None:
        record = {"last_error": None, "quarantine_count": 0, "document_version": None, "content_hash": None, "frontmatter_hash": None, **record}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO knowledge_index_state(project_id,document_version,content_hash,frontmatter_hash,index_version,status,last_success_at,last_error,quarantine_count)
                   VALUES (:project_id,:document_version,:content_hash,:frontmatter_hash,:index_version,:status,:last_success_at,:last_error,:quarantine_count)
                   ON CONFLICT(project_id) DO UPDATE SET document_version=:document_version, content_hash=:content_hash,
                     frontmatter_hash=:frontmatter_hash, index_version=:index_version, status=:status,
                     last_success_at=:last_success_at, last_error=:last_error, quarantine_count=:quarantine_count""",
                record,
            )

    @staticmethod
    def _is_fts_query_error(error: sqlite3.OperationalError) -> bool:
        """Recognize parser errors without masking database failures."""
        message = str(error).casefold()
        return message.startswith("fts5:") and any(
            marker in message for marker in ("syntax error", "parse error", "unterminated")
        )

    def knowledge_fts_search_with_status(self, project_id: str, query: str, limit: int = 12) -> tuple[list[dict[str, Any]], bool]:
        """Project-scoped FTS lookup; the flag is false when LIKE was used."""
        if not query.strip():
            return [], False
        with self._connect() as conn:
            if self.fts_available:
                tokens = [
                    token.strip("\"'();*")
                    for token in query.replace("'", " ").split()
                ]
                tokens = [token for token in tokens if token]
                sanitized = " ".join('"' + token.replace('"', '""') + '"' for token in tokens)
                if not sanitized:
                    return [], False
                try:
                    rows = conn.execute(
                        """SELECT c.* FROM knowledge_fts f
                           JOIN knowledge_chunks c ON c.id = f.chunk_id
                           WHERE knowledge_fts MATCH ? AND f.project_id = ?
                           ORDER BY rank LIMIT ?""",
                        (sanitized, project_id, limit),
                    ).fetchall()
                    return [self._decode_chunk(row) for row in rows], True
                except sqlite3.OperationalError as error:
                    if not self._is_fts_query_error(error):
                        raise
            like = f"%{query.strip()[:120]}%"
            rows = conn.execute(
                "SELECT * FROM knowledge_chunks WHERE project_id = ? AND body LIKE ? ORDER BY document_id, ordinal LIMIT ?",
                (project_id, like, limit),
            ).fetchall()
            return [self._decode_chunk(row) for row in rows], False

    def knowledge_fts_search(self, project_id: str, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Compatibility wrapper returning only candidate rows."""
        return self.knowledge_fts_search_with_status(project_id, query, limit)[0]

    # --- Project deletion (scoped, connection-shared) ---
    #
    # The Memory/Project Intelligence tables mostly carry project_id without a
    # foreign key to projects, so deleting a project cannot rely on SQLite
    # cascades. These helpers run on the caller's connection so every statement
    # participates in the single project-deletion transaction. They only touch
    # rows whose project_id matches; shared tables such as application_settings
    # and schema_migrations are never referenced.

    def count_active_learning_jobs(self, conn: sqlite3.Connection, project_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM intelligence_learning_jobs WHERE project_id = ? AND status IN ('QUEUED','RUNNING')",
            (project_id,),
        ).fetchone()
        return int(row["total"]) if row else 0

    def delete_project_data(self, conn: sqlite3.Connection, project_id: str) -> None:
        # Child rows whose foreign keys point at memories must be removed first,
        # then the memory/proposal rows themselves, then job/summary rows.
        conn.execute(
            "DELETE FROM revalidation_events WHERE memory_id IN (SELECT id FROM memories WHERE project_id = ?)",
            (project_id,),
        )
        for table in ("intelligence_evidence", "intelligence_links", "intelligence_observations", "intelligence_conflicts"):
            conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))  # noqa: S608 - fixed table names from a closed literal list
        conn.execute("DELETE FROM memory_proposals WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM memories WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM intelligence_learning_jobs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM review_learning_summaries WHERE project_id = ?", (project_id,))
        # Knowledge vault projections: chunks/wikilinks cascade from documents,
        # but the explicit delete keeps the operation obvious and FTS in sync.
        if self.fts_available:
            conn.execute(
                "DELETE FROM knowledge_fts WHERE project_id = ?",
                (project_id,),
            )
        for table in ("knowledge_wikilinks", "knowledge_chunks", "knowledge_documents", "knowledge_index_state"):
            conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (project_id,))  # noqa: S608 - fixed table names from a closed literal list
