from __future__ import annotations

from dataclasses import dataclass

from starlist_bangumi.clients.openlist import OpenListClient
from starlist_bangumi.config import AppConfig
from starlist_bangumi.models import OpenListEntry, ScanItem
from starlist_bangumi.pathing import is_same_or_child, join_openlist_path


@dataclass(frozen=True)
class TreeFile:
    relative_path: str
    path: str
    name: str
    size: int = 0


@dataclass(frozen=True)
class TreeSnapshot:
    text: str
    nodes_scanned: int
    truncated: bool
    files: list[TreeFile]


class SourceScanner:
    def __init__(self, openlist: OpenListClient, config: AppConfig) -> None:
        self._openlist = openlist
        self._config = config

    async def scan_first_level(self) -> list[ScanItem]:
        media = self._config.media_library
        entries = await self._openlist.list_dir(
            media.source_path,
            refresh=self._config.openlist.refresh_all_on_full_scan,
            per_page=500,
        )
        result: list[ScanItem] = []
        for entry in sorted(entries, key=lambda item: item.name.lower()):
            if not entry.is_dir or self._is_configured_target(entry):
                continue
            result.append(
                ScanItem(
                    name=entry.name,
                    path=join_openlist_path(media.source_path, entry.name),
                    modified=entry.modified,
                )
            )
        return result

    async def build_text_tree(
        self, root_path: str, *, max_depth: int = 4, max_nodes: int = 500
    ) -> TreeSnapshot:
        lines = [root_path.rstrip("/").split("/")[-1] or "/"]
        state = {"count": 0, "truncated": False}
        files: list[TreeFile] = []
        ignored_folder_names = {
            name.casefold() for name in self._config.scan.ignored_folder_names
        }

        async def visit(path: str, prefix: str, depth: int, relative_dir: str = "") -> None:
            if depth > max_depth or state["count"] >= max_nodes:
                state["truncated"] = True
                return
            entries = await self._openlist.list_dir(path, refresh=True, per_page=500)
            visible = sorted(
                [
                    entry
                    for entry in entries
                    if not entry.is_dir or entry.name.casefold() not in ignored_folder_names
                ],
                key=lambda item: (not item.is_dir, item.name.lower()),
            )
            for index, entry in enumerate(visible):
                if state["count"] >= max_nodes:
                    state["truncated"] = True
                    return
                state["count"] += 1
                connector = "`-- " if index == len(visible) - 1 else "|-- "
                suffix = "/" if entry.is_dir else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                relative_path = f"{relative_dir}/{entry.name}" if relative_dir else entry.name
                if entry.is_dir:
                    child_prefix = prefix + ("    " if index == len(visible) - 1 else "|   ")
                    await visit(
                        join_openlist_path(path, entry.name),
                        child_prefix,
                        depth + 1,
                        relative_path,
                    )
                else:
                    files.append(
                        TreeFile(
                            relative_path=relative_path,
                            path=join_openlist_path(path, entry.name),
                            name=entry.name,
                            size=entry.size,
                        )
                    )

        await visit(root_path, "", 1)
        if state["truncated"]:
            lines.append("... tree truncated ...")
        return TreeSnapshot(
            text="\n".join(lines),
            nodes_scanned=state["count"],
            truncated=state["truncated"],
            files=files,
        )

    def _is_configured_target(self, entry: OpenListEntry) -> bool:
        item_path = join_openlist_path(self._config.media_library.source_path, entry.name)
        targets = [
            self._config.media_library.tv_media_library_path,
            self._config.media_library.movie_media_library_path,
            self._config.media_library.archive_path,
        ]
        return any(is_same_or_child(target, item_path) for target in targets)
