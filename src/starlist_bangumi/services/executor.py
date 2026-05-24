from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from starlist_bangumi.clients.openlist import OpenListClient
from starlist_bangumi.exceptions import ExternalServiceError, OperationError
from starlist_bangumi.models import AnalysisResult, LibraryTarget, OrganizeOptions
from starlist_bangumi.pathing import join_openlist_path, split_openlist_path

ProgressLogger = Callable[[str, int, str], Awaitable[None]]
CONSISTENCY_ATTEMPTS = 8


class Executor:
    def __init__(self, openlist: OpenListClient) -> None:
        self._openlist = openlist

    async def organize(
        self,
        analysis: AnalysisResult,
        options: OrganizeOptions,
        log: ProgressLogger,
    ) -> None:
        self._validate_options(options)
        if analysis.status == "failed":
            raise OperationError("Failed analysis results cannot be organized")
        if analysis.status == "needs_review" and not options.allow_failed_analysis:
            raise OperationError(
                "Needs-review analysis result requires explicit organize confirmation"
            )

        library_targets = library_targets_for_analysis(analysis)
        await log("preflight", 5, "Checking target paths")
        await self._preflight(analysis, options, library_targets)

        if options.delete_target_before:
            await self._cleanup_paths(
                unique_mapping_cleanup_paths(analysis),
                log=log,
                progress=8,
                description="media target file",
            )

        if options.overwrite_archive_target_before:
            await self._cleanup_paths(
                [analysis.archive_target_path],
                log=log,
                progress=12,
                description="archive target folder",
            )

        await self._copy_archive(analysis, options, log)

        total = max(1, len(analysis.mappings))
        await log("library_prepare", 45, "Preparing media-library targets")
        for index, mapping in enumerate(analysis.mappings, start=1):
            progress = 45 + int(index / total * 35)
            await self._copy_mapping(
                mapping.source_path,
                mapping.target_path,
                options,
                log,
                progress,
            )

        await log("verify", 90, "Verifying archive and media-library targets")
        await self._verify(analysis)

        if options.delete_source_after:
            await self._openlist.sleep_between_operations()
            await log("cleanup_source", 95, "Deleting original source folder")
            await self._openlist.remove_path(analysis.source_path)

        await log("complete", 100, "Organize operation completed")

    async def _copy_archive(
        self,
        analysis: AnalysisResult,
        options: OrganizeOptions,
        log: ProgressLogger,
    ) -> None:
        archive_parent, archive_leaf = split_openlist_path(analysis.archive_target_path)
        _, source_folder_name = split_openlist_path(analysis.source_path)
        if archive_leaf != source_folder_name:
            raise OperationError(
                "Archive target must include source folder name",
                details={
                    "archive_target_path": analysis.archive_target_path,
                    "expected_archive_target_path": join_openlist_path(
                        analysis.archive_target_path,
                        source_folder_name,
                    ),
                    "source_folder_name": source_folder_name,
                },
            )
        if await self._openlist.exists(analysis.archive_target_path, refresh=True):
            await log("archive_skip", 35, "Archive target already exists; skipping archive copy")
            return
        await log("archive_copy", 15, f"Copying source folder to {archive_parent}")
        await self._openlist.ensure_dir(archive_parent)
        await self._openlist.copy_path(analysis.source_path, archive_parent)
        await wait_for_copy_tasks_if_available(self._openlist, analysis.source_path, archive_parent)
        await self._wait_for_path(
            analysis.archive_target_path,
            log=log,
            stage="archive_copy",
            progress=35,
            description="archive target",
        )
        await self._openlist.sleep_between_operations()

    async def _copy_mapping(
        self,
        source_path: str,
        target_path: str,
        options: OrganizeOptions,
        log: ProgressLogger,
        progress: int,
    ) -> None:
        if await self._openlist.exists(target_path, refresh=True):
            await log("library_skip", progress, f"Target already exists; skipping {target_path}")
            return
        target_parent, target_name = split_openlist_path(target_path)
        _, source_name = split_openlist_path(source_path)
        copied_path = join_openlist_path(target_parent, source_name)
        if copied_path != target_path and await self._openlist.exists(copied_path, refresh=True):
            await log(
                "library_resume",
                progress,
                f"Renaming existing copied file to {target_name}",
            )
            await self._rename_with_wait(copied_path, target_path, target_name, log, progress)
            await self._openlist.sleep_between_operations()
            return
        await log("library_copy", progress, f"Copying {source_path} to {target_parent}")
        await self._openlist.ensure_dir(target_parent)
        copy_submitted = False
        recovered_existing_staging = False
        try:
            await self._openlist.copy_path(source_path, target_parent)
            copy_submitted = True
        except ExternalServiceError as exc:
            recovered_existing_staging = await self._recover_from_staging_exists_error(
                exc,
                copied_path,
                target_path,
                target_name,
                log,
                progress,
            )
            if not recovered_existing_staging:
                raise
        if recovered_existing_staging:
            return
        if copy_submitted:
            await wait_for_copy_tasks_if_available(self._openlist, source_path, target_parent)
        if source_name != target_name:
            await self._wait_for_path(
                copied_path,
                log=log,
                stage="library_copy",
                progress=progress,
                description="copied staging file",
            )
            await self._openlist.sleep_between_operations()
            await log("library_rename", progress, f"Renaming copied file to {target_name}")
            await self._rename_with_wait(copied_path, target_path, target_name, log, progress)
        else:
            await self._wait_for_path(
                target_path,
                log=log,
                stage="library_copy",
                progress=progress,
                description="copied target file",
            )
        await self._openlist.sleep_between_operations()

    async def _preflight(
        self,
        analysis: AnalysisResult,
        options: OrganizeOptions,
        library_targets: list[LibraryTarget],
    ) -> None:
        media_conflicts: list[str] = []
        archive_conflicts: list[str] = []
        staging_conflicts: list[str] = []

        source_exists = await self._openlist.exists(analysis.source_path)
        if not source_exists and not options.resume_existing:
            raise OperationError(
                "Source path does not exist",
                details={"source_path": analysis.source_path},
            )
        if not source_exists and options.resume_existing:
            await self._verify(analysis)
            return

        if not options.delete_target_before and not options.resume_existing:
            for target_path in unique_mapping_target_paths(analysis):
                if await self._openlist.exists(target_path):
                    media_conflicts.append(target_path)

        if (
            not options.overwrite_archive_target_before
            and not options.resume_existing
            and await self._openlist.exists(analysis.archive_target_path)
        ):
            archive_conflicts.append(analysis.archive_target_path)

        archive_parent, _ = split_openlist_path(analysis.archive_target_path)
        _, source_folder_name = split_openlist_path(analysis.source_path)
        archive_staging_path = join_openlist_path(archive_parent, source_folder_name)
        if (
            archive_staging_path != analysis.archive_target_path
            and not options.resume_existing
            and await self._openlist.exists(archive_staging_path)
        ):
            staging_conflicts.append(archive_staging_path)

        if media_conflicts or archive_conflicts or staging_conflicts:
            raise OperationError(
                "Target paths already exist",
                details={
                    "media_targets": media_conflicts,
                    "archive_targets": archive_conflicts,
                    "staging_paths": staging_conflicts,
                    "resolution": (
                        "Enable media target cleanup, enable archive overwrite, "
                        "or change the path templates."
                    ),
                },
            )

    async def _verify(self, analysis: AnalysisResult) -> None:
        missing: list[str] = []
        if not await self._wait_for_path(analysis.archive_target_path):
            missing.append(analysis.archive_target_path)
        for mapping in analysis.mappings:
            if not await self._wait_for_path(mapping.target_path):
                missing.append(mapping.target_path)
        if missing:
            raise OperationError("Organize verification failed", details={"missing": missing})

    async def _rename_with_wait(
        self,
        copied_path: str,
        target_path: str,
        target_name: str,
        log: ProgressLogger,
        progress: int,
    ) -> None:
        last_error: Exception | None = None
        for attempt in range(CONSISTENCY_ATTEMPTS):
            if await self._openlist.exists(target_path, refresh=True):
                return
            if not await self._openlist.exists(copied_path, refresh=True):
                await self._sleep_consistency_delay(attempt)
                continue
            try:
                await self._openlist.rename_path(copied_path, target_name)
            except Exception as exc:  # pragma: no cover - surfaced through integration behavior
                last_error = exc
                await log(
                    "library_rename",
                    progress,
                    f"Rename attempt {attempt + 1} failed; waiting for OpenList consistency",
                )
                await self._sleep_consistency_delay(attempt)
                continue
            if await self._wait_for_path(target_path):
                return
        details: dict[str, object] = {
            "copied_path": copied_path,
            "target_path": target_path,
            "attempts": CONSISTENCY_ATTEMPTS,
        }
        if last_error is not None:
            details["last_error"] = str(last_error)
        raise OperationError("Copied file could not be renamed after waiting", details=details)

    async def _wait_for_path(
        self,
        path: str,
        *,
        log: ProgressLogger | None = None,
        stage: str = "verify",
        progress: int = 90,
        description: str = "path",
    ) -> bool:
        for attempt in range(CONSISTENCY_ATTEMPTS):
            if await self._openlist.exists(path, refresh=True):
                return True
            if log is not None and attempt == 0:
                await log(stage, progress, f"Waiting for OpenList to expose {description}: {path}")
            await self._sleep_consistency_delay(attempt)
        return False

    async def _wait_for_path_removal(self, path: str) -> None:
        for attempt in range(CONSISTENCY_ATTEMPTS):
            if not await self._openlist.exists(path, refresh=True):
                return
            await self._sleep_consistency_delay(attempt)
        raise OperationError(
            "OpenList path still exists after cleanup",
            details={"path": path, "attempts": CONSISTENCY_ATTEMPTS},
        )

    async def _cleanup_paths(
        self,
        paths: list[str],
        *,
        log: ProgressLogger,
        progress: int,
        description: str,
    ) -> None:
        for parent, names in group_names_by_parent(paths).items():
            await log(
                "cleanup",
                progress,
                f"Deleting {len(names)} existing {description}(s) from {parent}",
            )
            await remove_paths_if_available(self._openlist, paths_for_parent(parent, names))
            await self._wait_for_names_removal(parent, names)
            await self._openlist.sleep_between_operations()

    async def _wait_for_names_removal(self, parent: str, names: list[str]) -> None:
        for attempt in range(CONSISTENCY_ATTEMPTS):
            remaining = await existing_names_if_available(
                self._openlist,
                parent,
                names,
                refresh=True,
            )
            if not remaining:
                return
            await self._sleep_consistency_delay(attempt)
        raise OperationError(
            "OpenList paths still exist after cleanup",
            details={
                "parent": parent,
                "names": names,
                "remaining": sorted(remaining),
                "attempts": CONSISTENCY_ATTEMPTS,
            },
        )

    async def _sleep_consistency_delay(self, attempt: int) -> None:
        await self._openlist.sleep_between_operations()
        if attempt >= 2:
            await asyncio.sleep(min(5, 0.5 * (attempt - 1)))

    def _validate_options(self, options: OrganizeOptions) -> None:
        if not options.resume_existing:
            return
        conflicting_options: list[str] = []
        if options.delete_target_before:
            conflicting_options.append("delete_target_before")
        if options.overwrite_archive_target_before:
            conflicting_options.append("overwrite_archive_target_before")
        if conflicting_options:
            raise OperationError(
                "Resume mode cannot be combined with target cleanup options",
                details={
                    "conflicting_options": conflicting_options,
                    "resolution": (
                        "Run with resume_existing only to continue a partial organize, "
                        "or run without resume_existing to intentionally clear targets."
                    ),
                },
            )

    async def _recover_from_staging_exists_error(
        self,
        exc: ExternalServiceError,
        copied_path: str,
        target_path: str,
        target_name: str,
        log: ProgressLogger,
        progress: int,
    ) -> bool:
        if copied_path == target_path:
            return False
        if not is_openlist_file_exists_error(exc):
            return False
        if not await self._openlist.exists(copied_path, refresh=True):
            return False
        await log(
            "library_resume",
            progress,
            f"OpenList reported copied file already exists; renaming to {target_name}",
        )
        await self._rename_with_wait(copied_path, target_path, target_name, log, progress)
        await self._openlist.sleep_between_operations()
        return True


def library_targets_for_analysis(analysis: AnalysisResult) -> list[LibraryTarget]:
    if analysis.work_plan and analysis.work_plan.library_targets:
        unique_targets: dict[str, LibraryTarget] = {}
        for target in analysis.work_plan.library_targets:
            unique_targets[target.target_path] = target
        return list(unique_targets.values())
    if not analysis.media_target_path:
        return []
    return [
        LibraryTarget(
            media_type="movie" if analysis.media_type == "movie" else "tv",
            target_path=analysis.media_target_path,
            title=analysis.title,
            year=analysis.year,
            tmdb_id=analysis.tmdb_id,
        )
    ]


def unique_mapping_target_paths(analysis: AnalysisResult) -> list[str]:
    unique: dict[str, None] = {}
    for mapping in analysis.mappings:
        unique[mapping.target_path] = None
    return list(unique)


def unique_mapping_cleanup_paths(analysis: AnalysisResult) -> list[str]:
    unique: dict[str, None] = {}
    for mapping in analysis.mappings:
        unique[mapping.target_path] = None
        staging_path = mapping_staging_path(mapping.source_path, mapping.target_path)
        if staging_path != mapping.target_path:
            unique[staging_path] = None
    return list(unique)


def mapping_staging_path(source_path: str, target_path: str) -> str:
    target_parent, _ = split_openlist_path(target_path)
    _, source_name = split_openlist_path(source_path)
    return join_openlist_path(target_parent, source_name)


def group_names_by_parent(paths: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for path in paths:
        parent, name = split_openlist_path(path)
        if not name:
            continue
        key = (parent, name)
        if key in seen:
            continue
        grouped.setdefault(parent, []).append(name)
        seen.add(key)
    return grouped


def paths_for_parent(parent: str, names: list[str]) -> list[str]:
    return [join_openlist_path(parent, name) for name in names]


async def remove_paths_if_available(openlist: OpenListClient, paths: list[str]) -> None:
    remove_paths = getattr(openlist, "remove_paths", None)
    if remove_paths is not None:
        await remove_paths(paths)
        return
    for path in paths:
        await openlist.remove_path(path)


async def existing_names_if_available(
    openlist: OpenListClient,
    parent: str,
    names: list[str],
    *,
    refresh: bool,
) -> set[str]:
    existing_names = getattr(openlist, "existing_names", None)
    if existing_names is not None:
        return await existing_names(parent, names, refresh=refresh)
    remaining: set[str] = set()
    for name in names:
        if await openlist.exists(join_openlist_path(parent, name), refresh=refresh):
            remaining.add(name)
    return remaining


async def wait_for_copy_tasks_if_available(
    openlist: OpenListClient,
    source_path: str,
    destination_dir: str,
) -> None:
    wait_for_copy_tasks = getattr(openlist, "wait_for_copy_tasks", None)
    if wait_for_copy_tasks is None:
        return
    await wait_for_copy_tasks(source_path, destination_dir)


def is_openlist_file_exists_error(exc: ExternalServiceError) -> bool:
    details = exc.details
    messages: list[str] = [str(exc)]
    last_error = details.get("last_error")
    if last_error:
        messages.append(str(last_error))
    body = details.get("body")
    if isinstance(body, dict):
        messages.append(str(body.get("message") or body.get("msg") or ""))
    last_error_details = details.get("last_error_details")
    if isinstance(last_error_details, dict):
        nested_body = last_error_details.get("body")
        if isinstance(nested_body, dict):
            messages.append(str(nested_body.get("message") or nested_body.get("msg") or ""))
    normalized = " ".join(messages).casefold()
    return "file" in normalized and "exists" in normalized
