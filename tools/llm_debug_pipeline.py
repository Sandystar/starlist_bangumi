from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from starlist_bangumi.clients import LlmClient, OpenListClient, TmdbClient
from starlist_bangumi.config import (
    DEFAULT_CONFIG_PATH,
    ConfigManager,
    sanitize_openlist_segment,
)
from starlist_bangumi.exceptions import ExternalServiceError
from starlist_bangumi.models import (
    AnalysisRequestItem,
    LlmCandidateSelectionOutput,
    LlmIdentifyWorkOutput,
    LlmMappingOutput,
)
from starlist_bangumi.pathing import join_openlist_path
from starlist_bangumi.services.plan_builder import (
    analysis_result_from_work_plan,
    analysis_candidate_files,
    build_organized_target_tree,
    build_work_plan_report_tree,
    candidate_file_json,
    candidate_json,
    compact_movie_details,
    compact_season_details,
    compact_tv_details,
    dedupe_candidates,
    load_prompt,
    message_char_count,
    prompt_json,
    prune_tv_special_movie_selection,
    review_reason_from_work_plan,
    tmdb_season_numbers,
)
from starlist_bangumi.services.scanner import SourceScanner, TreeFile, TreeSnapshot


@dataclass(frozen=True)
class DebugPaths:
    root: Path
    request_dir: Path
    response_dir: Path
    artifact_dir: Path


class DebugRun:
    """Tracks stage timing and generated files for a debug run."""

    def __init__(self, paths: DebugPaths) -> None:
        self.paths = paths
        self.timings: list[dict[str, Any]] = []
        self.files: list[dict[str, str]] = []

    async def timed(self, name: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        print(f"[stage] {name} ...")
        started_at = time.perf_counter()
        result = await operation()
        elapsed = round(time.perf_counter() - started_at, 3)
        self.timings.append({"stage": name, "elapsed_seconds": elapsed})
        print(f"[stage] {name} done in {elapsed:.3f}s")
        return result

    def timed_sync(self, name: str, operation: Callable[[], Any]) -> Any:
        print(f"[stage] {name} ...")
        started_at = time.perf_counter()
        result = operation()
        elapsed = round(time.perf_counter() - started_at, 3)
        self.timings.append({"stage": name, "elapsed_seconds": elapsed})
        print(f"[stage] {name} done in {elapsed:.3f}s")
        return result

    def add_file(self, path: Path, label: str, kind: str = "artifact") -> None:
        self.files.append(
            {
                "label": label,
                "kind": kind,
                "path": str(path),
            }
        )
        print(f"[file] {label}: {path}")

    def save_manifest(self) -> Path:
        manifest_path = self.paths.root / "manifest.json"
        write_json(
            manifest_path,
            {
                "run_dir": str(self.paths.root),
                "timings": self.timings,
                "files": self.files,
            },
        )
        return manifest_path


class DebugLlmClient:
    """LLM wrapper that saves exact request and response payloads."""

    def __init__(self, client: LlmClient, run: DebugRun) -> None:
        self._client = client
        self._run = run

    async def chat_json(self, stage: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        request_path = self._run.paths.request_dir / f"{stage}.json"
        write_json(
            request_path,
            {
                "stage": stage,
                "input_chars": message_char_count(messages),
                "messages": messages,
            },
        )
        self._run.add_file(request_path, f"{stage} request", "request")
        save_request_message_contents(self._run, stage, messages)
        started_at = time.perf_counter()
        try:
            result = await self._client.chat_json(messages)
        except ExternalServiceError as exc:
            elapsed = round(time.perf_counter() - started_at, 1)
            error_path = self._run.paths.response_dir / f"{stage}.error.json"
            write_json(
                error_path,
                {
                    "stage": stage,
                    "elapsed_seconds": elapsed,
                    "error": str(exc),
                    "details": exc.details,
                },
            )
            self._run.add_file(error_path, f"{stage} error response", "response")
            raise
        elapsed = round(time.perf_counter() - started_at, 1)
        response_path = self._run.paths.response_dir / f"{stage}.json"
        write_json(
            response_path,
            {"stage": stage, "elapsed_seconds": elapsed, "result": result},
        )
        self._run.add_file(response_path, f"{stage} response", "response")
        return result


async def main() -> None:
    args = parse_args()
    config = ConfigManager(Path(args.config)).load()
    paths = create_debug_paths(Path(args.output_root), args.folder_name)
    run = DebugRun(paths)
    item = AnalysisRequestItem(
        name=args.folder_name,
        path=join_openlist_path(config.media_library.source_path, args.folder_name),
        prompt=args.prompt,
    )

    openlist = OpenListClient(config.openlist)
    llm = DebugLlmClient(LlmClient(config.llm), run)
    tmdb = TmdbClient(config.tmdb)
    scanner = SourceScanner(openlist, config)

    extra_prompt_path = paths.artifact_dir / "extra_prompt.txt"
    write_text(extra_prompt_path, item.prompt)
    run.add_file(extra_prompt_path, "extra prompt")
    run_input_path = paths.artifact_dir / "run_input.json"
    write_json(
        run_input_path,
        {
            "folder_name": item.name,
            "source_path": item.path,
            "config_path": str(Path(args.config).resolve()),
            "tree_max_depth": config.scan.tree_max_depth,
            "tree_max_nodes": config.scan.tree_max_nodes,
            "video_extensions": config.scan.video_extensions,
            "subtitle_extensions": config.scan.subtitle_extensions,
            "manual_tv_id": args.tv_id,
            "manual_movie_ids": args.movie_id,
            "auto_select_first_tv": args.auto_select_first_tv,
            "selection_mode": selection_mode(args),
        },
    )
    run.add_file(run_input_path, "run input")

    print(f"Debug output: {paths.root}")
    print(f"Source path: {item.path}")

    identity = await run.timed("01 identify_work LLM", lambda: identify_work(llm, item))
    identity_path = paths.artifact_dir / "identity.json"
    write_model(identity_path, identity)
    run.add_file(identity_path, "identified work")
    print(f"LLM identity: {identity.canonical_title}")

    tv_candidates, movie_candidates = await run.timed(
        "02 TMDB search", lambda: search_tmdb(tmdb, identity)
    )
    save_candidate_artifacts(run, tv_candidates, movie_candidates)
    print(f"TMDB candidates: {len(tv_candidates)} TV, {len(movie_candidates)} movie")

    snapshot = await run.timed(
        "03 source tree scan",
        lambda: scanner.build_text_tree(
            item.path,
            max_depth=config.scan.tree_max_depth,
            max_nodes=config.scan.tree_max_nodes,
        ),
    )
    media_files = analysis_candidate_files(
        snapshot.files,
        video_extensions=set(config.scan.video_extensions),
        subtitle_extensions=set(config.scan.subtitle_extensions),
    )
    save_tree_artifacts(run, snapshot, media_files, item.path, config)
    print(f"Scanned {snapshot.nodes_scanned} node(s), {len(media_files)} candidate file(s)")

    selection = await run.timed(
        "04 select_candidates",
        lambda: select_candidates(
            llm=llm,
            item=item,
            identity=identity,
            tv_candidates=tv_candidates,
            movie_candidates=movie_candidates,
            args=args,
        ),
    )
    selected_path = paths.artifact_dir / "selected_tmdb_ids.json"
    write_model(selected_path, selection)
    run.add_file(selected_path, "selected TMDB ids")
    print(
        "Selected TMDB: "
        f"tv={selection.selected_tv_series_id or '-'}, "
        f"movies={selection.selected_movie_ids or []}"
    )

    tv_details, movie_details, season_details = await run.timed(
        "05 TMDB details", lambda: fetch_tmdb_details(tmdb, selection)
    )
    write_model(selected_path, selection)
    save_tmdb_artifacts(run, tv_details, movie_details, season_details)
    print(
        "TMDB details: "
        f"TV={'yes' if tv_details else 'no'}, "
        f"movies={len(movie_details)}, seasons={selection.season_numbers_to_fetch}"
    )

    mapping_output = await run.timed(
        "06 decide_mappings LLM",
        lambda: decide_mappings(
            llm,
            item,
            identity,
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
        "07 validate mapping",
        lambda: validate_mapping(
            scanner=scanner,
            item=item,
            identity=identity,
            mapping_output=mapping_output,
            media_files=media_files,
            source_files=snapshot.files,
            tv_candidates=tv_candidates,
            movie_candidates=movie_candidates,
            tv_details=tv_details,
            movie_details=movie_details,
            season_details=season_details,
        ),
    )
    review_reason = review_reason_from_work_plan(work_plan)
    analysis_result = analysis_result_from_work_plan(
        item=item,
        work_plan=work_plan,
        media_type=analysis_media_type(work_plan),
        confidence=0.92 if work_plan.validated_mappings else 0.55,
    )
    work_plan_path = paths.artifact_dir / "work_plan.json"
    write_model(work_plan_path, work_plan)
    run.add_file(work_plan_path, "validated work plan")
    analysis_result_path = paths.artifact_dir / "analysis_result.json"
    write_model(analysis_result_path, analysis_result)
    run.add_file(analysis_result_path, "analysis result")
    result_tree_path = paths.artifact_dir / "dry_run_result_tree.txt"
    write_text(result_tree_path, build_work_plan_report_tree(work_plan))
    run.add_file(result_tree_path, "dry-run result tree")
    organized_tree_path = paths.artifact_dir / "organized_target_tree.txt"
    write_text(organized_tree_path, build_organized_target_tree(work_plan))
    run.add_file(organized_tree_path, "organized target tree")
    validation_summary_path = paths.artifact_dir / "validation_summary.json"
    status_payload = analysis_status_payload(work_plan, review_reason)
    write_json(
        validation_summary_path,
        status_payload,
    )
    run.add_file(validation_summary_path, "validation summary")
    status_path = paths.root / "analysis_status.json"
    write_json(status_path, status_payload)
    run.add_file(status_path, "analysis status")
    timings_path = paths.root / "timings.json"
    write_json(timings_path, run.timings)
    run.add_file(timings_path, "stage timings", "timing")
    manifest_path = run.save_manifest()
    print_validation_summary(work_plan, review_reason)
    print_timings(run.timings)
    print(f"Manifest: {manifest_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an LLM/TMDB debug analysis.")
    parser.add_argument(
        "folder_name",
        help="First-level folder name under media_library.source_path",
    )
    parser.add_argument("--prompt", default="", help="Extra hint passed to both LLM calls")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON")
    parser.add_argument("--tv-id", default="", help="Manual TMDB TV id to use for mapping")
    parser.add_argument(
        "--movie-id",
        action="append",
        default=[],
        help="Manual TMDB movie id to use for mapping; can be repeated",
    )
    parser.add_argument(
        "--seasons",
        default="",
        help="Comma-separated season numbers to fetch; defaults to inferred season hints",
    )
    parser.add_argument(
        "--auto-select-first-tv",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Debug shortcut: use the first TV candidate when --tv-id is omitted",
    )
    parser.add_argument(
        "--output-root",
        default="data/runs",
        help="Directory used to store request, response, and artifact files",
    )
    return parser.parse_args()


def create_debug_paths(output_root: Path, folder_name: str) -> DebugPaths:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_name = sanitize_openlist_segment(folder_name)[:80] or "folder"
    root = output_root / f"{timestamp}-{safe_slug(safe_name)}"
    paths = DebugPaths(
        root=root,
        request_dir=root / "requests",
        response_dir=root / "responses",
        artifact_dir=root / "artifacts",
    )
    for path in [paths.request_dir, paths.response_dir, paths.artifact_dir]:
        path.mkdir(parents=True, exist_ok=True)
    return paths


def save_tree_artifacts(
    run: DebugRun,
    snapshot: TreeSnapshot,
    candidate_files: list[TreeFile],
    source_root: str,
    config: Any,
) -> None:
    source_tree_path = run.paths.artifact_dir / "source_tree.txt"
    write_text(source_tree_path, snapshot.text)
    run.add_file(source_tree_path, "source tree")
    source_tree_meta_path = run.paths.artifact_dir / "source_tree_meta.json"
    write_json(
        source_tree_meta_path,
        {
            "nodes_scanned": snapshot.nodes_scanned,
            "truncated": snapshot.truncated,
            "file_count": len(snapshot.files),
            "candidate_file_count": len(candidate_files),
        },
    )
    run.add_file(source_tree_meta_path, "source tree metadata")
    candidate_prompt_path = run.paths.artifact_dir / "candidate_files.prompt.json"
    write_text(
        candidate_prompt_path,
        candidate_file_json(
            candidate_files,
            video_extensions=set(config.scan.video_extensions),
            subtitle_extensions=set(config.scan.subtitle_extensions),
            source_root=source_root,
        ),
    )
    run.add_file(candidate_prompt_path, "candidate files prompt input")
    candidate_full_path = run.paths.artifact_dir / "candidate_files.full.json"
    write_json(
        candidate_full_path,
        [
            {
                "relative_path": file.relative_path,
                "source_path": file.path,
                "name": file.name,
                "size": file.size,
            }
            for file in candidate_files
        ],
    )
    run.add_file(candidate_full_path, "candidate files full input")


async def identify_work(
    llm: DebugLlmClient,
    item: AnalysisRequestItem,
) -> LlmIdentifyWorkOutput:
    messages = [
        {"role": "system", "content": load_prompt("identify_work")},
        {
            "role": "user",
            "content": (
                f"Folder name: {item.name}\n"
                f"Extra hint: {item.prompt or '(none)'}"
            ),
        },
    ]
    result = await llm.chat_json("01-identify_work", messages)
    return LlmIdentifyWorkOutput.model_validate(result)


async def search_tmdb(
    tmdb: TmdbClient, identity: LlmIdentifyWorkOutput
) -> tuple[list[Any], list[Any]]:
    queries = unique_non_empty([identity.canonical_title, *identity.aliases])
    tv_candidates = []
    movie_candidates = []
    for query in queries:
        tv_candidates.extend(await tmdb.search_candidates(query, media_type="tv", limit=5))
        movie_candidates.extend(await tmdb.search_candidates(query, media_type="movie", limit=5))
    return dedupe_candidates(tv_candidates), dedupe_candidates(movie_candidates)


def save_candidate_artifacts(
    run: DebugRun,
    tv_candidates: list[Any],
    movie_candidates: list[Any],
) -> None:
    payload = {
        "tv_candidates": [candidate.model_dump(mode="json") for candidate in tv_candidates],
        "movie_candidates": [
            candidate.model_dump(mode="json") for candidate in movie_candidates
        ],
    }
    candidates_full_path = run.paths.artifact_dir / "tmdb_candidates.full.json"
    write_json(candidates_full_path, payload)
    run.add_file(candidates_full_path, "TMDB candidates full input")
    tv_prompt_path = run.paths.artifact_dir / "tmdb_tv_candidates.prompt.json"
    write_text(tv_prompt_path, candidate_json(tv_candidates))
    run.add_file(tv_prompt_path, "TMDB TV candidates prompt input")
    movie_prompt_path = run.paths.artifact_dir / "tmdb_movie_candidates.prompt.json"
    write_text(
        movie_prompt_path,
        candidate_json(movie_candidates),
    )
    run.add_file(movie_prompt_path, "TMDB movie candidates prompt input")


async def select_candidates(
    *,
    llm: DebugLlmClient,
    item: AnalysisRequestItem,
    identity: LlmIdentifyWorkOutput,
    tv_candidates: list[Any],
    movie_candidates: list[Any],
    args: argparse.Namespace,
) -> LlmCandidateSelectionOutput:
    manual_selection = build_manual_selection(args=args, tv_candidates=tv_candidates)
    if manual_selection is not None:
        return manual_selection

    if not tv_candidates and not movie_candidates:
        return LlmCandidateSelectionOutput(
            needs_user_choice=True,
            reason="No TMDB candidates were found.",
        )

    messages = [
        {"role": "system", "content": load_prompt("select_candidates")},
        {
            "role": "user",
            "content": (
                f"Folder name: {item.name}\n"
                f"Extra hint: {item.prompt or '(none)'}\n"
                f"Identified work:\n{identity.model_dump_json()}\n"
                f"TV candidates:\n{candidate_json(tv_candidates)}\n"
                f"Movie candidates:\n{candidate_json(movie_candidates)}"
            ),
        },
    ]
    result = await llm.chat_json("02-select_candidates", messages)
    selection = LlmCandidateSelectionOutput.model_validate(result)
    validate_candidate_selection(selection, tv_candidates, movie_candidates)
    return selection


def build_manual_selection(
    *,
    args: argparse.Namespace,
    tv_candidates: list[Any],
) -> LlmCandidateSelectionOutput | None:
    tv_id = args.tv_id
    if not tv_id and args.auto_select_first_tv and tv_candidates:
        tv_id = str(tv_candidates[0].tmdb_id)
    if not tv_id and not args.movie_id:
        return None
    season_numbers = parse_seasons(args.seasons)
    return LlmCandidateSelectionOutput(
        selected_tv_series_id=tv_id,
        selected_movie_ids=[str(movie_id) for movie_id in args.movie_id if str(movie_id)],
        season_numbers_to_fetch=season_numbers,
        needs_user_choice=False,
        reason="Manual TMDB id selection from CLI arguments.",
    )


def validate_candidate_selection(
    selection: LlmCandidateSelectionOutput,
    tv_candidates: list[Any],
    movie_candidates: list[Any],
) -> None:
    valid_tv_ids = {candidate.tmdb_id for candidate in tv_candidates}
    valid_movie_ids = {candidate.tmdb_id for candidate in movie_candidates}
    if selection.selected_tv_series_id and selection.selected_tv_series_id not in valid_tv_ids:
        selection.needs_user_choice = True
        selection.reason = "LLM selected a TV candidate that is not in the candidate list."
    invalid_movies = [
        movie_id for movie_id in selection.selected_movie_ids if movie_id not in valid_movie_ids
    ]
    if invalid_movies:
        selection.needs_user_choice = True
        selection.reason = "LLM selected a movie candidate that is not in the candidate list."
    removed_special_movies = prune_tv_special_movie_selection(selection, movie_candidates)
    if removed_special_movies:
        suffix = (
            "Removed OVA/OAD/SP-like movie candidates because a TV series was selected; "
            "these should be matched through TMDB Season 0."
        )
        selection.reason = f"{selection.reason} {suffix}".strip()


def selection_mode(args: argparse.Namespace) -> str:
    if args.tv_id or args.movie_id:
        return "manual"
    if args.auto_select_first_tv:
        return "auto_select_first_tv"
    return "llm"


def parse_seasons(value: str) -> list[int]:
    if not value.strip():
        return []
    seasons: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        seasons.append(int(part))
    return sorted(set(seasons))


async def fetch_tmdb_details(
    tmdb: TmdbClient, selection: LlmCandidateSelectionOutput
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
    tv_details = None
    season_details: dict[int, dict[str, Any]] = {}
    if selection.selected_tv_series_id:
        tv_details = await tmdb.tv_details(selection.selected_tv_series_id)
        season_numbers = tmdb_season_numbers(tv_details)
        selection.season_numbers_to_fetch = season_numbers
        for season_number in season_numbers:
            details = await tmdb.season_details(selection.selected_tv_series_id, season_number)
            if details:
                season_details[season_number] = details
    movie_details: dict[str, dict[str, Any]] = {}
    for movie_id in selection.selected_movie_ids:
        details = await tmdb.movie_details(movie_id)
        if details:
            movie_details[movie_id] = details
    return tv_details, movie_details, season_details


def save_tmdb_artifacts(
    run: DebugRun,
    tv_details: dict[str, Any] | None,
    movie_details: dict[str, dict[str, Any]],
    season_details: dict[int, dict[str, Any]],
) -> None:
    tv_raw_path = run.paths.artifact_dir / "tmdb_tv_details.raw.json"
    write_json(tv_raw_path, tv_details or {})
    run.add_file(tv_raw_path, "TMDB TV details raw input")
    tv_prompt_path = run.paths.artifact_dir / "tmdb_tv_details.prompt.json"
    write_json(tv_prompt_path, compact_tv_details(tv_details))
    run.add_file(tv_prompt_path, "TMDB TV details prompt input")
    movie_raw_path = run.paths.artifact_dir / "tmdb_movie_details.raw.json"
    write_json(movie_raw_path, movie_details)
    run.add_file(movie_raw_path, "TMDB movie details raw input")
    movie_prompt_path = run.paths.artifact_dir / "tmdb_movie_details.prompt.json"
    write_json(
        movie_prompt_path,
        compact_movie_details(movie_details),
    )
    run.add_file(movie_prompt_path, "TMDB movie details prompt input")
    season_raw_path = run.paths.artifact_dir / "tmdb_season_details.raw.json"
    write_json(
        season_raw_path,
        {str(key): value for key, value in sorted(season_details.items())},
    )
    run.add_file(season_raw_path, "TMDB season details raw input")
    season_prompt_path = run.paths.artifact_dir / "tmdb_season_details.prompt.json"
    write_json(
        season_prompt_path,
        compact_season_details(season_details),
    )
    run.add_file(season_prompt_path, "TMDB season details prompt input")


async def decide_mappings(
    llm: DebugLlmClient,
    item: AnalysisRequestItem,
    identity: LlmIdentifyWorkOutput,
    media_files: list[TreeFile],
    tv_details: dict[str, Any] | None,
    movie_details: dict[str, dict[str, Any]],
    season_details: dict[int, dict[str, Any]],
    config: Any,
) -> LlmMappingOutput:
    candidate_files_json = candidate_file_json(
        media_files,
        video_extensions=set(config.scan.video_extensions),
        subtitle_extensions=set(config.scan.subtitle_extensions),
        source_root=item.path,
    )
    messages = [
        {"role": "system", "content": load_prompt("decide_mappings")},
        {
            "role": "user",
            "content": (
                f"Folder name: {item.name}\n"
                f"Extra hint: {item.prompt or '(none)'}\n"
                f"Identified work:\n{identity.model_dump_json()}\n"
                f"TV details:\n{prompt_json(compact_tv_details(tv_details))}\n"
                f"Season details:\n{prompt_json(compact_season_details(season_details))}\n"
                f"Movie details:\n{prompt_json(compact_movie_details(movie_details))}\n"
                f"Candidate files:\n{candidate_files_json}"
            ),
        },
    ]
    result = await llm.chat_json("03-decide_mappings", messages)
    return LlmMappingOutput.model_validate(result)


def validate_mapping(
    *,
    scanner: SourceScanner,
    item: AnalysisRequestItem,
    identity: LlmIdentifyWorkOutput,
    mapping_output: LlmMappingOutput,
    media_files: list[TreeFile],
    source_files: list[TreeFile],
    tv_candidates: list[Any],
    movie_candidates: list[Any],
    tv_details: dict[str, Any] | None,
    movie_details: dict[str, dict[str, Any]],
    season_details: dict[int, dict[str, Any]],
    warnings: list[str] | None = None,
) -> Any:
    plan_builder = scanner_based_plan_builder(scanner)
    return plan_builder._validate_mapping_output(
        item=item,
        identity=identity,
        mapping_output=mapping_output,
        media_files=media_files,
        source_files=source_files,
        tv_candidates=tv_candidates,
        movie_candidates=movie_candidates,
        tv_details=tv_details,
        movie_details=movie_details,
        season_details=season_details,
        warnings=warnings or [],
    )


def scanner_based_plan_builder(scanner: SourceScanner) -> Any:
    from starlist_bangumi.services.plan_builder import LlmPlanBuilder

    return LlmPlanBuilder(
        scanner,
        LlmClient(scanner._config.llm),
        TmdbClient(scanner._config.tmdb),
        scanner._config,
    )


def print_validation_summary(work_plan: Any, review_reason: str) -> None:
    print("Validation:")
    print(f"  status: {'needs_review' if review_reason else 'succeeded'}")
    print(f"  validated: {len(work_plan.validated_mappings)}")
    print(f"  rejected: {len(work_plan.rejected_mappings)}")
    print(f"  missing episodes: {len(work_plan.missing_tmdb_episodes)}")
    print(f"  missing movies: {len(work_plan.missing_movies)}")
    print(f"  unmapped: {len(work_plan.unmapped_files)}")
    if review_reason:
        print(f"  reason: {review_reason}")


def analysis_status_payload(work_plan: Any, review_reason: str) -> dict[str, object]:
    validated_videos = [
        mapping for mapping in work_plan.validated_mappings if mapping.target_kind != "subtitle"
    ]
    validated_subtitles = [
        mapping for mapping in work_plan.validated_mappings if mapping.target_kind == "subtitle"
    ]
    return {
        "status": "needs_review" if review_reason else "succeeded",
        "review_reason": review_reason,
        "validated": len(work_plan.validated_mappings),
        "validated_videos": len(validated_videos),
        "validated_subtitles": len(validated_subtitles),
        "rejected": len(work_plan.rejected_mappings),
        "missing_tmdb_episodes": len(work_plan.missing_tmdb_episodes),
        "missing_movies": len(work_plan.missing_movies),
        "unmapped_files": len(work_plan.unmapped_files),
        "library_targets": [
            target.model_dump(mode="json") for target in work_plan.library_targets
        ],
        "archive_target_path": work_plan.archive_target_path,
    }


def analysis_media_type(work_plan: Any) -> str:
    if work_plan.selected_movies and not work_plan.selected_tv_series:
        return "movie"
    if work_plan.selected_tv_series:
        return "tv"
    if work_plan.library_targets:
        return work_plan.library_targets[0].media_type
    return "unknown"


def print_timings(timings: list[dict[str, Any]]) -> None:
    print("Timings:")
    for item in timings:
        print(f"  {item['stage']}: {item['elapsed_seconds']:.3f}s")


def unique_non_empty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(str(value or "").split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        result.append(normalized)
        seen.add(key)
    return result


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "folder"


def save_request_message_contents(
    run: DebugRun,
    stage: str,
    messages: list[dict[str, str]],
) -> list[Path]:
    paths: list[Path] = []
    total = max(len(messages), 1)
    digits = len(str(total))
    for index, message in enumerate(messages, start=1):
        role = safe_slug(str(message.get("role") or "message"))
        path = run.paths.request_dir / f"{stage}.message-{index:0{digits}d}-{role}-content.txt"
        write_text(path, str(message.get("content") or ""))
        run.add_file(path, f"{stage} {role} content", "request")
        paths.append(path)
    return paths


def write_model(path: Path, value: BaseModel) -> None:
    path.write_text(value.model_dump_json(indent=2), encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    path.write_text(text, encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
