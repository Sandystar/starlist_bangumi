from __future__ import annotations

import argparse
import json
from pathlib import Path

from starlist_bangumi.run_index import RunIndex, RunIndexFilters, RunSummary


def main() -> None:
    args = parse_args()
    runs = RunIndex(Path(args.root)).list_runs(
        RunIndexFilters(
            status=args.status,
            organize_status=args.organize_status,
            source=args.source,
            latest_only=args.latest_only,
            limit=args.limit,
        )
    )
    if args.json:
        print(
            json.dumps(
                [run.model_dump(mode="json") for run in runs],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print_runs(runs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List file-based Starlist run records.")
    parser.add_argument("--root", default="data/runs", help="Run folder root")
    parser.add_argument(
        "--status",
        default="",
        choices=["", "succeeded", "needs_review", "failed", "unknown"],
        help="Filter by analysis status",
    )
    parser.add_argument(
        "--organize-status",
        default="",
        choices=["", "not_started", "running", "succeeded", "failed"],
        help="Filter by organize status",
    )
    parser.add_argument("--source", default="", help="Filter by source name/path substring")
    parser.add_argument("--latest-only", action="store_true", help="Show latest run per source")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of runs to show")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    return parser.parse_args()


def print_runs(runs: list[RunSummary]) -> None:
    if not runs:
        print("No runs found.")
        return
    rows = [
        [
            run.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            run.analysis_status,
            run.organize_status,
            run.source_name[:36],
            run.title[:28],
            str(run.validated),
            str(run.missing_tmdb_episodes),
            run.run_id,
        ]
        for run in runs
    ]
    headers = ["created", "analysis", "organize", "source", "title", "ok", "miss", "run_id"]
    widths = [
        max(len(str(row[index])) for row in [headers, *rows])
        for index in range(len(headers))
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


if __name__ == "__main__":
    main()
