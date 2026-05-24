import httpx
import pytest

from starlist_bangumi.clients.llm import LlmClient
from starlist_bangumi.clients.openlist import OpenListClient, normalize_task_items
from starlist_bangumi.clients.tmdb import TmdbClient
from starlist_bangumi.config import LlmConfig, OpenListConfig, TmdbConfig
from starlist_bangumi.error_formatting import exception_message
from starlist_bangumi.exceptions import ExternalServiceError


@pytest.mark.asyncio
async def test_tmdb_request_error_has_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("starlist_bangumi.clients.tmdb.httpx.AsyncClient", FailingAsyncClient)
    client = TmdbClient(TmdbConfig(api_key="secret"))

    with pytest.raises(ExternalServiceError) as exc_info:
        await client.search_candidates("Machikado Mazoku", media_type="tv", limit=1)

    assert str(exc_info.value) == "TMDB API request failed: ConnectError"
    assert exc_info.value.details["path"] == "/search/tv"
    assert exc_info.value.details["error_type"] == "ConnectError"
    assert "api_key" not in exc_info.value.details["params"]


@pytest.mark.asyncio
async def test_llm_request_error_has_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("starlist_bangumi.clients.llm.httpx.AsyncClient", FailingAsyncClient)
    client = LlmClient(LlmConfig(base_url="https://example.test/v1", api_key="secret", model="m"))

    with pytest.raises(ExternalServiceError) as exc_info:
        await client.list_models()

    assert str(exc_info.value) == "LLM API request failed: ConnectError"
    assert exc_info.value.details["path"] == "/models"
    assert exc_info.value.details["error_type"] == "ConnectError"


def test_exception_message_falls_back_to_exception_type_for_empty_message() -> None:
    message = exception_message(httpx.ConnectError(""))

    assert message == "httpx.ConnectError"


def test_openlist_task_items_accepts_list_and_dict_shapes() -> None:
    assert normalize_task_items([{"id": "1"}, "ignored"]) == [{"id": "1"}]
    assert normalize_task_items({"content": [{"id": "2"}]}) == [{"id": "2"}]
    assert normalize_task_items({"a": {"id": "3"}, "b": {"name": "copy"}}) == [
        {"id": "3"},
        {"name": "copy"},
    ]
    assert normalize_task_items(None) == []


@pytest.mark.asyncio
async def test_openlist_request_error_keeps_endpoint_and_payload_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("starlist_bangumi.clients.openlist.httpx.AsyncClient", FailingAsyncClient)
    client = OpenListClient(OpenListConfig(username="user", password="secret"))

    with pytest.raises(ExternalServiceError) as exc_info:
        await client.login(force=True)

    assert str(exc_info.value) == "OpenList request failed after retries"
    assert exc_info.value.details["attempts"] == 3
    nested = exc_info.value.details["last_error_details"]
    assert nested["method"] == "POST"
    assert nested["path"] == "/api/auth/login"
    assert nested["payload"]["username"] == "user"
    assert nested["payload"]["password"] == "***"
    assert nested["error_type"] == "ConnectError"


class FailingAsyncClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "FailingAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def request(self, *args: object, **kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", "https://example.test")
        raise httpx.ConnectError("", request=request)
