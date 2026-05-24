from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from starlist_bangumi.clients import LlmClient, OpenListClient
from starlist_bangumi.config import AppConfig, ConfigManager, LlmConfig, OpenListConfig
from starlist_bangumi.exceptions import AppError
from starlist_bangumi.models import (
    AnalysisRequestItem,
    ManualEpisodeMapping,
    ManualEpisodeMappingFile,
    OrganizeOptions,
)
from starlist_bangumi.run_index import RunIndexFilters
from starlist_bangumi.run_cleanup import ensure_inside_root
from starlist_bangumi.services.runtime import AppRuntime


class AnalysisSubmitRequest(BaseModel):
    items: list[AnalysisRequestItem]


class OrganizeSubmitRequest(BaseModel):
    run_id: str = ""
    run_ids: list[str] = []
    analysis_id: str = ""
    analysis_ids: list[str] = []
    options: OrganizeOptions = Field(default_factory=OrganizeOptions)


class ManualEpisodeMappingSubmitRequest(BaseModel):
    source_path: str
    season_number: int = Field(ge=0)
    episode_number: int = Field(ge=1)
    reason: str = "Manual mapping from WebUI."


class RunDeleteRequest(BaseModel):
    run_ids: list[str] = Field(default_factory=list)


def create_app(config_path: Path | None = None) -> FastAPI:
    manager = ConfigManager(config_path or ConfigManager().path)
    runtime = AppRuntime(manager)
    app = FastAPI(title="Starlist Bangumi", version="0.1.0")
    app.state.runtime = runtime

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def get_config() -> AppConfig:
        return runtime.config_manager.get()

    @app.get("/api/runtime/config-status")
    async def runtime_config_status() -> dict[str, object]:
        saved = runtime.config_manager.get()
        active = runtime.config
        return {
            "restart_required": active.model_dump(mode="json") != saved.model_dump(mode="json"),
            "active": {
                "llm_configured": bool(
                    active.llm.base_url and active.llm.api_key and active.llm.model
                ),
                "tmdb_configured": bool(active.tmdb.api_key),
                "openlist_configured": bool(
                    active.openlist.base_url
                    and active.openlist.username
                    and active.openlist.password
                ),
            },
            "saved": {
                "llm_configured": bool(
                    saved.llm.base_url and saved.llm.api_key and saved.llm.model
                ),
                "tmdb_configured": bool(saved.tmdb.api_key),
                "openlist_configured": bool(
                    saved.openlist.base_url
                    and saved.openlist.username
                    and saved.openlist.password
                ),
            },
        }

    @app.put("/api/config")
    async def save_config(config: AppConfig) -> AppConfig:
        saved = runtime.config_manager.save(config)
        return saved

    @app.post("/api/config/test-openlist")
    async def test_openlist(config: OpenListConfig | None = None) -> dict[str, object]:
        client = OpenListClient(config or runtime.config.openlist)
        return await _guard(client.test_connection())

    @app.get("/api/llm/models")
    async def llm_models() -> dict[str, list[str]]:
        return {"models": await _guard(runtime.llm.list_models())}

    @app.post("/api/llm/models")
    async def llm_models_with_config(config: LlmConfig | None = None) -> dict[str, list[str]]:
        client = LlmClient(config or runtime.config.llm)
        return {"models": await _guard(client.list_models())}

    @app.post("/api/scans")
    async def scan_source() -> dict[str, object]:
        return {"items": await _guard(runtime.scanner.scan_first_level())}

    @app.post("/api/analysis")
    async def submit_analysis(request: AnalysisSubmitRequest) -> dict[str, object]:
        tasks = await runtime.web_tasks.submit_analysis(request.items)
        return {"accepted": True, "tasks": [task.public() for task in tasks]}

    @app.get("/api/analysis-tasks")
    async def list_analysis_tasks() -> dict[str, object]:
        return {"tasks": runtime.web_tasks.list_tasks(kind="analysis")}

    @app.get("/api/runs")
    async def list_runs(
        latest_only: bool = False,
        limit: int = 100,
        status: str = "",
        organize_status: str = "",
        source: str = "",
    ) -> dict[str, object]:
        runs = runtime.run_index.list_runs(
            RunIndexFilters(
                latest_only=latest_only,
                limit=limit,
                status=status,
                organize_status=organize_status,
                source=source,
            )
        )
        return {"runs": [run.model_dump(mode="json") for run in runs]}

    @app.delete("/api/runs")
    async def delete_runs(request: RunDeleteRequest) -> dict[str, object]:
        run_ids = unique_non_empty(request.run_ids)
        if not run_ids:
            raise HTTPException(status_code=400, detail="No run_ids provided")
        deleted: list[str] = []
        failed: list[dict[str, str]] = []
        root = runtime.run_index.root.resolve()
        for run_id in run_ids:
            run_dir = (root / run_id).resolve()
            try:
                ensure_inside_root(str(run_dir), root)
                if not run_dir.exists() or not run_dir.is_dir():
                    raise FileNotFoundError(f"Run not found: {run_id}")
                shutil.rmtree(run_dir)
                deleted.append(run_id)
            except Exception as exc:
                failed.append({"run_id": run_id, "error": str(exc)})
        return {"deleted": deleted, "failed": failed}

    @app.get("/api/results")
    async def list_results(latest_only: bool = False, limit: int = 100) -> dict[str, object]:
        result_limit = max(1, min(limit, 1000))
        runs = runtime.run_index.list_runs(RunIndexFilters(latest_only=False, limit=1000))
        results = []
        seen_sources: set[str] = set()
        for run in runs:
            try:
                analysis = runtime.run_index.load_analysis(run.run_id)
            except FileNotFoundError:
                continue
            if latest_only:
                source_key = analysis.source_path or run.source_path or run.run_id
                if source_key in seen_sources:
                    continue
                seen_sources.add(source_key)
            result = analysis.model_dump(mode="json")
            result["run_id"] = run.run_id
            result["run_dir"] = run.run_dir
            results.append(result)
            if len(results) >= result_limit:
                break
        return {"results": results}

    @app.get("/api/runs/{run_id}/manual-episode-mappings")
    async def get_manual_episode_mappings(run_id: str) -> ManualEpisodeMappingFile:
        path = manual_episode_mapping_path(runtime, run_id)
        if not path.exists():
            return default_manual_episode_mapping_file(runtime, run_id)
        return ManualEpisodeMappingFile.model_validate_json(path.read_text(encoding="utf-8"))

    @app.post("/api/runs/{run_id}/manual-episode-mappings")
    async def save_manual_episode_mapping(
        run_id: str, request: ManualEpisodeMappingSubmitRequest
    ) -> ManualEpisodeMappingFile:
        path = manual_episode_mapping_path(runtime, run_id)
        mapping_file = (
            ManualEpisodeMappingFile.model_validate_json(path.read_text(encoding="utf-8"))
            if path.exists()
            else default_manual_episode_mapping_file(runtime, run_id)
        )
        analysis = runtime.run_index.load_analysis(run_id)
        mapping = manual_episode_mapping_from_source_path(
            source_root=analysis.source_path,
            request=request,
        )
        mapping_file.episode_mappings = [
            item
            for item in mapping_file.episode_mappings
            if (item.season_number, item.episode_number)
            != (mapping.season_number, mapping.episode_number)
        ]
        mapping_file.episode_mappings.append(mapping)
        mapping_file.episode_mappings.sort(
            key=lambda item: (item.season_number, item.episode_number)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(mapping_file.model_dump_json(indent=2), encoding="utf-8")
        return mapping_file

    @app.get("/api/tasks")
    async def list_tasks(scope: str = "all") -> dict[str, object]:
        if scope not in {"all", "active", "completed"}:
            scope = "all"
        return {"tasks": runtime.web_tasks.list_tasks(kind="organize", scope=scope)}

    @app.post("/api/organize")
    async def submit_organize(request: OrganizeSubmitRequest) -> dict[str, object]:
        run_ids = resolve_organize_run_ids(runtime, request)
        tasks = await runtime.web_tasks.submit_organize(run_ids, request.options)
        return {"accepted": True, "tasks": [task.public() for task in tasks]}

    @app.post("/api/organize/batch")
    async def submit_batch_organize(request: OrganizeSubmitRequest) -> dict[str, object]:
        run_ids = resolve_organize_run_ids(runtime, request)
        tasks = await runtime.web_tasks.submit_organize(run_ids, request.options)
        return {"accepted": True, "tasks": [task.public() for task in tasks]}

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: str) -> dict[str, object]:
        try:
            task = await runtime.web_tasks.retry(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"accepted": True, "task": task.public()}

    @app.post("/api/tasks/retry-failed")
    async def retry_failed() -> dict[str, object]:
        tasks = await runtime.web_tasks.retry_failed(kind="organize")
        return {"accepted": True, "tasks": [task.public() for task in tasks]}

    static_dir = Path(__file__).with_name("static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.mount("/", StaticFiles(directory=static_dir), name="static")
    return app


async def _guard(awaitable):
    try:
        return await awaitable
    except AppError as exc:
        raise HTTPException(
            status_code=400, detail={"message": str(exc), "details": exc.details}
        ) from exc


def resolve_organize_run_ids(runtime: AppRuntime, request: OrganizeSubmitRequest) -> list[str]:
    run_ids = [value for value in [request.run_id, *request.run_ids] if value]
    analysis_ids = [value for value in [request.analysis_id, *request.analysis_ids] if value]
    if analysis_ids:
        runs = runtime.run_index.list_runs(RunIndexFilters(limit=1000))
        by_analysis_id = {}
        for run in runs:
            try:
                analysis = runtime.run_index.load_analysis(run.run_id)
            except FileNotFoundError:
                continue
            by_analysis_id[analysis.id] = run.run_id
        run_ids.extend(by_analysis_id[value] for value in analysis_ids if value in by_analysis_id)
    unique: list[str] = []
    seen: set[str] = set()
    for run_id in run_ids:
        if run_id not in seen:
            unique.append(run_id)
            seen.add(run_id)
    if not unique:
        raise HTTPException(status_code=400, detail="No run_id or analysis_id provided")
    return unique


def unique_non_empty(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def manual_episode_mapping_path(runtime: AppRuntime, run_id: str) -> Path:
    root = runtime.run_index.root.resolve()
    run_dir = (root / run_id).resolve()
    if root not in [run_dir, *run_dir.parents]:
        raise HTTPException(status_code=400, detail="Invalid run_id")
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run_dir / "artifacts" / "manual_episode_mappings.json"


def default_manual_episode_mapping_file(
    runtime: AppRuntime, run_id: str
) -> ManualEpisodeMappingFile:
    analysis = runtime.run_index.load_analysis(run_id)
    work_plan = analysis.work_plan
    tv_tmdb_id = (
        work_plan.selected_tv_series.tmdb_id
        if work_plan and work_plan.selected_tv_series
        else ""
    )
    movie_tmdb_ids = [
        movie.tmdb_id for movie in work_plan.selected_movies
    ] if work_plan else []
    return ManualEpisodeMappingFile(tv_tmdb_id=tv_tmdb_id, movie_tmdb_ids=movie_tmdb_ids)


def manual_episode_mapping_from_source_path(
    *,
    source_root: str,
    request: ManualEpisodeMappingSubmitRequest,
) -> ManualEpisodeMapping:
    root = source_root.rstrip("/")
    source_path = request.source_path.strip()
    prefix = f"{root}/"
    if not source_path.startswith(prefix):
        raise HTTPException(status_code=400, detail="Source path is outside this run source folder")
    relative_path = source_path[len(prefix) :].strip("/")
    path = PurePosixPath(relative_path)
    folder_path = "" if path.parent.as_posix() == "." else path.parent.as_posix()
    if not path.name:
        raise HTTPException(status_code=400, detail="Source path does not point to a file")
    return ManualEpisodeMapping(
        folder_path=folder_path,
        file_name=path.name,
        season_number=request.season_number,
        episode_number=request.episode_number,
        reason=request.reason,
    )
