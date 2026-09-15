from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import intelligence_core as core
from .schemas import canonical_memory_type


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

    INTELLIGENCE_SCHEMA_VERSION = 1

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._migrate_legacy_memories()

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
                """
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
                "SELECT 1 FROM schema_migrations WHERE version = ?", (self.INTELLIGENCE_SCHEMA_VERSION,)
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
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?,?)",
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
        state = record.get("state") or core.LEGACY_STATE_MIGRATION.get(record.get("status", ""), record.get("status", ""))
        record = {
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
        with self._connect() as conn:
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
                {**record, "scope_json": self._encode(record["scope"]), "evidence_json": self._encode(record["evidence"]), "source_json": self._encode(record["source"])},
            )

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

    def create_learning_job(self, record: dict[str, Any]) -> None:
        record = {"error": None, "prompt_version": None, "started_at": None, "completed_at": None, **record}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO intelligence_learning_jobs(id,project_id,trigger,trigger_payload_json,status,progress_json,error,prompt_version,created_at,started_at,completed_at)
                   VALUES (:id,:project_id,:trigger,:trigger_payload_json,:status,:progress_json,:error,:prompt_version,:created_at,:started_at,:completed_at)""",
                {**record, "trigger_payload_json": self._encode(record.get("trigger_payload") or {}), "progress_json": self._encode(record.get("progress") or [])},
            )

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
