from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from starlist_bangumi.cli_runs import (
    analysis_media_type,
    analysis_status_payload,
    create_debug_paths,
    write_json,
    write_model,
    write_text,
)
from starlist_bangumi.config import PROJECT_ROOT
from starlist_bangumi.exceptions import AppError
from starlist_bangumi.models import (
    AnalysisRequestItem,
    AnalysisResult,
    ManualEpisodeMappingFile,
    OrganizeOptions,
)
from starlist_bangumi.services.plan_builder import (
    apply_manual_episode_mappings_to_analysis,
    build_organized_target_tree,
    build_work_plan_report_tree,
    review_reason_from_work_plan,
)

WebTaskKind = Literal["analysis", "organize"]
WebTaskScope = Literal["all", "active", "completed"]
WebTaskStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]


class WebTaskLog(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stage: str
    progress: int
    message: str


@dataclass
class WebTask:
    id: str
    kind: WebTaskKind
    source_name: str
    source_path: str = ""
    status: WebTaskStatus = "queued"
    stage: str = "queued"
    progress: int = 0
    run_id: str = ""
    run_dir: str = ""
    media_target_path: str = ""
    archive_target_path: str = ""
    analysis_status: str = ""
    organize_status: str = ""
    error: str = ""
    item: AnalysisRequestItem | None = None
    options: OrganizeOptions = field(default_factory=OrganizeOptions)
    logs: list[WebTaskLog] = field(default_factory=list)

    async def log(self, stage: str, progress: int, message: str) -> None:
        self.stage = stage
        self.progress = progress
        self.logs.append(WebTaskLog(stage=stage, progress=progress, message=message))

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "source_name": self.source_name,
            "source_path": self.source_path,
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "media_target_path": self.media_target_path,
            "archive_target_path": self.archive_target_path,
            "analysis_status": self.analysis_status,
            "organize_status": self.organize_status,
            "error": self.error,
            "options": self.options.model_dump(mode="json"),
            "logs": [log.model_dump(mode="json") for log in self.logs[-80:]],
        }


class WebTaskManager:
    """Small in-process serial queue for WebUI analysis and organize actions."""

    def __init__(self, runtime: Any, *, run_root: Path | None = None) -> None:
        self._runtime = runtime
        self._run_root = run_root or PROJECT_ROOT / "data" / "runs"
        self._tasks: dict[str, WebTask] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def submit_analysis(self, items: list[AnalysisRequestItem]) -> list[WebTask]:
        tasks: list[WebTask] = []
        for item in items:
            task = WebTask(
                id=uuid.uuid4().hex,
                kind="analysis",
                source_name=item.name,
                source_path=item.path,
                item=item,
            )
            await task.log("queued", 0, "Analysis queued")
            self._tasks[task.id] = task
            await self._queue.put(task.id)
            tasks.append(task)
        self._ensure_worker()
        return tasks

    async def submit_organize(
        self,
        run_ids: list[str],
        options: OrganizeOptions,
    ) -> list[WebTask]:
        tasks: list[WebTask] = []
        for run_id in run_ids:
            summary = self._runtime.run_index.summarize(self._runtime.run_index.root / run_id)
            task = WebTask(
                id=uuid.uuid4().hex,
                kind="organize",
                source_name=summary.source_name if summary else run_id,
                source_path=summary.source_path if summary else "",
                run_id=run_id,
                run_dir=summary.run_dir if summary else str(self._runtime.run_index.root / run_id),
                media_target_path="\n".join(summary.library_targets) if summary else "",
                archive_target_path=summary.archive_target_path if summary else "",
                analysis_status=str(summary.analysis_status) if summary else "",
                options=options,
            )
            self._write_organize_request(Path(task.run_dir), task.options)
            await task.log("queued", 0, "Organize queued")
            self._tasks[task.id] = task
            await self._queue.put(task.id)
            tasks.append(task)
        self._ensure_worker()
        return tasks

    async def retry(self, task_id: str) -> WebTask:
        old = self._tasks.get(task_id)
        if old is None and task_id.startswith("persisted-organize-"):
            run_id = task_id.removeprefix("persisted-organize-")
            persisted = self._persisted_organize_task_for(run_id)
            options = retry_organize_options(persisted.options if persisted else OrganizeOptions())
            return (await self.submit_organize([run_id], options))[0]
        if old is None:
            raise KeyError(task_id)
        if old.kind == "analysis" and old.item is not None:
            return (await self.submit_analysis([old.item]))[0]
        if old.kind == "organize" and old.run_id:
            return (
                await self.submit_organize(
                    [old.run_id],
                    retry_organize_options(old.options),
                )
            )[0]
        raise ValueError(f"Task cannot be retried: {task_id}")

    async def retry_failed(self, *, kind: WebTaskKind | None = None) -> list[WebTask]:
        failed = [
            task
            for task in self._tasks.values()
            if task.status == "failed" and (kind is None or task.kind == kind)
        ]
        retried: list[WebTask] = []
        for task in failed:
            retried.append(await self.retry(task.id))
        if kind in (None, "organize"):
            for task in self._persisted_organize_tasks():
                if task.status in {"failed", "interrupted"}:
                    options = retry_organize_options(task.options)
                    retried.append(
                        (await self.submit_organize([task.run_id], options))[0]
                    )
        return retried

    def list_tasks(
        self,
        kind: WebTaskKind | None = None,
        *,
        scope: WebTaskScope = "all",
    ) -> list[dict[str, Any]]:
        tasks: list[WebTask] = [
            task
            for task in self._tasks.values()
            if (kind is None or task.kind == kind) and task_matches_scope(task, scope)
        ]
        if kind in (None, "organize"):
            in_memory_run_ids = {
                task.run_id for task in tasks if task.kind == "organize" and task.run_id
            }
            tasks.extend(
                task
                for task in self._persisted_organize_tasks(scope=scope)
                if task.run_id not in in_memory_run_ids
            )
        tasks.sort(key=lambda task: task.logs[0].at if task.logs else datetime.min, reverse=True)
        return [task.public() for task in tasks[:100]]

    def _persisted_organize_tasks(self, *, scope: WebTaskScope = "all") -> list[WebTask]:
        summaries = self._runtime.run_index.list_runs()
        tasks: list[WebTask] = []
        for summary in summaries:
            run_dir = Path(summary.run_dir)
            status_path = run_dir / "organize_status.json"
            if not status_path.exists():
                continue
            status_data = load_json_object(status_path)
            status = str(status_data.get("status") or summary.organize_status or "not_started")
            if status == "not_started":
                continue
            task_status = persisted_task_status(status)
            if not status_matches_scope(task_status, scope):
                continue
            task = WebTask(
                id=f"persisted-organize-{summary.run_id}",
                kind="organize",
                source_name=summary.source_name or summary.run_id,
                source_path=summary.source_path,
                status=task_status,
                stage=persisted_task_stage(status, run_dir),
                progress=persisted_task_progress(status, run_dir),
                run_id=summary.run_id,
                run_dir=summary.run_dir,
                media_target_path=str(
                    status_data.get("media_target_path")
                    or "\n".join(summary.library_targets)
                    or ""
                ),
                archive_target_path=str(
                    status_data.get("archive_target_path") or summary.archive_target_path or ""
                ),
                analysis_status=str(summary.analysis_status),
                organize_status=status,
                error=persisted_task_error(status_data),
                options=load_organize_options(run_dir, status_data),
                logs=load_organize_logs(run_dir),
            )
            if task.status == "interrupted" and not task.error:
                task.error = "服务重启或进程退出时任务仍处于运行状态。"
            tasks.append(task)
        return tasks

    def _persisted_organize_task_for(self, run_id: str) -> WebTask | None:
        for task in self._persisted_organize_tasks():
            if task.run_id == run_id:
                return task
        return None

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_worker())

    async def _run_worker(self) -> None:
        while not self._queue.empty():
            task_id = await self._queue.get()
            task = self._tasks.get(task_id)
            if task is None:
                self._queue.task_done()
                continue
            try:
                if task.kind == "analysis":
                    await self._run_analysis(task)
                else:
                    await self._run_organize(task)
            except Exception as exc:  # pragma: no cover - exercised through API behavior
                if not (task.status == "failed" and task.error):
                    task.status = "failed"
                    task.progress = 100
                    task.stage = "failed"
                    task.error = exception_message(exc)
                    await task.log("failed", 100, task.error)
            finally:
                self._queue.task_done()

    async def _run_analysis(self, task: WebTask) -> None:
        if task.item is None:
            raise ValueError("Analysis task is missing input item")
        task.status = "running"
        paths = create_debug_paths(self._run_root, task.item.name)
        task.run_id = paths.root.name
        task.run_dir = str(paths.root)
        await task.log("prepare", 2, f"Created run folder: {paths.root}")
        write_text(paths.artifact_dir / "extra_prompt.txt", task.item.prompt)
        write_json(
            paths.artifact_dir / "run_input.json",
            {
                "folder_name": task.item.name,
                "source_path": task.item.path,
                "config_path": str(self._runtime.config_manager.path.resolve()),
                "manual_tv_id": task.item.tv_tmdb_id,
                "manual_movie_ids": task.item.movie_tmdb_ids,
                "selection_mode": "web",
            },
        )

        started_at = time.perf_counter()
        try:
            analysis = await self._runtime.plan_builder.analyze(task.item, log=task.log)
            elapsed = round(time.perf_counter() - started_at, 3)
            task.analysis_status = analysis.status
            task.media_target_path = analysis.media_target_path
            task.archive_target_path = analysis.archive_target_path
            await self._save_analysis_outputs(task, paths.root, analysis, elapsed)
            task.status = "succeeded"
            await task.log("complete", 100, f"Analysis completed with status: {analysis.status}")
        except Exception as exc:
            elapsed = round(time.perf_counter() - started_at, 3)
            task.status = "failed"
            task.progress = 100
            task.stage = "failed"
            task.analysis_status = "failed"
            task.error = exception_message(exc)
            await task.log("failed", 100, task.error)
            await self._save_failed_analysis_outputs(task, paths.root, exc, elapsed)
            raise

    async def _save_analysis_outputs(
        self,
        task: WebTask,
        run_dir: Path,
        analysis: AnalysisResult,
        elapsed_seconds: float,
    ) -> None:
        artifact_dir = run_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if analysis.work_plan is not None:
            work_plan = analysis.work_plan
            write_model(artifact_dir / "work_plan.json", work_plan)
            write_text(
                artifact_dir / "dry_run_result_tree.txt",
                build_work_plan_report_tree(work_plan),
            )
            write_text(
                artifact_dir / "organized_target_tree.txt",
                build_organized_target_tree(work_plan),
            )
            review_reason = review_reason_from_work_plan(work_plan)
            status_payload = analysis_status_payload(work_plan, review_reason)
        else:
            status_payload = {
                "status": analysis.status,
                "review_reason": "",
                "validated": len(analysis.mappings),
                "rejected": 0,
                "missing_tmdb_episodes": 0,
                "missing_movies": 0,
                "unmapped_files": 0,
                "library_targets": [],
                "archive_target_path": analysis.archive_target_path,
            }
        status_payload["source_path"] = analysis.source_path
        status_payload["elapsed_seconds"] = elapsed_seconds
        status_payload["media_type"] = (
            analysis_media_type(analysis.work_plan)
            if analysis.work_plan
            else analysis.media_type
        )
        write_model(artifact_dir / "analysis_result.json", analysis)
        write_json(artifact_dir / "validation_summary.json", status_payload)
        write_json(run_dir / "analysis_status.json", status_payload)
        write_json(
            run_dir / "timings.json",
            [{"stage": "web.analysis", "elapsed_seconds": elapsed_seconds}],
        )
        write_json(
            run_dir / "manifest.json",
            {
                "run_dir": str(run_dir),
                "task_id": task.id,
                "created_by": "web",
                "elapsed_seconds": elapsed_seconds,
            },
        )

    async def _save_failed_analysis_outputs(
        self,
        task: WebTask,
        run_dir: Path,
        exc: Exception,
        elapsed_seconds: float,
    ) -> None:
        artifact_dir = run_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        item = task.item
        if item is None:
            return
        error_payload = exception_payload(exc)
        error_message = exception_message(exc)
        failed_analysis = AnalysisResult(
            id=task.id,
            source_name=item.name,
            source_path=item.path,
            status="failed",
            confidence=0,
            media_type="unknown",
            title=item.name,
            original_title="",
            year="0000",
            tmdb_id="",
            tvdb_id="",
            media_target_path="",
            archive_target_path="",
            report_tree="",
            summary=error_message,
            warnings=[error_message],
            mappings=[],
            work_plan=None,
        )
        write_model(artifact_dir / "analysis_result.json", failed_analysis)
        status_payload = {
            "status": "failed",
            "review_reason": error_message,
            "validated": 0,
            "validated_videos": 0,
            "validated_subtitles": 0,
            "rejected": 0,
            "missing_tmdb_episodes": 0,
            "missing_movies": 0,
            "unmapped_files": 0,
            "library_targets": [],
            "archive_target_path": "",
            "source_path": item.path,
            "elapsed_seconds": elapsed_seconds,
            "media_type": "unknown",
            "error": error_payload,
        }
        write_json(artifact_dir / "validation_summary.json", status_payload)
        write_json(run_dir / "analysis_status.json", status_payload)
        write_json(
            run_dir / "timings.json",
            [{"stage": "web.analysis", "elapsed_seconds": elapsed_seconds}],
        )
        write_json(
            run_dir / "manifest.json",
            {
                "run_dir": str(run_dir),
                "task_id": task.id,
                "created_by": "web",
                "elapsed_seconds": elapsed_seconds,
                "status": "failed",
            },
        )

    async def _run_organize(self, task: WebTask) -> None:
        task.status = "running"
        task.organize_status = "running"
        run_dir = Path(task.run_dir or self._runtime.run_index.root / task.run_id)
        analysis = self._load_analysis_for_organize(task.run_id, run_dir)
        task.media_target_path = "\n".join(
            target.target_path for target in analysis.work_plan.library_targets
        ) if analysis.work_plan else analysis.media_target_path
        task.archive_target_path = analysis.archive_target_path
        started_at = time.perf_counter()
        await self._write_organize_status(
            run_dir,
            "running",
            analysis,
            task.options,
            0,
            current_task=task,
        )
        try:
            await task.log("start", 0, f"Organizing run folder: {run_dir}")
            await self._runtime.executor.organize(analysis, task.options, task.log)
        except Exception as exc:
            elapsed = round(time.perf_counter() - started_at, 3)
            task.status = "failed"
            task.progress = 100
            task.stage = "failed"
            task.error = exception_message(exc)
            await task.log("failed", 100, task.error)
            await self._write_organize_status(
                run_dir,
                "failed",
                analysis,
                task.options,
                elapsed,
                error=exception_payload(exc),
                current_task=task,
            )
            raise
        elapsed = round(time.perf_counter() - started_at, 3)
        await task.log("complete", 100, f"Organize completed in {elapsed:.3f}s")
        await self._write_organize_status(
            run_dir,
            "succeeded",
            analysis,
            task.options,
            elapsed,
            current_task=task,
        )
        task.organize_status = "succeeded"
        task.status = "succeeded"

    def _load_analysis_for_organize(self, run_id: str, run_dir: Path) -> AnalysisResult:
        analysis = self._runtime.run_index.load_analysis(run_id)
        manual_mapping_path = run_dir / "artifacts" / "manual_episode_mappings.json"
        if not manual_mapping_path.exists() or analysis.work_plan is None:
            return analysis
        manual_mapping = ManualEpisodeMappingFile.model_validate_json(
            manual_mapping_path.read_text(encoding="utf-8")
        )
        updated = apply_manual_episode_mappings_to_analysis(
            analysis=analysis,
            manual_mapping=manual_mapping,
            config=self._runtime.config,
        )
        if updated == analysis:
            return analysis
        artifact_dir = run_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        write_model(artifact_dir / "analysis_result.json", updated)
        if updated.work_plan is not None:
            write_model(artifact_dir / "work_plan.json", updated.work_plan)
            write_text(
                artifact_dir / "dry_run_result_tree.txt",
                build_work_plan_report_tree(updated.work_plan),
            )
            write_text(
                artifact_dir / "organized_target_tree.txt",
                build_organized_target_tree(updated.work_plan),
            )
            review_reason = review_reason_from_work_plan(updated.work_plan)
            status_payload = analysis_status_payload(updated.work_plan, review_reason)
            existing_validation_summary = artifact_dir / "validation_summary.json"
            elapsed_seconds = 0.0
            if existing_validation_summary.exists():
                try:
                    payload = json.loads(existing_validation_summary.read_text(encoding="utf-8"))
                    elapsed_seconds = float(payload.get("elapsed_seconds") or 0.0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    elapsed_seconds = 0.0
            status_payload["elapsed_seconds"] = elapsed_seconds
            status_payload["source_path"] = updated.source_path
            status_payload["media_type"] = analysis_media_type(updated.work_plan)
            write_json(artifact_dir / "validation_summary.json", status_payload)
            write_json(run_dir / "analysis_status.json", status_payload)
        return updated

    async def _write_organize_status(
        self,
        run_dir: Path,
        status: str,
        analysis: AnalysisResult,
        options: OrganizeOptions,
        elapsed_seconds: float,
        *,
        error: dict[str, Any] | None = None,
        current_task: WebTask | None = None,
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
        write_json(run_dir / "organize_status.json", payload)
        task_log = self._organize_task_log_for(run_dir, current_task=current_task)
        if task_log:
            write_text(
                run_dir / "organize_log.txt",
                "\n".join(
                    f"{log.at.isoformat()} [{log.progress:03d}%] {log.stage}: {log.message}"
                    for log in task_log
                )
                + "\n",
            )
            (run_dir / "organize_log.jsonl").write_text(
                "".join(log.model_dump_json() + "\n" for log in task_log),
                encoding="utf-8",
            )
        if analysis.work_plan:
            write_text(
                run_dir / "artifacts" / "organized_target_tree.txt",
                build_organized_target_tree(analysis.work_plan),
            )

    def _write_organize_request(self, run_dir: Path, options: OrganizeOptions) -> None:
        if not run_dir:
            return
        write_json(
            run_dir / "organize_request.json",
            {
                "updated_at": datetime.now(UTC).isoformat(),
                "options": options.model_dump(mode="json"),
            },
        )

    def _organize_task_log_for(
        self,
        run_dir: Path,
        *,
        current_task: WebTask | None = None,
    ) -> list[WebTaskLog]:
        if current_task is not None:
            return current_task.logs
        run_dir_text = str(run_dir)
        for task in reversed(list(self._tasks.values())):
            if task.kind == "organize" and task.run_dir == run_dir_text:
                return task.logs
        return []


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


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def load_organize_options(run_dir: Path, status_data: dict[str, Any]) -> OrganizeOptions:
    request_data = load_json_object(run_dir / "organize_request.json")
    raw_options = request_data.get("options") or status_data.get("options") or {}
    return OrganizeOptions.model_validate(raw_options)


def retry_organize_options(options: OrganizeOptions) -> OrganizeOptions:
    return options.model_copy(
        update={
            "resume_existing": True,
            "delete_target_before": False,
            "overwrite_archive_target_before": False,
        }
    )


def load_organize_logs(run_dir: Path) -> list[WebTaskLog]:
    jsonl_path = run_dir / "organize_log.jsonl"
    if not jsonl_path.exists():
        return []
    logs: list[WebTaskLog] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            logs.append(WebTaskLog.model_validate_json(line))
        except Exception:
            continue
    return logs[-80:]


def task_matches_scope(task: WebTask, scope: WebTaskScope) -> bool:
    return status_matches_scope(task.status, scope)


def status_matches_scope(status: WebTaskStatus, scope: WebTaskScope) -> bool:
    if scope == "active":
        return status not in {"succeeded", "failed", "interrupted"}
    if scope == "completed":
        return status in {"succeeded", "failed", "interrupted"}
    return True


def persisted_task_status(status: str) -> WebTaskStatus:
    if status == "running":
        return "interrupted"
    if status in {"succeeded", "failed", "interrupted"}:
        return status  # type: ignore[return-value]
    return "failed"


def persisted_task_stage(status: str, run_dir: Path) -> str:
    logs = load_organize_logs(run_dir)
    if status == "running":
        return last_non_terminal_stage(logs)
    if logs:
        return logs[-1].stage
    if status in {"succeeded", "failed", "interrupted"}:
        return status
    return "failed"


def persisted_task_progress(status: str, run_dir: Path) -> int:
    logs = load_organize_logs(run_dir)
    if status == "succeeded":
        return 100
    if status == "failed":
        return 100
    if logs:
        return logs[-1].progress
    return 0


def last_non_terminal_stage(logs: list[WebTaskLog]) -> str:
    for log in reversed(logs):
        if log.stage not in {"failed", "complete"}:
            return log.stage
    return "start"


def persisted_task_error(status_data: dict[str, Any]) -> str:
    error = status_data.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        details = error.get("details")
        if details:
            return f"{message}: {json.dumps(details, ensure_ascii=False)}"
        return message
    return ""
