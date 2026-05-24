from __future__ import annotations

import json
import re
from typing import Any

import httpx

from starlist_bangumi.config import LlmConfig
from starlist_bangumi.exceptions import ExternalServiceError


class LlmClient:
    """OpenAI-compatible async LLM client."""

    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    @property
    def is_configured(self) -> bool:
        return bool(self._config.base_url and self._config.api_key and self._config.model)

    async def list_models(self) -> list[str]:
        if not self._config.base_url or not self._config.api_key:
            return []
        data = await self._request("GET", "/models")
        models = data.get("data", [])
        return sorted(str(item.get("id")) for item in models if item.get("id"))

    async def chat_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0
    ) -> dict[str, Any]:
        if not self.is_configured:
            raise ExternalServiceError("LLM service is not configured")
        payload = {
            "model": self._config.model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        body = await self._request("POST", "/chat/completions", json_body=payload)
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        return _parse_json_content(str(content))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_url = self._config.base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._config.request_timeout_seconds)
        ) as client:
            try:
                response = await client.request(
                    method, f"{base_url}{path}", json=json_body, headers=headers
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ExternalServiceError(
                    f"LLM API failed with HTTP {exc.response.status_code}",
                    details={"method": method, "path": path, "body": exc.response.text[:500]},
                ) from exc
            except httpx.RequestError as exc:
                raise ExternalServiceError(
                    f"LLM API request failed: {type(exc).__name__}",
                    details={"method": method, "path": path, "error_type": type(exc).__name__},
                ) from exc
        try:
            return response.json()
        except ValueError as exc:
            raise ExternalServiceError(
                "LLM API response was not valid JSON",
                details={"method": method, "path": path, "body": response.text[:500]},
            ) from exc


def _parse_json_content(content: str) -> dict[str, Any]:
    content = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.S)
    if fenced:
        content = fenced.group(1).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExternalServiceError(
            "LLM response was not valid JSON", details={"content": content[:500]}
        ) from exc
    if not isinstance(parsed, dict):
        raise ExternalServiceError("LLM response JSON must be an object")
    return parsed
