from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable
from uuid import uuid4

from fastapi import HTTPException, status

from .indexer import SOURCE_EXTENSIONS, FirmwareIndexer, compute_topology_fingerprint, language_for_path, safe_project_path
from .i18n import message as _msg
from .i18n import normalize_locale
from .memory_lifetime_service import MemoryLifetimeService
from .platform_repository import PlatformRepository, ProjectActiveWork
from .platform_schemas import IndexRead, ProjectCreate, ProjectDirectoryImport, ProjectRead, ProjectSourceType, SymbolRead

if TYPE_CHECKING:
    from .repository import MemoryRepository


def now() -> str:
    return datetime.now(UTC).isoformat()


def _locale_of(resolver: Callable[[], str] | None) -> str:
    try:
        return normalize_locale(resolver() if resolver else None)
    except Exception:  # noqa: BLE001 - error rendering must never raise
        return "en"


class TopologyDerivationError(RuntimeError):
    """Raised when the derived topology cannot be persisted safely.

    This is a narrow, server-side derivation failure (FS-FIX-014). It is raised
    before SQLite when two distinct relation payloads share one stable id, so a
    raw ``sqlite3.IntegrityError`` cannot surface as an opaque HTTP 500. The
    route boundary converts it into a fixed, safe JSON error.
    """


class ProjectService:
    def __init__(self, repository: PlatformRepository, indexer: FirmwareIndexer, import_root: str | Path | None = None, memory_repository: "MemoryRepository | None" = None, lifetime_service: MemoryLifetimeService | None = None, language_resolver: Callable[[], str] | None = None) -> None:
        self.repository, self.indexer = repository, indexer
        self.language_resolver = language_resolver
        self.lifetime_service = lifetime_service or MemoryLifetimeService()
        # The Memory/Project Intelligence tables share this SQLite file but do
        # not all carry a projects foreign key, so deletion needs them here to
        # stay inside the same transaction.
        self.memory_repository = memory_repository
        self.language_resolver = language_resolver
        configured_root = import_root or os.getenv("FIRMSIGHT_IMPORT_ROOT")
        self.import_root = Path(configured_root).expanduser() if configured_root else None

    def _t(self, key: str, /, **params: object) -> str:
        resolver = getattr(self, "language_resolver", None)
        return _msg(key, _locale_of(resolver), **params)

    def _error(self, status_code: int, key: str, /, **params: object) -> HTTPException:
        return HTTPException(status_code, self._t(key, **params))

    def create(self, request: ProjectCreate, *, source_directory: str | None = None) -> ProjectRead:
        timestamp = now()
        record = {"id": f"PRJ-{uuid4().hex[:10].upper()}", **request.model_dump(mode="json"), "language": None, "framework": None, "target": None, "build_system": None, "source_directory": source_directory, "created_at": timestamp, "updated_at": timestamp}
        self.repository.create_project(record)
        return self.get(record["id"])

    def import_directory(self, request: ProjectDirectoryImport) -> ProjectRead:
        directory, files = self._read_local_directory(request.directory)
        prepared = self._prepared_files("", files)

        # Validate the full source derivation *before* persisting any project
        # record. A malformed index/topology result surfaces here while nothing
        # has been written, so a failed import cannot leave a partial project,
        # source snapshot, or topology behind.
        raw_files = [{"path": item["path"], "content": item["content"], "language": item["language"], "content_hash": item["content_hash"]} for item in prepared]
        project = self.create(
            ProjectCreate(name=request.name or directory.name, description=request.description, source_type=ProjectSourceType.LOCAL_DIRECTORY),
            source_directory=str(directory),
        )
        try:
            result, lifetime = self._compute_index(project.id, raw_files)
            self.add_files(project.id, files)
            self._commit_index(project.id, raw_files, result, lifetime)
        except Exception:
            self._rollback_new_project(project.id)
            raise
        return self.get(project.id)

    def _rollback_new_project(self, project_id: str) -> None:
        """Remove every persisted record created by a failed local import.

        The project row, its files, topology rows, and any Project Intelligence
        side effects are removed together with the same transactional cleanup
        used by project deletion. Some intelligence tables lack a projects
        foreign key, so the memory repository must be consulted to stay inside
        the same transaction. The original import failure is re-raised by the
        caller with its source exception preserved for diagnostics.
        """
        memory = self.memory_repository

        def related_cleanup(conn, target_project_id: str) -> None:
            if memory is not None:
                memory.delete_project_data(conn, target_project_id)

        try:
            self.repository.delete_project(project_id, related_cleanup=related_cleanup)
        except LookupError:
            pass

    def refresh_local_directory(self, project_id: str, directory_override: str | None = None) -> tuple[IndexRead, list[str]]:
        record = self.repository.get_project(project_id)
        if not record:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.project_not_found")
        if record["source_type"] != ProjectSourceType.LOCAL_DIRECTORY:
            raise self._error(status.HTTP_409_CONFLICT, "error.sync_requires_local")
        directory_value = directory_override or record.get("source_directory")
        if not directory_value:
            raise self._error(status.HTTP_409_CONFLICT, "error.attach_before_sync")
        directory, files = self._read_local_directory(directory_value)
        previous = {file["path"]: file["content_hash"] for file in self.repository.raw_files(project_id)}
        prepared = self._prepared_files(project_id, files)
        current = {file["path"]: file["content_hash"] for file in prepared}
        changed = sorted(path for path, digest in current.items() if previous.get(path) != digest)
        changed.extend(sorted(path for path in previous if path not in current))
        # FS-FIX-014 (REV-042): derive and finalize the topology from the new
        # `prepared` snapshot *before* persisting any file or directory pointer.
        # If the final relation invariant raises TopologyDerivationError, nothing
        # has been written, so the prior source, source-directory pointer, and
        # topology stay mutually consistent. This keeps local source sync as
        # derivation-safe as directory import.
        result, lifetime = self._compute_index(project_id, prepared)
        if directory_override:
            self.repository.update_project_source_directory(project_id, str(directory))
        self.repository.replace_files(project_id, prepared, now())
        self._commit_index(project_id, prepared, result, lifetime)
        return self._index_read(project_id), changed

    def current_source_snapshot_hash(self, project_id: str) -> str:
        """Return a safe fingerprint of the source currently available to a project.

        Local projects are read from their configured directory without mutating
        persisted files. Manual projects use their persisted file snapshot. The
        fingerprint contains only ordered paths and content hashes, never source
        text, so it is safe to use as a resume guard.
        """
        record = self.repository.get_project(project_id)
        if not record:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.project_not_found")
        if record["source_type"] == ProjectSourceType.LOCAL_DIRECTORY:
            directory_value = record.get("source_directory")
            if not directory_value:
                raise self._error(status.HTTP_409_CONFLICT, "error.attach_before_resume")
            _, files = self._read_local_directory(directory_value)
            return self.snapshot_hash(self._prepared_files(project_id, files))
        return self.snapshot_hash(self.repository.raw_files(project_id))

    @staticmethod
    def snapshot_hash(files: list[dict[str, str]]) -> str:
        digest = hashlib.sha256()
        for file in sorted(files, key=lambda item: item["path"]):
            digest.update(file["path"].encode("utf-8"))
            digest.update(b"\0")
            digest.update(file["content_hash"].encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _read_local_directory(self, directory_value: str) -> tuple[Path, dict[str, str]]:
        root = self._validated_import_root()
        requested = Path(directory_value).expanduser()
        if requested.is_symlink():
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.project_dir_symlink")
        try:
            directory = requested.resolve(strict=True)
        except OSError as error:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.project_dir_not_found") from error
        if not directory.is_dir():
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.project_path_not_dir")
        if not directory.is_relative_to(root):
            raise self._error(status.HTTP_403_FORBIDDEN, "error.project_dir_outside")

        files: dict[str, str] = {}
        for candidate in sorted(directory.rglob("*")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                continue
            relative_path = candidate.relative_to(directory).as_posix()
            if not self._is_analysis_path(relative_path):
                continue
            if candidate.stat().st_size > 1_000_000:
                continue
            if len(files) >= 500:
                raise self._error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "error.project_file_limit")
            try:
                files[relative_path] = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.path_not_utf8", path=relative_path) from error
        if not files:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.no_source_files")
        return directory, files

    def _validated_import_root(self) -> Path:
        if not self.import_root:
            raise self._error(status.HTTP_409_CONFLICT, "error.import_root_missing")
        try:
            root = self.import_root.resolve(strict=True)
        except OSError as error:
            raise self._error(status.HTTP_409_CONFLICT, "error.import_root_not_exist") from error
        if not root.is_dir():
            raise self._error(status.HTTP_409_CONFLICT, "error.import_root_not_dir")
        if root == Path(root.anchor):
            raise self._error(status.HTTP_409_CONFLICT, "error.import_root_is_fs_root")
        return root

    @staticmethod
    def _is_allowed_import_path(path: str) -> bool:
        basename = path.rsplit("/", 1)[-1].lower()
        return any(basename.endswith(extension) for extension in SOURCE_EXTENSIONS) or basename in {"sdkconfig", "sdkconfig.defaults", "idf_component.yml", "platformio.ini", "cmakelists.txt"}

    @classmethod
    def _is_analysis_path(cls, path: str) -> bool:
        """Limit local-directory analysis to the user-selected firmware scope."""
        normalized = safe_project_path(path)
        parts = normalized.split("/")
        basename = parts[-1].lower()
        if len(parts) == 1:
            return basename in {"platformio.ini", "sdkconfig", "sdkconfig.defaults"}
        return parts[0] in {"lib", "src", "include"} and cls._is_allowed_import_path(normalized)

    def get(self, project_id: str) -> ProjectRead:
        record = self.repository.get_project(project_id)
        if not record:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.project_not_found")
        record["source_sync_available"] = bool(record.get("source_directory"))
        return ProjectRead.model_validate(record)

    def list(self) -> list[ProjectRead]:
        return [ProjectRead.model_validate({**project, "source_sync_available": bool(project.get("source_directory"))}) for project in self.repository.list_projects()]

    def delete(self, project_id: str) -> None:
        """Permanently delete one project's persisted FirmSight records.

        Scope is exactly the given project id plus nothing else: platform rows
        go through the existing foreign-key cascades, Memory/Project
        Intelligence rows are removed explicitly, and global settings or other
        projects are never touched. The engineer's local firmware directory is
        outside FirmSight's ownership and is never deleted. A running review or
        in-flight Project Intelligence job rejects the request with 409 so a
        background worker cannot recreate records for a half-deleted project.
        """
        self.get(project_id)
        memory = self.memory_repository

        def preconditions(conn) -> None:
            if self.repository.count_running_reviews(conn, project_id):
                raise ProjectActiveWork(self._t("error.project_active_review"))
            if memory is not None and memory.count_active_learning_jobs(conn, project_id):
                raise ProjectActiveWork(self._t("error.project_active_intelligence"))

        def related_cleanup(conn, target_project_id: str) -> None:
            if memory is not None:
                memory.delete_project_data(conn, target_project_id)

        try:
            self.repository.delete_project(project_id, preconditions=preconditions, related_cleanup=related_cleanup)
        except ProjectActiveWork as error:
            # ProjectActiveWork messages are already produced through the
            # localized catalog inside the preconditions closure.
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        except LookupError as error:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.project_not_found") from error

    def add_files(self, project_id: str, files: dict[str, str]) -> None:
        self.get(project_id)
        prepared = self._prepared_files(project_id, files)
        if prepared:
            self.repository.add_files(project_id, prepared)

    def _prepared_files(self, project_id: str, files: dict[str, str]) -> list[dict[str, str]]:
        prepared = []
        for path, content in files.items():
            try:
                safe_path = safe_project_path(path)
            except ValueError as error:
                raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.path_invalid", message=str(error)) from error
            if not self._is_allowed_import_path(safe_path):
                raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.path_not_allowed", path=safe_path)
            if len(content.encode("utf-8")) > 1_000_000:
                raise self._error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "error.path_too_large", path=safe_path)
            prepared.append({"id": f"FIL-{uuid4().hex[:12]}", "project_id": project_id, "path": safe_path, "content": content, "language": language_for_path(safe_path), "content_hash": self.indexer.digest(content), "updated_at": now()})
        return prepared

    def files(self, project_id: str):
        self.get(project_id)
        return self.repository.list_files(project_id)

    def file(self, project_id: str, path: str):
        self.get(project_id)
        try:
            record = self.repository.get_file(project_id, safe_project_path(path))
        except ValueError as error:
            raise self._error(status.HTTP_422_UNPROCESSABLE_CONTENT, "error.path_invalid", message=str(error)) from error
        if not record:
            raise self._error(status.HTTP_404_NOT_FOUND, "error.project_file_not_found")
        return record

    def index(self, project_id: str) -> IndexRead:
        self.get(project_id)
        raw_files = self.repository.raw_files(project_id)
        result, lifetime = self._compute_index(project_id, raw_files)
        self._commit_index(project_id, raw_files, result, lifetime)
        return self._index_read(project_id)

    def _index_read(self, project_id: str) -> IndexRead:
        project = self.get(project_id)
        return IndexRead(project_id=project_id, file_count=project.file_count, symbol_count=project.symbol_count, language=project.language, framework=project.framework, target=project.target, build_system=project.build_system)

    def _compute_index(self, project_id: str, raw_files: list[dict[str, Any]]):
        """Run the indexer and lifetime analysis against raw files without persisting.

        Keeping this pure lets directory import compute and validate the derivation
        before any project record is written, so a malformed result cannot leave a
        partially imported project behind.
        """
        result = self.indexer.index(raw_files, project_id=project_id)
        lifetime = self.lifetime_service.analyze(project_id, raw_files, result.symbols, result.relations, result.allocations)
        result.relations.extend(lifetime.ownership_relations)
        # FS-FIX-014: finalize the *combined* indexer + lifetime relation list here,
        # the single boundary shared by directory import, reindex, and local source
        # sync. The persisted list (and therefore the topology fingerprint computed
        # in _commit_index) is exactly this finalized list. IndexResult is frozen,
        # so the field's list is replaced in place.
        result.relations[:] = self._finalize_relations(result.relations)
        return result, lifetime

    @classmethod
    def _finalize_relations(cls, relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Final topology relation invariant before fingerprinting and persistence.

        Relations arrive already deduplicated per producer (the indexer dedupes
        its own; lifetime relations are appended after). This finalizes the
        *combined* list:

        - relations with a unique stable id are kept as-is, in order;
        - byte-for-byte equivalent relations that share one stable id collapse to
          a single deterministic instance (the first occurrence);
        - two different payloads that share one stable id are an internal
          topology derivation failure and raise :class:`TopologyDerivationError`
          so SQLite never throws a raw ``IntegrityError``.

        Deduplication is by full payload equality only. It never merges relations
        that differ by kind/file/line/metadata, so distinct source evidence is
        preserved. No ``INSERT OR IGNORE`` or database-side conflict suppression
        is used.
        """
        seen: dict[str, dict[str, Any]] = {}
        finalized: list[dict[str, Any]] = []
        for relation in relations:
            relation_id = relation.get("id")
            if not relation_id:
                raise TopologyDerivationError("Derived topology relation is missing a stable id.")
            existing = seen.get(relation_id)
            if existing is None:
                seen[relation_id] = relation
                finalized.append(relation)
                continue
            if existing == relation:
                continue
            raise TopologyDerivationError("Derived topology contains conflicting relations with the same stable id.")
        return finalized

    def _commit_index(self, project_id: str, raw_files: list[dict[str, Any]], result, lifetime) -> None:
        # Compute the topology fingerprint over the *final* persisted facts only:
        # after the memory-lifetime ownership relations are merged into the
        # relations list and the allocation ownership states are finalized. The
        # indexer's own `relation_fingerprint` is computed before those lifetime
        # facts, so it must never be persisted here — otherwise an
        # ownership-escape-only source change would silently alter persisted
        # topology while leaving the fingerprint (and therefore linked
        # intelligence revalidation) unchanged.
        #
        # FS-FIX-014: `result.relations` is the finalized list returned by
        # _finalize_relations(); the fingerprint is a pure function of exactly the
        # relation rows that replace_topology() persists.
        relation_fingerprint = compute_topology_fingerprint(result.relations, result.allocations, self.indexer.digest)
        self.repository.replace_topology(
            project_id,
            result.symbols,
            result.relations,
            result.allocations,
            source_snapshot_hash=self.snapshot_hash(raw_files),
            relation_fingerprint=relation_fingerprint,
            indexed_at=now(),
        )
        self.repository.update_project_metadata(project_id, {"language": result.language, "framework": result.framework, "target": result.target, "build_system": result.build_system, "updated_at": now()})

    def symbols(self, project_id: str, search: str | None) -> list[SymbolRead]:
        self.get(project_id)
        return [SymbolRead.model_validate(item) for item in self.repository.list_symbols(project_id, search)]

    def topology(self, project_id: str, search: str | None = None) -> dict[str, list[dict]]:
        self.get(project_id)
        return {
            "symbols": self.repository.list_indexed_symbols(project_id, search),
            "relations": self.repository.list_source_relations(project_id),
            "allocations": self.repository.list_allocation_events(project_id),
            "snapshot": self.repository.topology_snapshot(project_id),
        }

    def topology_path(self, project_id: str, symbol_name: str, depth: int = 1, cap: int = 64) -> dict:
        self.get(project_id)
        return self.repository.topology_path(project_id, symbol_name, depth=depth, cap=cap)

    def lifetime_analysis(self, project_id: str):
        self.get(project_id)
        raw_files = self.repository.raw_files(project_id)
        topology = self.topology(project_id)
        return self.lifetime_service.analyze(project_id, raw_files, topology["symbols"], topology["relations"], topology["allocations"])
