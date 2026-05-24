from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from starlist_bangumi.config import sanitize_openlist_segment
from starlist_bangumi.models import AnalysisResult, WorkPlan
from starlist_bangumi.run_index import RunIndex, RunSummary, load_json_object


@dataclass(frozen=True)
class ReportExportResult:
    path: Path
    markdown: str


class MarkdownReportExporter:
    """Exports a file-based run folder as a Markdown dry-run report."""

    def __init__(self, run_index: RunIndex, output_root: Path) -> None:
        self._run_index = run_index
        self._output_root = output_root

    def export_run(self, run_id: str, *, overwrite: bool = False) -> ReportExportResult:
        summary = self._summary_for(run_id)
        analysis = self._run_index.load_analysis(run_id)
        run_dir = Path(summary.run_dir)
        markdown = build_markdown_report(
            summary=summary,
            analysis=analysis,
            dry_run_tree=read_optional_text(run_dir / "artifacts" / "dry_run_result_tree.txt"),
            organized_tree=read_optional_text(
                run_dir / "artifacts" / "organized_target_tree.txt"
            ),
            organize_status=load_json_object(run_dir / "organize_status.json"),
        )
        path = self._report_path(summary, analysis)
        if path.exists() and not overwrite:
            raise FileExistsError(f"Report already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        return ReportExportResult(path=path, markdown=markdown)

    def _summary_for(self, run_id: str) -> RunSummary:
        summary = self._run_index.summarize(self._run_index.root / run_id)
        if summary is None:
            raise FileNotFoundError(f"Run folder not found: {run_id}")
        return summary

    def _report_path(self, summary: RunSummary, analysis: AnalysisResult) -> Path:
        timestamp = summary.created_at.strftime("%Y%m%d-%H%M%S")
        safe_title = safe_report_name(analysis.title or summary.title or summary.source_name)
        return self._output_root / f"{timestamp}_{safe_title}_v{analysis.analysis_version}.md"


def build_markdown_report(
    *,
    summary: RunSummary,
    analysis: AnalysisResult,
    dry_run_tree: str = "",
    organized_tree: str = "",
    organize_status: dict[str, object] | None = None,
) -> str:
    work_plan = analysis.work_plan
    lines: list[str] = [
        f"# {analysis.title or summary.title}",
        "",
        "## Summary",
        "",
        markdown_table(
            [
                ("Run", summary.run_id),
                ("Analysis status", str(summary.analysis_status)),
                ("Organize status", str(summary.organize_status)),
                ("Source", analysis.source_path),
                ("Media type", analysis.media_type),
                ("Analysis version", str(analysis.analysis_version)),
                ("Created at", summary.created_at.isoformat()),
            ]
        ),
        "",
        analysis.summary,
        "",
    ]
    if summary.review_reason:
        lines.extend(["**Review reason:** " + summary.review_reason, ""])
    if summary.error:
        lines.extend(["**Organize error:** " + summary.error, ""])
    lines.extend(path_section(summary, analysis, work_plan))
    lines.extend(tmdb_section(work_plan))
    lines.extend(validation_section(summary))
    lines.extend(warnings_section(analysis, work_plan))
    lines.extend(mapping_section(work_plan))
    lines.extend(missing_section(work_plan))
    lines.extend(rejected_section(work_plan))
    lines.extend(unmapped_section(work_plan))
    lines.extend(tree_section("Dry-run Tree", dry_run_tree or analysis.report_tree))
    lines.extend(tree_section("Organized Target Tree", organized_tree))
    if organize_status:
        lines.extend(organize_status_section(organize_status))
    return "\n".join(lines).rstrip() + "\n"


def path_section(
    summary: RunSummary,
    analysis: AnalysisResult,
    work_plan: WorkPlan | None,
) -> list[str]:
    library_targets = (
        [target.target_path for target in work_plan.library_targets]
        if work_plan
        else summary.library_targets
    )
    rows = [("Archive target", analysis.archive_target_path or summary.archive_target_path)]
    rows.extend((f"Library target {index}", path) for index, path in enumerate(library_targets, 1))
    return ["## Paths", "", markdown_table(rows), ""]


def tmdb_section(work_plan: WorkPlan | None) -> list[str]:
    if work_plan is None:
        return []
    rows: list[tuple[str, str]] = []
    if work_plan.selected_tv_series:
        tv = work_plan.selected_tv_series
        rows.append(("TV", f"{tv.title} ({tv.year}) [tmdbid={tv.tmdb_id}]"))
    rows.extend(
        ("Movie", f"{movie.title} ({movie.year}) [tmdbid={movie.tmdb_id}]")
        for movie in work_plan.selected_movies
    )
    if not rows:
        return []
    return ["## Selected TMDB", "", markdown_table(rows), ""]


def validation_section(summary: RunSummary) -> list[str]:
    return [
        "## Validation",
        "",
        markdown_table(
            [
                ("Validated mappings", str(summary.validated)),
                ("Rejected mappings", str(summary.rejected)),
                ("Missing TMDB episodes", str(summary.missing_tmdb_episodes)),
                ("Missing movies", str(summary.missing_movies)),
                ("Unmapped files", str(summary.unmapped_files)),
            ]
        ),
        "",
    ]


def warnings_section(analysis: AnalysisResult, work_plan: WorkPlan | None) -> list[str]:
    warnings = list(analysis.warnings)
    if work_plan:
        warnings.extend(warning for warning in work_plan.warnings if warning not in warnings)
    if not warnings:
        return []
    return ["## Warnings", "", *[f"- {escape_inline(warning)}" for warning in warnings], ""]


def mapping_section(work_plan: WorkPlan | None) -> list[str]:
    if not work_plan or not work_plan.validated_mappings:
        return []
    lines = ["## Validated Mappings", ""]
    for mapping in work_plan.validated_mappings:
        label = mapping_label(mapping.season_number, mapping.episode_number, mapping.tmdb_movie_id)
        source = escape_inline(mapping.source_path)
        target = escape_inline(mapping.target_path)
        lines.append(f"- `{source}` -> `{target}`")
        detail = " / ".join(
            part for part in [mapping.target_kind, label, mapping.episode_name] if part
        )
        if detail:
            lines.append(f"  - {escape_inline(detail)}")
        if mapping.reason:
            lines.append(f"  - {escape_inline(mapping.reason)}")
    lines.append("")
    return lines


def missing_section(work_plan: WorkPlan | None) -> list[str]:
    if work_plan is None:
        return []
    lines: list[str] = []
    if work_plan.missing_tmdb_episodes:
        lines.extend(["## Missing TMDB Episodes", ""])
        for episode in work_plan.missing_tmdb_episodes:
            lines.append(
                "- "
                f"S{episode.season_number:02d}E{episode.episode_number:02d} "
                f"{escape_inline(episode.episode_name)} - {escape_inline(episode.reason)}"
            )
        lines.append("")
    if work_plan.missing_movies:
        lines.extend(["## Missing Movies", ""])
        for movie in work_plan.missing_movies:
            lines.append(
                f"- {escape_inline(movie.title or movie.tmdb_movie_id)} "
                f"({escape_inline(movie.year or '0000')}) - {escape_inline(movie.reason)}"
            )
        lines.append("")
    return lines


def rejected_section(work_plan: WorkPlan | None) -> list[str]:
    if not work_plan or not work_plan.rejected_mappings:
        return []
    lines = ["## Rejected Mappings", ""]
    for mapping in work_plan.rejected_mappings:
        lines.append(
            f"- `{escape_inline(mapping.source_path)}` - "
            f"{escape_inline(mapping.reason)} {escape_inline(mapping.details)}".rstrip()
        )
    lines.append("")
    return lines


def unmapped_section(work_plan: WorkPlan | None) -> list[str]:
    if not work_plan or not work_plan.unmapped_files:
        return []
    lines = ["## Unmapped Files", ""]
    for item in work_plan.unmapped_files:
        lines.append(
            f"- `{escape_inline(item.source_path)}` - "
            f"{escape_inline(item.file_kind)} / {escape_inline(item.reason)}"
        )
    lines.append("")
    return lines


def tree_section(title: str, tree_text: str) -> list[str]:
    if not tree_text.strip():
        return []
    return [f"## {title}", "", "```text", tree_text.rstrip(), "```", ""]


def organize_status_section(status: dict[str, object]) -> list[str]:
    public_status = {
        key: value
        for key, value in status.items()
        if key in {"status", "updated_at", "elapsed_seconds", "options", "error"}
    }
    return [
        "## Organize Status",
        "",
        "```json",
        json.dumps(public_status, ensure_ascii=False, indent=2, default=str),
        "```",
        "",
    ]


def markdown_table(rows: list[tuple[str, str]]) -> str:
    lines = ["| Field | Value |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {escape_cell(key)} | {escape_cell(value)} |")
    return "\n".join(lines)


def mapping_label(
    season_number: int | None,
    episode_number: int | None,
    tmdb_movie_id: str,
) -> str:
    if season_number is not None and episode_number is not None:
        return f"S{season_number:02d}E{episode_number:02d}"
    if tmdb_movie_id:
        return f"tmdb_movie_id={tmdb_movie_id}"
    return ""


def safe_report_name(value: str) -> str:
    safe = sanitize_openlist_segment(value)
    return safe.replace(" ", "_")[:80] or "report"


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def escape_cell(value: object) -> str:
    return escape_inline(str(value)).replace("\n", "<br>")


def escape_inline(value: str) -> str:
    return value.replace("|", "\\|")
