from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from starlist_bangumi.cli_runs import create_debug_paths
from starlist_bangumi.models import (
    CoverageScope,
    LibraryTarget,
    LlmMappingOutput,
    ManualEpisodeMappingFile,
    SelectedMovie,
    WorkPlan,
)
from tools.remap_run import (
    DebugRun,
    RemapSource,
    apply_manual_episode_mapping,
    load_remap_source,
    manual_selection_from_args,
    resolve_manual_mapping_path,
    save_remap_outputs,
)


def test_load_remap_source_reuses_existing_run_artifacts(tmp_path: Path) -> None:
    run_dir = write_source_run(tmp_path)

    source = load_remap_source(run_dir)

    assert source.item.name == "[Group] Example"
    assert source.item.path == "/Inbox/[Group] Example"
    assert source.item.prompt == "use the 2024 TV entry"
    assert source.identity.canonical_title == "Example"
    assert source.tv_candidates[0].tmdb_id == "100"
    assert source.movie_candidates[0].tmdb_id == "200"
    assert source.config_path == Path("data/config.json")


def test_load_remap_source_requires_identity_artifact(tmp_path: Path) -> None:
    run_dir = write_source_run(tmp_path)
    (run_dir / "artifacts" / "identity.json").unlink()

    with pytest.raises(SystemExit, match="identity.json"):
        load_remap_source(run_dir)


def test_manual_selection_from_args_requires_at_least_one_tmdb_id() -> None:
    with pytest.raises(SystemExit, match="Provide --tv-id"):
        manual_selection_from_args(
            argparse.Namespace(tv_id="", movie_id=[])
        )


def test_manual_selection_from_args_accepts_tv_and_movies() -> None:
    selection = manual_selection_from_args(
        argparse.Namespace(tv_id="100", movie_id=["200", " 201 "])
    )

    assert selection.selected_tv_series_id == "100"
    assert selection.selected_movie_ids == ["200", "201"]
    assert selection.season_numbers_to_fetch == []
    assert selection.needs_user_choice is False


def test_manual_selection_from_args_uses_mapping_file_tmdb_ids() -> None:
    selection = manual_selection_from_args(
        argparse.Namespace(tv_id="", movie_id=[]),
        manual_mapping=ManualEpisodeMappingFile(
            tv_tmdb_id="100",
            movie_tmdb_ids=["200"],
            episode_mappings=[],
        ),
    )

    assert selection.selected_tv_series_id == "100"
    assert selection.selected_movie_ids == ["200"]


def test_resolve_manual_mapping_path_defaults_to_run_artifact(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    mapping_path = run_dir / "artifacts" / "manual_episode_mappings.json"
    mapping_path.parent.mkdir(parents=True)
    mapping_path.write_text("{}", encoding="utf-8")

    assert resolve_manual_mapping_path(run_dir, "") == mapping_path
    assert resolve_manual_mapping_path(run_dir, str(tmp_path / "custom.json")) == (
        tmp_path / "custom.json"
    )


def test_apply_manual_episode_mapping_overrides_previous_decision_and_ignored_file() -> None:
    output = apply_manual_episode_mapping(
        LlmMappingOutput.model_validate(
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {
                        "type": "tv_season",
                        "season_number": 1,
                        "complete": False,
                        "note": "LLM was unsure",
                    }
                ],
                "decisions": [
                    {
                        "folder_path": "Season 1",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "confidence": 0.7,
                        "reason": "old",
                        "file_infos": [
                            {
                                "file_name": "Example 01.mkv",
                                "episode_number": 1,
                                "confidence": 0.7,
                                "reason": "old",
                            }
                        ],
                    }
                ],
                "ignored_files": [
                    {
                        "folder_path": "Season 1",
                        "file_name": "Example 02.mkv",
                        "reason": "missing before review",
                    }
                ],
                "notes": ["old note"],
            }
        ),
        ManualEpisodeMappingFile.model_validate(
            {
                "schema_version": "1.0",
                "tv_tmdb_id": "100",
                "episode_mappings": [
                    {
                        "folder_path": "Season 1",
                        "file_name": "Example 02.mkv",
                        "season_number": 1,
                        "episode_number": 2,
                        "reason": "human matched episode 2",
                    },
                    {
                        "folder_path": "Season 1",
                        "file_name": "Example 01.mkv",
                        "season_number": 1,
                        "episode_number": 1,
                    },
                ],
                "notes": ["review note"],
            }
        ),
        season_details={1: {"episodes": [{"episode_number": 1}, {"episode_number": 2}]}},
    )

    assert output.ignored_files == []
    assert output.coverage_scope[0].complete is True
    assert len(output.decisions) == 2
    assert output.decisions[0].file_infos[0].file_name == "Example 02.mkv"
    assert output.decisions[0].file_infos[0].episode_number == 2
    assert output.decisions[1].file_infos[0].file_name == "Example 01.mkv"
    assert "Applied 2 manual episode mapping(s)." in output.notes


def test_save_remap_outputs_marks_source_run_in_status(tmp_path: Path) -> None:
    paths = create_debug_paths(tmp_path, "Example-remap")
    run = DebugRun(paths)
    source = RemapSource(
        run_dir=tmp_path / "old-run",
        item=load_remap_source(write_source_run(tmp_path)).item,
        identity=load_remap_source(write_source_run(tmp_path)).identity,
        tv_candidates=[],
        movie_candidates=[],
    )
    work_plan = WorkPlan(
        work_title="Example",
        source_name="[Group] Example",
        source_path="/Inbox/[Group] Example",
        archive_target_path="/Archive/Example",
        coverage_scope=[CoverageScope(type="movie", tmdb_movie_id="200")],
        selected_movies=[SelectedMovie(tmdb_id="200", title="Example", year="2024")],
        library_targets=[
            LibraryTarget(
                media_type="movie",
                target_path="/Movies/Example (2024) [tmdbid=200]",
                title="Example",
                year="2024",
                tmdb_id="200",
            )
        ],
    )

    save_remap_outputs(run, source, work_plan, analysis_result=remap_analysis(work_plan))

    status = json.loads((paths.root / "analysis_status.json").read_text(encoding="utf-8"))
    assert status["source_run_dir"] == str(source.run_dir)
    assert (paths.artifact_dir / "work_plan.json").exists()
    assert (paths.artifact_dir / "analysis_result.json").exists()


def test_write_remap_metadata_copies_manual_mapping_file(tmp_path: Path) -> None:
    from tools.remap_run import write_remap_metadata

    paths = create_debug_paths(tmp_path, "Example-remap")
    run = DebugRun(paths)
    source = load_remap_source(write_source_run(tmp_path))
    mapping = ManualEpisodeMappingFile(
        tv_tmdb_id="100",
        episode_mappings=[
            {
                "folder_path": "",
                "file_name": "Example 01.mkv",
                "season_number": 1,
                "episode_number": 1,
            }
        ],
    )

    write_remap_metadata(
        run,
        source,
        manual_selection_from_args(argparse.Namespace(tv_id="100", movie_id=[])),
        Path("data/config.json"),
        manual_mapping_path=tmp_path / "source.json",
        manual_mapping=mapping,
    )

    request = json.loads(
        (paths.artifact_dir / "manual_remap_request.json").read_text(encoding="utf-8")
    )
    copied = ManualEpisodeMappingFile.model_validate_json(
        (paths.artifact_dir / "manual_episode_mappings.json").read_text(encoding="utf-8")
    )
    assert request["manual_episode_mapping_source_file"] == str(tmp_path / "source.json")
    assert request["manual_episode_mapping_file"].endswith("manual_episode_mappings.json")
    assert copied.episode_mappings[0].file_name == "Example 01.mkv"


def write_source_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "source-run"
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "run_input.json").write_text(
        json.dumps(
            {
                "folder_name": "[Group] Example",
                "source_path": "/Inbox/[Group] Example",
                "config_path": "data/config.json",
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "extra_prompt.txt").write_text("use the 2024 TV entry", encoding="utf-8")
    (artifacts / "identity.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "fixture",
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "tmdb_candidates.full.json").write_text(
        json.dumps(
            {
                "tv_candidates": [
                    {
                        "media_type": "tv",
                        "tmdb_id": "100",
                        "title": "Example TV",
                    }
                ],
                "movie_candidates": [
                    {
                        "media_type": "movie",
                        "tmdb_id": "200",
                        "title": "Example Movie",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def remap_analysis(work_plan: WorkPlan):
    from starlist_bangumi.models import AnalysisRequestItem
    from starlist_bangumi.services.plan_builder import analysis_result_from_work_plan

    return analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name=work_plan.source_name,
            path=work_plan.source_path,
            prompt="",
        ),
        work_plan=work_plan,
        media_type="movie",
        confidence=0.55,
    )
