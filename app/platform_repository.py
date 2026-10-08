from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProjectActiveWork(Exception):
    """A project cannot be deleted while durable background work is in flight."""


class _ClosingConnection:
    """sqlite3.Connection proxy that closes the connection on with-block exit.

    ``sqlite3.Connection``'s own context manager only commits/rolls back the
    transaction — it never closes the connection, so every repository call
    leaked one SQLite connection (one fd). WAL readers holding leaked fds
    eventually exhaust the process fd limit and stall writes. The proxy keeps
    the exact commit/rollback semantics while always closing.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.__enter__()
        return self._connection

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            return self._connection.__exit__(exc_type, exc, tb)
        finally:
            self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


class PlatformRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        # FS-FIX-015: a review worker and the per-second UI poll write to the
        # same SQLite file concurrently. WAL lets readers and the single writer
        # proceed without blocking each other, and busy_timeout makes a writer
        # wait for the lock instead of failing immediately with "database is
        # locked". These pragmas are per-connection, so they are applied here.
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return _ClosingConnection(connection)

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, source_type TEXT NOT NULL,
                    language TEXT, framework TEXT, target TEXT, build_system TEXT, source_directory TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS project_files (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
                    language TEXT NOT NULL, content_hash TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    UNIQUE(project_id, path)
                );
                CREATE TABLE IF NOT EXISTS project_symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                    file TEXT NOT NULL, line INTEGER NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS indexed_symbols (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                    file TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
                    signature TEXT NOT NULL DEFAULT '', component TEXT, source_hash TEXT NOT NULL,
                    file_hash TEXT NOT NULL DEFAULT '', symbol_hash TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS source_relations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, relation_kind TEXT NOT NULL,
                    source_symbol_id TEXT, target_symbol_id TEXT, target_name TEXT, file TEXT NOT NULL,
                    line INTEGER NOT NULL, evidence_hash TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0,
                    relation_state TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS allocation_events (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, symbol_id TEXT, variable TEXT,
                    event_kind TEXT NOT NULL, allocator_or_releaser TEXT NOT NULL, file TEXT NOT NULL,
                    line INTEGER NOT NULL, evidence_hash TEXT NOT NULL, ownership_state TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0, metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS topology_snapshots (
                    project_id TEXT PRIMARY KEY, source_snapshot_hash TEXT NOT NULL,
                    relation_fingerprint TEXT NOT NULL, indexed_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, scope TEXT NOT NULL, focus_json TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '[]', context_chars INTEGER NOT NULL DEFAULT 42000,
                    source_snapshot_hash TEXT,
                    total_batches INTEGER NOT NULL DEFAULT 0, validated_batches INTEGER NOT NULL DEFAULT 0,
                    unavailable_batches INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, progress_json TEXT NOT NULL,
                    execution_progress_json TEXT NOT NULL DEFAULT '{}',
                    execution_attempt INTEGER NOT NULL DEFAULT 1,
                    unit_states_json TEXT NOT NULL DEFAULT '{}',
                    output_budget_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT, last_activity_at TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS review_diagnostics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    operation TEXT NOT NULL, role TEXT NOT NULL, batch_number INTEGER, total_batches INTEGER,
                    file_count INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
                    execution_attempt INTEGER NOT NULL DEFAULT 1,
                    repair_attempted INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL, model TEXT NOT NULL,
                    endpoint TEXT NOT NULL, created_at TEXT NOT NULL, elapsed_ms INTEGER, http_status INTEGER,
                    content_type TEXT, request_chars INTEGER, response_chars INTEGER, usage_json TEXT,
                    error_kind TEXT, error_message TEXT, content_state TEXT,
                    finish_reason TEXT, validation_category TEXT,
                    validation_fields_json TEXT, retry_suppressed INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(review_id) REFERENCES reviews(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS review_unit_cache (
                    project_id TEXT NOT NULL, cache_key TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    outcome_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, cache_key), FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT NOT NULL, project_id TEXT NOT NULL, review_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    decision TEXT NOT NULL, decision_reason TEXT, resolution_status TEXT NOT NULL DEFAULT 'OPEN', resolved_at TEXT, remediation_json TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, id),
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    FOREIGN KEY(review_id) REFERENCES reviews(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                    created_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS yaml_generations (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, content TEXT NOT NULL, valid INTEGER NOT NULL,
                    errors_json TEXT NOT NULL, generated_at TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS application_settings (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id, path);
                CREATE INDEX IF NOT EXISTS idx_indexed_symbols_project ON indexed_symbols(project_id, name, file);
                CREATE INDEX IF NOT EXISTS idx_source_relations_project ON source_relations(project_id, relation_kind, source_symbol_id);
                CREATE INDEX IF NOT EXISTS idx_source_relations_target ON source_relations(project_id, target_symbol_id, target_name);
                CREATE INDEX IF NOT EXISTS idx_allocation_events_project ON allocation_events(project_id, file, line);
                CREATE INDEX IF NOT EXISTS idx_findings_project ON findings(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_review_diagnostics_review ON review_diagnostics(review_id, id);
                """
            )
            self._ensure_column(conn, "reviews", "context_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "reviews", "context_chars", "INTEGER NOT NULL DEFAULT 42000")
            self._ensure_column(conn, "reviews", "source_snapshot_hash", "TEXT")
            self._ensure_column(conn, "reviews", "total_batches", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "reviews", "validated_batches", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "reviews", "unavailable_batches", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "reviews", "execution_progress_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "reviews", "execution_attempt", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "reviews", "unit_states_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "reviews", "output_budget_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "reviews", "error_message", "TEXT")
            self._ensure_column(conn, "reviews", "last_activity_at", "TEXT")
            self._ensure_column(conn, "indexed_symbols", "file_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "indexed_symbols", "symbol_hash", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "findings", "resolution_status", "TEXT NOT NULL DEFAULT 'OPEN'")
            self._ensure_column(conn, "findings", "resolved_at", "TEXT")
            self._ensure_column(conn, "findings", "remediation_json", "TEXT")
            self._ensure_column(conn, "projects", "source_directory", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "content_state", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "finish_reason", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "structured_mode", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "finalization_policy", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "finalization_recovery", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "review_diagnostics", "effective_max_tokens", "INTEGER")
            self._ensure_column(conn, "review_diagnostics", "validation_category", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "validation_fields_json", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "retry_suppressed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "review_diagnostics", "execution_attempt", "INTEGER NOT NULL DEFAULT 1")
            self._migrate_findings_composite_key(conn)
            # Demo projects belonged to an earlier development-only flow. They are not
            # valid user data and must never be surfaced by the normal application.
            conn.execute("DELETE FROM projects WHERE source_type = 'DEMO'")

    def _migrate_findings_composite_key(self, conn: sqlite3.Connection) -> None:
        """FS-FIND-001: allow the same sequential id (``FS-001``) per project.

        Findings now use human-readable, project-scoped sequential ids, so two
        projects can each own ``FS-001``. Older databases still have the findings
        table keyed by ``id`` alone; rebuild it with a composite
        ``(project_id, id)`` primary key. Existing rows (including legacy random
        hex ids) are copied verbatim, so no id is ever renamed.
        """
        info = conn.execute("PRAGMA table_info(findings)").fetchall()
        primary_key = [row["name"] for row in info if row["pk"]]
        if primary_key == ["project_id", "id"]:
            return  # already migrated
        conn.executescript(
            """
            CREATE TABLE findings_migrated (
                id TEXT NOT NULL, project_id TEXT NOT NULL, review_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                decision TEXT NOT NULL, decision_reason TEXT, resolution_status TEXT NOT NULL DEFAULT 'OPEN', resolved_at TEXT, remediation_json TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(project_id, id),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(review_id) REFERENCES reviews(id) ON DELETE CASCADE
            );
            INSERT OR IGNORE INTO findings_migrated(id,project_id,review_id,payload_json,decision,decision_reason,resolution_status,resolved_at,remediation_json,created_at)
                SELECT id,project_id,review_id,payload_json,decision,decision_reason,resolution_status,resolved_at,remediation_json,created_at FROM findings;
            DROP TABLE findings;
            ALTER TABLE findings_migrated RENAME TO findings;
            CREATE INDEX IF NOT EXISTS idx_findings_project ON findings(project_id, created_at DESC);
            """
        )

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def create_project(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects(id,name,description,source_type,language,framework,target,build_system,source_directory,created_at,updated_at) "
                "VALUES (:id,:name,:description,:source_type,:language,:framework,:target,:build_system,:source_directory,:created_at,:updated_at)",
                record,
            )

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM project_files f WHERE f.project_id=p.id) file_count, (SELECT COUNT(*) FROM project_symbols s WHERE s.project_id=p.id) symbol_count FROM projects p WHERE p.id=?", (project_id,)
            ).fetchone()
        return self._row(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT p.*, (SELECT COUNT(*) FROM project_files f WHERE f.project_id=p.id) file_count, (SELECT COUNT(*) FROM project_symbols s WHERE s.project_id=p.id) symbol_count FROM projects p ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def add_files(self, project_id: str, files: list[dict[str, str]]) -> None:
        with self._connect() as conn:
            conn.executemany("INSERT OR REPLACE INTO project_files VALUES (:id,:project_id,:path,:content,:language,:content_hash)", files)
            conn.execute("UPDATE projects SET updated_at=:updated_at WHERE id=:id", {"id": project_id, "updated_at": files[0]["updated_at"]})

    def replace_files(self, project_id: str, files: list[dict[str, str]], updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM project_files WHERE project_id=?", (project_id,))
            if files:
                conn.executemany("INSERT INTO project_files VALUES (:id,:project_id,:path,:content,:language,:content_hash)", files)
            conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (updated_at, project_id))

    def list_files(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path, language, length(content) AS size FROM project_files WHERE project_id=? ORDER BY path", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_file(self, project_id: str, path: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT path, language, length(content) AS size, content FROM project_files WHERE project_id=? AND path=?", (project_id, path)).fetchone()
        return self._row(row)

    def raw_files(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path, content, language, content_hash FROM project_files WHERE project_id=? ORDER BY path", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def replace_symbols(self, project_id: str, symbols: list[dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM project_symbols WHERE project_id=?", (project_id,))
            conn.executemany("INSERT INTO project_symbols(project_id,name,kind,file,line) VALUES (:project_id,:name,:kind,:file,:line)", symbols)

    def replace_topology(
        self,
        project_id: str,
        symbols: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        allocations: list[dict[str, Any]],
        *,
        source_snapshot_hash: str,
        relation_fingerprint: str,
        indexed_at: str,
    ) -> None:
        """Replace one project's derived topology atomically.

        ``project_symbols`` remains a compatibility projection for existing
        callers. The typed tables are the source for topology-aware features;
        all rows are project-scoped and are replaced together so a failed
        index cannot leave a mixed snapshot.
        """
        legacy = [
            {"project_id": project_id, "name": item["name"], "kind": item["kind"], "file": item["file"], "line": item["line_start"]}
            for item in symbols
        ]
        with self._connect() as conn:
            conn.execute("DELETE FROM project_symbols WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM source_relations WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM allocation_events WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM indexed_symbols WHERE project_id=?", (project_id,))
            if legacy:
                conn.executemany("INSERT INTO project_symbols(project_id,name,kind,file,line) VALUES (:project_id,:name,:kind,:file,:line)", legacy)
            if symbols:
                conn.executemany(
                    """INSERT INTO indexed_symbols(id,project_id,name,kind,file,line_start,line_end,signature,component,source_hash,file_hash,symbol_hash,confidence)
                       VALUES (:id,:project_id,:name,:kind,:file,:line_start,:line_end,:signature,:component,:source_hash,:file_hash,:symbol_hash,:confidence)""",
                    [{
                        **item, "project_id": project_id, "signature": item.get("signature", ""),
                        "component": item.get("component"), "confidence": item.get("confidence", 1.0),
                        # `source_hash` remains the legacy file hash. New callers
                        # use explicit fields; empty migrated hashes mean unknown.
                        "file_hash": item.get("file_hash") or item.get("source_hash", ""),
                        "symbol_hash": item.get("symbol_hash") or "",
                    } for item in symbols],
                )
            if relations:
                conn.executemany(
                    """INSERT INTO source_relations(id,project_id,relation_kind,source_symbol_id,target_symbol_id,target_name,file,line,evidence_hash,confidence,relation_state,metadata_json)
                       VALUES (:id,:project_id,:relation_kind,:source_symbol_id,:target_symbol_id,:target_name,:file,:line,:evidence_hash,:confidence,:relation_state,:metadata_json)""",
                    [{**item, "project_id": project_id, "metadata_json": json.dumps(item.get("metadata") or {}, sort_keys=True), "confidence": item.get("confidence", 1.0)} for item in relations],
                )
            if allocations:
                conn.executemany(
                    """INSERT INTO allocation_events(id,project_id,symbol_id,variable,event_kind,allocator_or_releaser,file,line,evidence_hash,ownership_state,confidence,metadata_json)
                       VALUES (:id,:project_id,:symbol_id,:variable,:event_kind,:allocator_or_releaser,:file,:line,:evidence_hash,:ownership_state,:confidence,:metadata_json)""",
                    [{**item, "project_id": project_id, "metadata_json": json.dumps(item.get("metadata") or {}, sort_keys=True), "confidence": item.get("confidence", 1.0)} for item in allocations],
                )
            conn.execute(
                """INSERT INTO topology_snapshots(project_id,source_snapshot_hash,relation_fingerprint,indexed_at)
                   VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET source_snapshot_hash=excluded.source_snapshot_hash,
                   relation_fingerprint=excluded.relation_fingerprint,indexed_at=excluded.indexed_at""",
                (project_id, source_snapshot_hash, relation_fingerprint, indexed_at),
            )

    @staticmethod
    def _decode_topology_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        if "metadata_json" in value:
            try:
                value["metadata"] = json.loads(value.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                value["metadata"] = {}
                value.pop("metadata_json", None)
        return value

    def list_indexed_symbols(self, project_id: str, search: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM indexed_symbols WHERE project_id=?"
        params: list[Any] = [project_id]
        if search:
            query += " AND name LIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY file,line_start,name"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_source_relations(self, project_id: str, *, source_symbol_id: str | None = None, target_symbol_id: str | None = None, relation_kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM source_relations WHERE project_id=?"
        params: list[Any] = [project_id]
        for column, value in (("source_symbol_id", source_symbol_id), ("target_symbol_id", target_symbol_id), ("relation_kind", relation_kind)):
            if value is not None:
                query += f" AND {column}=?"
                params.append(value)
        query += " ORDER BY file,line,id"
        with self._connect() as conn:
            return [self._decode_topology_row(row) for row in conn.execute(query, params).fetchall()]

    def list_allocation_events(self, project_id: str, symbol_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM allocation_events WHERE project_id=?"
        params: list[Any] = [project_id]
        if symbol_id:
            query += " AND symbol_id=?"
            params.append(symbol_id)
        query += " ORDER BY file,line,id"
        with self._connect() as conn:
            return [self._decode_topology_row(row) for row in conn.execute(query, params).fetchall()]

    def topology_snapshot(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topology_snapshots WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def topology_path(self, project_id: str, symbol_name: str, depth: int = 1, cap: int = 64) -> dict[str, Any]:
        """Return a bounded source-backed caller/callee path for one symbol."""
        symbols = self.list_indexed_symbols(project_id)
        by_id = {item["id"]: item for item in symbols}
        matches = [item for item in symbols if item["name"] == symbol_name and item.get("kind") == "function"]
        if len(matches) != 1:
            return {"nodes": matches[:cap], "relations": [], "allocations": [], "truncated": len(matches) > cap}
        seen = {matches[0]["id"]}
        frontier = [matches[0]["id"]]
        relations: list[dict[str, Any]] = []
        for _ in range(max(0, min(depth, 3)) + 1):
            if not frontier or len(relations) >= cap:
                break
            next_frontier: list[str] = []
            for relation in self.list_source_relations(project_id):
                if relation.get("source_symbol_id") in frontier or relation.get("target_symbol_id") in frontier:
                    if relation not in relations:
                        relations.append(relation)
                    for node_id in (relation.get("source_symbol_id"), relation.get("target_symbol_id")):
                        if node_id and node_id in by_id and node_id not in seen:
                            seen.add(node_id); next_frontier.append(node_id)
                    if len(relations) >= cap:
                        break
            frontier = next_frontier
        node_ids = list(seen)[:cap]
        allocations = [item for item in self.list_allocation_events(project_id) if item.get("symbol_id") in node_ids][:cap]
        fingerprint = hashlib.sha256(json.dumps({"nodes": node_ids, "relations": [item["id"] for item in relations], "allocations": [item["id"] for item in allocations]}, sort_keys=True).encode()).hexdigest()
        return {"nodes": [by_id[item] for item in node_ids], "relations": relations[:cap], "allocations": allocations, "truncated": len(seen) > cap or len(relations) > cap, "fingerprint": fingerprint}

    def list_symbols(self, project_id: str, search: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT name,kind,file,line FROM project_symbols WHERE project_id=?", [project_id]
        if search:
            query += " AND name LIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY name, file"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_project_metadata(self, project_id: str, metadata: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE projects SET language=:language,framework=:framework,target=:target,build_system=:build_system,updated_at=:updated_at WHERE id=:id", {"id": project_id, **metadata})

    def update_project_source_directory(self, project_id: str, source_directory: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE projects SET source_directory=?, updated_at=? WHERE id=?", (source_directory, datetime.now(UTC).isoformat(), project_id))

    def create_review(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reviews(id,project_id,scope,focus_json,context_json,context_chars,source_snapshot_hash,total_batches,validated_batches,unavailable_batches,status,progress_json,execution_progress_json,execution_attempt,unit_states_json,output_budget_json,error_message,last_activity_at,created_at,completed_at) "
                "VALUES (:id,:project_id,:scope,:focus_json,:context_json,:context_chars,:source_snapshot_hash,:total_batches,:validated_batches,:unavailable_batches,:status,:progress_json,:execution_progress_json,:execution_attempt,:unit_states_json,:output_budget_json,:error_message,:last_activity_at,:created_at,:completed_at)",
                {
                    **record,
                    "focus_json": json.dumps(record["focus"]),
                    "context_json": json.dumps(record.get("context_files", [])),
                    "context_chars": record.get("context_chars", 42_000),
                    "source_snapshot_hash": record.get("source_snapshot_hash"),
                    "total_batches": record.get("total_batches", 0),
                    "validated_batches": record.get("validated_batches", 0),
                    "unavailable_batches": record.get("unavailable_batches", 0),
                    "progress_json": json.dumps(record["progress"]),
                    "execution_progress_json": json.dumps(record.get("execution_progress") or {}),
                    "execution_attempt": record.get("execution_attempt", 1),
                    "unit_states_json": json.dumps(record.get("unit_states") or {}),
                    "output_budget_json": json.dumps(record.get("output_budget_snapshot") or {}),
                    "error_message": record.get("error"),
                    "last_activity_at": record.get("last_activity_at", record.get("created_at")),
                },
            )

    def list_reviews(self, project_id: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            try:
                record["focus"] = json.loads(record.pop("focus_json"))
            except json.JSONDecodeError:
                record["focus"] = []
            try:
                record["progress"] = json.loads(record.pop("progress_json"))
            except json.JSONDecodeError:
                record["progress"] = []
            record.pop("execution_progress_json", None)
            record.pop("unit_states_json", None)
            record.pop("output_budget_json", None)
            record.pop("context_json", None)
            record["error"] = record.pop("error_message", None)
            records.append(record)
        return records

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT r.*, (SELECT COUNT(*) FROM findings f WHERE f.review_id=r.id) finding_count FROM reviews r WHERE id=?", (review_id,)).fetchone()
        if not row:
            return None
        record = dict(row); record["focus"] = json.loads(record.pop("focus_json")); record["context_files"] = json.loads(record.pop("context_json")); record["error"] = record.pop("error_message"); record["progress"] = json.loads(record.pop("progress_json")); raw_execution = record.pop("execution_progress_json", "{}") or "{}"; raw_unit_states = record.pop("unit_states_json", "{}") or "{}"; raw_output_budget = record.pop("output_budget_json", "{}") or "{}";
        try:
            record["execution_progress"] = json.loads(raw_execution)
        except json.JSONDecodeError:
            record["execution_progress"] = {}
        try:
            record["unit_states"] = json.loads(raw_unit_states)
        except json.JSONDecodeError:
            record["unit_states"] = {}
        try:
            record["output_budget_snapshot"] = json.loads(raw_output_budget)
        except json.JSONDecodeError:
            record["output_budget_snapshot"] = {}
        record["diagnostics"] = self.review_diagnostics(review_id); return record

    def create_review_diagnostic(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO review_diagnostics(review_id,request_id,operation,role,batch_number,total_batches,file_count,state,attempt,execution_attempt,repair_attempted,provider,model,endpoint,created_at,elapsed_ms,http_status,content_type,request_chars,response_chars,usage_json,error_kind,error_message,content_state,finish_reason,structured_mode,finalization_policy,finalization_recovery,effective_max_tokens,validation_category,validation_fields_json,retry_suppressed) "
                "VALUES (:review_id,:request_id,:operation,:role,:batch_number,:total_batches,:file_count,:state,:attempt,:execution_attempt,:repair_attempted,:provider,:model,:endpoint,:created_at,:elapsed_ms,:http_status,:content_type,:request_chars,:response_chars,:usage_json,:error_kind,:error_message,:content_state,:finish_reason,:structured_mode,:finalization_policy,:finalization_recovery,:effective_max_tokens,:validation_category,:validation_fields_json,:retry_suppressed)",
                {**record, "batch_number": record.get("batch_number"), "total_batches": record.get("total_batches"), "file_count": record.get("file_count", 0), "attempt": record.get("attempt", 1), "execution_attempt": record.get("execution_attempt", 1), "repair_attempted": int(bool(record.get("repair_attempted", False))), "elapsed_ms": record.get("elapsed_ms"), "http_status": record.get("http_status"), "content_type": record.get("content_type"), "request_chars": record.get("request_chars"), "response_chars": record.get("response_chars"), "usage_json": json.dumps(record["usage"]) if record.get("usage") else None, "error_kind": record.get("error_kind"), "error_message": record.get("error_message"), "content_state": record.get("content_state"), "finish_reason": record.get("finish_reason"), "structured_mode": record.get("structured_mode"), "finalization_policy": record.get("finalization_policy"), "finalization_recovery": int(bool(record.get("finalization_recovery", False))), "effective_max_tokens": record.get("effective_max_tokens"), "validation_category": record.get("validation_category"), "validation_fields_json": json.dumps(record.get("validation_fields", [])), "retry_suppressed": int(bool(record.get("retry_suppressed", False)))},
            )

    def review_diagnostics(self, review_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM review_diagnostics WHERE review_id=? ORDER BY id", (review_id,)).fetchall()
        diagnostics: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["repair_attempted"] = bool(record["repair_attempted"])
            record["usage"] = json.loads(record.pop("usage_json")) if record.get("usage_json") else None
            record["validation_fields"] = json.loads(record.pop("validation_fields_json")) if record.get("validation_fields_json") else []
            record["retry_suppressed"] = bool(record.get("retry_suppressed", 0))
            record["finalization_recovery"] = bool(record.get("finalization_recovery", 0))
            diagnostics.append(record)
        return diagnostics

    def get_review_unit_cache(self, project_id: str, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT schema_version, outcome_json, created_at FROM review_unit_cache WHERE project_id=? AND cache_key=?", (project_id, cache_key)).fetchone()
        if not row:
            return None
        try:
            outcome = json.loads(row["outcome_json"])
        except json.JSONDecodeError:
            return None
        return {"schema_version": row["schema_version"], "outcome": outcome, "created_at": row["created_at"]}

    def set_review_unit_cache(self, project_id: str, cache_key: str, outcome: dict[str, Any], schema_version: int = 1) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO review_unit_cache(project_id,cache_key,schema_version,outcome_json,created_at) VALUES (?,?,?,?,?) ON CONFLICT(project_id,cache_key) DO UPDATE SET schema_version=excluded.schema_version,outcome_json=excluded.outcome_json,created_at=excluded.created_at",
                (project_id, cache_key, schema_version, json.dumps(outcome), datetime.now(UTC).isoformat()),
            )

    def update_review(
        self,
        review_id: str,
        *,
        status: str,
        progress: list[str],
        completed_at: str | None = None,
        error: str | None = None,
        source_snapshot_hash: str | None = None,
        total_batches: int | None = None,
        validated_batches: int | None = None,
        unavailable_batches: int | None = None,
        execution_progress: dict[str, Any] | None = None,
        execution_attempt: int | None = None,
        unit_states: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            values: list[Any] = [status, json.dumps(progress), datetime.now(UTC).isoformat()]
            assignments = ["status=?", "progress_json=?", "last_activity_at=?"]
            if execution_progress is not None:
                assignments.append("execution_progress_json=?")
                values.append(json.dumps(execution_progress))
            if execution_attempt is not None:
                assignments.append("execution_attempt=?")
                values.append(execution_attempt)
            if unit_states is not None:
                assignments.append("unit_states_json=?")
                values.append(json.dumps(unit_states))
            for column, value in (
                ("total_batches", total_batches),
                ("validated_batches", validated_batches),
                ("unavailable_batches", unavailable_batches),
            ):
                if value is not None:
                    assignments.append(f"{column}=?")
                    values.append(value)
            if completed_at is not None:
                assignments.extend(["error_message=?", "completed_at=?"])
                values.extend([error, completed_at])
            if source_snapshot_hash is not None:
                assignments.append("source_snapshot_hash=?")
                values.append(source_snapshot_hash)
            values.append(review_id)
            conn.execute(f"UPDATE reviews SET {', '.join(assignments)} WHERE id=?", values)

    def touch_review_activity(self, review_id: str) -> None:
        """Refresh only ``last_activity_at`` for a live review worker (FS-FIX-015).

        This is the heartbeat write: it deliberately never changes ``status``,
        ``progress_json`` or any terminal field, so it can never resurrect a
        review that the stale path or an error handler already marked terminal.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE reviews SET last_activity_at=? WHERE id=? AND status='RUNNING'",
                (datetime.now(UTC).isoformat(), review_id),
            )

    def review_status(self, review_id: str) -> str | None:
        """Return the persisted review status, or ``None`` if it is gone."""
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM reviews WHERE id=?", (review_id,)).fetchone()
        return row["status"] if row else None

    def create_finding(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO findings(id,project_id,review_id,payload_json,decision,decision_reason,resolution_status,resolved_at,created_at) "
                "VALUES (:id,:project_id,:review_id,:payload_json,:decision,:decision_reason,:resolution_status,:resolved_at,:created_at)",
                {**record, "payload_json": json.dumps(record["payload"]), "resolution_status": record.get("resolution", "OPEN"), "resolved_at": record.get("resolved_at")},
            )

    def findings(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM findings WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._decode_finding(dict(row)) for row in rows]

    def finding(self, finding_id: str, project_id: str | None = None) -> dict[str, Any] | None:
        """Return one finding.

        FS-FIND-001 ids are project-scoped, so callers should pass ``project_id``
        to disambiguate when the same sequential id exists in multiple projects.
        """
        with self._connect() as conn:
            if project_id is not None:
                row = conn.execute("SELECT * FROM findings WHERE id=? AND project_id=?", (finding_id, project_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        return self._decode_finding(dict(row)) if row else None

    @staticmethod
    def _decode_finding(record: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(record.pop("payload_json"))
        remediation = record.pop("remediation_json", None)
        record["resolution"] = record.pop("resolution_status")
        record["remediation"] = json.loads(remediation) if remediation else {"status": "UNVERIFIED", "notes": "No source recheck has been run.", "verified_at": None, "source_refreshed": False, "changed_files": []}
        return {**payload, **record}

    def update_finding_decision(self, finding_id: str, decision: str, reason: str | None, project_id: str | None = None) -> None:
        with self._connect() as conn:
            if project_id is not None:
                conn.execute("UPDATE findings SET decision=?, decision_reason=? WHERE id=? AND project_id=?", (decision, reason, finding_id, project_id))
            else:
                conn.execute("UPDATE findings SET decision=?, decision_reason=? WHERE id=?", (decision, reason, finding_id))

    def update_finding_resolution(self, finding_id: str, resolution: str, resolved_at: str | None, project_id: str | None = None) -> None:
        with self._connect() as conn:
            if project_id is not None:
                conn.execute("UPDATE findings SET resolution_status=?, resolved_at=? WHERE id=? AND project_id=?", (resolution, resolved_at, finding_id, project_id))
            else:
                conn.execute("UPDATE findings SET resolution_status=?, resolved_at=? WHERE id=?", (resolution, resolved_at, finding_id))

    def update_finding_remediation(self, finding_id: str, remediation: dict[str, Any], resolution: str, resolved_at: str | None, project_id: str | None = None) -> None:
        with self._connect() as conn:
            if project_id is not None:
                conn.execute(
                    "UPDATE findings SET remediation_json=?, resolution_status=?, resolved_at=? WHERE id=? AND project_id=?",
                    (json.dumps(remediation), resolution, resolved_at, finding_id, project_id),
                )
            else:
                conn.execute(
                    "UPDATE findings SET remediation_json=?, resolution_status=?, resolved_at=? WHERE id=?",
                    (json.dumps(remediation), resolution, resolved_at, finding_id),
                )

    def add_message(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO chat_messages VALUES (:id,:project_id,:role,:content,:created_at)", record)

    def messages(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM chat_messages WHERE project_id=? ORDER BY created_at", (project_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_yaml(self, record: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO yaml_generations VALUES (:id,:project_id,:content,:valid,:errors_json,:generated_at)", {**record, "errors_json": json.dumps(record["errors"])})

    def latest_yaml(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM yaml_generations WHERE project_id=? ORDER BY generated_at DESC LIMIT 1", (project_id,)).fetchone()
        if not row:
            return None
        record = dict(row); record["errors"] = json.loads(record.pop("errors_json")); record["valid"] = bool(record["valid"]); return record

    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value_json FROM application_settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_setting(self, key: str, value: dict[str, Any], updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO application_settings(key,value_json,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, json.dumps(value), updated_at))

    # --- Atomic project deletion ---

    def count_running_reviews(self, conn: sqlite3.Connection, project_id: str) -> int:
        row = conn.execute("SELECT COUNT(*) AS total FROM reviews WHERE project_id = ? AND status = 'RUNNING'", (project_id,)).fetchone()
        return int(row["total"]) if row else 0

    def delete_project(
        self,
        project_id: str,
        *,
        preconditions: Callable[[sqlite3.Connection], None] | None = None,
        related_cleanup: Callable[[sqlite3.Connection, str], None] | None = None,
    ) -> None:
        """Atomically delete one project and all of its persisted records.

        A single connection and a single transaction cover the existence check,
        caller preconditions, memory/intelligence cleanup, and the platform rows
        removed through the existing projects cascades; everything rolls back if
        any statement fails. Foreign keys are explicitly enabled because the
        platform cascades are inert without them. Global rows
        (application_settings, schema_migrations) and any other project's data
        are never touched, and no filesystem path is read or deleted here.
        """
        connection = sqlite3.connect(self.database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone() is None:
                raise LookupError(project_id)
            if preconditions is not None:
                preconditions(connection)
            if related_cleanup is not None:
                related_cleanup(connection, project_id)
            if connection.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount != 1:
                raise LookupError(project_id)
            connection.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
