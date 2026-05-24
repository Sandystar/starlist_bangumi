from __future__ import annotations

import shutil
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from starlist_bangumi.run_index import RunIndex, RunSummary


class RunCleanupOptions(BaseModel):
    status: str = ""
    organize_status: str = ""
    older_than_days: int | None = Field(default=None, ge=0)
    keep_latest_per_source: int | None = Field(default=None, ge=0)
    include_manual: bool = False
    all: bool = False
    execute: bool = False

    @model_validator(mode="after")
    def validate_filter_present(self) -> RunCleanupOptions:
        if self.all:
            return self
        has_filter = any(
            [
                self.status,
                self.organize_status,
                self.older_than_days is not None,
                self.keep_latest_per_source is not None,
            ]
        )
        if not has_filter:
            raise ValueError("Run cleanup requires at least one filter or all=True")
        return self


class CleanupCandidate(BaseModel):
    run_id: str
    run_dir: str
    source_path: str = ""
    analysis_status: str = ""
    organize_status: str = ""
    created_at: datetime
    has_manual_episode_mappings: bool = False
    reason: str


class CleanupResult(BaseModel):
    executed: bool
    candidates: list[CleanupCandidate] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    failed: list[dict[str, str]] = Field(default_factory=list)


class RunCleaner:
    """Selects and optionally deletes file-based run folders."""

    def __init__(self, run_index: RunIndex) -> None:
        self._run_index = run_index

    def cleanup(self, options: RunCleanupOptions) -> CleanupResult:
        candidates = self.preview(options)
        result = CleanupResult(executed=options.execute, candidates=candidates)
        if not options.execute:
            return result
        for candidate in candidates:
            try:
                ensure_inside_root(candidate.run_dir, self._run_index.root)
                shutil.rmtree(candidate.run_dir)
                result.deleted.append(candidate.run_id)
            except Exception as exc:  # pragma: no cover - platform-specific failure path
                result.failed.append(
                    {
                        "run_id": candidate.run_id,
                        "run_dir": candidate.run_dir,
                        "error": str(exc),
                    }
                )
        return result

    def preview(self, options: RunCleanupOptions) -> list[CleanupCandidate]:
        runs = self._run_index.list_runs()
        protected_latest = latest_run_ids_per_source(
            runs,
            keep_count=options.keep_latest_per_source,
        )
        candidates: list[CleanupCandidate] = []
        for run in runs:
            reasons = candidate_reasons(
                run,
                options=options,
                protected_latest=protected_latest,
            )
            if reasons:
                candidates.append(
                    CleanupCandidate(
                        run_id=run.run_id,
                        run_dir=run.run_dir,
                        source_path=run.source_path,
                        analysis_status=str(run.analysis_status),
                        organize_status=str(run.organize_status),
                        created_at=run.created_at,
                        has_manual_episode_mappings=run.has_manual_episode_mappings,
                        reason="; ".join(reasons),
                    )
                )
        return candidates


def candidate_reasons(
    run: RunSummary,
    *,
    options: RunCleanupOptions,
    protected_latest: set[str],
) -> list[str]:
    if run.run_id in protected_latest:
        return []
    if run.has_manual_episode_mappings and not options.include_manual:
        return []

    reasons: list[str] = []
    if options.all:
        reasons.append("all")
    if options.status:
        if run.analysis_status != options.status:
            return []
        reasons.append(f"analysis_status={options.status}")
    if options.organize_status:
        if run.organize_status != options.organize_status:
            return []
        reasons.append(f"organize_status={options.organize_status}")
    if options.older_than_days is not None:
        cutoff = datetime.now(UTC).astimezone() - timedelta(days=options.older_than_days)
        if run.created_at > cutoff:
            return []
        reasons.append(f"older_than_days={options.older_than_days}")
    if options.keep_latest_per_source is not None:
        reasons.append(f"outside_latest_per_source={options.keep_latest_per_source}")
    return reasons


def latest_run_ids_per_source(
    runs: list[RunSummary],
    *,
    keep_count: int | None,
) -> set[str]:
    if keep_count is None:
        return set()
    by_source: dict[str, list[RunSummary]] = defaultdict(list)
    for run in runs:
        key = run.source_path or run.source_name or run.run_id
        by_source[key].append(run)
    protected: set[str] = set()
    for source_runs in by_source.values():
        ordered = sorted(source_runs, key=lambda run: run.created_at, reverse=True)
        protected.update(run.run_id for run in ordered[:keep_count])
    return protected


def ensure_inside_root(run_dir: str, root: object) -> None:
    root_path = Path(str(root)).resolve()
    run_path = Path(run_dir).resolve()
    if run_path == root_path or root_path not in run_path.parents:
        raise ValueError(f"Refusing to delete path outside run root: {run_path}")
