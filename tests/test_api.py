import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from starlist_bangumi.api import create_app
from starlist_bangumi.config import AppConfig, ConfigManager
from starlist_bangumi.exceptions import ExternalServiceError
from starlist_bangumi.models import (
    AnalysisRequestItem,
    CoverageScope,
    LibraryTarget,
    MissingEpisode,
    OrganizeOptions,
    SelectedTvSeries,
    UnmappedFile,
    WorkPlan,
)
from starlist_bangumi.run_index import RunIndex
from starlist_bangumi.services.plan_builder import analysis_result_from_work_plan
from starlist_bangumi.services.web_tasks import WebTask, WebTaskManager
from tests.test_run_index import write_run


def test_submit_analysis_creates_web_analysis_task(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())

    with TestClient(create_app(config_path)) as client:
        app = client.app
        app.state.runtime.web_tasks.submit_analysis = AsyncMock(return_value=[])
        response = client.post(
            "/api/analysis",
            json={
                "items": [
                    {
                        "name": "Example",
                        "path": "/Inbox/Example",
                        "prompt": "",
                        "tv_tmdb_id": "100",
                        "movie_tmdb_ids": ["200"],
                    }
                ]
            },
        )
        assert response.status_code == 200
        payload = response.json()

        assert payload["accepted"] is True
        app.state.runtime.web_tasks.submit_analysis.assert_awaited_once()
        submitted_items = app.state.runtime.web_tasks.submit_analysis.await_args.args[0]
        assert submitted_items[0].tv_tmdb_id == "100"
        assert submitted_items[0].movie_tmdb_ids == ["200"]


def test_tasks_endpoint_lists_only_organize_web_tasks(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())

    with TestClient(create_app(config_path)) as client:
        client.app.state.runtime.web_tasks.list_tasks = lambda kind=None, scope="all": [
            {"id": "organize-1", "kind": kind, "scope": scope}
        ]
        response = client.get("/api/tasks")

        assert response.status_code == 200
        assert response.json()["tasks"] == [
            {"id": "organize-1", "kind": "organize", "scope": "all"}
        ]


def test_tasks_endpoint_lists_persisted_organize_runs_after_restart(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    run_dir = write_run(
        run_root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    (run_dir / "organize_status.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "updated_at": "2026-05-19T01:05:00+00:00",
                "source_path": "/Inbox/Example",
                "archive_target_path": "/Archive/Example Canonical/Example",
                "media_target_path": "/Movies/Example (2024) [tmdbid=1]",
                "options": {"resume_existing": True},
                "error": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "organize_log.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "at": "2026-05-19T01:00:00Z",
                        "stage": "preflight",
                        "progress": 5,
                        "message": "Checking target paths",
                    }
                ),
                json.dumps(
                    {
                        "at": "2026-05-19T01:05:00Z",
                        "stage": "complete",
                        "progress": 100,
                        "message": "Organize operation completed",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.get("/api/tasks")

    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert tasks[0]["id"] == "persisted-organize-20260519-010203-Example"
    assert tasks[0]["status"] == "succeeded"
    assert tasks[0]["stage"] == "complete"
    assert tasks[0]["logs"][0]["stage"] == "preflight"


def test_tasks_endpoint_scope_active_skips_completed_runs(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    write_run(
        run_root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    app.state.runtime.web_tasks._tasks["active-1"] = WebTask(
        id="active-1",
        kind="organize",
        source_name="Example",
        run_id="active-1",
        status="running",
        logs=[],
    )

    with TestClient(app) as client:
        response = client.get("/api/tasks?scope=active")

    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert [task["id"] for task in tasks] == ["active-1"]


def test_tasks_endpoint_marks_persisted_running_run_as_interrupted(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    run_dir = write_run(
        run_root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    (run_dir / "organize_status.json").write_text(
        json.dumps(
            {
                "status": "running",
                "updated_at": "2026-05-19T01:05:00+00:00",
                "source_path": "/Inbox/Example",
                "archive_target_path": "/Archive/Example Canonical/Example",
                "media_target_path": "/Movies/Example (2024) [tmdbid=1]",
                "options": {},
                "error": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "organize_log.jsonl").write_text(
        json.dumps(
            {
                "at": "2026-05-19T01:05:00Z",
                "stage": "library_copy",
                "progress": 63,
                "message": "Copying media",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.get("/api/tasks")

    assert response.status_code == 200
    task = response.json()["tasks"][0]
    assert task["status"] == "interrupted"
    assert task["stage"] == "library_copy"
    assert task["progress"] == 63
    assert "仍处于运行状态" in task["error"]


def test_retry_failed_persisted_organize_uses_resume_without_cleanup(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    run_dir = write_run(
        run_root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    (run_dir / "organize_status.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "updated_at": "2026-05-19T01:05:00+00:00",
                "source_path": "/Inbox/Example",
                "archive_target_path": "/Archive/Example Canonical/Example",
                "media_target_path": "/Movies/Example (2024) [tmdbid=1]",
                "options": {
                    "allow_failed_analysis": True,
                    "delete_target_before": True,
                    "overwrite_archive_target_before": True,
                    "delete_source_after": True,
                },
                "error": {"message": "OpenList request failed after retries"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.post("/api/tasks/retry-failed")

    assert response.status_code == 200
    task = response.json()["tasks"][0]
    assert task["options"]["allow_failed_analysis"] is True
    assert task["options"]["resume_existing"] is True
    assert task["options"]["delete_target_before"] is False
    assert task["options"]["overwrite_archive_target_before"] is False
    assert task["options"]["delete_source_after"] is True


def test_retry_persisted_organize_restores_saved_confirmation_options(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    run_dir = write_run(
        run_root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="needs_review",
    )
    (run_dir / "organize_request.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-05-19T01:00:00+00:00",
                "options": {
                    "allow_failed_analysis": True,
                    "delete_target_before": True,
                    "overwrite_archive_target_before": True,
                    "delete_source_after": False,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "organize_status.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "updated_at": "2026-05-19T01:05:00+00:00",
                "source_path": "/Inbox/Example",
                "archive_target_path": "/Archive/Example Canonical/Example",
                "media_target_path": "/Movies/Example (2024) [tmdbid=1]",
                "options": {
                    "allow_failed_analysis": False,
                    "resume_existing": True,
                },
                "error": {"message": "Needs-review analysis result requires explicit confirmation"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.post("/api/tasks/persisted-organize-20260519-010203-Example/retry")

    assert response.status_code == 200
    options = response.json()["task"]["options"]
    assert options["allow_failed_analysis"] is True
    assert options["resume_existing"] is True
    assert options["delete_target_before"] is False
    assert options["overwrite_archive_target_before"] is False


def test_write_organize_status_uses_current_task_logs(tmp_path: Path) -> None:
    runtime = SimpleNamespace()
    manager = WebTaskManager(runtime)
    run_dir = tmp_path / "runs" / "20260519-010203-Example"
    run_dir.mkdir(parents=True)
    old = WebTask(
        id="old",
        kind="organize",
        source_name="Example",
        run_id="20260519-010203-Example",
        run_dir=str(run_dir),
    )
    old.logs = []
    current = WebTask(
        id="current",
        kind="organize",
        source_name="Example",
        run_id="20260519-010203-Example",
        run_dir=str(run_dir),
    )
    manager._tasks = {"old": old, "current": current}

    import anyio

    async def run() -> None:
        await old.log("failed", 100, "old failed log")
        await current.log("start", 0, "current start")
        await manager._write_organize_status(
            run_dir,
            "failed",
            write_run_analysis(run_dir),
            options=OrganizeOptions(),
            elapsed_seconds=1,
            current_task=current,
        )

    anyio.run(run)

    log_text = (run_dir / "organize_log.jsonl").read_text(encoding="utf-8")
    assert "current start" in log_text
    assert "old failed log" not in log_text


def test_write_organize_status_persists_current_failed_log(tmp_path: Path) -> None:
    manager = WebTaskManager(SimpleNamespace())
    run_dir = tmp_path / "runs" / "20260519-010203-Example"
    run_dir.mkdir(parents=True)
    task = WebTask(
        id="current",
        kind="organize",
        source_name="Example",
        run_id="20260519-010203-Example",
        run_dir=str(run_dir),
    )

    import anyio

    async def run() -> None:
        await task.log("start", 0, "current start")
        await task.log("failed", 100, "current failed")
        await manager._write_organize_status(
            run_dir,
            "failed",
            write_run_analysis(run_dir),
            options=OrganizeOptions(),
            elapsed_seconds=1,
            current_task=task,
        )

    anyio.run(run)

    log_text = (run_dir / "organize_log.jsonl").read_text(encoding="utf-8")
    assert "current start" in log_text
    assert "current failed" in log_text


@pytest.mark.asyncio
async def test_web_organize_applies_manual_episode_mappings_before_executor(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"
    run_dir = write_tv_review_run(run_root, "20260519-040506-Example")
    (run_dir / "artifacts" / "manual_episode_mappings.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "tv_tmdb_id": "100",
                "movie_tmdb_ids": [],
                "episode_mappings": [
                    {
                        "folder_path": "Season 1",
                        "file_name": "Example 02.mkv",
                        "season_number": 1,
                        "episode_number": 2,
                        "reason": "Manual mapping from WebUI.",
                    }
                ],
                "notes": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    executor = SimpleNamespace(organize=AsyncMock())
    runtime = SimpleNamespace(
        run_index=RunIndex(run_root),
        config=AppConfig(),
        executor=executor,
    )
    manager = WebTaskManager(runtime, run_root=run_root)
    task = WebTask(
        id="organize-1",
        kind="organize",
        source_name="Example",
        run_id=run_dir.name,
        run_dir=str(run_dir),
        options=OrganizeOptions(allow_failed_analysis=True),
    )

    await manager._run_organize(task)

    organized_analysis = executor.organize.await_args.args[0]
    assert any(
        mapping.source_path.endswith("/Season 1/Example 02.mkv")
        and mapping.target_relative_path == "Season 01/Example - S01E02 - Second.mkv"
        for mapping in organized_analysis.mappings
    )
    updated = RunIndex(run_root).load_analysis(run_dir.name)
    assert any(
        mapping.target_relative_path == "Season 01/Example - S01E02 - Second.mkv"
        for mapping in updated.mappings
    )


@pytest.mark.asyncio
async def test_failed_analysis_persists_failed_artifacts_and_run_index(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    runtime = SimpleNamespace(
        config_manager=SimpleNamespace(path=config_path),
        plan_builder=SimpleNamespace(
            analyze=AsyncMock(
                side_effect=ExternalServiceError(
                    "LLM API request failed",
                    details={"method": "POST", "path": "/chat/completions"},
                )
            )
        ),
    )
    manager = WebTaskManager(runtime, run_root=tmp_path / "runs")
    task = WebTask(
        id="task-1",
        kind="analysis",
        source_name="[UHA-WINGS&VCB-Studio] AKEBI's sailor uniform [Ma10p_1080p]",
        source_path="/NetDisk/PikPak/Download/[UHA-WINGS&VCB-Studio] AKEBI's sailor uniform [Ma10p_1080p]",
        item=AnalysisRequestItem(
            name="[UHA-WINGS&VCB-Studio] AKEBI's sailor uniform [Ma10p_1080p]",
            path="/NetDisk/PikPak/Download/[UHA-WINGS&VCB-Studio] AKEBI's sailor uniform [Ma10p_1080p]",
            prompt="",
        ),
    )

    with pytest.raises(ExternalServiceError):
        await manager._run_analysis(task)

    run_dir = Path(task.run_dir)
    analysis_path = run_dir / "artifacts" / "analysis_result.json"
    status_path = run_dir / "analysis_status.json"
    assert task.status == "failed"
    assert task.error.startswith("LLM API request failed")
    assert analysis_path.exists()
    assert status_path.exists()

    analysis = RunIndex(tmp_path / "runs").load_analysis(task.run_id)
    assert analysis.status == "failed"
    assert analysis.summary.startswith("LLM API request failed")

    runs = RunIndex(tmp_path / "runs").list_runs()
    assert runs[0].run_id == task.run_id
    assert runs[0].analysis_status == "failed"


def test_api_runs_endpoint_reads_file_index(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    app.state.runtime.run_index.root = tmp_path / "runs"
    write_run(
        app.state.runtime.run_index.root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )

    with TestClient(app) as client:
        response = client.get("/api/runs")

        assert response.status_code == 200
        runs = response.json()["runs"]
        assert runs[0]["run_id"] == "20260519-010203-Example"
        assert runs[0]["analysis_status"] == "succeeded"


def test_api_results_are_keyed_by_run_folder_not_title(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    write_run(
        run_root,
        "20260519-010203-Machikado-Mazoku-S1",
        source_path="/Inbox/Machikado Mazoku S1",
        status="needs_review",
    )
    write_run(
        run_root,
        "20260519-020304-Machikado-Mazoku-S2",
        source_path="/Inbox/Machikado Mazoku S2",
        status="needs_review",
    )

    with TestClient(app) as client:
        response = client.get("/api/results?latest_only=false")

        assert response.status_code == 200
        run_ids = [item["run_id"] for item in response.json()["results"]]
        assert run_ids == [
            "20260519-020304-Machikado-Mazoku-S2",
            "20260519-010203-Machikado-Mazoku-S1",
        ]


def test_api_results_latest_only_ignores_runs_without_analysis_result(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    write_run(
        run_root,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    pending_run = run_root / "20260519-020304-Example"
    (pending_run / "artifacts").mkdir(parents=True)
    (pending_run / "artifacts" / "run_input.json").write_text(
        json.dumps({"folder_name": "Example", "source_path": "/Inbox/Example"}, indent=2),
        encoding="utf-8",
    )
    (pending_run / "manifest.json").write_text(
        json.dumps({"run_dir": str(pending_run)}, indent=2),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        response = client.get("/api/results?latest_only=true")

        assert response.status_code == 200
        run_ids = [item["run_id"] for item in response.json()["results"]]
        assert run_ids == ["20260519-010203-Example"]


def test_api_deletes_selected_run_folders(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    run_a = write_run(
        run_root,
        "20260519-010203-Example-A",
        source_path="/Inbox/Example A",
        status="succeeded",
    )
    run_b = write_run(
        run_root,
        "20260519-020304-Example-B",
        source_path="/Inbox/Example B",
        status="failed",
    )

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/runs",
            json={
                "run_ids": [
                    "20260519-010203-Example-A",
                    "20260519-010203-Example-A",
                    "../outside",
                ]
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["deleted"] == ["20260519-010203-Example-A"]
        assert payload["failed"][0]["run_id"] == "../outside"
        assert not run_a.exists()
        assert run_b.exists()


def test_api_saves_manual_episode_mapping_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    ConfigManager(config_path).save(AppConfig())
    app = create_app(config_path)
    run_root = tmp_path / "runs"
    app.state.runtime.run_index.root = run_root
    write_tv_review_run(run_root, "20260519-040506-Example")

    with TestClient(app) as client:
        response = client.post(
            "/api/runs/20260519-040506-Example/manual-episode-mappings",
            json={
                "source_path": "/Inbox/Example/Season 1/Example 02.mkv",
                "season_number": 1,
                "episode_number": 2,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["tv_tmdb_id"] == "100"
        assert payload["episode_mappings"] == [
            {
                "folder_path": "Season 1",
                "file_name": "Example 02.mkv",
                "season_number": 1,
                "episode_number": 2,
                "reason": "Manual mapping from WebUI.",
            }
        ]
        saved = json.loads(
            (
                run_root
                / "20260519-040506-Example"
                / "artifacts"
                / "manual_episode_mappings.json"
            ).read_text(encoding="utf-8")
        )
        assert saved["episode_mappings"][0]["file_name"] == "Example 02.mkv"


def write_tv_review_run(root: Path, name: str) -> Path:
    run_dir = root / name
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    work_plan = WorkPlan(
        work_title="Example",
        source_name="Example",
        source_path="/Inbox/Example",
        archive_target_path="/Archive/Example",
        coverage_scope=[CoverageScope(type="tv_season", season_number=1, complete=False)],
        selected_tv_series=SelectedTvSeries(tmdb_id="100", title="Example", year="2024"),
        library_targets=[
            LibraryTarget(
                media_type="tv",
                target_path="/TV/Example (2024) [tmdbid=100]",
                title="Example",
                year="2024",
                tmdb_id="100",
            )
        ],
        missing_tmdb_episodes=[
            MissingEpisode(
                season_number=1,
                episode_number=2,
                episode_name="Second",
                reason="TMDB episode has no mapped source file.",
            )
        ],
        unmapped_files=[
            UnmappedFile(
                source_path="/Inbox/Example/Season 1/Example 02.mkv",
                file_kind="video",
                reason="not_selected_for_media_library",
            )
        ],
    )
    analysis = analysis_result_from_work_plan(
        item=AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt=""),
        work_plan=work_plan,
        media_type="tv",
        confidence=0.55,
    )
    (artifacts / "analysis_result.json").write_text(
        analysis.model_dump_json(indent=2), encoding="utf-8"
    )
    (artifacts / "work_plan.json").write_text(
        work_plan.model_dump_json(indent=2), encoding="utf-8"
    )
    (run_dir / "analysis_status.json").write_text(
        json.dumps({"status": "needs_review"}, indent=2), encoding="utf-8"
    )
    return run_dir


def write_run_analysis(run_dir: Path):
    source_path = "/Inbox/Example"
    write_run(run_dir.parent, run_dir.name, source_path=source_path, status="succeeded")
    return analysis_result_from_work_plan(
        item=AnalysisRequestItem(name="Example", path=source_path, prompt=""),
        work_plan=WorkPlan(
            work_title="Example",
            source_name="Example",
            source_path=source_path,
            archive_target_path="/Archive/Example Canonical/Example",
            library_targets=[
                LibraryTarget(
                    media_type="movie",
                    target_path="/Movies/Example (2024) [tmdbid=1]",
                    title="Example",
                    year="2024",
                    tmdb_id="1",
                )
            ],
        ),
        media_type="movie",
        confidence=0.92,
    )

