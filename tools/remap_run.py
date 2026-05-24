from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlist_bangumi.cli_runs import (
    DebugLlmClient,
    DebugRun,
    analysis_media_type,
    analysis_status_payload,
    create_debug_paths,
    decide_mappings,
    fetch_tmdb_details,
    print_timings,
    print_validation_summary,
    save_candidate_artifacts,
    save_tmdb_artifacts,
    save_tree_artifacts,
    validate_mapping,
    write_json,
    write_model,
    write_text,
)
from starlist_bangumi.clients import LlmClient, OpenListClient, TmdbClient
from starlist_bangumi.config import DEFAULT_CONFIG_PATH, ConfigManager
from starlist_bangumi.models import (
    AnalysisRequestItem,
    CoverageScope,
    LlmCandidateSelectionOutput,
    LlmIdentifyWorkOutput,
    LlmMappingDecision,
    LlmMappingFileInfo,
    LlmMappingOutput,
    ManualEpisodeMapping,
    ManualEpisodeMappingFile,
    TmdbCandidate,
)
from starlist_bangumi.services.plan_builder import (
    analysis_candidate_files,
    build_organized_target_tree,
    build_work_plan_report_tree,
    review_reason_from_work_plan,
)
from starlist_bangumi.services.scanner import SourceScanner


@dataclass(frozen=True)
class RemapSource:
    run_dir: Path
    item: AnalysisRequestItem
    identity: LlmIdentifyWorkOutput
    tv_candidates: list[TmdbCandidate]
    movie_candidates: list[TmdbCandidate]
    config_path: Path | None = None


async def main() -> None:
    args = parse_args()
    source = load_remap_source(Path(args.run_dir))
    config_path = Path(args.config) if args.config else source.config_path or DEFAULT_CONFIG_PATH
    config = ConfigManager(config_path).load()
    manual_mapping_path = resolve_manual_mapping_path(source.run_dir, args.mapping_file)
    manual_mapping = (
        load_manual_episode_mapping(manual_mapping_path) if manual_mapping_path else None
    )

    selection = manual_selection_from_args(args, manual_mapping=manual_mapping)
    paths = create_debug_paths(Path(args.output_root), f"{source.item.name}-remap")
    run = DebugRun(paths)
    openlist = OpenListClient(config.openlist)
    scanner = SourceScanner(openlist, config)
    tmdb = TmdbClient(config.tmdb)
    llm = DebugLlmClient(LlmClient(config.llm), run)

    write_remap_metadata(
        run,
        source,
        selection,
        config_path,
        manual_mapping_path=manual_mapping_path,
        manual_mapping=manual_mapping,
    )
    save_candidate_artifacts(run, source.tv_candidates, source.movie_candidates)
    print(f"Remap output: {paths.root}")
    print(f"Source run: {source.run_dir}")
    print(f"Source path: {source.item.path}")
    print(
        "Manual TMDB: "
        f"tv={selection.selected_tv_series_id or '-'}, "
        f"movies={selection.selected_movie_ids or []}"
    )

    snapshot = await run.timed(
        "01 source tree rescan",
        lambda: scanner.build_text_tree(
            source.item.path,
            max_depth=config.scan.tree_max_depth,
            max_nodes=config.scan.tree_max_nodes,
        ),
    )
    media_files = analysis_candidate_files(
        snapshot.files,
        video_extensions=set(config.scan.video_extensions),
        subtitle_extensions=set(config.scan.subtitle_extensions),
    )
    save_tree_artifacts(run, snapshot, media_files, source.item.path, config)
    print(f"Scanned {snapshot.nodes_scanned} node(s), {len(media_files)} candidate file(s)")

    tv_details, movie_details, season_details = await run.timed(
        "02 TMDB details",
        lambda: fetch_tmdb_details(tmdb, selection),
    )
    selected_path = paths.artifact_dir / "selected_tmdb_ids.json"
    write_model(selected_path, selection)
    run.add_file(selected_path, "selected TMDB ids")
    save_tmdb_artifacts(run, tv_details, movie_details, season_details)
    print(
        "TMDB details: "
        f"TV={'yes' if tv_details else 'no'}, "
        f"movies={len(movie_details)}, seasons={selection.season_numbers_to_fetch}"
    )

    if manual_mapping is not None:
        mapping_output = run.timed_sync(
            "03 apply manual episode mapping",
            lambda: apply_manual_episode_mapping(
                load_previous_mapping_output(source.run_dir),
                manual_mapping,
                season_details=season_details,
            ),
        )
    else:
        mapping_output = await run.timed(
            "03 decide_mappings LLM",
            lambda: decide_mappings(
                llm,
                source.item,
                source.identity,
                media_files,
                tv_details,
                movie_details,
                season_details,
                config,
            ),
        )
    mapping_output_path = paths.artifact_dir / "mapping_output.expanded.json"
    write_model(mapping_output_path, mapping_output)
    run.add_file(mapping_output_path, "expanded mapping output")

    work_plan = run.timed_sync(
        "04 validate mapping",
        lambda: validate_mapping(
            scanner=scanner,
            item=source.item,
            identity=source.identity,
            mapping_output=mapping_output,
            media_files=media_files,
            source_files=snapshot.files,
            tv_candidates=source.tv_candidates,
            movie_candidates=source.movie_candidates,
            tv_details=tv_details,
            movie_details=movie_details,
            season_details=season_details,
            warnings=[f"Manual TMDB remap from {source.run_dir}."],
        ),
    )
    analysis_result = analysis_result_from_remap(source, work_plan)
    save_remap_outputs(run, source, work_plan, analysis_result)
    timings_path = paths.root / "timings.json"
    write_json(timings_path, run.timings)
    run.add_file(timings_path, "stage timings", "timing")
    manifest_path = run.save_manifest()
    print_validation_summary(work_plan, review_reason_from_work_plan(work_plan))
    print_timings(run.timings)
    print(f"Manifest: {manifest_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a new run folder by manually remapping TMDB IDs."
    )
    parser.add_argument("run_dir", help="Existing run folder to remap")
    parser.add_argument(
        "--config",
        default="",
        help="Path to config JSON; defaults to original run config",
    )
    parser.add_argument("--tv-id", default="", help="Manual TMDB TV id")
    parser.add_argument(
        "--movie-id",
        action="append",
        default=[],
        help="Manual TMDB movie id; can be repeated",
    )
    parser.add_argument(
        "--mapping-file",
        default="",
        help=(
            "Manual episode mapping JSON. Defaults to "
            "artifacts/manual_episode_mappings.json under the source run when present."
        ),
    )
    parser.add_argument(
        "--output-root",
        default="data/runs",
        help="Directory used to store the new remap run folder",
    )
    return parser.parse_args()


def load_remap_source(run_dir: Path) -> RemapSource:
    if not run_dir.exists():
        raise SystemExit(f"Run folder does not exist: {run_dir}")
    artifact_dir = run_dir / "artifacts"
    run_input = load_json(required_file(artifact_dir / "run_input.json"))
    identity = LlmIdentifyWorkOutput.model_validate_json(
        required_file(artifact_dir / "identity.json").read_text(encoding="utf-8")
    )
    candidates = load_candidates(artifact_dir / "tmdb_candidates.full.json")
    source_path = str(run_input.get("source_path") or "")
    folder_name = str(run_input.get("folder_name") or Path(source_path).name)
    config_path_value = str(run_input.get("config_path") or "").strip()
    return RemapSource(
        run_dir=run_dir,
        item=AnalysisRequestItem(
            name=folder_name,
            path=source_path,
            prompt=load_optional_text(artifact_dir / "extra_prompt.txt"),
        ),
        identity=identity,
        tv_candidates=candidates[0],
        movie_candidates=candidates[1],
        config_path=Path(config_path_value) if config_path_value else None,
    )


def load_candidates(path: Path) -> tuple[list[TmdbCandidate], list[TmdbCandidate]]:
    if not path.exists():
        return [], []
    payload = load_json(path)
    return (
        [TmdbCandidate.model_validate(item) for item in payload.get("tv_candidates", [])],
        [TmdbCandidate.model_validate(item) for item in payload.get("movie_candidates", [])],
    )


def manual_selection_from_args(
    args: argparse.Namespace,
    *,
    manual_mapping: ManualEpisodeMappingFile | None = None,
) -> LlmCandidateSelectionOutput:
    movie_ids = [str(movie_id).strip() for movie_id in args.movie_id if str(movie_id).strip()]
    if manual_mapping is not None:
        movie_ids = movie_ids or [
            movie_id for movie_id in manual_mapping.movie_tmdb_ids if movie_id
        ]
    tv_id = str(args.tv_id or "").strip()
    if not tv_id and manual_mapping is not None:
        tv_id = manual_mapping.tv_tmdb_id.strip()
    if not tv_id and not movie_ids:
        raise SystemExit("Provide --tv-id, --movie-id, or both for manual remap.")
    return LlmCandidateSelectionOutput(
        selected_tv_series_id=tv_id,
        selected_movie_ids=movie_ids,
        season_numbers_to_fetch=[],
        needs_user_choice=False,
        reason="Manual TMDB remap from CLI arguments.",
    )


def write_remap_metadata(
    run: DebugRun,
    source: RemapSource,
    selection: LlmCandidateSelectionOutput,
    config_path: Path,
    manual_mapping_path: Path | None = None,
    manual_mapping: ManualEpisodeMappingFile | None = None,
) -> None:
    extra_prompt_path = run.paths.artifact_dir / "extra_prompt.txt"
    write_text(extra_prompt_path, source.item.prompt)
    run.add_file(extra_prompt_path, "extra prompt")
    identity_path = run.paths.artifact_dir / "identity.json"
    write_model(identity_path, source.identity)
    run.add_file(identity_path, "identified work")
    run_input_path = run.paths.artifact_dir / "run_input.json"
    write_json(
        run_input_path,
        {
            "folder_name": source.item.name,
            "source_path": source.item.path,
            "config_path": str(config_path.resolve()),
            "source_run_dir": str(source.run_dir),
            "manual_tv_id": selection.selected_tv_series_id,
            "manual_movie_ids": selection.selected_movie_ids,
            "selection_mode": "manual_remap",
        },
    )
    run.add_file(run_input_path, "run input")
    remap_request_path = run.paths.artifact_dir / "manual_remap_request.json"
    write_json(
        remap_request_path,
        {
            "source_run_dir": str(source.run_dir),
            "selected_tv_series_id": selection.selected_tv_series_id,
            "selected_movie_ids": selection.selected_movie_ids,
            "season_numbers_to_fetch": selection.season_numbers_to_fetch,
            "manual_episode_mapping_source_file": str(manual_mapping_path or ""),
            "manual_episode_mapping_file": (
                str(run.paths.artifact_dir / "manual_episode_mappings.json")
                if manual_mapping is not None
                else ""
            ),
        },
    )
    run.add_file(remap_request_path, "manual remap request")
    if manual_mapping is not None:
        manual_copy_path = run.paths.artifact_dir / "manual_episode_mappings.json"
        write_model(manual_copy_path, manual_mapping)
        run.add_file(manual_copy_path, "manual episode mappings")


def resolve_manual_mapping_path(run_dir: Path, value: str) -> Path | None:
    if value.strip():
        return Path(value)
    default_path = run_dir / "artifacts" / "manual_episode_mappings.json"
    return default_path if default_path.exists() else None


def load_manual_episode_mapping(path: Path) -> ManualEpisodeMappingFile:
    if not path.exists():
        raise SystemExit(f"Manual episode mapping file does not exist: {path}")
    return ManualEpisodeMappingFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_previous_mapping_output(run_dir: Path) -> LlmMappingOutput:
    path = required_file(run_dir / "artifacts" / "mapping_output.expanded.json")
    return LlmMappingOutput.model_validate_json(path.read_text(encoding="utf-8"))


def apply_manual_episode_mapping(
    previous: LlmMappingOutput,
    manual_mapping: ManualEpisodeMappingFile,
    *,
    season_details: dict[int, dict[str, Any]] | None = None,
) -> LlmMappingOutput:
    manual_keys = {
        mapping_key(item.folder_path, item.file_name) for item in manual_mapping.episode_mappings
    }
    decisions = [
        remove_file_infos_from_decision(decision, manual_keys)
        for decision in previous.decisions
    ]
    decisions = [decision for decision in decisions if decision.file_infos]
    decisions.extend(manual_decisions(manual_mapping.episode_mappings))
    ignored_files = [
        ignored
        for ignored in previous.ignored_files
        if mapping_key(ignored.folder_path, ignored.file_name) not in manual_keys
    ]
    notes = [
        *previous.notes,
        *manual_mapping.notes,
        f"Applied {len(manual_mapping.episode_mappings)} manual episode mapping(s).",
    ]
    return LlmMappingOutput(
        coverage_scope=manual_coverage_scope(season_details or {}, previous.coverage_scope),
        decisions=decisions,
        ignored_files=ignored_files,
        notes=notes,
    )


def manual_coverage_scope(
    season_details: dict[int, dict[str, Any]],
    fallback: list[CoverageScope],
) -> list[CoverageScope]:
    if not season_details:
        return fallback
    return [
        CoverageScope(
            type="tv_season",
            season_number=season_number,
            complete=True,
            note="Manual review mapping; completeness is verified against TMDB episodes.",
        )
        for season_number in sorted(season_details)
    ]


def remove_file_infos_from_decision(
    decision: LlmMappingDecision,
    manual_keys: set[tuple[str, str]],
) -> LlmMappingDecision:
    file_infos = [
        file_info
        for file_info in decision.file_infos
        if mapping_key(decision.folder_path, file_info.file_name) not in manual_keys
    ]
    return LlmMappingDecision(
        folder_path=decision.folder_path,
        target_kind=decision.target_kind,
        season_number=decision.season_number,
        tmdb_movie_id=decision.tmdb_movie_id,
        confidence=decision.confidence,
        reason=decision.reason,
        file_infos=file_infos,
    )


def manual_decisions(mappings: list[ManualEpisodeMapping]) -> list[LlmMappingDecision]:
    return [
        LlmMappingDecision(
            folder_path=mapping.folder_path,
            target_kind="tv_episode",
            season_number=mapping.season_number,
            confidence=1,
            reason=mapping.reason or "Manual episode mapping from review file.",
            file_infos=[
                LlmMappingFileInfo(
                    file_name=mapping.file_name,
                    episode_number=mapping.episode_number,
                    confidence=1,
                    reason=mapping.reason or "Manual episode mapping from review file.",
                )
            ],
        )
        for mapping in mappings
    ]


def mapping_key(folder_path: str, file_name: str) -> tuple[str, str]:
    folder = str(folder_path or "").strip().strip("/")
    return folder.casefold(), str(file_name).strip().casefold()


def analysis_result_from_remap(source: RemapSource, work_plan: Any) -> Any:
    from starlist_bangumi.services.plan_builder import analysis_result_from_work_plan

    return analysis_result_from_work_plan(
        item=source.item,
        work_plan=work_plan,
        media_type=analysis_media_type(work_plan),
        confidence=0.92 if work_plan.validated_mappings else 0.55,
    )


def save_remap_outputs(
    run: DebugRun,
    source: RemapSource,
    work_plan: Any,
    analysis_result: Any,
) -> None:
    work_plan_path = run.paths.artifact_dir / "work_plan.json"
    write_model(work_plan_path, work_plan)
    run.add_file(work_plan_path, "validated work plan")
    analysis_result_path = run.paths.artifact_dir / "analysis_result.json"
    write_model(analysis_result_path, analysis_result)
    run.add_file(analysis_result_path, "analysis result")
    result_tree_path = run.paths.artifact_dir / "dry_run_result_tree.txt"
    write_text(result_tree_path, build_work_plan_report_tree(work_plan))
    run.add_file(result_tree_path, "dry-run result tree")
    organized_tree_path = run.paths.artifact_dir / "organized_target_tree.txt"
    write_text(organized_tree_path, build_organized_target_tree(work_plan))
    run.add_file(organized_tree_path, "organized target tree")
    status_payload = analysis_status_payload(work_plan, review_reason_from_work_plan(work_plan))
    status_payload["source_run_dir"] = str(source.run_dir)
    validation_summary_path = run.paths.artifact_dir / "validation_summary.json"
    write_json(validation_summary_path, status_payload)
    run.add_file(validation_summary_path, "validation summary")
    status_path = run.paths.root / "analysis_status.json"
    write_json(status_path, status_payload)
    run.add_file(status_path, "analysis status")


def required_file(path: Path) -> Path:
    if not path.exists():
        raise SystemExit(f"Required remap artifact is missing: {path}")
    return path


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"Expected JSON object: {path}")
    return payload


def load_optional_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


if __name__ == "__main__":
    asyncio.run(main())
