from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from starlist_bangumi.config import OpenListConfig
from starlist_bangumi.exceptions import ExternalServiceError, OperationError
from starlist_bangumi.models import OpenListEntry
from starlist_bangumi.pathing import join_openlist_path, split_openlist_path


class OpenListClient:
    """Small async client for the OpenList HTTP API."""

    def __init__(self, config: OpenListConfig) -> None:
        self._config = config
        self._token: str | None = None

    async def test_connection(self) -> dict[str, Any]:
        token = await self.login(force=True)
        root = await self.list_dir("/", refresh=False)
        return {"token_present": bool(token), "root_count": len(root)}

    async def login(self, *, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        payload = {"username": self._config.username, "password": self._config.password}
        data = await self._request("POST", "/api/auth/login", json=payload, auth=False)
        if not isinstance(data, dict):
            data = {}
        token = str(data.get("token") or "")
        if not token:
            raise ExternalServiceError("OpenList login did not return a token")
        self._token = token
        return token

    async def list_dir(
        self,
        path: str,
        *,
        refresh: bool = False,
        page: int = 1,
        per_page: int = 200,
    ) -> list[OpenListEntry]:
        payload = {
            "path": join_openlist_path(path),
            "password": "",
            "refresh": refresh,
            "page": page,
            "per_page": per_page,
        }
        data = await self._request("POST", "/api/fs/list", json=payload)
        if not isinstance(data, dict):
            return []
        content = data.get("content") or []
        return [OpenListEntry.model_validate(item) for item in content]

    async def exists(self, path: str, *, refresh: bool = False) -> bool:
        parent, name = split_openlist_path(path)
        if not name:
            return True
        try:
            entries = await self.list_dir(parent, refresh=refresh)
        except ExternalServiceError:
            return False
        return any(entry.name == name for entry in entries)

    async def existing_names(
        self,
        parent: str,
        names: list[str],
        *,
        refresh: bool = False,
    ) -> set[str]:
        requested = {name for name in names if name}
        if not requested:
            return set()
        try:
            entries = await self.list_dir(parent, refresh=refresh)
        except ExternalServiceError:
            return set()
        return {entry.name for entry in entries if entry.name in requested}

    async def ensure_dir(self, path: str) -> None:
        normalized = join_openlist_path(path)
        if normalized == "/":
            return
        current = "/"
        for part in normalized.strip("/").split("/"):
            current = join_openlist_path(current, part)
            if await self.exists(current):
                continue
            await self.mkdir(current)
            await self.sleep_between_operations()

    async def mkdir(self, path: str) -> None:
        await self._request("POST", "/api/fs/mkdir", json={"path": join_openlist_path(path)})

    async def remove_path(self, path: str) -> None:
        await self.remove_paths([path])

    async def remove_paths(self, paths: list[str]) -> None:
        for parent, names in group_names_by_parent(paths).items():
            existing = await self.existing_names(parent, names, refresh=True)
            if not existing:
                continue
            await self._request(
                "POST",
                "/api/fs/remove",
                json={"dir": parent, "names": [name for name in names if name in existing]},
            )

    async def copy_path(self, source_path: str, destination_dir: str) -> None:
        source_parent, source_name = split_openlist_path(source_path)
        if not source_name:
            raise OperationError("Cannot copy OpenList root")
        await self.ensure_dir(destination_dir)
        await self._request(
            "POST",
            "/api/fs/copy",
            json={
                "src_dir": source_parent,
                "dst_dir": join_openlist_path(destination_dir),
                "names": [source_name],
            },
        )

    async def task_items(self, task_type: str, state: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/task/{task_type}/{state}")
        return normalize_task_items(data)

    async def wait_for_task_type(
        self,
        task_type: str,
        *,
        source_path: str | None = None,
        destination_dir: str | None = None,
        timeout_seconds: float = 3600,
    ) -> None:
        """Wait for matching OpenList background tasks to leave the undone list.

        The copy API returns null even when it creates a background task, so task
        matching is intentionally best-effort and path verification still remains
        the final source of truth.
        """

        try:
            await self.task_items(task_type, "undone")
        except ExternalServiceError:
            return

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        saw_matching_task = False
        while True:
            try:
                undone = await self.task_items(task_type, "undone")
            except ExternalServiceError:
                return
            matching = [
                task
                for task in undone
                if task_matches_copy_context(
                    task,
                    source_path=source_path,
                    destination_dir=destination_dir,
                )
            ]
            failed = [task for task in matching if task_is_failed(task)]
            if failed:
                raise ExternalServiceError(
                    f"OpenList {task_type} task failed",
                    details={"tasks": compact_task_items(failed)},
                )
            if not matching:
                if saw_matching_task:
                    return
                return
            else:
                saw_matching_task = True
            if asyncio.get_running_loop().time() >= deadline:
                raise ExternalServiceError(
                    f"OpenList {task_type} task did not finish before timeout",
                    details={
                        "timeout_seconds": timeout_seconds,
                        "source_path": source_path,
                        "destination_dir": destination_dir,
                        "tasks": compact_task_items(matching),
                    },
                )
            await self.sleep_between_operations()

    async def wait_for_copy_tasks(self, source_path: str, destination_dir: str) -> None:
        await self.wait_for_task_type(
            "copy",
            source_path=source_path,
            destination_dir=destination_dir,
        )

    async def move_path(self, source_path: str, destination_dir: str) -> None:
        source_parent, source_name = split_openlist_path(source_path)
        if not source_name:
            raise OperationError("Cannot move OpenList root")
        await self.ensure_dir(destination_dir)
        await self._request(
            "POST",
            "/api/fs/move",
            json={
                "src_dir": source_parent,
                "dst_dir": join_openlist_path(destination_dir),
                "names": [source_name],
            },
        )

    async def rename_path(self, path: str, new_name: str) -> None:
        await self._request(
            "POST", "/api/fs/rename", json={"path": join_openlist_path(path), "name": new_name}
        )

    async def sleep_between_operations(self) -> None:
        if self._config.operation_interval_seconds:
            await asyncio.sleep(self._config.operation_interval_seconds)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        request_context = {
            "method": method,
            "path": path,
            "payload": sanitize_payload(json or {}),
        }

        async def send() -> Any:
            headers: dict[str, str] = {}
            if auth:
                headers["Authorization"] = await self.login()
            timeout = httpx.Timeout(self._config.request_timeout_seconds)
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method,
                        f"{self._config.base_url}{path}",
                        json=json,
                        headers=headers,
                    )
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPStatusError as exc:
                raise ExternalServiceError(
                    f"OpenList API failed with HTTP {exc.response.status_code}",
                    details={
                        **request_context,
                        "status_code": exc.response.status_code,
                        "body": exc.response.text[:1000],
                    },
                ) from exc
            except httpx.RequestError as exc:
                raise ExternalServiceError(
                    f"OpenList API request failed: {type(exc).__name__}",
                    details={
                        **request_context,
                        "error_type": type(exc).__name__,
                    },
                ) from exc
            except ValueError as exc:
                raise ExternalServiceError(
                    "OpenList API returned invalid JSON",
                    details=request_context,
                ) from exc
            if not isinstance(body, dict):
                raise ExternalServiceError(
                    "OpenList API returned invalid JSON",
                    details=request_context,
                )
            if body.get("code") != 200:
                raise ExternalServiceError(
                    f"OpenList API failed: {body.get('message') or body.get('msg') or body}",
                    details={**request_context, "body": body},
                )
            return body.get("data")

        return await self._with_retries(send)

    async def _with_retries(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._config.retry_count + 1):
            try:
                return await operation()
            except (httpx.RequestError, ExternalServiceError) as exc:
                if (
                    isinstance(exc, ExternalServiceError)
                    and exc.details.get("status_code") == 401
                    and attempt == 0
                ):
                    self._token = None
                last_error = exc
            if attempt < self._config.retry_count:
                await asyncio.sleep(max(0.2, self._config.operation_interval_seconds))
        details: dict[str, Any] = {
            "attempts": self._config.retry_count + 1,
        }
        if isinstance(last_error, ExternalServiceError):
            details["last_error"] = str(last_error)
            details["last_error_details"] = last_error.details
        elif last_error is not None:
            details["last_error"] = f"{type(last_error).__name__}: {last_error}"
        raise ExternalServiceError(
            "OpenList request failed after retries",
            details=details,
        ) from last_error


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(payload)
    for key in ("password", "token", "Authorization", "authorization"):
        if key in sanitized:
            sanitized[key] = "***"
    return sanitized


def group_names_by_parent(paths: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for path in paths:
        parent, name = split_openlist_path(path)
        if not name:
            continue
        key = (parent, name)
        if key in seen:
            continue
        grouped[parent].append(name)
        seen.add(key)
    return dict(grouped)


def normalize_task_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("content", "items", "tasks", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if all(isinstance(value, dict) for value in data.values()):
        return [value for value in data.values() if isinstance(value, dict)]
    return [data] if "id" in data or "name" in data else []


def task_matches_copy_context(
    task: dict[str, Any],
    *,
    source_path: str | None,
    destination_dir: str | None,
) -> bool:
    if not source_path and not destination_dir:
        return True
    haystack = json.dumps(task, ensure_ascii=False, sort_keys=True).casefold()
    source_parent, source_name = split_openlist_path(source_path or "")
    destination_parent, destination_name = split_openlist_path(destination_dir or "")
    source_tokens = [source_path or "", source_parent, source_name]
    destination_tokens = [destination_dir or "", destination_parent, destination_name]
    return token_matches(haystack, source_tokens) and token_matches(haystack, destination_tokens)


def token_matches(haystack: str, tokens: list[str]) -> bool:
    present_tokens = [token.casefold() for token in tokens if token and token != "/"]
    if not present_tokens:
        return True
    return any(token in haystack for token in present_tokens)


def task_is_failed(task: dict[str, Any]) -> bool:
    state = str(task.get("state") or task.get("status") or "").casefold()
    error = str(task.get("error") or "").strip()
    return bool(error) or state in {"failed", "error", "errored", "canceled", "cancelled"}


def compact_task_items(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("id", "name", "state", "status", "progress", "error")
    return [{key: task.get(key) for key in keys if key in task} for task in tasks]
