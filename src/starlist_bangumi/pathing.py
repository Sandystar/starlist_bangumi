from __future__ import annotations


def join_openlist_path(*parts: str) -> str:
    cleaned: list[str] = []
    for part in parts:
        if part is None:
            continue
        text = str(part).replace("\\", "/").strip("/")
        if text:
            cleaned.extend(piece for piece in text.split("/") if piece)
    return "/" + "/".join(cleaned)


def split_openlist_path(path: str) -> tuple[str, str]:
    normalized = join_openlist_path(path)
    if normalized == "/":
        return "/", ""
    parent, _, name = normalized.rpartition("/")
    return parent or "/", name


def is_same_or_child(path: str, possible_parent: str) -> bool:
    path = join_openlist_path(path)
    possible_parent = join_openlist_path(possible_parent)
    return path == possible_parent or path.startswith(possible_parent.rstrip("/") + "/")
