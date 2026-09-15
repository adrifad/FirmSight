from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException, status

from .indexer import SOURCE_EXTENSIONS, FirmwareIndexer, language_for_path, safe_project_path
from .platform_repository import PlatformRepository, ProjectActiveWork
from .platform_schemas import IndexRead, ProjectCreate, ProjectDirectoryImport, ProjectRead, ProjectSourceType, SymbolRead

if TYPE_CHECKING:
    from .repository import MemoryRepository


def now() -> str:
    return datetime.now(UTC).isoformat()


class ProjectService:
    def __init__(self, repository: PlatformRepository, indexer: FirmwareIndexer, import_root: str | Path | None = None, memory_repository: "MemoryRepository | None" = None) -> None:
        self.repository, self.indexer = repository, indexer
        # The Memory/Project Intelligence tables share this SQLite file but do
        # not all carry a projects foreign key, so deletion needs them here to
        # stay inside the same transaction.
        self.memory_repository = memory_repository
        configured_root = import_root or os.getenv("FIRMSIGHT_IMPORT_ROOT")
        self.import_root = Path(configured_root).expanduser() if configured_root else None

    def create(self, request: ProjectCreate, *, source_directory: str | None = None) -> ProjectRead:
        timestamp = now()
        record = {"id": f"PRJ-{uuid4().hex[:10].upper()}", **request.model_dump(mode="json"), "language": None, "framework": None, "target": None, "build_system": None, "source_directory": source_directory, "created_at": timestamp, "updated_at": timestamp}
        self.repository.create_project(record)
        return self.get(record["id"])

    def import_directory(self, request: ProjectDirectoryImport) -> ProjectRead:
        directory, files = self._read_local_directory(request.directory)
        project = self.create(
            ProjectCreate(name=request.name or directory.name, description=request.description, source_type=ProjectSourceType.LOCAL_DIRECTORY),
            source_directory=str(directory),
        )
        self.add_files(project.id, files)
        self.index(project.id)
        return self.get(project.id)

    def refresh_local_directory(self, project_id: str, directory_override: str | None = None) -> tuple[IndexRead, list[str]]:
        record = self.repository.get_project(project_id)
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        if record["source_type"] != ProjectSourceType.LOCAL_DIRECTORY:
            raise HTTPException(status.HTTP_409_CONFLICT, "Source sync requires a project imported from a local directory.")
        directory_value = directory_override or record.get("source_directory")
        if not directory_value:
            raise HTTPException(status.HTTP_409_CONFLICT, "Attach the current local source directory before syncing it.")
        directory, files = self._read_local_directory(directory_value)
        if directory_override:
            self.repository.update_project_source_directory(project_id, str(directory))
        previous = {file["path"]: file["content_hash"] for file in self.repository.raw_files(project_id)}
        prepared = self._prepared_files(project_id, files)
        current = {file["path"]: file["content_hash"] for file in prepared}
        changed = sorted(path for path, digest in current.items() if previous.get(path) != digest)
        changed.extend(sorted(path for path in previous if path not in current))
        self.repository.replace_files(project_id, prepared, now())
        return self.index(project_id), changed

    def current_source_snapshot_hash(self, project_id: str) -> str:
        """Return a safe fingerprint of the source currently available to a project.

        Local projects are read from their configured directory without mutating
        persisted files. Manual projects use their persisted file snapshot. The
        fingerprint contains only ordered paths and content hashes, never source
        text, so it is safe to use as a resume guard.
        """
        record = self.repository.get_project(project_id)
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        if record["source_type"] == ProjectSourceType.LOCAL_DIRECTORY:
            directory_value = record.get("source_directory")
            if not directory_value:
                raise HTTPException(status.HTTP_409_CONFLICT, "Attach the current local source directory before resuming this review.")
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
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Project directory cannot be a symbolic link")
        try:
            directory = requested.resolve(strict=True)
        except OSError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project directory was not found") from error
        if not directory.is_dir():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Project path must be a directory")
        if not directory.is_relative_to(root):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Project directory is outside FIRMSIGHT_IMPORT_ROOT")

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
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Project directory exceeds the 500-file import limit")
            try:
                files[relative_path] = candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{relative_path} is not valid UTF-8 source text") from error
        if not files:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No supported firmware source or project metadata files were found")
        return directory, files

    def _validated_import_root(self) -> Path:
        if not self.import_root:
            raise HTTPException(status.HTTP_409_CONFLICT, "Set FIRMSIGHT_IMPORT_ROOT in the backend environment before importing a local directory")
        try:
            root = self.import_root.resolve(strict=True)
        except OSError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, "Configured FIRMSIGHT_IMPORT_ROOT does not exist") from error
        if not root.is_dir():
            raise HTTPException(status.HTTP_409_CONFLICT, "Configured FIRMSIGHT_IMPORT_ROOT must be a directory")
        if root == Path(root.anchor):
            raise HTTPException(status.HTTP_409_CONFLICT, "Configured FIRMSIGHT_IMPORT_ROOT must not be the filesystem root")
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
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
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
                raise ProjectActiveWork("This project has a running AI review. Wait for it to finish before deleting.")
            if memory is not None and memory.count_active_learning_jobs(conn, project_id):
                raise ProjectActiveWork("This project has queued or running Project Intelligence work. Wait for it to finish before deleting.")

        def related_cleanup(conn, target_project_id: str) -> None:
            if memory is not None:
                memory.delete_project_data(conn, target_project_id)

        try:
            self.repository.delete_project(project_id, preconditions=preconditions, related_cleanup=related_cleanup)
        except ProjectActiveWork as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        except LookupError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from error

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
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
            if not self._is_allowed_import_path(safe_path):
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{safe_path} is not an allowed source or project metadata file")
            if len(content.encode("utf-8")) > 1_000_000:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{safe_path} exceeds the per-file import limit")
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
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
        if not record:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project file not found")
        return record

    def index(self, project_id: str) -> IndexRead:
        self.get(project_id)
        result = self.indexer.index(self.repository.raw_files(project_id))
        self.repository.replace_symbols(project_id, [{**symbol, "project_id": project_id} for symbol in result.symbols])
        self.repository.update_project_metadata(project_id, {"language": result.language, "framework": result.framework, "target": result.target, "build_system": result.build_system, "updated_at": now()})
        project = self.get(project_id)
        return IndexRead(project_id=project_id, file_count=project.file_count, symbol_count=project.symbol_count, language=project.language, framework=project.framework, target=project.target, build_system=project.build_system)

    def symbols(self, project_id: str, search: str | None) -> list[SymbolRead]:
        self.get(project_id)
        return [SymbolRead.model_validate(item) for item in self.repository.list_symbols(project_id, search)]
