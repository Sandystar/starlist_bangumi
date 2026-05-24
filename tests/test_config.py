from pathlib import Path

from starlist_bangumi.config import AppConfig, ConfigManager, render_path_template


def test_config_manager_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    manager = ConfigManager(path)
    config = AppConfig()
    config.openlist.base_url = "http://example.test/"

    manager.save(config)
    loaded = manager.load()

    assert loaded.openlist.base_url == "http://example.test"


def test_media_library_legacy_fields_migrate_to_tv_defaults() -> None:
    config = AppConfig.model_validate(
        {
            "media_library": {
                "media_library_path": "/Old/TV",
                "media_library_path_template": "{year}/{title}",
            }
        }
    )

    assert config.media_library.tv_media_library_path == "/Old/TV"
    assert config.media_library.tv_media_library_path_template == "{year}/{title}"
    assert config.media_library.movie_media_library_path == "/NetDisk/PikPak/Library/Bangumi-Movie"


def test_new_config_defaults_match_design() -> None:
    config = AppConfig()

    assert config.media_library.source_path == "/NetDisk/PikPak/Download"
    assert config.media_library.tv_media_library_path_template == (
        "{year}/{title} ({year}) [tmdbid={tmdb_id}]"
    )
    assert config.scan.tree_max_depth == 6
    assert config.scan.tree_max_nodes == 1000
    assert config.scan.ignored_folder_names == ["SPs", "Scans", "CDs"]
    assert ".mkv" in config.scan.video_extensions
    assert ".ass" in config.scan.subtitle_extensions


def test_scan_config_normalizes_ignored_folder_names_case_insensitively() -> None:
    config = AppConfig.model_validate(
        {"scan": {"ignored_folder_names": [" SPs ", "sps", "Scans", "", " CDs "]}}
    )

    assert config.scan.ignored_folder_names == ["SPs", "Scans", "CDs"]


def test_render_path_template_sanitizes_segments() -> None:
    rendered = render_path_template(
        "{year}/{title} ({year}) [tvdbid={tvdb_id}]",
        {"year": "2024", "title": "A/B: C", "tvdb_id": "123"},
    )

    assert rendered == "2024/A-B C (2024) [tvdbid=123]"
