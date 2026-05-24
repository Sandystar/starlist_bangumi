from pathlib import Path

import pytest
from pydantic import ValidationError

from starlist_bangumi.run_cleanup import RunCleaner, RunCleanupOptions, ensure_inside_root
from starlist_bangumi.run_index import RunIndex
from tests.test_run_index import write_run


def test_cleanup_requires_filter_or_all() -> None:
    with pytest.raises(ValidationError):
        RunCleanupOptions()


def test_cleanup_preview_keeps_latest_per_source(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    write_run(
        tmp_path,
        "20260519-020304-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    cleaner = RunCleaner(RunIndex(tmp_path))

    result = cleaner.cleanup(
        RunCleanupOptions(
            all=True,
            keep_latest_per_source=1,
        )
    )

    assert result.executed is False
    assert [candidate.run_id for candidate in result.candidates] == [
        "20260519-010203-Example"
    ]
    assert (tmp_path / "20260519-010203-Example").exists()


def test_cleanup_execute_deletes_candidates(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="failed",
    )
    cleaner = RunCleaner(RunIndex(tmp_path))

    result = cleaner.cleanup(RunCleanupOptions(status="failed", execute=True))

    assert result.deleted == ["20260519-010203-Example"]
    assert not (tmp_path / "20260519-010203-Example").exists()


def test_cleanup_protects_manual_mapping_runs_by_default(tmp_path: Path) -> None:
    run_dir = write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="needs_review",
    )
    (run_dir / "artifacts" / "manual_episode_mappings.json").write_text(
        '{"schema_version":"1.0","episode_mappings":[]}',
        encoding="utf-8",
    )
    cleaner = RunCleaner(RunIndex(tmp_path))

    protected = cleaner.cleanup(RunCleanupOptions(status="needs_review", execute=True))
    deleted = cleaner.cleanup(
        RunCleanupOptions(
            status="needs_review",
            include_manual=True,
            execute=True,
        )
    )

    assert protected.deleted == []
    assert deleted.deleted == ["20260519-010203-Example"]


def test_cleanup_refuses_to_delete_outside_run_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ensure_inside_root(str(tmp_path.parent), tmp_path)
