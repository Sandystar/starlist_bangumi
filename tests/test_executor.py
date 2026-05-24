import pytest

from starlist_bangumi.exceptions import ExternalServiceError, OperationError
from starlist_bangumi.models import (
    AnalysisResult,
    FileMapping,
    LibraryTarget,
    OrganizeOptions,
    ValidatedMapping,
    WorkPlan,
)
from starlist_bangumi.pathing import join_openlist_path, split_openlist_path
from starlist_bangumi.services.executor import Executor


@pytest.mark.asyncio
async def test_organize_fails_when_media_target_exists_without_cleanup() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(existing={analysis.source_path, analysis.mappings[0].target_path})
    executor = Executor(openlist)

    with pytest.raises(OperationError) as exc_info:
        await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    assert exc_info.value.details["media_targets"] == [analysis.mappings[0].target_path]
    assert openlist.copied == []


@pytest.mark.asyncio
async def test_media_cleanup_deletes_same_named_target_files_not_library_root() -> None:
    analysis = movie_analysis()
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    _, source_name = split_openlist_path(analysis.mappings[0].source_path)
    staging_path = join_openlist_path(target_parent, source_name)
    openlist = FakeOpenList(
        existing={
            analysis.source_path,
            analysis.media_target_path,
            analysis.mappings[0].target_path,
            staging_path,
        }
    )
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(delete_target_before=True),
        log=noop_log,
    )

    assert analysis.media_target_path not in openlist.removed
    assert analysis.mappings[0].target_path in openlist.removed
    assert staging_path in openlist.removed
    assert analysis.archive_target_path not in openlist.removed
    assert analysis.archive_target_path in openlist.existing
    assert analysis.mappings[0].target_path in openlist.existing


@pytest.mark.asyncio
async def test_media_cleanup_batches_files_by_parent_directory() -> None:
    analysis = movie_analysis()
    media_target = analysis.media_target_path
    second_mapping = FileMapping(
        source_path="/Inbox/Example/Example 02.mkv",
        target_relative_path="Example 02 (2024).mkv",
        target_path=f"{media_target}/Example 02 (2024).mkv",
        reason="test",
    )
    analysis.mappings.append(second_mapping)
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    cleanup_paths = [
        analysis.mappings[0].target_path,
        join_openlist_path(target_parent, "Example.mkv"),
        second_mapping.target_path,
        join_openlist_path(target_parent, "Example 02.mkv"),
    ]
    openlist = FakeOpenList(existing={analysis.source_path, *cleanup_paths})
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(delete_target_before=True),
        log=noop_log,
    )

    media_batches = [batch for batch in openlist.remove_batches if batch[0] == target_parent]
    assert media_batches == [
        (
            target_parent,
            [
                "Example (2024).mkv",
                "Example.mkv",
                "Example 02 (2024).mkv",
                "Example 02.mkv",
            ],
        )
    ]


@pytest.mark.asyncio
async def test_archive_target_requires_explicit_overwrite() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(existing={analysis.source_path, analysis.archive_target_path})
    executor = Executor(openlist)

    with pytest.raises(OperationError) as exc_info:
        await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    assert exc_info.value.details["archive_targets"] == [analysis.archive_target_path]

    await executor.organize(
        analysis,
        OrganizeOptions(overwrite_archive_target_before=True),
        log=noop_log,
    )

    assert analysis.archive_target_path in openlist.removed


@pytest.mark.asyncio
async def test_archive_overwrite_deletes_only_archive_leaf_folder() -> None:
    analysis = movie_analysis()
    archive_parent, _ = split_openlist_path(analysis.archive_target_path)
    openlist = FakeOpenList(
        existing={
            analysis.source_path,
            archive_parent,
            analysis.archive_target_path,
            join_openlist_path(archive_parent, "Other Release"),
        }
    )
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(overwrite_archive_target_before=True),
        log=noop_log,
    )

    assert analysis.archive_target_path in openlist.removed
    assert archive_parent not in openlist.removed
    assert join_openlist_path(archive_parent, "Other Release") in openlist.existing
    assert analysis.archive_target_path in openlist.existing


@pytest.mark.asyncio
async def test_archive_target_without_source_leaf_fails_before_copying() -> None:
    analysis = movie_analysis()
    analysis.archive_target_path = "/Archive/2024/Example Canonical"
    if analysis.work_plan is not None:
        analysis.work_plan.archive_target_path = analysis.archive_target_path
    openlist = FakeOpenList(existing={analysis.source_path})
    executor = Executor(openlist)

    with pytest.raises(OperationError) as exc_info:
        await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    assert exc_info.value.details["expected_archive_target_path"] == (
        "/Archive/2024/Example Canonical/Example"
    )
    assert openlist.copied == []


@pytest.mark.asyncio
async def test_needs_review_requires_explicit_confirmation() -> None:
    analysis = movie_analysis()
    analysis.status = "needs_review"
    openlist = FakeOpenList(existing={analysis.source_path})
    executor = Executor(openlist)

    with pytest.raises(OperationError, match="requires explicit organize confirmation"):
        await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    await executor.organize(
        analysis,
        OrganizeOptions(allow_failed_analysis=True),
        log=noop_log,
    )

    assert analysis.archive_target_path in openlist.existing


@pytest.mark.asyncio
async def test_failed_analysis_cannot_be_forced_to_organize() -> None:
    analysis = movie_analysis()
    analysis.status = "failed"
    openlist = FakeOpenList(existing={analysis.source_path})
    executor = Executor(openlist)

    with pytest.raises(OperationError, match="Failed analysis results cannot be organized"):
        await executor.organize(
            analysis,
            OrganizeOptions(allow_failed_analysis=True),
            log=noop_log,
        )

    assert openlist.copied == []


@pytest.mark.asyncio
async def test_resume_existing_skips_completed_targets() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(
        existing={
            analysis.source_path,
            analysis.archive_target_path,
            analysis.mappings[0].target_path,
        }
    )
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(resume_existing=True),
        log=noop_log,
    )

    assert openlist.copied == []
    assert openlist.renamed == []


@pytest.mark.asyncio
async def test_resume_existing_renames_staging_paths() -> None:
    analysis = movie_analysis()
    archive_parent, _ = split_openlist_path(analysis.archive_target_path)
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    mapping_staging = join_openlist_path(target_parent, "Example.mkv")
    openlist = FakeOpenList(
        existing={analysis.source_path, analysis.archive_target_path, mapping_staging}
    )
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(resume_existing=True),
        log=noop_log,
    )

    assert (mapping_staging, "Example (2024).mkv") in openlist.renamed
    assert analysis.archive_target_path in openlist.existing
    assert analysis.mappings[0].target_path in openlist.existing


@pytest.mark.asyncio
async def test_copy_mapping_waits_for_staging_file_before_rename() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(existing={analysis.source_path})
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    _, source_name = split_openlist_path(analysis.mappings[0].source_path)
    staging_path = join_openlist_path(target_parent, source_name)
    openlist.delayed_exists[staging_path] = 2
    executor = Executor(openlist)

    await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    assert (staging_path, "Example (2024).mkv") in openlist.renamed
    assert analysis.mappings[0].target_path in openlist.existing
    assert (analysis.mappings[0].source_path, target_parent) in openlist.waited_copy_tasks


@pytest.mark.asyncio
async def test_copy_mapping_renames_existing_staging_file_without_copying() -> None:
    analysis = movie_analysis()
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    _, source_name = split_openlist_path(analysis.mappings[0].source_path)
    staging_path = join_openlist_path(target_parent, source_name)
    openlist = FakeOpenList(
        existing={analysis.source_path, analysis.archive_target_path, staging_path}
    )
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(resume_existing=True),
        log=noop_log,
    )

    assert (staging_path, "Example (2024).mkv") in openlist.renamed
    assert analysis.mappings[0].source_path not in [
        source for source, _destination in openlist.copied
    ]
    assert analysis.mappings[0].target_path in openlist.existing


@pytest.mark.asyncio
async def test_copy_mapping_recovers_when_copy_reports_staging_file_exists() -> None:
    analysis = movie_analysis()
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    _, source_name = split_openlist_path(analysis.mappings[0].source_path)
    staging_path = join_openlist_path(target_parent, source_name)
    openlist = FakeOpenList(existing={analysis.source_path, analysis.archive_target_path})
    openlist.copy_file_exists_errors.add(analysis.mappings[0].source_path)
    openlist.staging_after_copy_error.add(staging_path)
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(resume_existing=True),
        log=noop_log,
    )

    assert (staging_path, "Example (2024).mkv") in openlist.renamed
    assert analysis.mappings[0].target_path in openlist.existing


@pytest.mark.asyncio
async def test_copy_mapping_retries_rename_when_openlist_is_eventually_consistent() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(existing={analysis.source_path})
    target_parent, _ = split_openlist_path(analysis.mappings[0].target_path)
    _, source_name = split_openlist_path(analysis.mappings[0].source_path)
    staging_path = join_openlist_path(target_parent, source_name)
    openlist.rename_failures[staging_path] = 1
    executor = Executor(openlist)

    await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    assert openlist.renamed.count((staging_path, "Example (2024).mkv")) == 2
    assert analysis.mappings[0].target_path in openlist.existing


@pytest.mark.asyncio
async def test_verify_waits_for_delayed_targets() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(existing={analysis.source_path})
    openlist.delayed_exists[analysis.mappings[0].target_path] = 2
    executor = Executor(openlist)

    await executor.organize(analysis, OrganizeOptions(), log=noop_log)

    assert analysis.mappings[0].target_path in openlist.existing


@pytest.mark.asyncio
async def test_resume_existing_allows_missing_source_when_outputs_verify() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(
        existing={analysis.archive_target_path, analysis.mappings[0].target_path}
    )
    executor = Executor(openlist)

    await executor.organize(
        analysis,
        OrganizeOptions(resume_existing=True),
        log=noop_log,
    )

    assert openlist.copied == []


@pytest.mark.asyncio
async def test_resume_existing_rejects_cleanup_options() -> None:
    analysis = movie_analysis()
    openlist = FakeOpenList(existing={analysis.source_path})
    executor = Executor(openlist)

    with pytest.raises(OperationError) as exc_info:
        await executor.organize(
            analysis,
            OrganizeOptions(
                resume_existing=True,
                delete_target_before=True,
                overwrite_archive_target_before=True,
            ),
            log=noop_log,
        )

    assert exc_info.value.details["conflicting_options"] == [
        "delete_target_before",
        "overwrite_archive_target_before",
    ]
    assert openlist.removed == []


def movie_analysis() -> AnalysisResult:
    media_target = "/Movies/2024/Example (2024) [tmdbid=1]"
    archive_target = "/Archive/2024/Example Canonical/Example"
    mapping = FileMapping(
        source_path="/Inbox/Example/Example.mkv",
        target_relative_path="Example (2024).mkv",
        target_path=f"{media_target}/Example (2024).mkv",
        reason="test",
    )
    plan = WorkPlan(
        work_title="Example",
        source_name="Example",
        source_path="/Inbox/Example",
        archive_target_path=archive_target,
        library_targets=[
            LibraryTarget(
                media_type="movie",
                target_path=media_target,
                title="Example",
                year="2024",
                tmdb_id="1",
            )
        ],
        validated_mappings=[
            ValidatedMapping(
                source_path=mapping.source_path,
                target_path=mapping.target_path,
                target_relative_path=mapping.target_relative_path,
                target_kind="movie",
                media_type="movie",
                tmdb_movie_id="1",
            )
        ],
    )
    return AnalysisResult(
        id="analysis-1",
        source_name="Example",
        source_path="/Inbox/Example",
        status="succeeded",
        confidence=1,
        media_type="movie",
        title="Example",
        year="2024",
        tmdb_id="1",
        media_target_path=media_target,
        archive_target_path=archive_target,
        report_tree="dry-run",
        summary="Example",
        mappings=[mapping],
        work_plan=plan,
    )


async def noop_log(stage: str, progress: int, message: str) -> None:
    return None


class FakeOpenList:
    def __init__(self, *, existing: set[str]) -> None:
        self.existing = set(existing)
        self.removed: list[str] = []
        self.remove_batches: list[tuple[str, list[str]]] = []
        self.copied: list[tuple[str, str]] = []
        self.renamed: list[tuple[str, str]] = []
        self.delayed_exists: dict[str, int] = {}
        self.rename_failures: dict[str, int] = {}
        self.copy_file_exists_errors: set[str] = set()
        self.staging_after_copy_error: set[str] = set()
        self.waited_copy_tasks: list[tuple[str, str]] = []

    async def exists(self, path: str, *, refresh: bool = False) -> bool:
        remaining = self.delayed_exists.get(path)
        if remaining is not None and path in self.existing:
            if remaining > 0:
                self.delayed_exists[path] = remaining - 1
                return False
            self.delayed_exists.pop(path, None)
        return path in self.existing

    async def ensure_dir(self, path: str) -> None:
        current = "/"
        for part in path.strip("/").split("/"):
            if not part:
                continue
            current = join_openlist_path(current, part)
            self.existing.add(current)

    async def remove_path(self, path: str) -> None:
        await self.remove_paths([path])

    async def remove_paths(self, paths: list[str]) -> None:
        grouped: dict[str, list[str]] = {}
        for path in paths:
            parent, name = split_openlist_path(path)
            if not name:
                continue
            grouped.setdefault(parent, []).append(name)
        for parent, names in grouped.items():
            self.remove_batches.append((parent, names))
            for name in names:
                path = join_openlist_path(parent, name)
                self.removed.append(path)
                self.existing = {
                    existing
                    for existing in self.existing
                    if existing != path and not existing.startswith(path.rstrip("/") + "/")
                }

    async def existing_names(
        self,
        parent: str,
        names: list[str],
        *,
        refresh: bool = False,
    ) -> set[str]:
        requested = set(names)
        return {
            name
            for name in requested
            if join_openlist_path(parent, name) in self.existing
        }

    async def copy_path(self, source_path: str, destination_dir: str) -> None:
        self.copied.append((source_path, destination_dir))
        await self.ensure_dir(destination_dir)
        _, source_name = split_openlist_path(source_path)
        if source_path in self.copy_file_exists_errors:
            self.existing.update(self.staging_after_copy_error)
            raise ExternalServiceError(
                "OpenList request failed after retries",
                details={
                    "last_error": f"OpenList API failed: file [{source_name}] exists",
                    "last_error_details": {
                        "body": {"code": 403, "message": f"file [{source_name}] exists"}
                    },
                },
            )
        self.existing.add(join_openlist_path(destination_dir, source_name))

    async def wait_for_copy_tasks(self, source_path: str, destination_dir: str) -> None:
        self.waited_copy_tasks.append((source_path, destination_dir))

    async def rename_path(self, path: str, new_name: str) -> None:
        self.renamed.append((path, new_name))
        remaining_failures = self.rename_failures.get(path, 0)
        if remaining_failures:
            self.rename_failures[path] = remaining_failures - 1
            raise ExternalServiceError("transient rename failure")
        parent, _ = split_openlist_path(path)
        new_path = join_openlist_path(parent, new_name)
        self.existing.discard(path)
        self.existing.add(new_path)

    async def sleep_between_operations(self) -> None:
        return None
