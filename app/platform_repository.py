from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProjectActiveWork(Exception):
    """A project cannot be deleted while durable background work is in flight."""


class PlatformRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

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
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, review_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    decision TEXT NOT NULL, decision_reason TEXT, resolution_status TEXT NOT NULL DEFAULT 'OPEN', resolved_at TEXT, remediation_json TEXT, created_at TEXT NOT NULL,
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
            self._ensure_column(conn, "findings", "resolution_status", "TEXT NOT NULL DEFAULT 'OPEN'")
            self._ensure_column(conn, "findings", "resolved_at", "TEXT")
            self._ensure_column(conn, "findings", "remediation_json", "TEXT")
            self._ensure_column(conn, "projects", "source_directory", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "content_state", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "finish_reason", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "validation_category", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "validation_fields_json", "TEXT")
            self._ensure_column(conn, "review_diagnostics", "retry_suppressed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "review_diagnostics", "execution_attempt", "INTEGER NOT NULL DEFAULT 1")
            # Demo projects belonged to an earlier development-only flow. They are not
            # valid user data and must never be surfaced by the normal application.
            conn.execute("DELETE FROM projects WHERE source_type = 'DEMO'")

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
                "INSERT INTO review_diagnostics(review_id,request_id,operation,role,batch_number,total_batches,file_count,state,attempt,execution_attempt,repair_attempted,provider,model,endpoint,created_at,elapsed_ms,http_status,content_type,request_chars,response_chars,usage_json,error_kind,error_message,content_state,finish_reason,validation_category,validation_fields_json,retry_suppressed) "
                "VALUES (:review_id,:request_id,:operation,:role,:batch_number,:total_batches,:file_count,:state,:attempt,:execution_attempt,:repair_attempted,:provider,:model,:endpoint,:created_at,:elapsed_ms,:http_status,:content_type,:request_chars,:response_chars,:usage_json,:error_kind,:error_message,:content_state,:finish_reason,:validation_category,:validation_fields_json,:retry_suppressed)",
                {**record, "batch_number": record.get("batch_number"), "total_batches": record.get("total_batches"), "file_count": record.get("file_count", 0), "attempt": record.get("attempt", 1), "execution_attempt": record.get("execution_attempt", 1), "repair_attempted": int(bool(record.get("repair_attempted", False))), "elapsed_ms": record.get("elapsed_ms"), "http_status": record.get("http_status"), "content_type": record.get("content_type"), "request_chars": record.get("request_chars"), "response_chars": record.get("response_chars"), "usage_json": json.dumps(record["usage"]) if record.get("usage") else None, "error_kind": record.get("error_kind"), "error_message": record.get("error_message"), "content_state": record.get("content_state"), "finish_reason": record.get("finish_reason"), "validation_category": record.get("validation_category"), "validation_fields_json": json.dumps(record.get("validation_fields", [])), "retry_suppressed": int(bool(record.get("retry_suppressed", False)))},
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

    def finding(self, finding_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        return self._decode_finding(dict(row)) if row else None

    @staticmethod
    def _decode_finding(record: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(record.pop("payload_json"))
        remediation = record.pop("remediation_json", None)
        record["resolution"] = record.pop("resolution_status")
        record["remediation"] = json.loads(remediation) if remediation else {"status": "UNVERIFIED", "notes": "No source recheck has been run.", "verified_at": None, "source_refreshed": False, "changed_files": []}
        return {**payload, **record}

    def update_finding_decision(self, finding_id: str, decision: str, reason: str | None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE findings SET decision=?, decision_reason=? WHERE id=?", (decision, reason, finding_id))

    def update_finding_resolution(self, finding_id: str, resolution: str, resolved_at: str | None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE findings SET resolution_status=?, resolved_at=? WHERE id=?", (resolution, resolved_at, finding_id))

    def update_finding_remediation(self, finding_id: str, remediation: dict[str, Any], resolution: str, resolved_at: str | None) -> None:
        with self._connect() as conn:
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
