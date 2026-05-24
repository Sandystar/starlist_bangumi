from __future__ import annotations

from typing import Any

import httpx

from starlist_bangumi.config import TmdbConfig
from starlist_bangumi.exceptions import ExternalServiceError
from starlist_bangumi.models import TmdbCandidate


class TmdbClient:
    """TMDB search client for TV and movie metadata."""

    def __init__(self, config: TmdbConfig) -> None:
        self._config = config

    @property
    def is_configured(self) -> bool:
        return bool(self._config.api_key)

    async def search(self, query: str, *, media_type: str = "tv") -> dict[str, Any] | None:
        results = await self.search_raw(query, media_type=media_type, limit=1)
        return results[0] if results else None

    async def search_raw(
        self, query: str, *, media_type: str = "tv", limit: int = 5
    ) -> list[dict[str, Any]]:
        if not self.is_configured or not query.strip():
            return []
        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        params = {
            "query": query,
            "language": self._config.language,
            "include_adult": "false",
            "page": "1",
        }
        data = await self._request("GET", endpoint, params=params)
        results = data.get("results") or []
        return [item for item in results[:limit] if isinstance(item, dict)]

    async def search_candidates(
        self, query: str, *, media_type: str, limit: int = 5
    ) -> list[TmdbCandidate]:
        return [
            tmdb_candidate_from_result(item, media_type=media_type)
            for item in await self.search_raw(query, media_type=media_type, limit=limit)
        ]

    async def details(self, tmdb_id: str, *, media_type: str = "tv") -> dict[str, Any] | None:
        if not self.is_configured or not tmdb_id:
            return None
        endpoint = f"/movie/{tmdb_id}" if media_type == "movie" else f"/tv/{tmdb_id}"
        return await self._request(
            "GET",
            endpoint,
            params={"language": self._config.language, "append_to_response": "external_ids"},
        )

    async def tv_details(self, tmdb_id: str) -> dict[str, Any] | None:
        return await self.details(tmdb_id, media_type="tv")

    async def movie_details(self, tmdb_id: str) -> dict[str, Any] | None:
        return await self.details(tmdb_id, media_type="movie")

    async def season_details(self, tmdb_id: str, season_number: int) -> dict[str, Any] | None:
        if not self.is_configured or not tmdb_id:
            return None
        return await self._request(
            "GET",
            f"/tv/{tmdb_id}/season/{season_number}",
            params={"language": self._config.language},
        )

    async def _request(self, method: str, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        headers: dict[str, str] = {}
        request_params = dict(params)
        if self._config.api_key.startswith("ey"):
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        else:
            request_params["api_key"] = self._config.api_key
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._config.request_timeout_seconds)
        ) as client:
            try:
                response = await client.request(
                    method,
                    f"{self._config.base_url}{path}",
                    params=request_params,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                raise ExternalServiceError(
                    f"TMDB API request failed: {type(exc).__name__}",
                    details={
                        "method": method,
                        "path": path,
                        "params": _safe_params(params),
                        "error_type": type(exc).__name__,
                    },
                ) from exc
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"TMDB API failed with HTTP {response.status_code}",
                details={"method": method, "path": path, "body": response.text[:500]},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "TMDB API response was not valid JSON",
                details={"method": method, "path": path, "body": response.text[:500]},
            ) from exc


def tmdb_candidate_from_result(item: dict[str, Any], *, media_type: str) -> TmdbCandidate:
    title = item.get("name") or item.get("title") or ""
    original_title = item.get("original_name") or item.get("original_title") or ""
    date = item.get("first_air_date") or item.get("release_date") or ""
    return TmdbCandidate(
        media_type="movie" if media_type == "movie" else "tv",
        tmdb_id=str(item.get("id") or ""),
        title=str(title),
        original_title=str(original_title),
        year=str(date)[:4] if date else "",
        overview=str(item.get("overview") or ""),
        language=str(item.get("original_language") or ""),
    )


def _safe_params(params: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in params.items() if key.lower() != "api_key"}
