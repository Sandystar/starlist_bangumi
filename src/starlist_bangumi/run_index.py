from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from starlist_bangumi.models import AnalysisResult, AnalysisStatus
from starlist_bangumi.pathing import join_openlist_path, split_openlist_path

RunPhase = Literal["analysis", "organized", "unknown"]
OrganizeStatusValue = Literal["not_started", "running", "succeeded", "failed", "interrupted"]


class RunSummary(BaseModel):
    run_id: str
    run_dir: str
    created_at: datetime
    updated_at: datetime
    phase: RunPhase = "unknown"
    selection_mode: str = ""
    source_name: str = ""
    source_path: str = ""
    title: str = ""
    year: str = "0000"
    media_type: str = "unknown"
    analysis_status: AnalysisStatus | Literal["unknown"] = "unknown"
    organize_status: OrganizeStatusValue = "not_started"
    review_reason: str = ""
    summary: str = ""
    library_targets: list[str] = Field(default_factory=list)
    archive_target_path: str = ""
    validated: int = 0
    rejected: int = 0
    missing_tmdb_episodes: int = 0
    missing_movies: int = 0
    unmapped_files: int = 0
    has_manual_episode_mappings: bool = False
    source_run_dir: str = ""
    error: str = ""


class RunIndexFilters(BaseModel):
    status: str = ""
    organize_status: str = ""
    source: str = ""
    latest_only: bool = False
    limit: int = Field(default=100, ge=1, le=1000)


class RunIndex:
    """Read-only index over file-based CLI run folders."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def list_runs(self, filters: RunIndexFilters | None = None) -> list[RunSummary]:
        filters = filters or RunIndexFilters()
        runs = [summary for path in self._run_dirs() if (summary := self.summarize(path))]
        runs.sort(key=lambda run: run.created_at, reverse=True)
        runs = self._apply_filters(runs, filters)
        if filters.latest_only:
            runs = latest_runs_by_source(runs)
        return runs[: filters.limit]

    def summarize(self, run_dir: Path) -> RunSummary | None:
        if not run_dir.is_dir():
            return None
        artifact_dir = run_dir / "artifacts"
        analysis = load_analysis_result(artifact_dir / "analysis_result.json")
        analysis_status = load_json_object(run_dir / "analysis_status.json")
        organize_status = load_json_object(run_dir / "organize_status.json")
        manifest = load_json_object(run_dir / "manifest.json")
        run_input = load_json_object(artifact_dir / "run_input.json")
        work_plan = load_json_object(artifact_dir / "work_plan.json")
        manual_remap = load_json_object(artifact_dir / "manual_remap_request.json")
        files = self._summary_files(run_dir)
        created_at = infer_created_at(run_dir, files, manifest)
        updated_at = max_file_mtime(files) or created_at
        library_targets = library_targets_from(analysis, analysis_status, work_plan)
        source_name = source_name_from(analysis, run_input, run_dir)
        source_path = str(
            value_from_sources(
                analysis.source_path if analysis else "",
                run_input.get("source_path"),
                analysis_status.get("source_path"),
            )
        )
        archive_target = str(
            value_from_sources(
                analysis.archive_target_path if analysis else "",
                analysis_status.get("archive_target_path"),
                work_plan.get("archive_target_path"),
                organize_status.get("archive_target_path"),
            )
        )
        archive_target = archive_target_with_source_leaf(archive_target, source_path)
        return RunSummary(
            run_id=run_dir.name,
            run_dir=str(run_dir),
            created_at=created_at,
            updated_at=updated_at,
            phase="organized" if organize_status else "analysis",
            selection_mode=str(run_input.get("selection_mode") or ""),
            source_name=source_name,
            source_path=source_path,
            title=str(
                value_from_sources(
                    analysis.title if analysis else "",
                    work_plan.get("work_title"),
                )
            ),
            year=str(
                value_from_sources(
                    analysis.year if analysis else "",
                    first_target_year(work_plan),
                    "0000",
                )
            ),
            media_type=str(
                value_from_sources(
                    analysis.media_type if analysis else "",
                    first_target_media_type(work_plan),
                    "unknown",
                )
            ),
            analysis_status=analysis_status_value(analysis, analysis_status),
            organize_status=organize_status_value(organize_status),
            review_reason=str(analysis_status.get("review_reason") or ""),
            summary=str(
                value_from_sources(
                    analysis.summary if analysis else "",
                    analysis_status.get("summary"),
                )
            ),
            library_targets=library_targets,
            archive_target_path=archive_target,
            validated=integer_from_sources(
                analysis_status.get("validated"),
                count_work_plan(work_plan, "validated_mappings"),
            ),
            rejected=integer_from_sources(
                analysis_status.get("rejected"),
                count_work_plan(work_plan, "rejected_mappings"),
            ),
            missing_tmdb_episodes=integer_from_sources(
                analysis_status.get("missing_tmdb_episodes"),
                count_work_plan(work_plan, "missing_tmdb_episodes"),
            ),
            missing_movies=integer_from_sources(
                analysis_status.get("missing_movies"),
                count_work_plan(work_plan, "missing_movies"),
            ),
            unmapped_files=integer_from_sources(
                analysis_status.get("unmapped_files"),
                count_work_plan(work_plan, "unmapped_files"),
            ),
            has_manual_episode_mappings=(artifact_dir / "manual_episode_mappings.json").exists(),
            source_run_dir=str(
                manual_remap.get("source_run_dir")
                or analysis_status.get("source_run_dir")
                or ""
            ),
            error=organize_error(organize_status),
        )

    def load_analysis(self, run_id: str) -> AnalysisResult:
        run_dir = self.root / run_id
        analysis = load_analysis_result(run_dir / "artifacts" / "analysis_result.json")
        if analysis is None:
            raise FileNotFoundError(f"analysis_result.json not found for run: {run_id}")
        ensure_archive_target_source_leaf(analysis)
        return analysis

    def _run_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [path for path in self.root.iterdir() if path.is_dir()]

    def _apply_filters(
        self, runs: list[RunSummary], filters: RunIndexFilters
    ) -> list[RunSummary]:
        filtered = runs
        if filters.status:
            filtered = [run for run in filtered if run.analysis_status == filters.status]
        if filters.organize_status:
            filtered = [run for run in filtered if run.organize_status == filters.organize_status]
        if filters.source:
            source_key = filters.source.casefold()
            filtered = [
                run
                for run in filtered
                if source_key in run.source_name.casefold()
                or source_key in run.source_path.casefold()
            ]
        return filtered

    def _summary_files(self, run_dir: Path) -> list[Path]:
        artifact_dir = run_dir / "artifacts"
        candidates = [
            run_dir / "manifest.json",
            run_dir / "analysis_status.json",
            run_dir / "organize_status.json",
            run_dir / "timings.json",
            artifact_dir / "analysis_result.json",
            artifact_dir / "work_plan.json",
            artifact_dir / "run_input.json",
            artifact_dir / "manual_remap_request.json",
        ]
        return [path for path in candidates if path.exists()]


def latest_runs_by_source(runs: list[RunSummary]) -> list[RunSummary]:
    latest: dict[str, RunSummary] = {}
    without_source: list[RunSummary] = []
    for run in runs:
        key = run.source_path or run.source_name
        if not key:
            without_source.append(run)
            continue
        if key not in latest:
            latest[key] = run
    return list(latest.values()) + without_source


def load_analysis_result(path: Path) -> AnalysisResult | None:
    if not path.exists():
        return None
    try:
        return AnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def ensure_archive_target_source_leaf(analysis: AnalysisResult) -> None:
    updated = archive_target_with_source_leaf(analysis.archive_target_path, analysis.source_path)
    analysis.archive_target_path = updated
    if analysis.work_plan is not None:
        analysis.work_plan.archive_target_path = updated


def archive_target_with_source_leaf(archive_target_path: str, source_path: str) -> str:
    _, source_name = split_openlist_path(source_path)
    if not archive_target_path or not source_name:
        return archive_target_path
    _, archive_leaf = split_openlist_path(archive_target_path)
    if archive_leaf == source_name:
        return archive_target_path
    return join_openlist_path(archive_target_path, source_name)


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def infer_created_at(
    run_dir: Path,
    files: list[Path],
    manifest: dict[str, Any],
) -> datetime:
    run_dir_value = str(manifest.get("run_dir") or "")
    timestamp = timestamp_from_name(Path(run_dir_value).name or run_dir.name)
    if timestamp:
        return timestamp
    mtimes = [datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) for path in files]
    if mtimes:
        return min(mtimes)
    return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=UTC)


def timestamp_from_name(name: str) -> datetime | None:
    match = re.match(r"(?P<date>\d{8})-(?P<time>\d{6})", name)
    if not match:
        return None
    value = f"{match.group('date')}{match.group('time')}"
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").astimezone()
    except ValueError:
        return None


def max_file_mtime(files: list[Path]) -> datetime | None:
    if not files:
        return None
    return max(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC) for path in files)


def source_name_from(
    analysis: AnalysisResult | None,
    run_input: dict[str, Any],
    run_dir: Path,
) -> str:
    return str(
        value_from_sources(
            analysis.source_name if analysis else "",
            run_input.get("folder_name"),
            run_dir.name,
        )
    )


def value_from_sources(*values: object) -> object:
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def integer_from_sources(*values: object) -> int:
    for value in values:
        try:
            if value not in (None, ""):
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def count_work_plan(work_plan: dict[str, Any], key: str) -> int:
    value = work_plan.get(key)
    return len(value) if isinstance(value, list) else 0


def first_target_year(work_plan: dict[str, Any]) -> str:
    target = first_library_target(work_plan)
    return str(target.get("year") or "")


def first_target_media_type(work_plan: dict[str, Any]) -> str:
    target = first_library_target(work_plan)
    return str(target.get("media_type") or "")


def first_library_target(work_plan: dict[str, Any]) -> dict[str, Any]:
    targets = work_plan.get("library_targets")
    if isinstance(targets, list) and targets and isinstance(targets[0], dict):
        return targets[0]
    return {}


def library_targets_from(
    analysis: AnalysisResult | None,
    analysis_status: dict[str, Any],
    work_plan: dict[str, Any],
) -> list[str]:
    targets = analysis_status.get("library_targets")
    if isinstance(targets, list):
        paths = [
            str(target.get("target_path") or "")
            for target in targets
            if isinstance(target, dict) and target.get("target_path")
        ]
        if paths:
            return paths
    plan_targets = work_plan.get("library_targets")
    if isinstance(plan_targets, list):
        paths = [
            str(target.get("target_path") or "")
            for target in plan_targets
            if isinstance(target, dict) and target.get("target_path")
        ]
        if paths:
            return paths
    if analysis and analysis.work_plan:
        return [target.target_path for target in analysis.work_plan.library_targets]
    if analysis and analysis.media_target_path:
        return [analysis.media_target_path]
    return []


def analysis_status_value(
    analysis: AnalysisResult | None,
    analysis_status: dict[str, Any],
) -> AnalysisStatus | Literal["unknown"]:
    value = str(
        value_from_sources(
            analysis_status.get("status"),
            analysis.status if analysis else "",
        )
    )
    if value in {"succeeded", "needs_review", "failed"}:
        return value  # type: ignore[return-value]
    return "unknown"


def organize_status_value(organize_status: dict[str, Any]) -> OrganizeStatusValue:
    value = str(organize_status.get("status") or "")
    if value in {"running", "succeeded", "failed", "interrupted"}:
        return value  # type: ignore[return-value]
    return "not_started"


def organize_error(organize_status: dict[str, Any]) -> str:
    error = organize_status.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        details = error.get("details")
        if details:
            return f"{message}: {json.dumps(details, ensure_ascii=False)}"
        return message
    return ""
