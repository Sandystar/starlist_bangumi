import json
from pathlib import Path

from starlist_bangumi.models import (
    AnalysisRequestItem,
    CoverageScope,
    LibraryTarget,
    SelectedMovie,
    UnmappedFile,
    ValidatedMapping,
    WorkPlan,
)
from starlist_bangumi.run_index import RunIndex, RunIndexFilters
from starlist_bangumi.services.plan_builder import analysis_result_from_work_plan


def test_run_index_lists_file_based_runs(tmp_path: Path) -> None:
    run_dir = write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    (run_dir / "organize_status.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "archive_target_path": "/Archive/Example Canonical",
                "error": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    runs = RunIndex(tmp_path).list_runs()

    assert len(runs) == 1
    assert runs[0].run_id == "20260519-010203-Example"
    assert runs[0].analysis_status == "succeeded"
    assert runs[0].organize_status == "succeeded"
    assert runs[0].source_name == "Example"
    assert runs[0].library_targets == ["/Movies/Example (2024) [tmdbid=1]"]
    assert runs[0].archive_target_path == "/Archive/Example Canonical/Example"
    assert runs[0].created_at.year == 2026
    assert runs[0].created_at.utcoffset() is not None


def test_run_index_filters_and_latest_only_per_source(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="needs_review",
    )
    write_run(
        tmp_path,
        "20260519-020304-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    write_run(
        tmp_path,
        "20260519-030405-Other",
        source_path="/Inbox/Other",
        status="succeeded",
    )

    runs = RunIndex(tmp_path).list_runs(
        RunIndexFilters(status="succeeded", latest_only=True)
    )

    assert [run.run_id for run in runs] == [
        "20260519-030405-Other",
        "20260519-020304-Example",
    ]


def test_run_index_reads_manual_remap_metadata(tmp_path: Path) -> None:
    run_dir = write_run(
        tmp_path,
        "20260519-020304-Example-remap",
        source_path="/Inbox/Example",
        status="needs_review",
    )
    (run_dir / "artifacts" / "manual_episode_mappings.json").write_text(
        '{"schema_version":"1.0","episode_mappings":[]}',
        encoding="utf-8",
    )
    (run_dir / "artifacts" / "manual_remap_request.json").write_text(
        json.dumps({"source_run_dir": "data/runs/old-run"}, indent=2),
        encoding="utf-8",
    )

    run = RunIndex(tmp_path).list_runs()[0]

    assert run.has_manual_episode_mappings is True
    assert run.source_run_dir == "data/runs/old-run"


def test_run_index_load_analysis_backfills_archive_source_leaf(tmp_path: Path) -> None:
    write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )

    analysis = RunIndex(tmp_path).load_analysis("20260519-010203-Example")

    assert analysis.archive_target_path == "/Archive/Example Canonical/Example"
    assert analysis.work_plan is not None
    assert analysis.work_plan.archive_target_path == "/Archive/Example Canonical/Example"


def write_run(
    root: Path,
    name: str,
    *,
    source_path: str,
    status: str,
) -> Path:
    run_dir = root / name
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    work_plan = movie_work_plan(source_path)
    analysis = analysis_result_from_work_plan(
        item=AnalysisRequestItem(name=Path(source_path).name, path=source_path, prompt=""),
        work_plan=work_plan,
        media_type="movie",
        confidence=0.92,
    )
    analysis.status = status  # type: ignore[assignment]
    (artifacts / "analysis_result.json").write_text(
        analysis.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (artifacts / "work_plan.json").write_text(
        work_plan.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (artifacts / "run_input.json").write_text(
        json.dumps(
            {
                "folder_name": Path(source_path).name,
                "source_path": source_path,
                "selection_mode": "llm",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "analysis_status.json").write_text(
        json.dumps(
            {
                "status": status,
                "review_reason": "manual review" if status == "needs_review" else "",
                "validated": 1,
                "rejected": 0,
                "missing_tmdb_episodes": 0,
                "missing_movies": 0,
                "unmapped_files": 1,
                "library_targets": [
                    {
                        "media_type": "movie",
                        "target_path": "/Movies/Example (2024) [tmdbid=1]",
                    }
                ],
                "archive_target_path": "/Archive/Example Canonical",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({"run_dir": str(run_dir)}, indent=2),
        encoding="utf-8",
    )
    return run_dir


def movie_work_plan(source_path: str) -> WorkPlan:
    return WorkPlan(
        work_title="Example",
        source_name=Path(source_path).name,
        source_path=source_path,
        archive_target_path="/Archive/Example Canonical",
        coverage_scope=[CoverageScope(type="movie", tmdb_movie_id="1")],
        selected_movies=[SelectedMovie(tmdb_id="1", title="Example", year="2024")],
        library_targets=[
            LibraryTarget(
                media_type="movie",
                target_path="/Movies/Example (2024) [tmdbid=1]",
                title="Example",
                year="2024",
                tmdb_id="1",
            )
        ],
        validated_mappings=[
            ValidatedMapping(
                source_path=f"{source_path}/Example.mkv",
                target_path="/Movies/Example (2024) [tmdbid=1]/Example (2024).mkv",
                target_relative_path="Example (2024).mkv",
                target_kind="movie",
                media_type="movie",
                tmdb_movie_id="1",
            )
        ],
        unmapped_files=[
            UnmappedFile(
                source_path=f"{source_path}/Extra.mkv",
                file_kind="video",
                reason="not_selected_for_media_library",
            )
        ],
    )
