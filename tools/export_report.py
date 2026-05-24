from __future__ import annotations

import argparse
from pathlib import Path

from starlist_bangumi.report_exporter import MarkdownReportExporter
from starlist_bangumi.run_index import RunIndex


def main() -> None:
    args = parse_args()
    run_root = Path(args.run_root)
    run_id = resolve_run_id(args.run, run_root)
    exporter = MarkdownReportExporter(
        RunIndex(run_root),
        output_root=Path(args.output_root),
    )
    result = exporter.export_run(run_id, overwrite=args.overwrite)
    print(f"Report exported: {result.path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a run folder as a Markdown report.")
    parser.add_argument("run", help="Run id or run folder path")
    parser.add_argument("--run-root", default="data/runs", help="Root directory of run folders")
    parser.add_argument(
        "--output-root",
        default="data/reports",
        help="Directory for Markdown reports",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing report file",
    )
    return parser.parse_args()


def resolve_run_id(value: str, run_root: Path) -> str:
    path = Path(value)
    if path.exists():
        return path.name
    candidate = run_root / value
    if candidate.exists():
        return candidate.name
    return value


if __name__ == "__main__":
    main()
