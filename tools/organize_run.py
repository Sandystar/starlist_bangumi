from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlist_bangumi.clients import OpenListClient
from starlist_bangumi.config import DEFAULT_CONFIG_PATH, ConfigManager
from starlist_bangumi.exceptions import AppError
from starlist_bangumi.models import AnalysisRequestItem, AnalysisResult, OrganizeOptions, WorkPlan
from starlist_bangumi.services.executor import Executor
from starlist_bangumi.services.plan_builder import (
    analysis_result_from_work_plan,
    build_organized_target_tree,
    find_missing_episodes,
    review_reason_from_work_plan,
    tmdb_episode_index,
)
from starlist_bangumi.run_index import ensure_archive_target_source_leaf


class RunLogger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.log_path = run_dir / "organize_log.txt"
        self.jsonl_path = run_dir / "organize_log.jsonl"
        self.events: list[dict[str, Any]] = []

    async def log(self, stage: str, progress: int, message: str) -> None:
        event = {
            "at": datetime.now(UTC).isoformat(),
            "stage": stage,
            "progress": progress,
            "message": message,
        }
        self.events.append(event)
        line = f"{event['at']} [{progress:03d}%] {stage}: {message}"
        print(line)
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
        with self.jsonl_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False) + "\n")


async def main() -> None:
    args = parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        raise SystemExit(f"Run folder does not exist: {run_dir}")

    config = ConfigManager(Path(args.config)).load()
    analysis = load_analysis(run_dir)
    save_analysis_result(run_dir, analysis)
    options = OrganizeOptions(
        allow_failed_analysis=args.allow_failed_analysis,
        delete_target_before=args.delete_target_before,
        overwrite_archive_target_before=args.overwrite_archive_target_before,
        delete_source_after=args.delete_source_after,
        resume_existing=args.resume_existing,
    )
    logger = RunLogger(run_dir)
    executor = Executor(OpenListClient(config.openlist))

    started_at = time.perf_counter()
    await write_status(
        run_dir,
        status="running",
        analysis=analysis,
        options=options,
        elapsed_seconds=0,
    )
    try:
        await logger.log("start", 0, f"Organizing run folder: {run_dir}")
        await logger.log("start", 0, f"Source: {analysis.source_path}")
        await logger.log("start", 0, f"Archive target: {analysis.archive_target_path}")
        await executor.organize(analysis, options, logger.log)
    except Exception as exc:
        elapsed = round(time.perf_counter() - started_at, 3)
        await write_status(
            run_dir,
            status="failed",
            analysis=analysis,
            options=options,
            elapsed_seconds=elapsed,
            error=exception_payload(exc),
        )
        await logger.log("failed", 100, exception_message(exc))
        raise

    elapsed = round(time.perf_counter() - started_at, 3)
    await write_status(
        run_dir,
        status="succeeded",
        analysis=analysis,
        options=options,
        elapsed_seconds=elapsed,
    )
    await logger.log("complete", 100, f"Organize completed in {elapsed:.3f}s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute OpenList organize for a CLI run folder.")
    parser.add_argument(
        "run_dir",
        help="Run folder containing analysis_result.json or work_plan.json",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON")
    parser.add_argument(
        "--allow-failed-analysis",
        action="store_true",
        help="Confirm organizing a needs-review analysis result; failed analyses are never organized",
    )
    parser.add_argument(
        "--delete-target-before",
        action="store_true",
        help="Delete existing same-name media-library target files before organizing",
    )
    parser.add_argument(
        "--overwrite-archive-target-before",
        action="store_true",
        help="Delete the run-specific archive target folder before archiving",
    )
    parser.add_argument(
        "--delete-source-after",
        action="store_true",
        help="Delete source folder after archive and media-library verification succeeds",
    )
    parser.add_argument(
        "--resume-existing",
        action="store_true",
        help="Resume a previous organize attempt by skipping already-created final targets",
    )
    return parser.parse_args()


def load_analysis(run_dir: Path) -> AnalysisResult:
    analysis_path = first_existing(
        [
            run_dir / "artifacts" / "analysis_result.json",
            run_dir / "analysis_result.json",
        ]
    )
    if analysis_path.exists():
        analysis = AnalysisResult.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        backfill_missing_from_run_artifacts(run_dir, analysis)
        analysis = refresh_analysis_from_work_plan(analysis)
        ensure_archive_target_source_leaf(analysis)
        return analysis

    work_plan_path = first_existing(
        [
            run_dir / "artifacts" / "work_plan.json",
            run_dir / "work_plan.json",
        ]
    )
    if not work_plan_path.exists():
        raise SystemExit(
            "Run folder must contain analysis_result.json or work_plan.json "
            "at the root or under artifacts/"
        )
    work_plan = WorkPlan.model_validate_json(work_plan_path.read_text(encoding="utf-8"))
    media_type = "movie" if work_plan.selected_movies and not work_plan.selected_tv_series else "tv"
    analysis = analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name=work_plan.source_name,
            path=work_plan.source_path,
            prompt="",
        ),
        work_plan=work_plan,
        media_type=media_type,
        confidence=0.92 if work_plan.validated_mappings else 0.55,
    )
    ensure_archive_target_source_leaf(analysis)
    return analysis


def backfill_missing_from_run_artifacts(run_dir: Path, analysis: AnalysisResult) -> None:
    if analysis.work_plan is None:
        return
    season_details_path = first_existing(
        [
            run_dir / "artifacts" / "tmdb_season_details.raw.json",
            run_dir / "artifacts" / "tmdb_season_details.prompt.json",
        ]
    )
    if not season_details_path.exists():
        return
    season_details = load_season_details_artifact(season_details_path)
    if not season_details:
        return
    mapped_episode_keys = {
        (mapping.season_number, mapping.episode_number)
        for mapping in analysis.work_plan.validated_mappings
        if mapping.target_kind == "tv_episode"
        and mapping.season_number is not None
        and mapping.episode_number is not None
    }
    analysis.work_plan.missing_tmdb_episodes = find_missing_episodes(
        episode_index=tmdb_episode_index(season_details),
        mapped_episode_keys=mapped_episode_keys,
    )


def load_season_details_artifact(path: Path) -> dict[int, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for key, value in raw.items():
        try:
            season_number = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            result[season_number] = value
    return result


def refresh_analysis_from_work_plan(analysis: AnalysisResult) -> AnalysisResult:
    if analysis.work_plan is None:
        return analysis
    refreshed = analysis_result_from_work_plan(
        item=AnalysisRequestItem(
            name=analysis.source_name,
            path=analysis.source_path,
            prompt="",
        ),
        work_plan=analysis.work_plan,
        media_type=analysis.media_type,
        confidence=analysis.confidence,
    )
    refreshed.id = analysis.id
    refreshed.analysis_version = analysis.analysis_version
    refreshed.created_at = analysis.created_at
    return refreshed


def first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def save_analysis_result(run_dir: Path, analysis: AnalysisResult) -> None:
    artifact_dir = run_dir / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "analysis_result.json").write_text(
        analysis.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


async def write_status(
    run_dir: Path,
    *,
    status: str,
    analysis: AnalysisResult,
    options: OrganizeOptions,
    elapsed_seconds: float,
    error: dict[str, Any] | None = None,
) -> None:
    payload = {
        "status": status,
        "updated_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "source_path": analysis.source_path,
        "archive_target_path": analysis.archive_target_path,
        "media_target_path": analysis.media_target_path,
        "mapping_count": len(analysis.mappings),
        "options": options.model_dump(mode="json"),
        "error": error or {},
    }
    (run_dir / "organize_status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if analysis.work_plan:
        (run_dir / "artifacts" / "organized_target_tree.txt").write_text(
            build_organized_target_tree(analysis.work_plan),
            encoding="utf-8",
        )
        review_reason = review_reason_from_work_plan(analysis.work_plan)
        (run_dir / "analysis_status.json").write_text(
            json.dumps(
                {
                    "status": "needs_review" if review_reason else "succeeded",
                    "review_reason": review_reason,
                    "validated": len(analysis.work_plan.validated_mappings),
                    "rejected": len(analysis.work_plan.rejected_mappings),
                    "missing_tmdb_episodes": len(analysis.work_plan.missing_tmdb_episodes),
                    "missing_movies": len(analysis.work_plan.missing_movies),
                    "unmapped_files": len(analysis.work_plan.unmapped_files),
                    "organize_status": status,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def exception_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, AppError):
        payload["details"] = exc.details
    return payload


def exception_message(exc: Exception) -> str:
    payload = exception_payload(exc)
    if payload.get("details"):
        return f"{payload['message']}: {json.dumps(payload['details'], ensure_ascii=False)}"
    return str(payload["message"])


if __name__ == "__main__":
    asyncio.run(main())
