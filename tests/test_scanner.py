import pytest

from starlist_bangumi.config import AppConfig
from starlist_bangumi.models import OpenListEntry
from starlist_bangumi.services.scanner import SourceScanner


@pytest.mark.asyncio
async def test_build_text_tree_skips_configured_folder_names_case_insensitively() -> None:
    openlist = FakeOpenList(
        {
            "/Inbox/Example": [
                OpenListEntry(name="Scans", is_dir=True),
                OpenListEntry(name="sps", is_dir=True),
                OpenListEntry(name="Season 03", is_dir=True),
            ],
            "/Inbox/Example/Scans": [
                OpenListEntry(name="booklet.png", is_dir=False),
            ],
            "/Inbox/Example/sps": [
                OpenListEntry(name="extra.mkv", is_dir=False),
            ],
            "/Inbox/Example/Season 03": [
                OpenListEntry(name="Example S03E01.mkv", is_dir=False, size=100),
            ],
        }
    )
    config = AppConfig()
    scanner = SourceScanner(openlist, config)

    snapshot = await scanner.build_text_tree("/Inbox/Example", max_depth=6, max_nodes=10)

    assert "Scans" not in snapshot.text
    assert "sps" not in snapshot.text
    assert "Example S03E01.mkv" in snapshot.text
    assert [file.relative_path for file in snapshot.files] == [
        "Season 03/Example S03E01.mkv"
    ]
    assert "/Inbox/Example/Scans" not in openlist.visited_paths
    assert "/Inbox/Example/sps" not in openlist.visited_paths
    assert all(call["refresh"] is True for call in openlist.list_dir_calls)


class FakeOpenList:
    def __init__(self, entries_by_path: dict[str, list[OpenListEntry]]) -> None:
        self.entries_by_path = entries_by_path
        self.visited_paths: list[str] = []
        self.list_dir_calls: list[dict[str, object]] = []

    async def list_dir(
        self,
        path: str,
        *,
        refresh: bool = False,
        page: int = 1,
        per_page: int = 200,
    ) -> list[OpenListEntry]:
        self.visited_paths.append(path)
        self.list_dir_calls.append(
            {"path": path, "refresh": refresh, "page": page, "per_page": per_page}
        )
        return self.entries_by_path.get(path, [])
