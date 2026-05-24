# Starlist Bangumi Open Design Items

Date: 2026-05-17

This document tracks the remaining design contracts that were identified after
the grill-me decision ledger in `docs/design-questions.md`. These are not new
product directions; they are the missing implementation-level details needed to
finish the MVP without ambiguity.

## Status Legend

- `Open`: design still needs a decision.
- `Ready`: design is clear enough to implement.
- `In Progress`: implementation has started.
- `Done`: implemented and covered by focused verification.

## O01 CLI Analysis Module

Status: `Done`

Implemented contract:

- `tools/llm_debug_pipeline.py` runs the complete CLI analysis flow for one
  first-level OpenList inbox folder.
- The analysis flow is three LLM stages plus TMDB lookup:
  `identify_work -> select_candidates -> decide_mappings`.
- TMDB details are the source of truth. For a selected TV series, the program
  fetches every TMDB season with `episode_count > 0`.
- LLM mapping receives grouped candidate files, not arbitrary source paths.
  Videos are always included; subtitles are included only when they cannot be
  matched to a video by filename.
- The program validates every TV mapping against a concrete TMDB season/episode
  and every movie mapping against a selected TMDB movie ID.
- Missing TV episodes are computed from the fetched TMDB episode list directly:
  every TMDB episode must have a validated mapping.
- Extra source videos are allowed. They remain in `unmapped_files` and do not
  require review by themselves.
- External subtitles are bound after video validation by same-directory and
  same-prefix matching.
- Each run writes file-based records under `data/runs/`:
  requests, extracted prompt text, responses, TMDB artifacts, source tree,
  `work_plan.json`, `analysis_result.json`, status summaries, timings, and
  preview trees.

Verification:

- `tests/test_plan_builder.py` covers strict LLM schemas, grouped candidate
  mapping, TMDB season-list fetching, subtitle binding, missing episode
  detection, and extra unmapped video behavior.

## O02 CLI Organize Execution

Status: `Done`

Implemented contract:

- `tools/organize_run.py` reads a run folder containing
  `artifacts/analysis_result.json` or `artifacts/work_plan.json`.
- When loading an existing `analysis_result.json`, the command recalculates
  status from the embedded `WorkPlan` and any available TMDB season artifact.
- Organize is blocked unless the recalculated analysis status is `succeeded`.
  `needs_review` requires explicit confirmation (`--allow-failed-analysis` in
  the CLI, "确认整理" in WebUI). `failed` cannot be organized.
- The executor performs OpenList operations in this order:
  preflight, archive copy, media-library directory creation, mapped file copy,
  mapped file rename, verification, optional source deletion.
- Media-library operations are copy-only. Source deletion happens only with
  `--delete-source-after` after successful verification.
- Existing mapped media files fail unless `--delete-target-before` is set.
- Existing archive targets fail unless `--overwrite-archive-target-before` is
  set.
- Failed or interrupted execution can be resumed with explicit
  `--resume-existing`.
- Resume mode skips already-created final archive/media targets, renames
  existing staging paths to their final names, and then verifies all outputs.
- Resume mode allows a missing source folder only when all final outputs are
  already present.
- Resume mode is mutually exclusive with media-target cleanup and archive
  overwrite options.
- Execution writes `organize_status.json`, `organize_log.txt`, and
  `organize_log.jsonl` back into the same run folder.

Verification:

- `tests/test_executor.py` covers media/archive conflict handling and cleanup
  option separation, resume skips, staging rename recovery, missing-source
  verification, and invalid resume/cleanup option combinations.
- `tests/test_organize_run.py` covers work-plan fallback loading, stale status
  recalculation, missing TMDB episode backfill from run artifacts, and persisted
  resume options.

## O03 Manual TMDB Remapping

Status: `Done for CLI`

Implemented contract:

- `tools/remap_run.py` accepts an existing run folder plus `--tv-id` and/or one
  or more `--movie-id` values.
- `tools/remap_run.py` can also read a GUI-authored
  `artifacts/manual_episode_mappings.json` file that maps specific source
  `folder_path + file_name` entries to TMDB TV `season_number + episode_number`.
- Remap creates a new run folder under `data/runs/`; it does not modify the
  original run folder.
- The command reuses the original `run_input.json`, `identity.json`,
  `extra_prompt.txt`, and saved TMDB candidate artifacts.
- The command rescans the original source path so subtitle binding and candidate
  validation use the current file list.
- The command fetches TMDB details for the manual IDs. For TV, seasons are
  derived from TMDB TV details, not from CLI season hints.
- The command reruns only `decide_mappings`, then applies the same program
  validation and writes `work_plan.json`, `analysis_result.json`,
  `analysis_status.json`, validation summaries, prompt diagnostics, timings,
  and preview trees.
- When a manual episode mapping file is present, the command patches the
  previous `mapping_output.expanded.json` instead of calling the LLM:
  old decisions for the same file are replaced, matching ignored-file entries
  are removed, and the patched result is still validated against TMDB/source
  candidates.
- Diagnostics link to the original run through
  `artifacts/manual_remap_request.json` and `analysis_status.json.source_run_dir`.
- The remap run stores a copy of the manual episode mapping file under its own
  `artifacts/manual_episode_mappings.json` for auditability.

Verification:

- `tests/test_remap_run.py` covers remap source artifact loading, required
  manual ID validation, manual TV/Movie selection parsing, default mapping-file
  discovery, manual episode mapping patch behavior, and source-run linkage in
  the generated status files.

Remaining GUI/API contract:

- Candidate-card UI state, manual ID input validation in the WebView, and a
  future API request shape remain deferred while the MVP is CLI-first.

## O04 Organize Options and Safety

Status: `Done`

Accepted direction:

- Media-library targets and archive target use separate destructive options.
- Media-library organization is copy-only.
- Existing mapped media files fail unless same-name file deletion is enabled.
- Existing archive leaf target fails unless archive target deletion is enabled.
- Source deletion happens only after the full pipeline succeeds and only when
  explicitly enabled.

Implementation contract:

- `delete_target_before` means delete only mapped media target files before
  copying, not the media-library series/movie folder.
- `overwrite_archive_target_before` means delete only the archive leaf target
  for this run before archive copy.
- Archive target path is `archive_path + archive_path_template + source folder
  name`.
- Batch organize may submit only succeeded analyses by default.
- Single organize may use `allow_failed_analysis` only as deliberate
  needs-review confirmation; failed analyses are still blocked.
- Preflight reports all conflicting target paths before failing.
- Verification checks that archive target exists and every mapped library file
  target exists after copy/rename.

Implemented in this slice:

- `delete_target_before` now removes only same-name mapped files.
- `overwrite_archive_target_before` controls deletion of the run-specific
  archive leaf folder.
- Existing mapped media files/archive leaf targets fail preflight unless the
  matching option is enabled.
- Execution order now follows archive copy before library copy and ends with
  verification.
- `resume_existing` is a non-destructive recovery mode and cannot be combined
  with cleanup options.

## O05 Executor Multi-Target Behavior

Status: `Done`

Accepted direction: one `WorkPlan` can produce multiple library targets.

Implementation contract:

- Executor reads `analysis.work_plan.library_targets` when present.
- Fallback to legacy `analysis.media_target_path` only when no work plan exists.
- Preflight checks every library target.
- Library directory creation happens per target parent as files are copied.
- The task summary path can keep `analysis.media_target_path` as the primary
  path, while the result detail shows all targets.

Implemented in this slice: executor reads `analysis.work_plan.library_targets`
when available and deduplicates target paths before preflight/cleanup.

## O06 Diagnostics Retention and Redaction

Status: `Done for CLI`

Accepted direction: save LLM business requests/responses by default for MVP,
with manual cleanup only.

Implemented contract:

- Production analysis persists the three LLM stage diagnostics:
  `llm.identify_work`, `llm.select_candidates`, and `llm.decide_mappings`.
- Stored request data includes prompt messages, input character count, model
  name, and timeout.
- Stored response data includes parsed JSON result or structured error details.
- Config secrets, API keys, passwords, and Authorization headers are not included.

Remaining GUI/API contract:

- API for listing diagnostics by task/result.
- UI display shape for concise logs versus folded diagnostics.
- Manual cleanup endpoint and confirmation wording.
- Store TMDB request metadata and response IDs/counts, not full descriptions by
  default, if TMDB diagnostics are added later.

## O07 Run Record Index and Query

Status: `Done for CLI`

Implemented contract:

- `data/runs/` is the source of truth for run history.
- SQLite task/result/diagnostic storage has been removed from the MVP runtime.
- `src/starlist_bangumi/run_index.py` scans run folders and returns
  `RunSummary` records.
- The index reads `manifest.json`, `analysis_status.json`,
  `organize_status.json`, `artifacts/run_input.json`, `artifacts/work_plan.json`,
  `artifacts/analysis_result.json`, and `artifacts/manual_remap_request.json`.
- The index supports status filtering, organize-status filtering, source
  substring filtering, latest-only grouping by source path, and direct
  `AnalysisResult` loading by run id.
- `tools/list_runs.py` exposes the index as a CLI table or JSON.
- The API `/api/results` and `/api/tasks` now read from the file index. GUI
  task submission/retry endpoints are disabled while the MVP is CLI-first.
- The generated local `data/app.db` file is removed.

Verification:

- `tests/test_run_index.py` covers summary extraction, filtering, latest-only
  grouping, and manual-remap metadata.
- `tests/test_api.py` covers disabled GUI task submission paths.

## O08 Markdown Report Export

Status: `Done for CLI`

Implemented direction: export dry-run reports to `data/reports/` with filename
`{timestamp}_{safe_title}_v{analysis_version}.md`.

Implemented contract:

- `src/starlist_bangumi/report_exporter.py` renders Markdown from a file-based
  run folder.
- `tools/export_report.py` exports one run by run id or run folder path.
- The default output root is `data/reports/`.
- Existing report files are not overwritten unless `--overwrite` is passed.
- Report content is derived from `RunSummary`, `analysis_result.json`,
  `work_plan.json`, optional preview trees, and optional `organize_status.json`.
- Diagnostic snippets are not embedded in MVP reports; the report points to the
  run id and summarizes status, paths, selected TMDB entries, validation
  counts, mappings, missing items, rejected mappings, unmapped files, warnings,
  dry-run tree, target tree, and organize status.

Implemented report sections:

- Summary.
- Source path and analysis version.
- Selected TMDB TV/Movie entries.
- Library targets.
- Archive target.
- Validated mappings.
- Missing TMDB episodes/movies.
- Rejected mappings.
- Unmapped files.
- Warnings.
- Dry-run tree.
- Organized target tree.
- Organize status when available.

Remaining GUI/API contract:

- Add a WebView export button and download/open behavior.
- Add an API route if the GUI is revived as an active workflow surface.

Verification:

- `tests/test_report_exporter.py` covers report content, filename generation,
  and overwrite protection.

## O09 Task Cancellation, Deduplication, and History

Status: `Open`

Accepted direction:

- Queued tasks can be cancelled.
- Running tasks cannot be force-cancelled in MVP.
- Duplicate queued/running analyses for the same source are skipped.
- Latest analysis per source is shown by default; history is behind a toggle.

Missing contract:

- Cancel API and cancelled task status value.
- Whether skipped duplicate submissions return the existing task or an empty
  skip record.
- Result-list API parameter for latest-only versus all history.
- UI affordance for cancelled/skipped states.

## O10 Scan Item Organized State

Status: `Open`

Accepted direction: if source remains after organize, mark it organized and do
not select by default; if source is deleted, it disappears on next scan.

Missing contract:

- Where organized state is persisted.
- Whether organized state is tied to source path, source name, analysis ID, or
  organize task ID.
- Whether a changed source folder should clear the organized marker.

Proposed implementation direction:

- Add `organized_sources(source_path, analysis_id, organized_at)` table.
- Mark after successful organize when source is not deleted.
- Scanner annotates first-level items with `organized: true`.

## O11 OpenList Refresh Rules

Status: `Ready`

Accepted direction: inbox scan uses config; subdirectories and preflight default
false.

Implementation contract:

- `scan_first_level`: `refresh=config.openlist.refresh_all_on_full_scan`.
- `build_text_tree`: root directory uses
  `refresh=config.openlist.refresh_all_on_full_scan` only when invoked as a full
  analysis scan; child directories use `refresh=False`.
- Preflight and execution use `refresh=False`.
- No per-task refresh override in MVP.

## O12 UI Detail Drawer and Manual Candidate Controls

Status: `Open`

Accepted direction: resource page is a three-column workbench with detail drawer.

Missing contract:

- Drawer section ordering and collapsed defaults.
- Manual TMDB ID input validation.
- Candidate card selection behavior.
- How batch organize options apply when result-specific warnings exist.
- Export report button placement.

## O13 Run Folder Cleanup

Status: `Done for CLI`

Accepted direction: Settings -> Tasks and Diagnostics contains irreversible
manual cleanup actions. The MVP implements the cleanup contract as CLI-first
run folder cleanup.

Implemented contract:

- `tools/cleanup_runs.py` previews or deletes file-based run folders.
- Default mode is dry-run; deletion requires `--execute`.
- The command requires `--all` or at least one filter.
- Supported filters: analysis status, organize status, age in days, and
  "keep latest N per source".
- Runs containing `artifacts/manual_episode_mappings.json` are protected by
  default and require `--include-manual` to delete.
- Cleanup removes the whole run folder, including diagnostics, analysis result,
  organize logs, and manual mapping artifacts inside that run.
- Markdown reports are not removed by this command.

Remaining GUI/API contract:

- Add Settings UI actions if the GUI becomes active again.
- Decide whether report cleanup should be a separate command or a flag.

Verification:

- `tests/test_run_cleanup.py` covers required filters, dry-run behavior,
  execute deletion, latest-per-source protection, and manual mapping
  protection.
