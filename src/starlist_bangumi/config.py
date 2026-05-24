from __future__ import annotations

import json
from pathlib import Path
from string import Formatter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from starlist_bangumi.exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "config.json"


class OpenListConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = "http://127.0.0.1:5244"
    username: str = ""
    password: str = ""
    request_timeout_seconds: int = Field(default=30, ge=1, le=600)
    operation_interval_seconds: float = Field(default=1, ge=0, le=120)
    retry_count: int = Field(default=2, ge=0, le=10)
    refresh_all_on_full_scan: bool = False

    @field_validator("base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")


class LlmConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    request_timeout_seconds: int = Field(default=180, ge=1, le=600)

    @field_validator("base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")


class TmdbConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_url: str = "https://api.themoviedb.org/3"
    api_key: str = ""
    language: str = "zh-CN"
    allowed_languages: list[str] = Field(
        default_factory=lambda: ["zh-CN", "zh-TW", "ja-JP", "en-US"]
    )
    request_timeout_seconds: int = Field(default=30, ge=1, le=600)

    @field_validator("base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")


class MediaLibraryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_path: str = "/NetDisk/PikPak/Download"
    tv_media_library_path: str = "/NetDisk/PikPak/Library/Bangumi-TV"
    movie_media_library_path: str = "/NetDisk/PikPak/Library/Bangumi-Movie"
    archive_path: str = "/NetDisk/PikPak/Archive"
    archive_path_template: str = "{year}/{title}"
    tv_media_library_path_template: str = "{year}/{title} ({year}) [tmdbid={tmdb_id}]"
    movie_media_library_path_template: str = "{year}/{title} ({year}) [tmdbid={tmdb_id}]"
    include_episode_title_in_filename: bool = True

    @field_validator(
        "source_path",
        "tv_media_library_path",
        "movie_media_library_path",
        "archive_path",
    )
    @classmethod
    def normalize_openlist_path(cls, value: str) -> str:
        value = "/" + value.strip().strip("/")
        return value if value != "/" else "/"

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_media_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_path = migrated.pop("media_library_path", None)
        legacy_template = migrated.pop("media_library_path_template", None)
        if legacy_path and not migrated.get("tv_media_library_path"):
            migrated["tv_media_library_path"] = legacy_path
        if legacy_template and not migrated.get("tv_media_library_path_template"):
            migrated["tv_media_library_path_template"] = legacy_template
        return migrated


class UiConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pipeline_refresh_interval_seconds: int = Field(default=3, ge=1, le=300)


class ScanConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tree_max_depth: int = Field(default=6, ge=1, le=20)
    tree_max_nodes: int = Field(default=1000, ge=10, le=20000)
    ignored_folder_names: list[str] = Field(default_factory=lambda: ["SPs", "Scans", "CDs"])
    video_extensions: list[str] = Field(
        default_factory=lambda: [
            ".mkv",
            ".mp4",
            ".avi",
            ".mov",
            ".m4v",
            ".ts",
            ".wmv",
            ".flv",
            ".webm",
        ]
    )
    subtitle_extensions: list[str] = Field(
        default_factory=lambda: [".ass", ".ssa", ".srt", ".vtt", ".sub"]
    )

    @field_validator("video_extensions", "subtitle_extensions")
    @classmethod
    def normalize_extensions(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            extension = value.strip().lower()
            if not extension:
                continue
            if not extension.startswith("."):
                extension = f".{extension}"
            if extension not in normalized:
                normalized.append(extension)
        return normalized

    @field_validator("ignored_folder_names")
    @classmethod
    def normalize_ignored_folder_names(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            name = " ".join(str(value).strip().split())
            key = name.casefold()
            if not name or key in seen:
                continue
            normalized.append(name)
            seen.add(key)
        return normalized


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    openlist: OpenListConfig = Field(default_factory=OpenListConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    tmdb: TmdbConfig = Field(default_factory=TmdbConfig)
    media_library: MediaLibraryConfig = Field(default_factory=MediaLibraryConfig)
    scan: ScanConfig = Field(default_factory=ScanConfig)
    ui: UiConfig = Field(default_factory=UiConfig)


class SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def sanitize_openlist_segment(value: str) -> str:
    cleaned = "".join(" " if char in '<>:"\\|?*' else char for char in value)
    cleaned = cleaned.replace("/", "-")
    return " ".join(cleaned.split()).strip(" .") or "Unknown"


def render_path_template(template: str, values: dict[str, Any]) -> str:
    formatter = Formatter()
    safe_values = {
        key: sanitize_openlist_segment(str(value))
        for key, value in values.items()
        if value is not None and str(value) != ""
    }
    rendered_parts: list[str] = []
    for part in template.strip("/").split("/"):
        if not part:
            continue
        rendered = formatter.vformat(part, (), SafeFormatDict(safe_values))
        rendered_parts.append(sanitize_openlist_segment(rendered))
    return "/".join(rendered_parts)


class ConfigManager:
    """Load and save the JSON config used by the local backend."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path
        self._config: AppConfig | None = None
        self.version = 0

    def load(self) -> AppConfig:
        if not self.path.exists():
            self.save(AppConfig())
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._config = AppConfig.model_validate(raw)
        except Exception as exc:
            raise ConfigurationError(f"Unable to load config at {self.path}") from exc
        return self._config

    def get(self) -> AppConfig:
        return self._config or self.load()

    def save(self, config: AppConfig) -> AppConfig:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        validated = AppConfig.model_validate(config)
        self.path.write_text(
            json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._config = validated
        self.version += 1
        return validated
