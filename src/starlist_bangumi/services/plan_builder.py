from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from starlist_bangumi.clients.llm import LlmClient
from starlist_bangumi.clients.tmdb import TmdbClient
from starlist_bangumi.config import AppConfig, render_path_template, sanitize_openlist_segment
from starlist_bangumi.exceptions import ExternalServiceError
from starlist_bangumi.models import (
    AnalysisRequestItem,
    AnalysisResult,
    CoverageScope,
    ExpectedComponent,
    FileMapping,
    LibraryTarget,
    LlmCandidateSelectionOutput,
    LlmIdentifyWorkOutput,
    LlmMappingDecision,
    LlmMappingOutput,
    ManualEpisodeMappingFile,
    MediaType,
    MissingEpisode,
    MissingMovie,
    RejectedMapping,
    SelectedMovie,
    SelectedTvSeries,
    TmdbCandidate,
    UnmappedFile,
    ValidatedMapping,
    WorkPlan,
)
from starlist_bangumi.pathing import join_openlist_path
from starlist_bangumi.services.scanner import SourceScanner, TreeFile

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".flv", ".webm"}
SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt", ".vtt", ".sub"}
SPECIAL_EPISODE_MARKER_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:ova|oav|oad|ona|sp|specials?)(?:\d{1,3})?(?:[^a-z0-9]|$)",
    flags=re.I,
)
EPISODE_NUMBER_PATTERNS = [
    r"[Ss]\d{1,2}[Ee](\d{1,3})",
    r"第\s*(\d{1,3})\s*(?:话|話|集)",
    r"\b(\d{1,3})\s*(?:话|話|集)",
    r"(?:^|[^\d])(\d{1,3})(?:v\d+)?(?:[^\d]|$)",
]
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
AnalysisProgressLogger = Callable[[str, int, str], Awaitable[None]]


class DiagnosticSink(Protocol):
    def save_diagnostic(
        self,
        *,
        kind: str,
        task_id: str = "",
        analysis_id: str = "",
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        error: str = "",
    ) -> None: ...


class LlmPlanBuilder:
    def __init__(
        self,
        scanner: SourceScanner,
        llm: LlmClient,
        tmdb: TmdbClient,
        config: AppConfig,
        diagnostics: DiagnosticSink | None = None,
    ) -> None:
        self._scanner = scanner
        self._llm = llm
        self._tmdb = tmdb
        self._config = config
        self._diagnostics = diagnostics

    async def analyze(
        self, item: AnalysisRequestItem, log: AnalysisProgressLogger | None = None
    ) -> AnalysisResult:
        warnings: list[str] = []
        if item_has_manual_tmdb_selection(item):
            if not self._llm.is_configured:
                warnings.append("LLM is not configured; mapping cannot run.")
            if not self._tmdb.is_configured:
                warnings.append("TMDB is not configured; generated paths may miss external IDs.")
            identity = self._manual_identity(item)
            tv_candidates, movie_candidates = [], []
            selection = self._manual_selection(item)
            await emit_progress(
                log,
                "manual_tmdb",
                35,
                "Using manually provided TMDB ID(s); skipped identify_work and select_candidates.",
            )
        else:
            identity = await self._identify_work(item, log=log)
            await emit_progress(
                log,
                "tmdb_search",
                35,
                f"Searching TMDB candidates for {identity.canonical_title}",
            )
            tv_candidates, movie_candidates = await self._search_tmdb_candidates(identity)
            await emit_progress(
                log,
                "tmdb_search",
                40,
                (
                    f"TMDB returned {len(tv_candidates)} TV candidate(s), "
                    f"{len(movie_candidates)} movie candidate(s)"
                ),
            )
            if not self._llm.is_configured:
                warnings.append("LLM is not configured; used release-name heuristics.")
            if not self._tmdb.is_configured:
                warnings.append("TMDB is not configured; generated paths may miss external IDs.")
            selection = None
        await emit_progress(log, "scan", 45, "Scanning source folder tree")
        snapshot = await self._scanner.build_text_tree(
            item.path,
            max_depth=self._config.scan.tree_max_depth,
            max_nodes=self._config.scan.tree_max_nodes,
        )
        video_extensions = set(self._config.scan.video_extensions)
        subtitle_extensions = set(self._config.scan.subtitle_extensions)
        media_files = analysis_candidate_files(
            snapshot.files,
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        )
        video_candidate_count = sum(
            1
            for file in media_files
            if _file_kind(
                file.name,
                video_extensions=video_extensions,
                subtitle_extensions=subtitle_extensions,
            )
            == "video"
        )
        subtitle_candidate_count = len(media_files) - video_candidate_count
        subtitle_candidate_note = (
            f", {subtitle_candidate_count} unmatched subtitle candidate(s)"
            if subtitle_candidate_count
            else ""
        )
        await emit_progress(
            log,
            "scan",
            50,
            (
                f"Scanned {snapshot.nodes_scanned} tree node(s), "
                f"{video_candidate_count} video candidate(s){subtitle_candidate_note}"
            ),
        )
        if snapshot.truncated:
            warnings.append("Source tree was truncated during analysis.")
        if selection is None:
            selection = await self._select_candidates(
                item, identity, tv_candidates, movie_candidates, media_files, log=log
            )
        if (
            selection.needs_user_choice
            or not self._llm.is_configured
            or not self._tmdb.is_configured
        ):
            if selection.reason:
                warnings.append(selection.reason)
            work_plan = self._build_needs_review_plan(
                item=item,
                identity=identity,
                media_files=media_files,
                tv_candidates=tv_candidates,
                movie_candidates=movie_candidates,
                warnings=warnings,
            )
            return analysis_result_from_work_plan(
                item=item,
                work_plan=work_plan,
                media_type=primary_media_type(identity),
                confidence=0.45,
            )

        await emit_progress(log, "tmdb_details", 65, "Fetching selected TMDB details")
        tv_details, movie_details, season_details = await self._fetch_tmdb_details(selection)
        if item_has_manual_tmdb_selection(item):
            selected_candidates = candidates_from_selected_details(
                tv_details=tv_details,
                movie_details=movie_details,
            )
            tv_candidates = [
                candidate for candidate in selected_candidates if candidate.media_type == "tv"
            ]
            movie_candidates = [
                candidate for candidate in selected_candidates if candidate.media_type == "movie"
            ]
        await emit_progress(
            log,
            "tmdb_details",
            70,
            (
                f"Fetched TMDB details: TV={'yes' if tv_details else 'no'}, "
                f"movies={len(movie_details)}, seasons={len(season_details)}"
            ),
        )
        if not tv_details and not movie_details:
            warnings.append("No selected TMDB details could be fetched.")
            work_plan = self._build_needs_review_plan(
                item=item,
                identity=identity,
                media_files=media_files,
                tv_candidates=tv_candidates,
                movie_candidates=movie_candidates,
                warnings=warnings,
            )
            return analysis_result_from_work_plan(
                item=item,
                work_plan=work_plan,
                media_type=primary_media_type(identity),
                confidence=0.45,
            )

        mapping_output = await self._decide_mappings(
            item=item,
            identity=identity,
            tree_text=snapshot.text,
            media_files=media_files,
            tv_details=tv_details,
            movie_details=movie_details,
            season_details=season_details,
            log=log,
        )
        await emit_progress(log, "validate", 88, "Validating LLM mapping decisions")
        work_plan = self._validate_mapping_output(
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
            warnings=warnings,
        )
        return analysis_result_from_work_plan(
            item=item,
            work_plan=work_plan,
            media_type=primary_media_type(identity),
            confidence=0.92 if work_plan.validated_mappings else 0.55,
        )

    async def _identify_work(
        self,
        item: AnalysisRequestItem,
        *,
        log: AnalysisProgressLogger | None = None,
    ) -> LlmIdentifyWorkOutput:
        fallback = LlmIdentifyWorkOutput(
            canonical_title=clean_release_name(item.name),
            expected_components=[infer_media_type(item.name)],
            season_hints=[],
            reason="fallback identify_work",
        )
        if not self._llm.is_configured:
            return fallback
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
        result = await self._chat_json("identify_work", messages, log=log, progress=15)
        return LlmIdentifyWorkOutput.model_validate(result)

    def _manual_identity(self, item: AnalysisRequestItem) -> LlmIdentifyWorkOutput:
        components: list[ExpectedComponent] = []
        if item.tv_tmdb_id:
            components.append("tv")
        if item.movie_tmdb_ids:
            components.append("movie")
        return LlmIdentifyWorkOutput(
            canonical_title=clean_release_name(item.name),
            aliases=[],
            expected_components=components or [infer_media_type(item.name)],
            season_hints=[],
            reason="Manual TMDB id selection from WebUI input.",
        )

    def _manual_selection(self, item: AnalysisRequestItem) -> LlmCandidateSelectionOutput:
        return LlmCandidateSelectionOutput(
            selected_tv_series_id=item.tv_tmdb_id,
            selected_movie_ids=item.movie_tmdb_ids,
            season_numbers_to_fetch=[],
            needs_user_choice=False,
            reason="Manual TMDB id selection from WebUI input.",
        )

    async def _search_tmdb_candidates(
        self, identity: LlmIdentifyWorkOutput
    ) -> tuple[list[TmdbCandidate], list[TmdbCandidate]]:
        if not self._tmdb.is_configured:
            return [], []
        queries = unique_non_empty([identity.canonical_title, *identity.aliases])
        tv_candidates: list[TmdbCandidate] = []
        movie_candidates: list[TmdbCandidate] = []
        for query in queries:
            tv_candidates.extend(
                await self._tmdb.search_candidates(query, media_type="tv", limit=5)
            )
            movie_candidates.extend(
                await self._tmdb.search_candidates(query, media_type="movie", limit=5)
            )
        return dedupe_candidates(tv_candidates), dedupe_candidates(movie_candidates)

    async def _select_candidates(
        self,
        item: AnalysisRequestItem,
        identity: LlmIdentifyWorkOutput,
        tv_candidates: list[TmdbCandidate],
        movie_candidates: list[TmdbCandidate],
        media_files: list[TreeFile],
        *,
        log: AnalysisProgressLogger | None = None,
    ) -> LlmCandidateSelectionOutput:
        if not tv_candidates and not movie_candidates:
            return LlmCandidateSelectionOutput(
                needs_user_choice=True,
                season_numbers_to_fetch=default_season_numbers(identity, media_files),
                reason="No TMDB candidates were found.",
            )
        if not self._llm.is_configured:
            return LlmCandidateSelectionOutput(
                needs_user_choice=True,
                season_numbers_to_fetch=default_season_numbers(identity, media_files),
                reason="LLM is not configured; TMDB candidate selection needs user choice.",
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
        result = await self._chat_json("select_candidates", messages, log=log, progress=55)
        selection = LlmCandidateSelectionOutput.model_validate(result)
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
                " Removed OVA/OAD/SP-like movie candidates because a TV series was selected; "
                "these should be matched through TMDB Season 0."
            )
            selection.reason = f"{selection.reason} {suffix}".strip()
        return selection

    async def _fetch_tmdb_details(
        self, selection: LlmCandidateSelectionOutput
    ) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]], dict[int, dict[str, Any]]]:
        tv_details = None
        season_details: dict[int, dict[str, Any]] = {}
        if selection.selected_tv_series_id:
            tv_details = await self._tmdb.tv_details(selection.selected_tv_series_id)
            season_numbers = tmdb_season_numbers(tv_details)
            selection.season_numbers_to_fetch = season_numbers
            for season_number in season_numbers:
                details = await self._tmdb.season_details(
                    selection.selected_tv_series_id, season_number
                )
                if details:
                    season_details[season_number] = details
        movie_details: dict[str, dict[str, Any]] = {}
        for movie_id in selection.selected_movie_ids:
            details = await self._tmdb.movie_details(movie_id)
            if details:
                movie_details[movie_id] = details
        return tv_details, movie_details, season_details

    async def _decide_mappings(
        self,
        *,
        item: AnalysisRequestItem,
        identity: LlmIdentifyWorkOutput,
        tree_text: str,
        media_files: list[TreeFile],
        tv_details: dict[str, Any] | None,
        movie_details: dict[str, dict[str, Any]],
        season_details: dict[int, dict[str, Any]],
        log: AnalysisProgressLogger | None = None,
    ) -> LlmMappingOutput:
        candidate_files_json = candidate_file_json(
            media_files,
            video_extensions=set(self._config.scan.video_extensions),
            subtitle_extensions=set(self._config.scan.subtitle_extensions),
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
        result = await self._chat_json("decide_mappings", messages, log=log, progress=75)
        return LlmMappingOutput.model_validate(result)

    async def _chat_json(
        self,
        stage: str,
        messages: list[dict[str, str]],
        *,
        log: AnalysisProgressLogger | None,
        progress: int,
    ) -> dict[str, Any]:
        input_chars = message_char_count(messages)
        timeout_seconds = self._config.llm.request_timeout_seconds
        request_payload = {
            "stage": stage,
            "input_chars": input_chars,
            "messages": messages,
            "model": self._config.llm.model,
            "timeout_seconds": timeout_seconds,
        }
        await emit_progress(
            log,
            stage,
            progress,
            f"LLM {stage} request: {input_chars} input char(s), timeout {timeout_seconds}s",
        )
        started_at = time.perf_counter()
        try:
            result = await self._llm.chat_json(messages)
        except ExternalServiceError as exc:
            elapsed_seconds = round(time.perf_counter() - started_at, 1)
            details = dict(exc.details)
            details.update(
                {
                    "llm_stage": stage,
                    "input_chars": input_chars,
                    "elapsed_seconds": elapsed_seconds,
                    "request_timeout_seconds": timeout_seconds,
                    "model": self._config.llm.model,
                }
            )
            if details.get("error_type") == "ReadTimeout":
                details["hint"] = (
                    "The LLM gateway did not return before the configured timeout. "
                    "Try a faster model, increase LLM timeout, or reduce scan limits."
                )
            self._save_llm_diagnostic(
                stage=stage,
                request=request_payload,
                error=safe_json(details),
            )
            raise ExternalServiceError(
                f"LLM stage {stage} failed after {elapsed_seconds}s: {exc}",
                details=details,
            ) from exc
        elapsed_seconds = round(time.perf_counter() - started_at, 1)
        self._save_llm_diagnostic(
            stage=stage,
            request=request_payload,
            response={
                "stage": stage,
                "elapsed_seconds": elapsed_seconds,
                "result": result,
            },
        )
        await emit_progress(
            log,
            stage,
            min(progress + 8, 86),
            f"LLM {stage} completed in {elapsed_seconds}s",
        )
        return result

    def _save_llm_diagnostic(
        self,
        *,
        stage: str,
        request: dict[str, Any],
        response: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.save_diagnostic(
            kind=f"llm.{stage}",
            request=request,
            response=response or {},
            error=error,
        )

    def _validate_mapping_output(
        self,
        *,
        item: AnalysisRequestItem,
        identity: LlmIdentifyWorkOutput,
        mapping_output: LlmMappingOutput,
        media_files: list[TreeFile],
        source_files: list[TreeFile],
        tv_candidates: list[TmdbCandidate],
        movie_candidates: list[TmdbCandidate],
        tv_details: dict[str, Any] | None,
        movie_details: dict[str, dict[str, Any]],
        season_details: dict[int, dict[str, Any]],
        warnings: list[str],
    ) -> WorkPlan:
        media_file_by_path = {file.path: file for file in media_files}
        source_file_by_path = {file.path: file for file in source_files}
        video_paths = {
            file.path
            for file in media_files
            if _file_kind(
                file.name,
                video_extensions=set(self._config.scan.video_extensions),
                subtitle_extensions=set(self._config.scan.subtitle_extensions),
            )
            == "video"
        }
        subtitle_paths = {
            file.path
            for file in source_files
            if _file_kind(
                file.name,
                video_extensions=set(self._config.scan.video_extensions),
                subtitle_extensions=set(self._config.scan.subtitle_extensions),
            )
            == "subtitle"
        }
        library_targets: list[LibraryTarget] = []
        selected_tv_series: SelectedTvSeries | None = None
        tv_target: LibraryTarget | None = None
        tv_values: dict[str, str] | None = None
        if tv_details:
            tv_values = _template_values(
                identity.canonical_title,
                "tv",
                None,
                tv_details,
                source_name=item.name,
            )
            tv_target = LibraryTarget(
                media_type="tv",
                target_path=join_openlist_path(
                    self._config.media_library.tv_media_library_path,
                    render_path_template(
                        self._config.media_library.tv_media_library_path_template,
                        tv_values,
                    ),
                ),
                title=tv_values["title"],
                year=tv_values["year"],
                tmdb_id=tv_values["tmdb_id"],
            )
            library_targets.append(tv_target)
            selected_tv_series = SelectedTvSeries(
                tmdb_id=tv_values["tmdb_id"],
                title=tv_values["title"],
                original_title=tv_values["original_title"],
                year=tv_values["year"],
            )

        movie_targets: dict[str, LibraryTarget] = {}
        movie_values: dict[str, dict[str, str]] = {}
        selected_movies: list[SelectedMovie] = []
        for movie_id, details in movie_details.items():
            values = _template_values(
                identity.canonical_title,
                "movie",
                None,
                details,
                source_name=item.name,
            )
            target = LibraryTarget(
                media_type="movie",
                target_path=join_openlist_path(
                    self._config.media_library.movie_media_library_path,
                    render_path_template(
                        self._config.media_library.movie_media_library_path_template,
                        values,
                    ),
                ),
                title=values["title"],
                year=values["year"],
                tmdb_id=values["tmdb_id"],
            )
            movie_targets[movie_id] = target
            movie_values[movie_id] = values
            library_targets.append(target)
            selected_movies.append(
                SelectedMovie(
                    tmdb_id=movie_id,
                    title=values["title"],
                    original_title=values["original_title"],
                    year=values["year"],
                )
            )

        archive_values = archive_template_values(
            identity=identity,
            tv_details=tv_details,
            movie_details=movie_details,
            source_name=item.name,
        )
        archive_target_path = join_openlist_path(
            self._config.media_library.archive_path,
            render_path_template(self._config.media_library.archive_path_template, archive_values),
            item.name,
        )
        episode_index = tmdb_episode_index(season_details)
        validated: list[ValidatedMapping] = []
        rejected: list[RejectedMapping] = []
        used_video_sources: set[str] = set()
        used_subtitle_sources: set[str] = set()
        used_target_paths: set[str] = set()

        for source_path, decision in expand_grouped_mapping_decisions(mapping_output, media_files):
            if source_path not in media_file_by_path:
                rejected.append(
                    RejectedMapping(
                        source_path=source_path,
                        target_kind=decision.target_kind,
                        reason="source_not_in_candidate_list",
                        details="LLM selected a source path that was not provided.",
                    )
                )
                continue
            if source_path not in video_paths:
                rejected.append(
                    RejectedMapping(
                        source_path=source_path,
                        target_kind=decision.target_kind,
                        reason="source_is_not_video",
                        details="Only video files can be mapped as primary media.",
                    )
                )
                continue
            if source_path in used_video_sources:
                rejected.append(
                    RejectedMapping(
                        source_path=source_path,
                        target_kind=decision.target_kind,
                        reason="duplicate_source_mapping",
                    )
                )
                continue

            mapping: ValidatedMapping | None = None
            if decision.target_kind == "tv_episode":
                mapping = validate_tv_episode_decision(
                    decision=decision,
                    tv_target=tv_target,
                    tv_values=tv_values,
                    episode_index=episode_index,
                    include_episode_title=self._config.media_library.include_episode_title_in_filename,
                    source_path=source_path,
                    source_file=media_file_by_path[source_path],
                )
            elif decision.target_kind == "movie":
                mapping = validate_movie_decision(
                    decision=decision,
                    movie_targets=movie_targets,
                    movie_values=movie_values,
                    source_path=source_path,
                    source_file=media_file_by_path[source_path],
                )

            if mapping is None:
                rejected.append(
                    RejectedMapping(
                        source_path=source_path,
                        target_kind=decision.target_kind,
                        reason="tmdb_target_not_found",
                        details="Decision did not match selected TMDB details.",
                    )
                )
                continue
            if mapping.target_path in used_target_paths:
                rejected.append(
                    RejectedMapping(
                        source_path=source_path,
                        target_kind=decision.target_kind,
                        reason="duplicate_target_path",
                        details=mapping.target_path,
                    )
                )
                continue

            validated.append(mapping)
            used_video_sources.add(source_path)
            used_target_paths.add(mapping.target_path)
            for subtitle_path in matching_subtitle_source_paths(
                video_source_file=media_file_by_path[source_path],
                source_files=source_files,
                video_extensions=set(self._config.scan.video_extensions),
                subtitle_extensions=set(self._config.scan.subtitle_extensions),
            ):
                subtitle_mapping = validate_subtitle_decision(
                    subtitle_path=subtitle_path,
                    video_mapping=mapping,
                    video_source_file=media_file_by_path[source_path],
                    media_file_by_path=source_file_by_path,
                    subtitle_paths=subtitle_paths,
                    used_subtitle_sources=used_subtitle_sources,
                )
                if isinstance(subtitle_mapping, RejectedMapping):
                    rejected.append(subtitle_mapping)
                    continue
                if subtitle_mapping.target_path in used_target_paths:
                    rejected.append(
                        RejectedMapping(
                            source_path=subtitle_path,
                            target_kind="subtitle",
                            reason="duplicate_target_path",
                            details=subtitle_mapping.target_path,
                        )
                    )
                    continue
                validated.append(subtitle_mapping)
                used_subtitle_sources.add(subtitle_path)
                used_target_paths.add(subtitle_mapping.target_path)

        mapped_episode_keys = {
            (mapping.season_number, mapping.episode_number)
            for mapping in validated
            if mapping.target_kind == "tv_episode"
            and mapping.season_number is not None
            and mapping.episode_number is not None
        }
        mapped_movie_ids = {
            mapping.tmdb_movie_id for mapping in validated if mapping.target_kind == "movie"
        }
        ignored_reasons = expand_ignored_file_reasons(mapping_output, media_files)
        mapped_paths = {mapping.source_path for mapping in validated}
        unmapped_files = build_unmapped_files(
            media_files,
            mapped_paths=mapped_paths,
            ignored_reasons=ignored_reasons,
            video_extensions=set(self._config.scan.video_extensions),
            subtitle_extensions=set(self._config.scan.subtitle_extensions),
        )
        missing_episodes = find_missing_episodes(
            episode_index=episode_index,
            mapped_episode_keys=mapped_episode_keys,
        )
        missing_movies = find_missing_movies(
            movie_details,
            movie_values=movie_values,
            mapped_movie_ids=mapped_movie_ids,
        )
        all_warnings = [*warnings, *mapping_output.notes]
        needs_user_choice_reason = review_reason(
            validated=validated,
            rejected=rejected,
            missing_episodes=missing_episodes,
            missing_movies=missing_movies,
            coverage_scope=mapping_output.coverage_scope,
        )
        if not library_targets:
            library_targets.append(
                fallback_library_target(
                    config=self._config,
                    identity=identity,
                    source_name=item.name,
                )
            )
        work_plan = WorkPlan(
            work_title=archive_values["title"],
            source_name=item.name,
            source_path=item.path,
            archive_target_path=archive_target_path,
            coverage_scope=mapping_output.coverage_scope,
            tv_candidates=tv_candidates,
            movie_candidates=movie_candidates,
            needs_user_choice_reason=needs_user_choice_reason,
            selected_tv_series=selected_tv_series,
            selected_movies=selected_movies,
            library_targets=library_targets,
            validated_mappings=validated,
            rejected_mappings=rejected,
            missing_tmdb_episodes=missing_episodes,
            missing_movies=missing_movies,
            unmapped_files=unmapped_files,
            warnings=all_warnings,
        )
        return work_plan

    def _build_needs_review_plan(
        self,
        *,
        item: AnalysisRequestItem,
        identity: LlmIdentifyWorkOutput,
        media_files: list[TreeFile],
        tv_candidates: list[TmdbCandidate],
        movie_candidates: list[TmdbCandidate],
        warnings: list[str],
    ) -> WorkPlan:
        media_type = primary_media_type(identity)
        title = identity.canonical_title or clean_release_name(item.name)
        values = fallback_template_values(title=title, media_type=media_type, source_name=item.name)
        media_root = self._config.media_library.movie_media_library_path
        media_template = self._config.media_library.movie_media_library_path_template
        if media_type == "tv":
            media_root = self._config.media_library.tv_media_library_path
            media_template = self._config.media_library.tv_media_library_path_template
        media_target_path = join_openlist_path(
            media_root,
            render_path_template(media_template, values),
        )
        archive_target_path = join_openlist_path(
            self._config.media_library.archive_path,
            render_path_template(self._config.media_library.archive_path_template, values),
            item.name,
        )
        preview_mappings = build_fallback_mappings_from_files(
            source_path=item.path,
            media_type=media_type,
            title=values["title"],
            year=values["year"],
            media_target_path=media_target_path,
            files=media_files,
        )
        work_plan = build_fallback_work_plan(
            source_name=item.name,
            source_path=item.path,
            values=values,
            media_type=media_type,
            media_target_path=media_target_path,
            archive_target_path=archive_target_path,
            preview_mappings=preview_mappings,
            files=media_files,
            warnings=warnings,
        )
        work_plan.tv_candidates = tv_candidates
        work_plan.movie_candidates = movie_candidates
        work_plan.needs_user_choice_reason = "; ".join(warnings)
        return work_plan


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


async def emit_progress(
    log: AnalysisProgressLogger | None, stage: str, progress: int, message: str
) -> None:
    if log is not None:
        await log(stage, progress, message)


def message_char_count(messages: list[dict[str, str]]) -> int:
    return sum(len(message.get("content", "")) for message in messages)


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def prompt_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def media_candidate_files(
    files: list[TreeFile], *, video_extensions: set[str], subtitle_extensions: set[str]
) -> list[TreeFile]:
    video_extensions = normalize_extensions(video_extensions)
    return [
        file
        for file in sorted(files, key=lambda item: item.relative_path.lower())
        if PurePosixPath(file.name).suffix.lower() in video_extensions
    ]


def analysis_candidate_files(
    files: list[TreeFile], *, video_extensions: set[str], subtitle_extensions: set[str]
) -> list[TreeFile]:
    video_extensions = normalize_extensions(video_extensions)
    subtitle_extensions = normalize_extensions(subtitle_extensions)
    auto_bound_subtitle_paths = auto_bound_subtitle_source_paths(
        files,
        video_extensions=video_extensions,
        subtitle_extensions=subtitle_extensions,
    )
    candidates: list[TreeFile] = []
    for file in sorted(files, key=lambda item: item.relative_path.lower()):
        kind = _file_kind(
            file.name,
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        )
        if kind == "video" or (
            kind == "subtitle" and file.path not in auto_bound_subtitle_paths
        ):
            candidates.append(file)
    return candidates


def auto_bound_subtitle_source_paths(
    files: list[TreeFile], *, video_extensions: set[str], subtitle_extensions: set[str]
) -> set[str]:
    video_files: list[TreeFile] = []
    subtitle_files: list[TreeFile] = []
    for file in files:
        kind = _file_kind(
            file.name,
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        )
        if kind == "video":
            video_files.append(file)
        elif kind == "subtitle":
            subtitle_files.append(file)
    return {
        file.path
        for file in subtitle_files
        if prebind_subtitle_to_video(file, video_files) is not None
    }


def candidate_file_json(
    files: list[TreeFile],
    *,
    video_extensions: set[str] | None = None,
    subtitle_extensions: set[str] | None = None,
    source_root: str = "",
) -> str:
    _ = source_root
    groups: dict[str, list[TreeFile]] = {}
    for file in files:
        folder_path = PurePosixPath(file.relative_path).parent.as_posix()
        if folder_path == ".":
            folder_path = ""
        groups.setdefault(folder_path, []).append(file)
    payload: list[dict[str, object]] = []
    for folder_path, folder_files in sorted(groups.items()):
        payload.append(
            {
                "folder_path": folder_path,
                "files": [
                    {
                        "file_name": file.name,
                        "size": file.size,
                        "file_kind": _file_kind(
                            file.name,
                            video_extensions=video_extensions,
                            subtitle_extensions=subtitle_extensions,
                        ),
                    }
                    for file in sorted(folder_files, key=lambda item: item.name.lower())
                ],
            }
        )
    return prompt_json(payload)


def expand_grouped_mapping_decisions(
    mapping_output: LlmMappingOutput,
    media_files: list[TreeFile],
) -> list[tuple[str, LlmMappingDecision]]:
    path_by_key = media_file_path_by_group_key(media_files)
    expanded: list[tuple[str, LlmMappingDecision]] = []
    for decision in mapping_output.decisions:
        for file_info in decision.file_infos:
            source_path = path_by_key.get(
                mapping_group_key(decision.folder_path, file_info.file_name)
            )
            if source_path is None:
                source_path = missing_mapping_source_path(decision.folder_path, file_info.file_name)
            expanded.append(
                (
                    source_path,
                    LlmMappingDecision(
                        folder_path=decision.folder_path,
                        target_kind=decision.target_kind,
                        season_number=decision.season_number,
                        tmdb_movie_id=decision.tmdb_movie_id,
                        confidence=file_info.confidence or decision.confidence,
                        reason=file_info.reason or decision.reason,
                        file_infos=[
                            {
                                "file_name": file_info.file_name,
                                "episode_number": file_info.episode_number,
                                "confidence": file_info.confidence,
                                "reason": file_info.reason,
                            }
                        ],
                    ),
                )
            )
    return expanded


def expand_ignored_file_reasons(
    mapping_output: LlmMappingOutput,
    media_files: list[TreeFile],
) -> dict[str, str]:
    path_by_key = media_file_path_by_group_key(media_files)
    result: dict[str, str] = {}
    for ignored in mapping_output.ignored_files:
        source_path = path_by_key.get(mapping_group_key(ignored.folder_path, ignored.file_name))
        if source_path is None:
            source_path = missing_mapping_source_path(ignored.folder_path, ignored.file_name)
        result[source_path] = ignored.reason
    return result


def media_file_path_by_group_key(files: list[TreeFile]) -> dict[tuple[str, str], str]:
    return {
        mapping_group_key(relative_parent_path(file.relative_path), file.name): file.path
        for file in files
    }


def relative_parent_path(relative_path: str) -> str:
    parent = PurePosixPath(relative_path).parent.as_posix()
    return "" if parent == "." else parent


def mapping_group_key(folder_path: str, file_name: str) -> tuple[str, str]:
    folder = normalize_mapping_folder_path(folder_path)
    return folder.casefold(), file_name.casefold()


def normalize_mapping_folder_path(folder_path: str) -> str:
    folder_path = str(folder_path or "").strip().strip("/")
    return "" if folder_path == "." else folder_path


def missing_mapping_source_path(folder_path: str, file_name: str) -> str:
    folder_path = normalize_mapping_folder_path(folder_path)
    relative_path = f"{folder_path}/{file_name}" if folder_path else file_name
    return f"<missing candidate>/{relative_path}"


def candidate_json(candidates: list[TmdbCandidate]) -> str:
    compact = [
        {
            "media_type": candidate.media_type,
            "tmdb_id": candidate.tmdb_id,
            "title": candidate.title,
            "original_title": candidate.original_title,
            "year": candidate.year,
            "language": candidate.language,
            "overview": candidate.overview[:240],
        }
        for candidate in candidates
    ]
    return prompt_json(compact)


def item_has_manual_tmdb_selection(item: AnalysisRequestItem) -> bool:
    return bool(item.tv_tmdb_id or item.movie_tmdb_ids)


def candidates_from_selected_details(
    *,
    tv_details: dict[str, Any] | None,
    movie_details: dict[str, dict[str, Any]],
) -> list[TmdbCandidate]:
    candidates: list[TmdbCandidate] = []
    if tv_details:
        candidates.append(tmdb_candidate_from_details(tv_details, media_type="tv"))
    candidates.extend(
        tmdb_candidate_from_details(details, media_type="movie")
        for details in movie_details.values()
        if details
    )
    return candidates


def tmdb_candidate_from_details(details: dict[str, Any], *, media_type: str) -> TmdbCandidate:
    title = details.get("name") or details.get("title") or ""
    original_title = details.get("original_name") or details.get("original_title") or ""
    date = details.get("first_air_date") or details.get("release_date") or ""
    return TmdbCandidate(
        media_type="movie" if media_type == "movie" else "tv",
        tmdb_id=str(details.get("id") or ""),
        title=str(title),
        original_title=str(original_title),
        year=str(date)[:4] if date else "",
        overview=str(details.get("overview") or ""),
        language=str(details.get("original_language") or ""),
    )


def compact_tv_details(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {}
    seasons = details.get("seasons") or []
    return {
        "id": details.get("id"),
        "name": details.get("name"),
        "original_name": details.get("original_name"),
        "first_air_date": details.get("first_air_date"),
        "number_of_seasons": details.get("number_of_seasons"),
        "number_of_episodes": details.get("number_of_episodes"),
        "overview": str(details.get("overview") or "")[:300],
        "seasons": [
            {
                "season_number": season.get("season_number"),
                "name": season.get("name"),
                "episode_count": season.get("episode_count"),
            }
            for season in seasons
            if isinstance(season, dict)
        ],
    }


def tmdb_season_numbers(details: dict[str, Any] | None) -> list[int]:
    seasons = (details or {}).get("seasons") or []
    numbers: set[int] = set()
    if not isinstance(seasons, list):
        return []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        season_number = as_int(season.get("season_number"))
        episode_count = as_int(season.get("episode_count"))
        if season_number is None or season_number < 0:
            continue
        if episode_count is not None and episode_count <= 0:
            continue
        numbers.add(season_number)
    return sorted(numbers)


def compact_season_details(season_details: dict[int, dict[str, Any]]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for season_number, details in sorted(season_details.items()):
        episodes = details.get("episodes") or []
        compact[str(season_number)] = {
            "id": details.get("id"),
            "name": details.get("name"),
            "season_number": details.get("season_number", season_number),
            "episode_count": len(episodes) if isinstance(episodes, list) else 0,
            "episodes": [
                {
                    "episode_number": episode.get("episode_number"),
                    "name": episode.get("name"),
                    "air_date": episode.get("air_date"),
                }
                for episode in episodes
                if isinstance(episode, dict)
            ],
        }
    return compact


def compact_movie_details(movie_details: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        movie_id: {
            "id": details.get("id"),
            "title": details.get("title"),
            "original_title": details.get("original_title"),
            "release_date": details.get("release_date"),
            "overview": str(details.get("overview") or "")[:300],
        }
        for movie_id, details in sorted(movie_details.items())
    }


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


def dedupe_candidates(candidates: list[TmdbCandidate]) -> list[TmdbCandidate]:
    result: list[TmdbCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.media_type, candidate.tmdb_id)
        if not candidate.tmdb_id or key in seen:
            continue
        result.append(candidate)
        seen.add(key)
    return result


def prune_tv_special_movie_selection(
    selection: LlmCandidateSelectionOutput,
    movie_candidates: list[TmdbCandidate],
) -> list[str]:
    if not selection.selected_tv_series_id or not selection.selected_movie_ids:
        return []
    special_movie_ids = {
        candidate.tmdb_id
        for candidate in movie_candidates
        if is_special_movie_candidate(candidate)
    }
    if not special_movie_ids:
        return []
    kept_movie_ids = [
        movie_id for movie_id in selection.selected_movie_ids if movie_id not in special_movie_ids
    ]
    removed_movie_ids = [
        movie_id for movie_id in selection.selected_movie_ids if movie_id in special_movie_ids
    ]
    selection.selected_movie_ids = kept_movie_ids
    return removed_movie_ids


def is_special_movie_candidate(candidate: TmdbCandidate) -> bool:
    text = " ".join(
        [
            candidate.title,
            candidate.original_title,
            candidate.overview,
        ]
    ).casefold()
    return bool(re.search(r"\b(?:ova|oav|oad|ona|sp|specials?)\b", text))


def normalize_extensions(extensions: set[str]) -> set[str]:
    return {
        extension if extension.startswith(".") else f".{extension}"
        for extension in {item.strip().lower() for item in extensions}
        if extension
    }


def season_numbers_from_files(files: list[TreeFile]) -> set[int]:
    seasons: set[int] = set()
    has_episode_like_video = False
    for file in files:
        if _file_kind(file.name) != "video":
            continue
        path_text = file.relative_path.replace("\\", "/")
        lowered = path_text.lower()
        for pattern in [
            r"[Ss](\d{1,2})[Ee]\d{1,3}",
            r"(?:season|saison|第)\s*(\d{1,2})\s*(?:季)?",
        ]:
            match = re.search(pattern, path_text, flags=re.I)
            if match:
                seasons.add(int(match.group(1)))
        if any(marker in lowered for marker in ["ova", "oad", "special", "sp/", "sps/"]):
            seasons.add(0)
        if _extract_episode(PurePosixPath(file.name).stem):
            has_episode_like_video = True
    if not seasons and has_episode_like_video:
        seasons.add(1)
    return seasons


def default_season_numbers(
    identity: LlmIdentifyWorkOutput, media_files: list[TreeFile]
) -> list[int]:
    numbers = set(identity.season_hints)
    numbers.update(season_numbers_from_files(media_files))
    if "special" in identity.expected_components:
        numbers.add(0)
    if "tv" in identity.expected_components and not any(number > 0 for number in numbers):
        numbers.add(1)
    return sorted(number for number in numbers if number >= 0)


def primary_media_type(identity: LlmIdentifyWorkOutput) -> MediaType:
    components = set(identity.expected_components)
    if "tv" in components or "special" in components:
        return "tv"
    if "movie" in components:
        return "movie"
    return infer_media_type(identity.canonical_title)


def fallback_template_values(*, title: str, media_type: str, source_name: str) -> dict[str, str]:
    year_match = re.search(r"(?:19|20)\d{2}", f"{title} {source_name}")
    return {
        "title": sanitize_openlist_segment(title or source_name),
        "original_title": "",
        "year": year_match.group(0) if year_match else "0000",
        "tmdb_id": "",
        "tvdb_id": "",
        "media_type": media_type,
        "source_name": sanitize_openlist_segment(source_name),
    }


def archive_template_values(
    *,
    identity: LlmIdentifyWorkOutput,
    tv_details: dict[str, Any] | None,
    movie_details: dict[str, dict[str, Any]],
    source_name: str,
) -> dict[str, str]:
    media_type = primary_media_type(identity)
    details = tv_details or next(iter(movie_details.values()), None)
    if details:
        values = _template_values(
            identity.canonical_title,
            media_type,
            None,
            details,
            source_name=source_name,
        )
        values["title"] = sanitize_openlist_segment(identity.canonical_title or source_name)
        return values
    return fallback_template_values(
        title=identity.canonical_title or clean_release_name(source_name),
        media_type=media_type,
        source_name=source_name,
    )


def fallback_library_target(
    *, config: AppConfig, identity: LlmIdentifyWorkOutput, source_name: str
) -> LibraryTarget:
    media_type = primary_media_type(identity)
    values = fallback_template_values(
        title=identity.canonical_title or clean_release_name(source_name),
        media_type=media_type,
        source_name=source_name,
    )
    if media_type == "movie":
        root = config.media_library.movie_media_library_path
        template = config.media_library.movie_media_library_path_template
    else:
        root = config.media_library.tv_media_library_path
        template = config.media_library.tv_media_library_path_template
        media_type = "tv"
    return LibraryTarget(
        media_type=media_type,
        target_path=join_openlist_path(root, render_path_template(template, values)),
        title=values["title"],
        year=values["year"],
        tmdb_id=values["tmdb_id"],
    )


def tmdb_episode_index(
    season_details: dict[int, dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for season_number, details in season_details.items():
        episodes = details.get("episodes") or []
        if not isinstance(episodes, list):
            continue
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            episode_number = as_int(episode.get("episode_number"))
            if episode_number is None:
                continue
            result[(season_number, episode_number)] = episode
    return result


def validate_tv_episode_decision(
    *,
    decision: LlmMappingDecision,
    tv_target: LibraryTarget | None,
    tv_values: dict[str, str] | None,
    episode_index: dict[tuple[int, int], dict[str, Any]],
    include_episode_title: bool,
    source_path: str,
    source_file: TreeFile,
) -> ValidatedMapping | None:
    if tv_target is None or tv_values is None:
        return None
    file_info = first_mapping_file_info(decision)
    if file_info is None or decision.season_number is None or file_info.episode_number is None:
        return None
    episode = episode_index.get((decision.season_number, file_info.episode_number))
    if episode is None:
        return None
    extension = PurePosixPath(source_file.name).suffix.lower()
    episode_name = str(episode.get("name") or "")
    target_relative_path = tv_episode_target_relative_path(
        title=tv_values["title"],
        season_number=decision.season_number,
        episode_number=file_info.episode_number,
        episode_name=episode_name,
        extension=extension,
        include_episode_title=include_episode_title,
    )
    return ValidatedMapping(
        source_path=source_path,
        target_path=join_openlist_path(tv_target.target_path, target_relative_path),
        target_relative_path=target_relative_path,
        target_kind="tv_episode",
        media_type="tv",
        season_number=decision.season_number,
        episode_number=file_info.episode_number,
        episode_name=episode_name,
        tmdb_episode_id=str(episode.get("id") or ""),
        reason=decision.reason or "LLM matched TMDB episode",
    )


def validate_movie_decision(
    *,
    decision: LlmMappingDecision,
    movie_targets: dict[str, LibraryTarget],
    movie_values: dict[str, dict[str, str]],
    source_path: str,
    source_file: TreeFile,
) -> ValidatedMapping | None:
    if not decision.tmdb_movie_id:
        return None
    target = movie_targets.get(decision.tmdb_movie_id)
    values = movie_values.get(decision.tmdb_movie_id)
    if target is None or values is None:
        return None
    extension = PurePosixPath(source_file.name).suffix.lower()
    target_name = movie_target_name(values["title"], values["year"], extension)
    return ValidatedMapping(
        source_path=source_path,
        target_path=join_openlist_path(target.target_path, target_name),
        target_relative_path=target_name,
        target_kind="movie",
        media_type="movie",
        tmdb_movie_id=decision.tmdb_movie_id,
        reason=decision.reason or "LLM matched TMDB movie",
    )


def first_mapping_file_info(decision: LlmMappingDecision) -> Any | None:
    return decision.file_infos[0] if decision.file_infos else None


def validate_subtitle_decision(
    *,
    subtitle_path: str,
    video_mapping: ValidatedMapping,
    video_source_file: TreeFile,
    media_file_by_path: dict[str, TreeFile],
    subtitle_paths: set[str],
    used_subtitle_sources: set[str],
) -> ValidatedMapping | RejectedMapping:
    subtitle_file = media_file_by_path.get(subtitle_path)
    if subtitle_file is None:
        return RejectedMapping(
            source_path=subtitle_path,
            target_kind="subtitle",
            reason="source_not_in_candidate_list",
            details="LLM selected a subtitle path that was not provided.",
        )
    if subtitle_path not in subtitle_paths:
        return RejectedMapping(
            source_path=subtitle_path,
            target_kind="subtitle",
            reason="source_is_not_subtitle",
        )
    if subtitle_path in used_subtitle_sources:
        return RejectedMapping(
            source_path=subtitle_path,
            target_kind="subtitle",
            reason="duplicate_subtitle_mapping",
        )
    target_relative_path = subtitle_target_relative_path(
        video_target_relative_path=video_mapping.target_relative_path,
        video_source_file=video_source_file,
        subtitle_file=subtitle_file,
    )
    target_parent = video_mapping.target_path.rsplit("/", 1)[0]
    target_name = PurePosixPath(target_relative_path).name
    return ValidatedMapping(
        source_path=subtitle_path,
        target_path=join_openlist_path(target_parent, target_name),
        target_relative_path=target_relative_path,
        target_kind="subtitle",
        media_type=video_mapping.media_type,
        season_number=video_mapping.season_number,
        episode_number=video_mapping.episode_number,
        episode_name=video_mapping.episode_name,
        tmdb_episode_id=video_mapping.tmdb_episode_id,
        tmdb_movie_id=video_mapping.tmdb_movie_id,
        subtitle_for_source_path=video_mapping.source_path,
        reason="subtitle follows verified video mapping",
    )


def matching_subtitle_source_paths(
    *,
    video_source_file: TreeFile,
    source_files: list[TreeFile],
    video_extensions: set[str],
    subtitle_extensions: set[str],
) -> list[str]:
    subtitle_files = [
        file
        for file in source_files
        if _file_kind(
            file.name,
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        )
        == "subtitle"
    ]
    return [
        file.path
        for file in sorted(subtitle_files, key=lambda item: item.relative_path.lower())
        if prebind_subtitle_to_video(file, [video_source_file]) == video_source_file
    ]


def tv_episode_target_relative_path(
    *,
    title: str,
    season_number: int,
    episode_number: int,
    episode_name: str,
    extension: str,
    include_episode_title: bool,
) -> str:
    safe_title = sanitize_openlist_segment(title)
    base = f"{safe_title} - S{season_number:02d}E{episode_number:02d}"
    if include_episode_title and episode_name:
        base = f"{base} - {sanitize_openlist_segment(episode_name)}"
    season_dir = f"Season {season_number:02d}"
    return f"{season_dir}/{base}{extension}"


def movie_target_name(title: str, year: str, extension: str) -> str:
    return f"{sanitize_openlist_segment(title)} ({year or '0000'}){extension}"


def subtitle_target_relative_path(
    *, video_target_relative_path: str, video_source_file: TreeFile, subtitle_file: TreeFile
) -> str:
    video_target = PurePosixPath(video_target_relative_path)
    subtitle_suffix = subtitle_language_suffix(video_source_file.name, subtitle_file.name)
    target_name = f"{video_target.stem}{subtitle_suffix}{PurePosixPath(subtitle_file.name).suffix}"
    if str(video_target.parent) == ".":
        return target_name
    return f"{video_target.parent.as_posix()}/{target_name}"


def subtitle_language_suffix(video_name: str, subtitle_name: str) -> str:
    video_stem = PurePosixPath(video_name).stem
    subtitle_stem = PurePosixPath(subtitle_name).stem
    if subtitle_stem.casefold().startswith(video_stem.casefold()):
        suffix = subtitle_stem[len(video_stem) :].strip(". ")
        return normalized_subtitle_suffix(suffix)
    suffix = subtitle_stem.split(".", 1)[1] if "." in subtitle_stem else ""
    return normalized_subtitle_suffix(suffix)


def normalized_subtitle_suffix(suffix: str) -> str:
    parts = [sanitize_openlist_segment(part) for part in suffix.split(".") if part.strip()]
    return f".{'.'.join(parts)}" if parts else ""


def prebind_subtitle_to_video(
    subtitle_file: TreeFile, video_files: list[TreeFile]
) -> TreeFile | None:
    subtitle_path = PurePosixPath(subtitle_file.relative_path)
    subtitle_stem = PurePosixPath(subtitle_file.name).stem.casefold()
    same_dir_videos = [
        file
        for file in video_files
        if PurePosixPath(file.relative_path).parent == subtitle_path.parent
    ]
    matches = [
        file
        for file in same_dir_videos
        if subtitle_stem.startswith(PurePosixPath(file.name).stem.casefold())
    ]
    if matches:
        return max(matches, key=lambda file: len(PurePosixPath(file.name).stem))
    if has_special_episode_marker(subtitle_stem):
        return None
    subtitle_episode = _extract_episode(subtitle_stem)
    if subtitle_episode is None:
        return None
    episode_matches = [
        file
        for file in same_dir_videos
        if not has_special_episode_marker(PurePosixPath(file.name).stem.casefold())
        and _extract_episode(PurePosixPath(file.name).stem) == subtitle_episode
    ]
    if len(episode_matches) != 1:
        return None
    return episode_matches[0]


def has_special_episode_marker(stem: str) -> bool:
    return bool(SPECIAL_EPISODE_MARKER_RE.search(stem))


def build_unmapped_files(
    files: list[TreeFile],
    *,
    mapped_paths: set[str],
    ignored_reasons: dict[str, str],
    video_extensions: set[str],
    subtitle_extensions: set[str],
) -> list[UnmappedFile]:
    result: list[UnmappedFile] = []
    for file in files:
        kind = _file_kind(
            file.name,
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        )
        if kind not in {"video", "subtitle"} or file.path in mapped_paths:
            continue
        result.append(
            UnmappedFile(
                source_path=file.path,
                file_kind=kind,
                reason=ignored_reasons.get(file.path, "not_selected_for_media_library"),
            )
        )
    return result


def find_missing_episodes(
    *,
    episode_index: dict[tuple[int, int], dict[str, Any]],
    mapped_episode_keys: set[tuple[int | None, int | None]],
) -> list[MissingEpisode]:
    missing: list[MissingEpisode] = []
    for (season_number, episode_number), episode in sorted(episode_index.items()):
        if (season_number, episode_number) in mapped_episode_keys:
            continue
        missing.append(
            MissingEpisode(
                season_number=season_number,
                episode_number=episode_number,
                episode_name=str(episode.get("name") or ""),
                reason="TMDB episode has no mapped source file.",
            )
        )
    return missing


def find_missing_movies(
    movie_details: dict[str, dict[str, Any]],
    *,
    movie_values: dict[str, dict[str, str]],
    mapped_movie_ids: set[str],
) -> list[MissingMovie]:
    missing: list[MissingMovie] = []
    for movie_id in sorted(movie_details):
        if movie_id in mapped_movie_ids:
            continue
        values = movie_values.get(movie_id) or {}
        missing.append(
            MissingMovie(
                tmdb_movie_id=movie_id,
                title=values.get("title", ""),
                year=values.get("year", ""),
                reason="Selected TMDB movie has no mapped source file.",
            )
        )
    return missing


def review_reason(
    *,
    validated: list[ValidatedMapping],
    rejected: list[RejectedMapping],
    missing_episodes: list[MissingEpisode],
    missing_movies: list[MissingMovie],
    coverage_scope: list[CoverageScope],
) -> str:
    reasons: list[str] = []
    if not validated:
        reasons.append("No validated media mappings were produced.")
    if rejected:
        reasons.append(f"{len(rejected)} mapping decision(s) were rejected.")
    if missing_episodes:
        reasons.append(f"{len(missing_episodes)} TMDB episode(s) are missing.")
    if missing_movies:
        reasons.append(f"{len(missing_movies)} selected movie(s) are missing.")
    if not coverage_scope:
        reasons.append("LLM did not declare coverage scope.")
    if any(
        scope.type == "tv_season"
        and scope.season_number != 0
        and scope.complete is not True
        for scope in coverage_scope
    ):
        reasons.append("TV coverage scope is partial or ambiguous.")
    return " ".join(reasons)


def analysis_result_from_work_plan(
    *,
    item: AnalysisRequestItem,
    work_plan: WorkPlan,
    media_type: MediaType,
    confidence: float,
) -> AnalysisResult:
    status = "needs_review" if review_reason_from_work_plan(work_plan) else "succeeded"
    target = work_plan.library_targets[0] if work_plan.library_targets else None
    selected_original_title = ""
    if work_plan.selected_tv_series:
        selected_original_title = work_plan.selected_tv_series.original_title
    elif work_plan.selected_movies:
        selected_original_title = work_plan.selected_movies[0].original_title
    summary = build_analysis_summary(work_plan, status=status)
    return AnalysisResult(
        id=uuid.uuid4().hex,
        source_name=item.name,
        source_path=item.path,
        status=status,
        confidence=confidence,
        media_type=media_type,
        title=work_plan.work_title,
        original_title=selected_original_title,
        year=target.year if target else "0000",
        tmdb_id=target.tmdb_id if target else "",
        media_target_path=work_plan.primary_media_target_path,
        archive_target_path=work_plan.archive_target_path,
        report_tree=build_work_plan_report_tree(work_plan),
        summary=summary,
        warnings=work_plan.warnings,
        mappings=file_mappings_from_work_plan(work_plan),
        work_plan=work_plan,
    )


def apply_manual_episode_mappings_to_analysis(
    *,
    analysis: AnalysisResult,
    manual_mapping: ManualEpisodeMappingFile,
    config: AppConfig,
) -> AnalysisResult:
    if analysis.work_plan is None or not manual_mapping.episode_mappings:
        return analysis
    work_plan = apply_manual_episode_mappings_to_work_plan(
        work_plan=analysis.work_plan,
        manual_mapping=manual_mapping,
        config=config,
    )
    updated = analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name=analysis.source_name,
            path=analysis.source_path,
            prompt="",
        ),
        work_plan=work_plan,
        media_type=analysis.media_type,
        confidence=analysis.confidence,
    )
    return updated.model_copy(
        update={
            "id": analysis.id,
            "created_at": analysis.created_at,
        }
    )


def apply_manual_episode_mappings_to_work_plan(
    *,
    work_plan: WorkPlan,
    manual_mapping: ManualEpisodeMappingFile,
    config: AppConfig,
) -> WorkPlan:
    tv_target = next(
        (target for target in work_plan.library_targets if target.media_type == "tv"),
        None,
    )
    if tv_target is None or not manual_mapping.episode_mappings:
        return work_plan

    source_files = source_files_from_work_plan(work_plan)
    source_file_by_key = {
        mapping_group_key(relative_parent_path(file.relative_path), file.name): file
        for file in source_files
    }
    manual_source_files: list[tuple[Any, TreeFile]] = []
    for manual_item in manual_mapping.episode_mappings:
        key = mapping_group_key(manual_item.folder_path, manual_item.file_name)
        manual_source_files.append(
            (
                manual_item,
                source_file_by_key.get(key)
                or tree_file_from_manual_mapping(work_plan.source_path, manual_item),
            )
        )

    manual_source_paths = {file.path for _, file in manual_source_files}
    updated = work_plan.model_copy(deep=True)
    episode_meta = episode_metadata_by_key(work_plan)
    retained_mappings = [
        mapping
        for mapping in updated.validated_mappings
        if mapping.source_path not in manual_source_paths
        and mapping.subtitle_for_source_path not in manual_source_paths
    ]
    rejected = list(updated.rejected_mappings)
    used_target_paths = {mapping.target_path for mapping in retained_mappings}
    used_subtitle_sources = {
        mapping.source_path for mapping in retained_mappings if mapping.target_kind == "subtitle"
    }
    source_file_by_path = {file.path: file for file in source_files}
    for _, source_file in manual_source_files:
        source_file_by_path[source_file.path] = source_file

    video_extensions = set(config.scan.video_extensions)
    subtitle_extensions = set(config.scan.subtitle_extensions)
    subtitle_paths = {
        file.path
        for file in source_file_by_path.values()
        if _file_kind(
            file.name,
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        )
        == "subtitle"
    }

    for manual_item, source_file in manual_source_files:
        if (
            _file_kind(
                source_file.name,
                video_extensions=video_extensions,
                subtitle_extensions=subtitle_extensions,
            )
            != "video"
        ):
            rejected.append(
                RejectedMapping(
                    source_path=source_file.path,
                    target_kind="tv_episode",
                    reason="manual_source_is_not_video",
                    details="Manual episode mappings must point at video files.",
                )
            )
            continue

        episode_key = (manual_item.season_number, manual_item.episode_number)
        meta = episode_meta.setdefault(
            episode_key,
            {
                "episode_name": "",
                "tmdb_episode_id": "",
            },
        )
        target_relative_path = tv_episode_target_relative_path(
            title=tv_target.title,
            season_number=manual_item.season_number,
            episode_number=manual_item.episode_number,
            episode_name=str(meta.get("episode_name") or ""),
            extension=PurePosixPath(source_file.name).suffix.lower(),
            include_episode_title=config.media_library.include_episode_title_in_filename,
        )
        target_path = join_openlist_path(tv_target.target_path, target_relative_path)
        if target_path in used_target_paths:
            rejected.append(
                RejectedMapping(
                    source_path=source_file.path,
                    target_kind="tv_episode",
                    reason="duplicate_target_path",
                    details=target_path,
                )
            )
            continue
        video_mapping = ValidatedMapping(
            source_path=source_file.path,
            target_path=target_path,
            target_relative_path=target_relative_path,
            target_kind="tv_episode",
            media_type="tv",
            season_number=manual_item.season_number,
            episode_number=manual_item.episode_number,
            episode_name=str(meta.get("episode_name") or ""),
            tmdb_episode_id=str(meta.get("tmdb_episode_id") or ""),
            reason=manual_item.reason or "Manual episode mapping from review file.",
        )
        retained_mappings.append(video_mapping)
        used_target_paths.add(video_mapping.target_path)

        for subtitle_path in matching_subtitle_source_paths(
            video_source_file=source_file,
            source_files=list(source_file_by_path.values()),
            video_extensions=video_extensions,
            subtitle_extensions=subtitle_extensions,
        ):
            subtitle_mapping = validate_subtitle_decision(
                subtitle_path=subtitle_path,
                video_mapping=video_mapping,
                video_source_file=source_file,
                media_file_by_path=source_file_by_path,
                subtitle_paths=subtitle_paths,
                used_subtitle_sources=used_subtitle_sources,
            )
            if isinstance(subtitle_mapping, RejectedMapping):
                rejected.append(subtitle_mapping)
                continue
            if subtitle_mapping.target_path in used_target_paths:
                rejected.append(
                    RejectedMapping(
                        source_path=subtitle_path,
                        target_kind="subtitle",
                        reason="duplicate_target_path",
                        details=subtitle_mapping.target_path,
                    )
                )
                continue
            retained_mappings.append(subtitle_mapping)
            used_subtitle_sources.add(subtitle_path)
            used_target_paths.add(subtitle_mapping.target_path)

    mapped_episode_keys = {
        (mapping.season_number, mapping.episode_number)
        for mapping in retained_mappings
        if mapping.target_kind == "tv_episode"
        and mapping.season_number is not None
        and mapping.episode_number is not None
    }
    mapped_source_paths = {mapping.source_path for mapping in retained_mappings}
    updated.validated_mappings = retained_mappings
    updated.rejected_mappings = rejected
    updated.missing_tmdb_episodes = [
        MissingEpisode(
            season_number=season_number,
            episode_number=episode_number,
            episode_name=str(meta.get("episode_name") or ""),
            reason="TMDB episode has no mapped source file.",
        )
        for (season_number, episode_number), meta in sorted(episode_meta.items())
        if (season_number, episode_number) not in mapped_episode_keys
    ]
    updated.unmapped_files = [
        item for item in updated.unmapped_files if item.source_path not in mapped_source_paths
    ]
    applied_note = (
        f"Applied {len(manual_mapping.episode_mappings)} manual episode mapping(s)."
    )
    if applied_note not in updated.warnings:
        updated.warnings.append(applied_note)
    updated.needs_user_choice_reason = review_reason(
        validated=updated.validated_mappings,
        rejected=updated.rejected_mappings,
        missing_episodes=updated.missing_tmdb_episodes,
        missing_movies=updated.missing_movies,
        coverage_scope=updated.coverage_scope,
    )
    return updated


def source_files_from_work_plan(work_plan: WorkPlan) -> list[TreeFile]:
    paths: list[str] = []
    paths.extend(mapping.source_path for mapping in work_plan.validated_mappings)
    paths.extend(item.source_path for item in work_plan.unmapped_files)
    result: list[TreeFile] = []
    seen: set[str] = set()
    for source_path in paths:
        if not source_path or source_path in seen:
            continue
        seen.add(source_path)
        result.append(tree_file_from_source_path(source_path, work_plan.source_path))
    return result


def tree_file_from_manual_mapping(source_root: str, manual_item: Any) -> TreeFile:
    folder = normalize_mapping_folder_path(manual_item.folder_path)
    relative_path = f"{folder}/{manual_item.file_name}" if folder else manual_item.file_name
    return TreeFile(
        relative_path=relative_path,
        path=join_openlist_path(source_root, relative_path),
        name=PurePosixPath(manual_item.file_name).name,
    )


def tree_file_from_source_path(source_path: str, source_root: str) -> TreeFile:
    root = source_root.rstrip("/")
    relative_path = PurePosixPath(source_path).name
    if source_path.casefold().startswith(f"{root.casefold()}/"):
        relative_path = source_path[len(root) + 1 :]
    return TreeFile(
        relative_path=relative_path,
        path=source_path,
        name=PurePosixPath(source_path).name,
    )


def episode_metadata_by_key(work_plan: WorkPlan) -> dict[tuple[int, int], dict[str, str]]:
    result: dict[tuple[int, int], dict[str, str]] = {}
    for missing in work_plan.missing_tmdb_episodes:
        result[(missing.season_number, missing.episode_number)] = {
            "episode_name": missing.episode_name,
            "tmdb_episode_id": "",
        }
    for mapping in work_plan.validated_mappings:
        if (
            mapping.target_kind != "tv_episode"
            or mapping.season_number is None
            or mapping.episode_number is None
        ):
            continue
        result[(mapping.season_number, mapping.episode_number)] = {
            "episode_name": mapping.episode_name,
            "tmdb_episode_id": mapping.tmdb_episode_id,
        }
    return result


def review_reason_from_work_plan(work_plan: WorkPlan) -> str:
    return (
        review_reason(
            validated=work_plan.validated_mappings,
            rejected=work_plan.rejected_mappings,
            missing_episodes=work_plan.missing_tmdb_episodes,
            missing_movies=work_plan.missing_movies,
            coverage_scope=work_plan.coverage_scope,
        )
        or work_plan.needs_user_choice_reason
    )


def build_analysis_summary(work_plan: WorkPlan, *, status: str) -> str:
    mapped_video_count = len(
        [mapping for mapping in work_plan.validated_mappings if mapping.target_kind != "subtitle"]
    )
    subtitle_count = len(
        [mapping for mapping in work_plan.validated_mappings if mapping.target_kind == "subtitle"]
    )
    summary = (
        f"{work_plan.work_title}: {mapped_video_count} media file(s), "
        f"{subtitle_count} subtitle(s), {len(work_plan.library_targets)} library target(s)."
    )
    config_blockers = analysis_config_blockers(work_plan.warnings)
    if config_blockers:
        summary = f"{summary} Config issue: {', '.join(config_blockers)}."
    if status != "succeeded":
        reason = review_reason_from_work_plan(work_plan)
        summary = f"{summary} Needs review: {reason}"
    return summary


def analysis_config_blockers(warnings: list[str]) -> list[str]:
    blockers: list[str] = []
    if any("LLM is not configured" in warning for warning in warnings):
        blockers.append("LLM not configured")
    if any("TMDB is not configured" in warning for warning in warnings):
        blockers.append("TMDB not configured")
    if any("No TMDB candidates" in warning for warning in warnings):
        blockers.append("no TMDB candidates")
    return blockers


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_release_name(name: str) -> str:
    text = re.sub(r"\[[^\]]+\]", " ", name)
    text = re.sub(r"\([^)]+\)", " ", text)
    text = text.replace("_", " ").replace(".", " ")
    text = re.sub(
        r"\b(1080p|2160p|720p|ma10p|x264|x265|hevc|aac|flac|bdrip|web-dl)\b", " ", text, flags=re.I
    )
    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return text or name.strip()


def build_fallback_mappings(
    *,
    source_path: str,
    tree_text: str,
    title: str,
    year: str,
    media_type: str,
    media_target_path: str,
) -> list[FileMapping]:
    files = [
        TreeFile(
            relative_path=_line_to_name(raw_line),
            path=join_openlist_path(source_path, _line_to_name(raw_line)),
            name=PurePosixPath(_line_to_name(raw_line)).name,
        )
        for raw_line in tree_text.splitlines()[1:]
        if _line_to_name(raw_line) and not _line_to_name(raw_line).endswith("/")
    ]
    return build_fallback_mappings_from_files(
        source_path=source_path,
        files=files,
        title=title,
        year=year,
        media_type=media_type,
        media_target_path=media_target_path,
    )


def build_fallback_mappings_from_files(
    *,
    source_path: str,
    files: list[TreeFile],
    title: str,
    year: str,
    media_type: str,
    media_target_path: str,
) -> list[FileMapping]:
    mappings: list[FileMapping] = []
    candidates = [
        file for file in files if PurePosixPath(file.name).suffix.lower() in VIDEO_EXTENSIONS
    ]
    if media_type == "movie":
        candidates = _choose_movie_candidates(candidates)
    else:
        candidates = [file for file in candidates if not _looks_like_extra(file.relative_path)]

    used_names: dict[str, int] = {}
    for file in candidates:
        path = PurePosixPath(file.name)
        extension = path.suffix.lower()
        if not extension:
            continue
        episode = _extract_episode(path.stem)
        if media_type == "movie":
            target_name = f"{sanitize_openlist_segment(title)} ({year}){extension}"
        elif episode:
            target_name = f"{sanitize_openlist_segment(title)} - S01E{episode:02d}{extension}"
        else:
            safe_title = sanitize_openlist_segment(title)
            safe_stem = sanitize_openlist_segment(path.stem)
            target_name = f"{safe_title} - {safe_stem}{extension}"
        target_name = _dedupe_target_name(target_name, used_names, file)
        mappings.append(
            FileMapping(
                source_path=file.path or join_openlist_path(source_path, file.relative_path),
                target_relative_path=target_name,
                target_path=join_openlist_path(media_target_path, target_name),
                reason="fallback video-file mapping",
            )
        )
    return mappings


def build_report_tree(
    media_target_path: str, archive_target_path: str, mappings: list[FileMapping]
) -> str:
    media_name = media_target_path.rstrip("/").split("/")[-1]
    archive_name = archive_target_path.rstrip("/").split("/")[-1]
    lines = [
        "dry-run",
        f"|-- media: {media_name}/",
    ]
    for mapping in mappings:
        lines.append(f"|   |-- {mapping.target_relative_path}")
    lines.append(f"`-- archive: {archive_name}/")
    return "\n".join(lines)


def build_fallback_work_plan(
    *,
    source_name: str,
    source_path: str,
    values: dict[str, str],
    media_type: str,
    media_target_path: str,
    archive_target_path: str,
    preview_mappings: list[FileMapping],
    files: list[TreeFile],
    warnings: list[str],
) -> WorkPlan:
    library_type = "movie" if media_type == "movie" else "tv"
    tmdb_id = values.get("tmdb_id", "")
    library_targets = [
        LibraryTarget(
            media_type=library_type,
            target_path=media_target_path,
            title=values["title"],
            year=values["year"],
            tmdb_id=tmdb_id,
        )
    ]
    selected_tv_series = None
    selected_movies: list[SelectedMovie] = []
    coverage_scope: list[CoverageScope] = []
    if library_type == "tv":
        selected_tv_series = (
            SelectedTvSeries(
                tmdb_id=tmdb_id,
                title=values["title"],
                original_title=values["original_title"],
                year=values["year"],
            )
            if tmdb_id
            else None
        )
        coverage_scope.append(
            CoverageScope(type="tv_season", season_number=1, complete=None, note="fallback")
        )
    else:
        if tmdb_id:
            selected_movies.append(
                SelectedMovie(
                    tmdb_id=tmdb_id,
                    title=values["title"],
                    original_title=values["original_title"],
                    year=values["year"],
                )
            )
            coverage_scope.append(CoverageScope(type="movie", tmdb_movie_id=tmdb_id))

    validated: list[ValidatedMapping] = []
    rejected: list[RejectedMapping] = []
    for mapping in preview_mappings:
        target_kind = "movie" if library_type == "movie" else "tv_episode"
        season_number, episode_number = _episode_from_target(mapping.target_relative_path)
        if _is_strictly_valid_fallback_mapping(
            media_type=library_type,
            tmdb_id=tmdb_id,
            season_number=season_number,
            episode_number=episode_number,
        ):
            validated.append(
                ValidatedMapping(
                    source_path=mapping.source_path,
                    target_path=mapping.target_path,
                    target_relative_path=mapping.target_relative_path,
                    target_kind=target_kind,
                    media_type=library_type,
                    season_number=season_number,
                    episode_number=episode_number,
                    tmdb_movie_id=tmdb_id if library_type == "movie" else "",
                    reason=mapping.reason,
                )
            )
        else:
            reason = "tmdb_movie_id_missing" if library_type == "movie" else "tmdb_episode_missing"
            rejected.append(
                RejectedMapping(
                    source_path=mapping.source_path,
                    target_kind=target_kind,
                    reason=reason,
                    details="Fallback preview cannot satisfy strict TMDB validation yet.",
                )
            )

    mapped_paths = {mapping.source_path for mapping in preview_mappings}
    unmapped = []
    for file in files:
        kind = _file_kind(file.name)
        if kind not in {"video", "subtitle"} or file.path in mapped_paths:
            continue
        unmapped.append(
            UnmappedFile(
                source_path=file.path,
                file_kind=kind,
                reason="not_selected_for_media_library",
            )
        )
    return WorkPlan(
        work_title=values["title"],
        source_name=source_name,
        source_path=source_path,
        archive_target_path=archive_target_path,
        coverage_scope=coverage_scope,
        selected_tv_series=selected_tv_series,
        selected_movies=selected_movies,
        library_targets=library_targets,
        validated_mappings=validated,
        rejected_mappings=rejected,
        unmapped_files=unmapped,
        warnings=warnings,
    )


def build_work_plan_report_tree(work_plan: WorkPlan) -> str:
    lines = [
        "dry-run",
        f"|-- archive: {work_plan.archive_target_path}",
        "`-- library-targets",
    ]
    for target_index, target in enumerate(work_plan.library_targets):
        target_connector = "`-- " if target_index == len(work_plan.library_targets) - 1 else "|-- "
        child_prefix = "    " if target_index == len(work_plan.library_targets) - 1 else "|   "
        lines.append(f"    {target_connector}{target.media_type}: {target.target_path}")
        target_mappings = [
            mapping
            for mapping in work_plan.validated_mappings
            if mapping.target_path.startswith(target.target_path.rstrip("/") + "/")
        ]
        if not target_mappings:
            lines.append(f"{child_prefix}`-- no validated media mappings")
        for mapping_index, mapping in enumerate(target_mappings):
            mapping_connector = "`-- " if mapping_index == len(target_mappings) - 1 else "|-- "
            lines.append(f"{child_prefix}{mapping_connector}{mapping.target_relative_path}")
    if work_plan.rejected_mappings:
        lines.append(f"|-- rejected: {len(work_plan.rejected_mappings)}")
    if work_plan.unmapped_files:
        lines.append(f"`-- unmapped: {len(work_plan.unmapped_files)}")
    return "\n".join(lines)


def build_organized_target_tree(work_plan: WorkPlan) -> str:
    lines = [
        "organized-targets",
        "|-- archive",
        f"|   `-- {work_plan.archive_target_path}",
        "`-- library-targets",
    ]
    if not work_plan.library_targets:
        lines.append("    `-- no library targets")
        return "\n".join(lines)

    for target_index, target in enumerate(work_plan.library_targets):
        target_is_last = target_index == len(work_plan.library_targets) - 1
        target_connector = "`-- " if target_is_last else "|-- "
        target_prefix = "    " if target_is_last else "|   "
        lines.append(f"    {target_connector}{target.media_type}: {target.target_path}")
        target_mappings = sorted(
            [
                mapping
                for mapping in work_plan.validated_mappings
                if mapping.target_path.startswith(target.target_path.rstrip("/") + "/")
            ],
            key=lambda mapping: mapping.target_relative_path.lower(),
        )
        if not target_mappings:
            lines.append(f"{target_prefix}`-- no validated media mappings")
            continue
        target_tree_lines = tree_lines_from_relative_paths(
            [mapping.target_relative_path for mapping in target_mappings]
        )
        lines.extend(f"{target_prefix}{line}" for line in target_tree_lines)
    return "\n".join(lines)


def tree_lines_from_relative_paths(paths: list[str]) -> list[str]:
    root: dict[str, Any] = {}
    for path in paths:
        node = root
        for part in PurePosixPath(path).parts:
            if part in {"", "."}:
                continue
            node = node.setdefault(part, {})
    return render_tree_node(root)


def render_tree_node(node: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    items = sorted(node.items(), key=lambda item: item[0].lower())
    for index, (name, child) in enumerate(items):
        is_last = index == len(items) - 1
        connector = "`-- " if is_last else "|-- "
        lines.append(f"{connector}{name}")
        child_prefix = "    " if is_last else "|   "
        lines.extend(f"{child_prefix}{line}" for line in render_tree_node(child))
    return lines


def file_mappings_from_work_plan(work_plan: WorkPlan) -> list[FileMapping]:
    return [
        FileMapping(
            source_path=mapping.source_path,
            target_relative_path=mapping.target_relative_path,
            target_path=mapping.target_path,
            reason=mapping.reason,
        )
        for mapping in work_plan.validated_mappings
    ]


def normalize_mapping_for_plan(
    mapping: FileMapping, *, media_type: str, media_target_path: str
) -> FileMapping:
    if media_type == "movie":
        return mapping
    season_number, _ = _episode_from_target(mapping.target_relative_path)
    season_number = season_number or 1
    target_relative_path = f"Season {season_number}/{mapping.target_relative_path}"
    return FileMapping(
        source_path=mapping.source_path,
        target_relative_path=target_relative_path,
        target_path=join_openlist_path(media_target_path, target_relative_path),
        reason=mapping.reason,
    )


def _template_values(
    title: str,
    media_type: str,
    tmdb_result: dict[str, Any] | None,
    details: dict[str, Any] | None,
    *,
    source_name: str = "",
) -> dict[str, str]:
    source = details or tmdb_result or {}
    result_title = (
        source.get("name")
        or source.get("title")
        or source.get("original_name")
        or source.get("original_title")
        or title
    )
    original_title = source.get("original_name") or source.get("original_title") or ""
    first_date = source.get("first_air_date") or source.get("release_date") or ""
    year = str(first_date)[:4] if first_date else "0000"
    external = source.get("external_ids") or {}
    return {
        "title": sanitize_openlist_segment(str(result_title)),
        "original_title": sanitize_openlist_segment(str(original_title)),
        "year": year or "0000",
        "tmdb_id": str(source.get("id") or ""),
        "tvdb_id": str(external.get("tvdb_id") or ""),
        "media_type": media_type,
        "source_name": sanitize_openlist_segment(source_name),
    }


def _line_to_name(line: str) -> str:
    return re.sub(r"^[|` -]+", "", line).strip()


def _episode_from_target(target_relative_path: str) -> tuple[int | None, int | None]:
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", target_relative_path)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _is_strictly_valid_fallback_mapping(
    *, media_type: str, tmdb_id: str, season_number: int | None, episode_number: int | None
) -> bool:
    if media_type == "movie":
        return bool(tmdb_id)
    return bool(tmdb_id and season_number is not None and episode_number is not None)


def _file_kind(
    name: str,
    *,
    video_extensions: set[str] | None = None,
    subtitle_extensions: set[str] | None = None,
) -> str:
    video_extensions = normalize_extensions(video_extensions or VIDEO_EXTENSIONS)
    subtitle_extensions = normalize_extensions(subtitle_extensions or SUBTITLE_EXTENSIONS)
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in video_extensions:
        return "video"
    if suffix in subtitle_extensions:
        return "subtitle"
    return "other"


def infer_media_type(name: str) -> ExpectedComponent:
    lowered = name.lower()
    movie_markers = ["movie", "gekijouban", "劇場版", "theater", "ova"]
    return "movie" if any(marker in lowered for marker in movie_markers) else "tv"


def _choose_movie_candidates(candidates: list[TreeFile]) -> list[TreeFile]:
    non_extra = [file for file in candidates if not _looks_like_extra(file.relative_path)]
    pool = non_extra or candidates
    top_level = [file for file in pool if "/" not in file.relative_path]
    pool = top_level or pool
    if not pool:
        return []
    return [max(pool, key=lambda file: (file.size, -len(file.relative_path)))]


def _looks_like_extra(relative_path: str) -> bool:
    lowered = relative_path.lower()
    markers = ["/sps/", "sps/", "[cm", "[menu]", "[preview]", "nced", "ncop", "pv"]
    return any(marker in lowered for marker in markers)


def _dedupe_target_name(target_name: str, used_names: dict[str, int], file: TreeFile) -> str:
    count = used_names.get(target_name, 0)
    used_names[target_name] = count + 1
    if count == 0:
        return target_name
    path = PurePosixPath(target_name)
    source_stem = sanitize_openlist_segment(PurePosixPath(file.name).stem)
    return f"{path.stem} - {source_stem}{path.suffix}"


def _extract_episode(stem: str) -> int | None:
    match = _extract_episode_match(stem)
    if match:
        return int(match.group(1))
    return None


def _extract_episode_match(stem: str) -> re.Match[str] | None:
    candidates: list[tuple[int, int, int, re.Match[str]]] = []
    for pattern_index, pattern in enumerate(EPISODE_NUMBER_PATTERNS):
        for match in re.finditer(pattern, stem):
            value = int(match.group(1))
            if 0 < value < 200:
                candidates.append(
                    (
                        _episode_match_score(stem, match),
                        -pattern_index,
                        match.start(),
                        match,
                    )
                )
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3]


def _episode_match_score(stem: str, match: re.Match[str]) -> int:
    start, end = match.span(1)
    before = stem[start - 1] if start > 0 else ""
    after = stem[end] if end < len(stem) else ""
    score = 0
    if not before or not before.isalnum():
        score += 1
    if not after or not after.isalnum():
        score += 1
    return score
