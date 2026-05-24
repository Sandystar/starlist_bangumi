from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import ValidationError

from starlist_bangumi.run_cleanup import CleanupCandidate, RunCleaner, RunCleanupOptions
from starlist_bangumi.run_index import RunIndex


def main() -> None:
    args = parse_args()
    try:
        options = RunCleanupOptions(
            status=args.status,
            organize_status=args.organize_status,
            older_than_days=args.older_than_days,
            keep_latest_per_source=args.keep_latest_per_source,
            include_manual=args.include_manual,
            all=args.all,
            execute=args.execute,
        )
    except ValidationError as exc:
        raise SystemExit(str(exc)) from exc

    cleaner = RunCleaner(RunIndex(Path(args.root)))
    result = cleaner.cleanup(options)
    if args.json:
        print(result.model_dump_json(indent=2))
        return
    print_candidates(result.candidates)
    if result.executed:
        print(f"Deleted: {len(result.deleted)}")
        if result.failed:
            print("Failed:")
            for item in result.failed:
                print(f"  {item['run_id']}: {item['error']}")
    else:
        print("Dry-run only. Add --execute to delete these run folders.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or delete file-based run folders.")
    parser.add_argument("--root", default="data/runs", help="Run folder root")
    parser.add_argument(
        "--status",
        default="",
        choices=["", "succeeded", "needs_review", "failed", "unknown"],
        help="Only clean runs with this analysis status",
    )
    parser.add_argument(
        "--organize-status",
        default="",
        choices=["", "not_started", "running", "succeeded", "failed"],
        help="Only clean runs with this organize status",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=None,
        help="Only clean runs created at least this many days ago",
    )
    parser.add_argument(
        "--keep-latest-per-source",
        type=int,
        default=None,
        help="Protect the newest N run folders per source path",
    )
    parser.add_argument(
        "--include-manual",
        action="store_true",
        help="Allow deleting runs that contain manual_episode_mappings.json",
    )
    parser.add_argument("--all", action="store_true", help="Select every unprotected run")
    parser.add_argument("--execute", action="store_true", help="Actually delete selected folders")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    return parser.parse_args()


def print_candidates(candidates: list[CleanupCandidate]) -> None:
    if not candidates:
        print("No run folders selected.")
        return
    rows = [
        [
            candidate.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            candidate.analysis_status,
            candidate.organize_status,
            "yes" if candidate.has_manual_episode_mappings else "no",
            candidate.run_id,
            candidate.reason,
        ]
        for candidate in candidates
    ]
    headers = ["created", "analysis", "organize", "manual", "run_id", "reason"]
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
