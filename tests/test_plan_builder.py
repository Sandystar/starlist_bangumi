import pytest
from pydantic import ValidationError

from starlist_bangumi.clients.tmdb import tmdb_candidate_from_result
from starlist_bangumi.config import AppConfig
from starlist_bangumi.exceptions import ExternalServiceError
from starlist_bangumi.models import (
    AnalysisRequestItem,
    CoverageScope,
    LibraryTarget,
    LlmCandidateSelectionOutput,
    LlmIdentifyWorkOutput,
    LlmMappingOutput,
    TmdbCandidate,
    ValidatedMapping,
    WorkPlan,
)
from starlist_bangumi.services.plan_builder import (
    LlmPlanBuilder,
    analysis_candidate_files,
    analysis_config_blockers,
    build_fallback_mappings,
    build_organized_target_tree,
    candidate_file_json,
    clean_release_name,
    media_candidate_files,
    prune_tv_special_movie_selection,
    prebind_subtitle_to_video,
    subtitle_language_suffix,
    review_reason,
)
from starlist_bangumi.services.scanner import TreeFile, TreeSnapshot


def test_clean_release_name_removes_release_tags() -> None:
    assert (
        clean_release_name("[VCB-Studio] Kono Subarashii Sekai [Ma10p_1080p]")
        == "Kono Subarashii Sekai"
    )


def test_build_fallback_mappings_detects_video_files() -> None:
    tree = """Example
|-- Example - 01.mkv
|-- readme.txt
`-- Example - 02.mp4"""

    mappings = build_fallback_mappings(
        source_path="/Source/Example",
        tree_text=tree,
        title="Example",
        year="2024",
        media_type="tv",
        media_target_path="/Library/2024/Example",
    )

    assert [item.target_relative_path for item in mappings] == [
        "Example - S01E01.mkv",
        "Example - S01E02.mp4",
    ]


def test_llm_strict_schema_rejects_extra_fields_and_bad_version() -> None:
    with pytest.raises(ValidationError):
        LlmIdentifyWorkOutput.model_validate(
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "unexpected": True,
            }
        )

    with pytest.raises(ValidationError):
        LlmMappingOutput.model_validate(
            {
                "schema_version": "2.0",
                "coverage_scope": [],
                "decisions": [],
                "ignored_files": [],
                "notes": [],
            }
        )


def test_identify_work_normalizes_ova_component_to_special() -> None:
    output = LlmIdentifyWorkOutput.model_validate(
        {
            "schema_version": "1.0",
            "canonical_title": "Example",
            "expected_components": ["tv", "ova", "oad"],
            "season_hints": [1],
            "reason": "LLM used common anime component aliases",
        }
    )

    assert output.expected_components == ["tv", "special"]


def test_tv_selection_prunes_ova_movie_candidate() -> None:
    selection = LlmCandidateSelectionOutput(
        selected_tv_series_id="34742",
        selected_movie_ids=["382667", "999"],
    )
    removed = prune_tv_special_movie_selection(
        selection,
        [
            TmdbCandidate(
                media_type="movie",
                tmdb_id="382667",
                title="To LOVEる -とらぶる- OVA",
            ),
            TmdbCandidate(
                media_type="movie",
                tmdb_id="999",
                title="Real Theatrical Movie",
            ),
        ],
    )

    assert removed == ["382667"]
    assert selection.selected_movie_ids == ["999"]


def test_tmdb_candidate_conversion_for_tv_and_movie() -> None:
    tv_candidate = tmdb_candidate_from_result(
        {
            "id": 123,
            "name": "Example TV",
            "original_name": "Example Original",
            "first_air_date": "2024-04-01",
            "overview": "Overview",
            "original_language": "ja",
        },
        media_type="tv",
    )
    movie_candidate = tmdb_candidate_from_result(
        {"id": 456, "title": "Example Movie", "release_date": "2025-01-02"},
        media_type="movie",
    )

    assert tv_candidate.media_type == "tv"
    assert tv_candidate.tmdb_id == "123"
    assert tv_candidate.year == "2024"
    assert movie_candidate.media_type == "movie"
    assert movie_candidate.tmdb_id == "456"


def test_analysis_config_blockers_extracts_actionable_causes() -> None:
    assert analysis_config_blockers(
        [
            "LLM is not configured; used release-name heuristics.",
            "TMDB is not configured; generated paths may miss external IDs.",
            "No TMDB candidates were found.",
        ]
    ) == ["LLM not configured", "TMDB not configured", "no TMDB candidates"]


def test_partial_season_zero_does_not_force_review() -> None:
    reason = review_reason(
        validated=[
            ValidatedMapping(
                source_path="/Inbox/Example/Example - 01.mkv",
                target_path="/Library/Example/Season 01/Example - S01E01.mkv",
                target_relative_path="Season 01/Example - S01E01.mkv",
                target_kind="tv_episode",
                media_type="tv",
                season_number=1,
                episode_number=1,
            )
        ],
        rejected=[],
        missing_episodes=[],
        missing_movies=[],
        coverage_scope=[
            CoverageScope(type="tv_season", season_number=1, complete=True),
            CoverageScope(type="tv_season", season_number=0, complete=False),
        ],
    )

    assert "TV coverage scope is partial or ambiguous" not in reason


@pytest.mark.asyncio
async def test_missing_tmdb_episodes_require_review_even_when_extra_videos_exist() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            ),
            TreeFile(
                relative_path="Example - 13(OVA).mkv",
                path="/Inbox/Example/Example - 13(OVA).mkv",
                name="Example - 13(OVA).mkv",
                size=90,
            ),
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv", "special"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "episode number matches",
                        "file_infos": [
                            {
                                "file_name": "Example - 01.mkv",
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    }
                ],
                "ignored_files": [
                    {
                        "folder_path": "",
                        "file_name": "Example - 13(OVA).mkv",
                        "reason": "No concrete TMDB Season 0 episode match.",
                    }
                ],
                "notes": [],
            },
        ]
    )
    builder = LlmPlanBuilder(scanner, llm, FakeTmdb(seasons=[0, 1]), AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert result.status == "needs_review"
    assert result.work_plan is not None
    assert result.work_plan.unmapped_files[0].source_path.endswith("Example - 13(OVA).mkv")
    assert result.work_plan.missing_tmdb_episodes[0].season_number == 0
    assert result.work_plan.missing_tmdb_episodes[0].episode_number == 1


@pytest.mark.asyncio
async def test_unmapped_extra_video_does_not_require_review_when_tmdb_is_satisfied() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            ),
            TreeFile(
                relative_path="Extra PV.mkv",
                path="/Inbox/Example/Extra PV.mkv",
                name="Extra PV.mkv",
                size=90,
            ),
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "episode number matches",
                        "file_infos": [
                            {
                                "file_name": "Example - 01.mkv",
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    }
                ],
                "ignored_files": [
                    {
                        "folder_path": "",
                        "file_name": "Extra PV.mkv",
                        "reason": "Extra video not represented by TMDB.",
                    }
                ],
                "notes": [],
            },
        ]
    )
    builder = LlmPlanBuilder(scanner, llm, FakeTmdb(seasons=[1]), AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert result.status == "succeeded"
    assert result.work_plan is not None
    assert len(result.work_plan.unmapped_files) == 1
    assert result.work_plan.missing_tmdb_episodes == []


def test_media_candidate_files_and_prompt_payload_include_only_videos() -> None:
    files = [
        TreeFile(
            relative_path="Example - 01.mkv",
            path="/Inbox/Example/Example - 01.mkv",
            name="Example - 01.mkv",
        ),
        TreeFile(
            relative_path="Example - 01.zh.ass",
            path="/Inbox/Example/Example - 01.zh.ass",
            name="Example - 01.zh.ass",
        ),
    ]

    media_files = media_candidate_files(
        files,
        video_extensions={".mkv"},
        subtitle_extensions={".ass"},
    )
    payload = candidate_file_json(
        media_files,
        video_extensions={".mkv"},
        subtitle_extensions={".ass"},
        source_root="/Inbox/Example",
    )

    assert [file.name for file in media_files] == ["Example - 01.mkv"]
    assert "Example - 01.zh.ass" not in payload
    assert '"folder_path":""' in payload
    assert '"file_name":"Example - 01.mkv"' in payload
    assert '"file_kind":"video"' in payload


def test_analysis_candidate_files_include_unmatched_subtitles_only() -> None:
    files = [
        TreeFile(
            relative_path="Example - 01.mkv",
            path="/Inbox/Example/Example - 01.mkv",
            name="Example - 01.mkv",
        ),
        TreeFile(
            relative_path="Example - 01.zh.ass",
            path="/Inbox/Example/Example - 01.zh.ass",
            name="Example - 01.zh.ass",
        ),
        TreeFile(
            relative_path="Loose Episode 02.ass",
            path="/Inbox/Example/Loose Episode 02.ass",
            name="Loose Episode 02.ass",
        ),
        TreeFile(
            relative_path="readme.txt",
            path="/Inbox/Example/readme.txt",
            name="readme.txt",
        ),
    ]

    candidates = analysis_candidate_files(
        files,
        video_extensions={".mkv"},
        subtitle_extensions={".ass"},
    )
    payload = candidate_file_json(
        candidates,
        video_extensions={".mkv"},
        subtitle_extensions={".ass"},
        source_root="/Inbox/Example",
    )

    assert [file.name for file in candidates] == [
        "Example - 01.mkv",
        "Loose Episode 02.ass",
    ]
    assert "Example - 01.zh.ass" not in payload
    assert "readme.txt" not in payload
    assert '"file_name":"Loose Episode 02.ass"' in payload
    assert '"file_kind":"subtitle"' in payload


@pytest.mark.asyncio
async def test_subtitles_with_different_release_stem_bind_by_episode_number() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="[VCB-Studio] Example [01][Ma10p_1080p][x265_flac].mkv",
                path=(
                    "/Inbox/Example/"
                    "[VCB-Studio] Example [01][Ma10p_1080p][x265_flac].mkv"
                ),
                name="[VCB-Studio] Example [01][Ma10p_1080p][x265_flac].mkv",
                size=100,
            ),
            TreeFile(
                relative_path="Example - 01.friDay.zh-Hans.srt",
                path="/Inbox/Example/Example - 01.friDay.zh-Hans.srt",
                name="Example - 01.friDay.zh-Hans.srt",
                size=10,
            ),
            TreeFile(
                relative_path="Example - 01.friDay.zh-Hant.srt",
                path="/Inbox/Example/Example - 01.friDay.zh-Hant.srt",
                name="Example - 01.friDay.zh-Hant.srt",
                size=10,
            ),
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "episode number matches",
                        "file_infos": [
                            {
                                "file_name": (
                                    "[VCB-Studio] Example [01][Ma10p_1080p][x265_flac].mkv"
                                ),
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    }
                ],
                "ignored_files": [],
                "notes": [],
            },
        ]
    )
    builder = LlmPlanBuilder(scanner, llm, FakeTmdb(), AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert "Example - 01.friDay.zh-Hans.srt" not in llm.calls[2][1]["content"]
    assert result.work_plan is not None
    assert [mapping.target_kind for mapping in result.work_plan.validated_mappings] == [
        "tv_episode",
        "subtitle",
        "subtitle",
    ]
    assert [mapping.target_relative_path for mapping in result.work_plan.validated_mappings] == [
        "Season 01/Example - S01E01 - A Beginning.mkv",
        "Season 01/Example - S01E01 - A Beginning.friDay.zh-Hans.srt",
        "Season 01/Example - S01E01 - A Beginning.friDay.zh-Hant.srt",
    ]
    assert result.work_plan.unmapped_files == []


def test_subtitles_do_not_bind_to_season_number_when_episode_number_differs() -> None:
    video1 = TreeFile(
        relative_path=(
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 [Ma10p_1080p]/"
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 "
            "[01][Ma10p_1080p][x265_flac].mkv"
        ),
        path="/Inbox/Example/video1.mkv",
        name=(
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 "
            "[01][Ma10p_1080p][x265_flac].mkv"
        ),
        size=100,
    )
    video2 = TreeFile(
        relative_path=(
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 [Ma10p_1080p]/"
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 "
            "[02][Ma10p_1080p][x265_flac].mkv"
        ),
        path="/Inbox/Example/video2.mkv",
        name=(
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 "
            "[02][Ma10p_1080p][x265_flac].mkv"
        ),
        size=100,
    )
    subtitle2 = TreeFile(
        relative_path=(
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 [Ma10p_1080p]/"
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 "
            "[02][Ma10p_1080p][x265_flac].JPSC.ass"
        ),
        path="/Inbox/Example/subtitle2.ass",
        name=(
            "[Nekomoe kissaten&VCB-Studio] Kage no Jitsuryokusha ni Naritakute! S2 "
            "[02][Ma10p_1080p][x265_flac].JPSC.ass"
        ),
        size=10,
    )

    assert prebind_subtitle_to_video(subtitle2, [video1]) is None
    assert prebind_subtitle_to_video(subtitle2, [video2]) == video2


def test_special_subtitles_require_same_stem_binding() -> None:
    episode_1 = TreeFile(
        relative_path=(
            "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
            "[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].mkv"
        ),
        path="/Inbox/Example/episode1.mkv",
        name="[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].mkv",
        size=100,
    )
    ova_1 = TreeFile(
        relative_path=(
            "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
            "[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].mkv"
        ),
        path="/Inbox/Example/ova1.mkv",
        name="[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].mkv",
        size=100,
    )
    ova_1_subtitle = TreeFile(
        relative_path=(
            "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
            "[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].ass"
        ),
        path="/Inbox/Example/ova1.ass",
        name="[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].ass",
        size=10,
    )
    sp_subtitle = TreeFile(
        relative_path=(
            "[VCB-Studio] To LOVE-Ru Darkness 2nd [Ma10p_1080p]/"
            "[VCB-Studio] To LOVE-Ru Darkness 2nd [SP][Ma10p_720p][x265_flac].ass"
        ),
        path="/Inbox/Example/sp.ass",
        name="[VCB-Studio] To LOVE-Ru Darkness 2nd [SP][Ma10p_720p][x265_flac].ass",
        size=10,
    )

    assert prebind_subtitle_to_video(ova_1_subtitle, [episode_1]) is None
    assert prebind_subtitle_to_video(ova_1_subtitle, [episode_1, ova_1]) == ova_1
    assert prebind_subtitle_to_video(sp_subtitle, [episode_1]) is None


@pytest.mark.asyncio
async def test_special_same_name_subtitles_follow_video_without_duplicate_rejections() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path=(
                    "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].mkv"
                ),
                path=(
                    "/Inbox/Example/[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].mkv"
                ),
                name="[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].mkv",
                size=100,
            ),
            TreeFile(
                relative_path=(
                    "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].ass"
                ),
                path=(
                    "/Inbox/Example/[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].ass"
                ),
                name="[VCB-Studio] To LOVE-Ru [01][Ma10p_1080p][x265_flac_aac].ass",
                size=10,
            ),
            TreeFile(
                relative_path=(
                    "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].mkv"
                ),
                path=(
                    "/Inbox/Example/[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].mkv"
                ),
                name="[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].mkv",
                size=100,
            ),
            TreeFile(
                relative_path=(
                    "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].ass"
                ),
                path=(
                    "/Inbox/Example/[VCB-Studio] To LOVE-Ru [Ma10p_1080p]/"
                    "[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].ass"
                ),
                name="[VCB-Studio] To LOVE-Ru [OVA01][Ma10p_1080p][x265_flac].ass",
                size=10,
            ),
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv", "special"],
                "season_hints": [0, 1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [0, 1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 0, "complete": True, "note": ""},
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""},
                ],
                "decisions": [
                    {
                        "folder_path": "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "episode number matches",
                        "file_infos": [
                            {
                                "file_name": (
                                    "[VCB-Studio] To LOVE-Ru [01]"
                                    "[Ma10p_1080p][x265_flac_aac].mkv"
                                ),
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    },
                    {
                        "folder_path": "[VCB-Studio] To LOVE-Ru [Ma10p_1080p]",
                        "target_kind": "tv_episode",
                        "season_number": 0,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "OVA maps to special 1",
                        "file_infos": [
                            {
                                "file_name": (
                                    "[VCB-Studio] To LOVE-Ru [OVA01]"
                                    "[Ma10p_1080p][x265_flac].mkv"
                                ),
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "OVA maps to special 1",
                            }
                        ],
                    },
                ],
                "ignored_files": [],
                "notes": [],
            },
        ]
    )
    builder = LlmPlanBuilder(
        scanner,
        llm,
        FakeTmdb(seasons=[0, 1], episode_names={(0, 1): "OVA1", (1, 1): "Episode 1"}),
        AppConfig(),
    )

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert result.work_plan is not None
    assert result.work_plan.rejected_mappings == []
    assert [mapping.target_relative_path for mapping in result.work_plan.validated_mappings] == [
        "Season 01/Example - S01E01 - Episode 1.mkv",
        "Season 01/Example - S01E01 - Episode 1.ass",
        "Season 00/Example - S00E01 - OVA1.mkv",
        "Season 00/Example - S00E01 - OVA1.ass",
    ]


def test_subtitle_language_suffix_ignores_release_tag_digits() -> None:
    video_name = (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! Movie "
        "[Ma10p_1080p][x265_flac].mkv"
    )
    subtitle_name = (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! [OVA]"
        "[Ma10p_1080p][x265_flac].sc.ass"
    )

    assert subtitle_language_suffix(video_name, subtitle_name) == ".sc"


def test_subtitle_language_suffix_handles_single_prefix_group() -> None:
    video_name_01 = (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 "
        "[01][Ma10p_1080p][x265_flac_aac].mkv"
    )
    subtitle_name_01 = "[Moozzi2] Kono Subarashii Sekai ni Shukufuku o! 3 - 01 -.sc.ass"
    video_name_02 = (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 "
        "[02][Ma10p_1080p][x265_flac_aac].mkv"
    )
    subtitle_name_02 = "[Moozzi2] Kono Subarashii Sekai ni Shukufuku o! 3 - 02 -.tc.ass"

    assert subtitle_language_suffix(video_name_01, subtitle_name_01) == ".sc"
    assert subtitle_language_suffix(video_name_02, subtitle_name_02) == ".tc"


def test_subtitle_language_suffix_ignores_title_before_first_dot() -> None:
    video_name = (
        "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 3 "
        "[11(OVA)][Ma10p_1080p][x265_flac].mkv"
    )
    subtitle_name = "[Moozzi2] Kono Subarashii Sekai ni Shukufuku o! 3 - 11 END -.tc.ass"

    assert subtitle_language_suffix(video_name, subtitle_name) == ".tc"


def test_build_organized_target_tree_groups_target_relative_paths() -> None:
    tree = build_organized_target_tree(
        WorkPlan(
            work_title="Example",
            source_name="Example",
            source_path="/Inbox/Example",
            archive_target_path="/Archive/Example",
            library_targets=[
                LibraryTarget(
                    media_type="tv",
                    target_path="/Library/Example",
                    title="Example",
                    year="2024",
                    tmdb_id="1",
                )
            ],
            validated_mappings=[
                ValidatedMapping(
                    source_path="/Inbox/Example/01.mkv",
                    target_path="/Library/Example/Season 01/Example - S01E01.mkv",
                    target_relative_path="Season 01/Example - S01E01.mkv",
                    target_kind="tv_episode",
                    media_type="tv",
                    season_number=1,
                    episode_number=1,
                ),
                ValidatedMapping(
                    source_path="/Inbox/Example/01.zh.ass",
                    target_path="/Library/Example/Season 01/Example - S01E01.zh.ass",
                    target_relative_path="Season 01/Example - S01E01.zh.ass",
                    target_kind="subtitle",
                    media_type="tv",
                    season_number=1,
                    episode_number=1,
                ),
            ],
        )
    )

    assert "organized-targets" in tree
    assert "/Archive/Example" in tree
    assert "tv: /Library/Example" in tree
    assert "Season 01" in tree
    assert "Example - S01E01.mkv" in tree
    assert "Example - S01E01.zh.ass" in tree


@pytest.mark.asyncio
async def test_three_stage_llm_tmdb_flow_builds_valid_tv_plan() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            ),
            TreeFile(
                relative_path="Example - 01.zh.ass",
                path="/Inbox/Example/Example - 01.zh.ass",
                name="Example - 01.zh.ass",
                size=10,
            ),
            TreeFile(
                relative_path="Loose Episode 02.ass",
                path="/Inbox/Example/Loose Episode 02.ass",
                name="Loose Episode 02.ass",
                size=10,
            ),
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "episode number matches",
                        "file_infos": [
                            {
                                "file_name": "Example - 01.mkv",
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    }
                ],
                "ignored_files": [],
                "notes": [],
            },
        ]
    )
    builder = LlmPlanBuilder(scanner, llm, FakeTmdb(), AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert len(llm.calls) == 3
    assert "Candidate files" not in llm.calls[0][1]["content"]
    assert "File tree" not in llm.calls[0][1]["content"]
    assert "Candidate files" in llm.calls[2][1]["content"]
    assert "Example - 01.zh.ass" not in llm.calls[2][1]["content"]
    assert "Loose Episode 02.ass" in llm.calls[2][1]["content"]
    assert result.status == "succeeded"
    assert result.media_type == "tv"
    assert result.work_plan is not None
    assert [file.file_kind for file in result.work_plan.unmapped_files] == ["subtitle"]
    assert len(result.work_plan.validated_mappings) == 2
    assert result.archive_target_path == "/NetDisk/PikPak/Archive/2024/Example/Example"
    assert result.mappings[0].target_relative_path == (
        "Season 01/Example - S01E01 - A Beginning.mkv"
    )
    assert result.mappings[1].target_relative_path == (
        "Season 01/Example - S01E01 - A Beginning.zh.ass"
    )


@pytest.mark.asyncio
async def test_manual_tmdb_id_skips_identity_and_candidate_llm_stages() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            )
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "manual TMDB id provides the series context",
                        "file_infos": [
                            {
                                "file_name": "Example - 01.mkv",
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    }
                ],
                "ignored_files": [],
                "notes": [],
            },
        ]
    )
    tmdb = FakeTmdb(seasons=[1])
    builder = LlmPlanBuilder(scanner, llm, tmdb, AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(
            name="Example",
            path="/Inbox/Example",
            prompt="",
            tv_tmdb_id="100",
        )
    )

    assert len(llm.calls) == 1
    assert "Identified work" in llm.calls[0][1]["content"]
    assert result.status == "succeeded"
    assert result.work_plan is not None
    assert result.work_plan.tv_candidates[0].tmdb_id == "100"
    assert result.work_plan.selected_tv_series is not None
    assert result.work_plan.selected_tv_series.tmdb_id == "100"


@pytest.mark.asyncio
async def test_tmdb_details_fetch_uses_tmdb_season_list_not_llm_hints() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            )
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.98,
                        "reason": "episode number matches",
                        "file_infos": [
                            {
                                "file_name": "Example - 01.mkv",
                                "episode_number": 1,
                                "confidence": 0.98,
                                "reason": "episode number matches",
                            }
                        ],
                    }
                ],
                "ignored_files": [],
                "notes": [],
            },
        ]
    )
    tmdb = FakeTmdb(seasons=[0, 1, 2, 3])
    builder = LlmPlanBuilder(scanner, llm, tmdb, AppConfig())

    await builder.analyze(AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt=""))

    assert tmdb.fetched_seasons == [0, 1, 2, 3]


@pytest.mark.asyncio
async def test_mapping_validation_rejects_source_path_outside_candidate_list() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            )
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            {
                "schema_version": "1.0",
                "coverage_scope": [
                    {"type": "tv_season", "season_number": 1, "complete": True, "note": ""}
                ],
                "decisions": [
                    {
                        "folder_path": "",
                        "target_kind": "tv_episode",
                        "season_number": 1,
                        "tmdb_movie_id": "",
                        "confidence": 0.9,
                        "reason": "bad source",
                        "file_infos": [
                            {
                                "file_name": "NotProvided.mkv",
                                "episode_number": 1,
                                "confidence": 0.9,
                                "reason": "bad source",
                            }
                        ],
                    }
                ],
                "ignored_files": [],
                "notes": [],
            },
        ]
    )
    builder = LlmPlanBuilder(scanner, llm, FakeTmdb(), AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert result.status == "needs_review"
    assert result.work_plan is not None
    assert result.work_plan.rejected_mappings[0].reason == "source_not_in_candidate_list"


@pytest.mark.asyncio
async def test_llm_stage_error_includes_mapping_diagnostics() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            )
        ]
    )
    llm = FakeLlm(
        [
            {
                "schema_version": "1.0",
                "canonical_title": "Example",
                "aliases": [],
                "expected_components": ["tv"],
                "season_hints": [1],
                "reason": "folder title",
            },
            {
                "schema_version": "1.0",
                "selected_tv_series_id": "100",
                "selected_movie_ids": [],
                "season_numbers_to_fetch": [1],
                "needs_user_choice": False,
                "reason": "clear candidate",
            },
            ExternalServiceError(
                "LLM API request failed: ReadTimeout",
                details={"error_type": "ReadTimeout", "path": "/chat/completions"},
            ),
        ]
    )
    builder = LlmPlanBuilder(scanner, llm, FakeTmdb(), AppConfig())

    with pytest.raises(ExternalServiceError) as exc_info:
        await builder.analyze(
            AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
        )

    assert "LLM stage decide_mappings failed" in str(exc_info.value)
    assert exc_info.value.details["llm_stage"] == "decide_mappings"
    assert exc_info.value.details["input_chars"] > 0
    assert "hint" in exc_info.value.details


@pytest.mark.asyncio
async def test_unconfigured_llm_or_tmdb_returns_needs_review_plan() -> None:
    scanner = FakeScanner(
        [
            TreeFile(
                relative_path="Example - 01.mkv",
                path="/Inbox/Example/Example - 01.mkv",
                name="Example - 01.mkv",
                size=100,
            )
        ]
    )
    builder = LlmPlanBuilder(scanner, FakeUnconfiguredLlm(), FakeUnconfiguredTmdb(), AppConfig())

    result = await builder.analyze(
        AnalysisRequestItem(name="Example", path="/Inbox/Example", prompt="")
    )

    assert result.status == "needs_review"
    assert "LLM is not configured" in result.warnings[0]


class FakeScanner:
    def __init__(self, files: list[TreeFile]) -> None:
        self.files = files

    async def build_text_tree(
        self, root_path: str, *, max_depth: int, max_nodes: int
    ) -> TreeSnapshot:
        lines = [root_path.rstrip("/").split("/")[-1]]
        lines.extend(f"|-- {file.relative_path}" for file in self.files)
        return TreeSnapshot(
            text="\n".join(lines),
            nodes_scanned=len(self.files),
            truncated=False,
            files=self.files,
        )


class FakeLlm:
    is_configured = True

    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def chat_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0
    ) -> dict[str, object]:
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeUnconfiguredLlm:
    is_configured = False


class FakeTmdb:
    is_configured = True

    def __init__(
        self,
        seasons: list[int] | None = None,
        episode_names: dict[tuple[int, int], str] | None = None,
    ) -> None:
        self.seasons = seasons or [1]
        self.episode_names = episode_names or {}
        self.fetched_seasons: list[int] = []

    async def search_candidates(self, query: str, *, media_type: str, limit: int) -> list:
        if media_type != "tv":
            return []
        return [
            tmdb_candidate_from_result(
                {
                    "id": 100,
                    "name": "Example",
                    "first_air_date": "2024-01-01",
                    "overview": "TV candidate",
                },
                media_type="tv",
            )
        ]

    async def tv_details(self, tmdb_id: str) -> dict[str, object]:
        return {
            "id": 100,
            "name": "Example",
            "original_name": "Example",
            "first_air_date": "2024-01-01",
            "external_ids": {"tvdb_id": 200},
            "seasons": [
                {"season_number": season_number, "episode_count": 1}
                for season_number in self.seasons
            ],
        }

    async def season_details(self, tmdb_id: str, season_number: int) -> dict[str, object]:
        self.fetched_seasons.append(season_number)
        return {
            "id": 300,
            "season_number": season_number,
            "episodes": [
                {
                    "id": 400,
                    "episode_number": 1,
                    "name": self.episode_names.get((season_number, 1), "A Beginning"),
                    "air_date": "2024-01-01",
                }
            ],
        }

    async def movie_details(self, tmdb_id: str) -> dict[str, object] | None:
        return None


class FakeUnconfiguredTmdb:
    is_configured = False
