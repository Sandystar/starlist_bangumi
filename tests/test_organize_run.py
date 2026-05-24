from pathlib import Path

from starlist_bangumi.models import (
    AnalysisRequestItem,
    CoverageScope,
    LibraryTarget,
    OrganizeOptions,
    SelectedMovie,
    UnmappedFile,
    ValidatedMapping,
    WorkPlan,
)
from starlist_bangumi.services.plan_builder import analysis_result_from_work_plan
from tools.organize_run import load_analysis, save_analysis_result, write_status


def test_load_analysis_falls_back_to_work_plan(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    work_plan = movie_work_plan()
    (artifacts_dir / "work_plan.json").write_text(
        work_plan.model_dump_json(indent=2),
        encoding="utf-8",
    )

    analysis = load_analysis(run_dir)

    assert analysis.status == "succeeded"
    assert analysis.source_path == "/Inbox/Example"
    assert analysis.archive_target_path == "/Archive/Example Canonical/Example"
    assert analysis.mappings[0].target_path.endswith("Example (2024).mkv")


def test_load_analysis_accepts_root_work_plan_and_saves_analysis_result(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "work_plan.json").write_text(
        movie_work_plan().model_dump_json(indent=2),
        encoding="utf-8",
    )

    analysis = load_analysis(run_dir)
    save_analysis_result(run_dir, analysis)

    assert analysis.status == "succeeded"
    assert analysis.archive_target_path == "/Archive/Example Canonical/Example"
    assert (run_dir / "artifacts" / "analysis_result.json").exists()


def test_load_analysis_keeps_succeeded_status_for_unmapped_extra_video(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    work_plan = movie_work_plan()
    stale = analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name=work_plan.source_name,
            path=work_plan.source_path,
            prompt="",
        ),
        work_plan=work_plan,
        media_type="movie",
        confidence=0.92,
    )
    stale.status = "succeeded"
    assert stale.work_plan is not None
    stale.work_plan.unmapped_files = [
        UnmappedFile(
            source_path="/Inbox/Example/Example OVA.mkv",
            file_kind="video",
            reason="No concrete TMDB Season 0 episode match.",
        )
    ]
    (artifacts_dir / "analysis_result.json").write_text(
        stale.model_dump_json(indent=2),
        encoding="utf-8",
    )

    analysis = load_analysis(run_dir)

    assert analysis.status == "succeeded"
    assert "video file(s) were not mapped" not in analysis.summary


def test_load_analysis_backfills_missing_tmdb_episodes_from_run_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifacts_dir = run_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    work_plan = movie_work_plan()
    stale = analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name=work_plan.source_name,
            path=work_plan.source_path,
            prompt="",
        ),
        work_plan=work_plan,
        media_type="movie",
        confidence=0.92,
    )
    assert stale.work_plan is not None
    stale.work_plan.unmapped_files = [
        UnmappedFile(
            source_path="/Inbox/Example/Example OVA.mkv",
            file_kind="video",
            reason="No concrete TMDB Season 0 episode match.",
        )
    ]
    (artifacts_dir / "analysis_result.json").write_text(
        stale.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (artifacts_dir / "tmdb_season_details.prompt.json").write_text(
        """
        {
          "0": {
            "episodes": [
              {"episode_number": 1, "name": "OVA Beach Episode"}
            ]
          }
        }
        """,
        encoding="utf-8",
    )

    analysis = load_analysis(run_dir)

    assert analysis.status == "needs_review"
    assert analysis.work_plan is not None
    assert analysis.work_plan.missing_tmdb_episodes[0].season_number == 0
    assert analysis.work_plan.missing_tmdb_episodes[0].episode_number == 1


async def test_write_status_records_resume_existing_option(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "artifacts").mkdir(parents=True)
    analysis = analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name="Example",
            path="/Inbox/Example",
            prompt="",
        ),
        work_plan=movie_work_plan(),
        media_type="movie",
        confidence=0.92,
    )

    await write_status(
        run_dir,
        status="running",
        analysis=analysis,
        options=OrganizeOptions(resume_existing=True),
        elapsed_seconds=1.25,
    )

    status_text = (run_dir / "organize_status.json").read_text(encoding="utf-8")

    assert '"resume_existing": true' in status_text


def movie_work_plan() -> WorkPlan:
    return WorkPlan(
        work_title="Example",
        source_name="Example",
        source_path="/Inbox/Example",
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
                source_path="/Inbox/Example/Example.mkv",
                target_path="/Movies/Example (2024) [tmdbid=1]/Example (2024).mkv",
                target_relative_path="Example (2024).mkv",
                target_kind="movie",
                media_type="movie",
                tmdb_movie_id="1",
            )
        ],
    )
