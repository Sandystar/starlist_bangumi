from pathlib import Path

import pytest

from starlist_bangumi.report_exporter import MarkdownReportExporter, build_markdown_report
from starlist_bangumi.run_index import RunIndex
from tests.test_run_index import write_run


def test_build_markdown_report_includes_core_sections(tmp_path: Path) -> None:
    run_dir = write_run(
        tmp_path,
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    index = RunIndex(tmp_path)
    summary = index.list_runs()[0]
    analysis = index.load_analysis(run_dir.name)

    markdown = build_markdown_report(
        summary=summary,
        analysis=analysis,
        dry_run_tree="dry-run\n`-- movie",
    )

    assert "# Example" in markdown
    assert "## Summary" in markdown
    assert "## Paths" in markdown
    assert "## Selected TMDB" in markdown
    assert "## Validated Mappings" in markdown
    assert "## Unmapped Files" in markdown
    assert "```text\ndry-run\n`-- movie\n```" in markdown


def test_export_run_writes_expected_filename(tmp_path: Path) -> None:
    write_run(
        tmp_path / "runs",
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    exporter = MarkdownReportExporter(
        RunIndex(tmp_path / "runs"),
        output_root=tmp_path / "reports",
    )

    result = exporter.export_run("20260519-010203-Example")

    assert result.path.name == "20260519-010203_Example_v1.md"
    assert result.path.exists()
    assert "| Source | /Inbox/Example |" in result.markdown


def test_export_run_refuses_overwrite_without_flag(tmp_path: Path) -> None:
    write_run(
        tmp_path / "runs",
        "20260519-010203-Example",
        source_path="/Inbox/Example",
        status="succeeded",
    )
    exporter = MarkdownReportExporter(
        RunIndex(tmp_path / "runs"),
        output_root=tmp_path / "reports",
    )
    exporter.export_run("20260519-010203-Example")

    with pytest.raises(FileExistsError):
        exporter.export_run("20260519-010203-Example")

    result = exporter.export_run("20260519-010203-Example", overwrite=True)

    assert result.path.exists()
