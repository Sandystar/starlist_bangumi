from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AnalysisStatus = Literal["succeeded", "needs_review", "failed"]
MediaType = Literal["tv", "movie", "unknown"]
LibraryTargetType = Literal["tv", "movie"]
MappingTargetKind = Literal["tv_episode", "movie", "subtitle"]
CoverageType = Literal["tv_season", "movie"]
UnmappedFileKind = Literal["video", "subtitle", "other"]
ExpectedComponent = Literal["tv", "movie", "special"]


class OpenListEntry(BaseModel):
    name: str
    is_dir: bool
    size: int = 0
    modified: str | None = None


class ScanItem(BaseModel):
    name: str
    path: str
    modified: str | None = None
    prompt: str = ""


class AnalysisRequestItem(BaseModel):
    name: str
    path: str
    prompt: str = ""
    tv_tmdb_id: str = ""
    movie_tmdb_ids: list[str] = Field(default_factory=list)

    @field_validator("tv_tmdb_id", mode="before")
    @classmethod
    def normalize_tv_tmdb_id(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator("movie_tmdb_ids", mode="before")
    @classmethod
    def normalize_movie_tmdb_ids(cls, value: object) -> object:
        if value is None:
            return []
        values = value.split(",") if isinstance(value, str) else value
        if not isinstance(values, list):
            return value
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            movie_id = str(item or "").strip()
            if movie_id and movie_id not in seen:
                result.append(movie_id)
                seen.add(movie_id)
        return result


class FileMapping(BaseModel):
    source_path: str
    target_relative_path: str
    target_path: str
    reason: str = ""


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TmdbCandidate(BaseModel):
    media_type: LibraryTargetType
    tmdb_id: str
    title: str
    original_title: str = ""
    year: str = ""
    overview: str = ""
    language: str = ""


class SelectedTvSeries(BaseModel):
    tmdb_id: str = ""
    title: str = ""
    original_title: str = ""
    year: str = ""


class SelectedMovie(BaseModel):
    tmdb_id: str
    title: str = ""
    original_title: str = ""
    year: str = ""


class CoverageScope(BaseModel):
    type: CoverageType
    season_number: int | None = None
    tmdb_movie_id: str = ""
    complete: bool | None = None
    note: str = ""


class LibraryTarget(BaseModel):
    media_type: LibraryTargetType
    target_path: str
    title: str
    year: str = "0000"
    tmdb_id: str = ""
    season_number: int | None = None


class ValidatedMapping(BaseModel):
    source_path: str
    target_path: str
    target_relative_path: str
    target_kind: MappingTargetKind
    media_type: LibraryTargetType
    season_number: int | None = None
    episode_number: int | None = None
    episode_name: str = ""
    tmdb_episode_id: str = ""
    tmdb_movie_id: str = ""
    subtitle_for_source_path: str = ""
    reason: str = ""


class RejectedMapping(BaseModel):
    source_path: str
    target_kind: MappingTargetKind | None = None
    reason: str
    details: str = ""


class MissingEpisode(BaseModel):
    season_number: int
    episode_number: int
    episode_name: str = ""
    reason: str


class MissingMovie(BaseModel):
    tmdb_movie_id: str
    title: str = ""
    year: str = ""
    reason: str


class UnmappedFile(BaseModel):
    source_path: str
    file_kind: UnmappedFileKind
    reason: str


class LlmIdentifyWorkOutput(StrictSchemaModel):
    schema_version: str = "1.0"
    canonical_title: str
    aliases: list[str] = Field(default_factory=list)
    expected_components: list[ExpectedComponent] = Field(default_factory=list)
    season_hints: list[int] = Field(default_factory=list)
    reason: str = ""

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("Unsupported identify_work schema_version")
        return value

    @field_validator("expected_components", mode="before")
    @classmethod
    def normalize_expected_components(cls, value: object) -> object:
        if value is None:
            return []
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list):
            return value
        aliases = {
            "tv": "tv",
            "series": "tv",
            "movie": "movie",
            "film": "movie",
            "theatrical": "movie",
            "special": "special",
            "specials": "special",
            "sp": "special",
            "ova": "special",
            "oav": "special",
            "oad": "special",
            "ona": "special",
        }
        normalized: list[str] = []
        for item in values:
            key = str(item).strip().casefold().replace("-", "_")
            component = aliases.get(key)
            if component and component not in normalized:
                normalized.append(component)
        return normalized


class LlmCandidateSelectionOutput(StrictSchemaModel):
    schema_version: str = "1.0"
    selected_tv_series_id: str = ""
    selected_movie_ids: list[str] = Field(default_factory=list)
    season_numbers_to_fetch: list[int] = Field(default_factory=list)
    needs_user_choice: bool = False
    reason: str = ""

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("Unsupported select_candidates schema_version")
        return value


class LlmMappingFileInfo(StrictSchemaModel):
    file_name: str
    episode_number: int | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


class LlmMappingDecision(StrictSchemaModel):
    folder_path: str = ""
    target_kind: Literal["tv_episode", "movie"]
    season_number: int | None = None
    tmdb_movie_id: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""
    file_infos: list[LlmMappingFileInfo] = Field(default_factory=list)


class LlmIgnoredFile(StrictSchemaModel):
    folder_path: str = ""
    file_name: str
    reason: str


class LlmMappingOutput(StrictSchemaModel):
    schema_version: str = "1.0"
    coverage_scope: list[CoverageScope] = Field(default_factory=list)
    decisions: list[LlmMappingDecision] = Field(default_factory=list)
    ignored_files: list[LlmIgnoredFile] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("Unsupported decide_mappings schema_version")
        return value


class ManualEpisodeMapping(StrictSchemaModel):
    folder_path: str = ""
    file_name: str = Field(min_length=1)
    season_number: int = Field(ge=0)
    episode_number: int = Field(ge=1)
    reason: str = ""


class ManualEpisodeMappingFile(StrictSchemaModel):
    schema_version: str = "1.0"
    tv_tmdb_id: str = ""
    movie_tmdb_ids: list[str] = Field(default_factory=list)
    episode_mappings: list[ManualEpisodeMapping] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError("Unsupported manual episode mapping schema_version")
        return value


class WorkPlan(BaseModel):
    plan_version: str = "1.0"
    work_title: str
    source_name: str
    source_path: str
    archive_target_path: str
    coverage_scope: list[CoverageScope] = Field(default_factory=list)
    tv_candidates: list[TmdbCandidate] = Field(default_factory=list)
    movie_candidates: list[TmdbCandidate] = Field(default_factory=list)
    needs_user_choice_reason: str = ""
    selected_tv_series: SelectedTvSeries | None = None
    selected_movies: list[SelectedMovie] = Field(default_factory=list)
    library_targets: list[LibraryTarget] = Field(default_factory=list)
    validated_mappings: list[ValidatedMapping] = Field(default_factory=list)
    rejected_mappings: list[RejectedMapping] = Field(default_factory=list)
    missing_tmdb_episodes: list[MissingEpisode] = Field(default_factory=list)
    missing_movies: list[MissingMovie] = Field(default_factory=list)
    unmapped_files: list[UnmappedFile] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def primary_media_target_path(self) -> str:
        return self.library_targets[0].target_path if self.library_targets else ""


class AnalysisResult(BaseModel):
    id: str
    analysis_version: int = 1
    source_name: str
    source_path: str
    status: AnalysisStatus
    confidence: float = Field(ge=0, le=1)
    media_type: MediaType = "unknown"
    title: str
    original_title: str = ""
    year: str = "0000"
    tmdb_id: str = ""
    tvdb_id: str = ""
    media_target_path: str
    archive_target_path: str
    report_tree: str
    summary: str
    warnings: list[str] = Field(default_factory=list)
    mappings: list[FileMapping] = Field(default_factory=list)
    work_plan: WorkPlan | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrganizeOptions(BaseModel):
    allow_failed_analysis: bool = False
    delete_target_before: bool = False
    overwrite_archive_target_before: bool = False
    delete_source_after: bool = False
    resume_existing: bool = False
