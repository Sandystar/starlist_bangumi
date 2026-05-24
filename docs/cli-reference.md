# CLI Reference

This document records the current CLI-first module surface for Starlist
Bangumi: command interfaces, parameters, runtime inputs/outputs, and the
implementation flow behind each command.

All examples assume PowerShell from the repository root:

```powershell
.\.venv\Scripts\python.exe tools\{command}.py ...
```

## Shared Runtime Contract

- `data/config.json` is the default configuration file unless a command exposes
  and receives `--config`.
- `data/runs/` is the source of truth for analysis history, remap history,
  validation results, organize status, logs, and prompt diagnostics.
- Analysis and remap commands do not perform OpenList write operations. They
  only read OpenList/TMDB/LLM data and write local run artifacts.
- Organize is the only current CLI that performs OpenList write operations.
- Markdown reports are generated under `data/reports/` by default and are not
  part of the canonical run history.
- Secret values from config, API keys, passwords, and Authorization headers are
  not intentionally written into run artifacts.

## Command Map

| Command | Module | Purpose | Mutates OpenList |
| --- | --- | --- | --- |
| `tools/llm_debug_pipeline.py` | Analysis | Analyze one inbox folder and create a dry-run work plan. | No |
| `tools/remap_run.py` | Manual remap | Create a new run by manually selecting TMDB IDs or episode mappings. | No |
| `tools/organize_run.py` | Organize | Execute archive and media-library copy operations for a run. | Yes |
| `tools/list_runs.py` | Run index | Query file-based run records. | No |
| `tools/export_report.py` | Report export | Export one run as a Markdown report. | No |
| `tools/cleanup_runs.py` | Run cleanup | Preview or delete local run folders. | No |
| `python -m starlist_bangumi` | Desktop/web entry | Start the legacy WebView/browser backend. | Depends on UI action |

## Analysis CLI

Command:

```powershell
.\.venv\Scripts\python.exe tools\llm_debug_pipeline.py "[Folder Name]"
```

Although the filename still contains `debug`, this is the current primary
analysis CLI. It saves detailed diagnostics because the LLM/TMDB mapping flow is
the core behavior being stabilized.

Implementation entry points:

- CLI orchestration: `tools/llm_debug_pipeline.py`
- Shared CLI helpers copied from the active flow:
  `src/starlist_bangumi/cli_runs.py`
- OpenList/TMDB/LLM clients: `src/starlist_bangumi/clients/`
- Source scanning: `src/starlist_bangumi/services/scanner.py`
- Mapping validation and plan generation:
  `src/starlist_bangumi/services/plan_builder.py`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `folder_name` | Yes | - | First-level folder name under `media_library.source_path`. |
| `--prompt TEXT` | No | empty | Extra user hint passed to LLM identification and mapping stages. |
| `--config PATH` | No | `data/config.json` | Config JSON path. |
| `--tv-id ID` | No | empty | Manually select a TMDB TV ID instead of letting LLM select candidates. |
| `--movie-id ID` | No | empty | Manually select a TMDB movie ID. Can be repeated. |
| `--seasons LIST` | No | empty | Comma-separated season numbers accepted by the CLI. Current validation ultimately uses seasons fetched from TMDB TV details. |
| `--auto-select-first-tv` | No | `false` | Debug shortcut: select the first TV candidate when `--tv-id` is absent. |
| `--no-auto-select-first-tv` | No | - | Explicitly disable the shortcut. |
| `--output-root PATH` | No | `data/runs` | Root directory for the generated run folder. |

Read inputs:

- `data/config.json` or `--config`.
- Prompt files in `src/starlist_bangumi/prompts/`.
- OpenList source folder:
  `{media_library.source_path}/{folder_name}`.
- TMDB search/details API.
- OpenAI-compatible LLM API.

Implementation flow:

1. Resolve the source OpenList path from `media_library.source_path` and
   `folder_name`.
2. Create a timestamped run folder under `--output-root`.
3. Save `extra_prompt.txt` and `run_input.json`.
4. Call LLM stage `01-identify_work` to identify canonical work title and
   aliases.
5. Search TMDB TV and Movie candidates using the identified title and aliases.
6. Scan the source folder tree and build the candidate list using configured
   video/subtitle extensions and ignored folder names. Videos are always
   included; subtitles are included only when they cannot be filename-matched
   to a video.
7. Call LLM stage `02-select_candidates`, unless manual IDs or
   `--auto-select-first-tv` decide selection locally.
8. Fetch selected TMDB TV, Movie, and TV season details. TV seasons are derived
   from TMDB details.
9. Call LLM stage `03-decide_mappings` with compact TMDB details and grouped
   candidate files.
10. Validate mapped files against the source candidates and fetched TMDB
    episodes/movies.
11. Bind same-name external subtitles after video validation.
12. Generate the `WorkPlan`, `AnalysisResult`, validation summary, dry-run tree,
    organized target tree, timings, and manifest.

Written files:

```text
data/runs/{timestamp}-{safe_folder_name}/
|-- manifest.json
|-- analysis_status.json
|-- timings.json
|-- requests/
|   |-- 01-identify_work.json
|   |-- 01-identify_work.message-*-content.txt
|   |-- 02-select_candidates.json
|   |-- 03-decide_mappings.json
|-- responses/
|   |-- 01-identify_work.json
|   |-- 02-select_candidates.json
|   `-- 03-decide_mappings.json
`-- artifacts/
    |-- run_input.json
    |-- extra_prompt.txt
    |-- identity.json
    |-- source_tree.txt
    |-- candidate_files.prompt.json
    |-- candidate_files.full.json
    |-- tmdb_candidates.full.json
    |-- tmdb_*_details.*.json
    |-- mapping_output.expanded.json
    |-- work_plan.json
    |-- analysis_result.json
    |-- validation_summary.json
    |-- dry_run_result_tree.txt
    `-- organized_target_tree.txt
```

Status behavior:

- `succeeded`: every fetched TMDB episode/movie required by the selected work
  has a validated mapping, at least one media mapping is validated, and no
  mapping decisions were rejected.
- `needs_review`: at least one fetched TMDB episode/movie is missing, or the
  mapping contains rejected program-validated decisions, or the LLM did not
  declare an acceptable coverage scope.
- Extra source videos may remain unmapped without causing review.
- If a stage raises, the command exits non-zero and keeps any partial
  diagnostics already written.

## Manual TMDB Remap CLI

Command:

```powershell
.\.venv\Scripts\python.exe tools\remap_run.py "data/runs/{old_run}" --tv-id 35753
```

Manual episode mapping command:

```powershell
.\.venv\Scripts\python.exe tools\remap_run.py "data/runs/{old_run}" --mapping-file "data/runs/{old_run}/artifacts/manual_episode_mappings.json"
```

Implementation entry points:

- CLI orchestration: `tools/remap_run.py`
- Shared helpers: `src/starlist_bangumi/cli_runs.py`
- Validation and target generation:
  `src/starlist_bangumi/services/plan_builder.py`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `run_dir` | Yes | - | Existing source run folder to remap. |
| `--config PATH` | No | Original run config path, then `data/config.json` | Config JSON path for rescanning and service calls. |
| `--tv-id ID` | Conditional | empty | Manual TMDB TV ID. Required unless supplied by mapping file or movie IDs. |
| `--movie-id ID` | Conditional | empty | Manual TMDB movie ID. Can be repeated. |
| `--mapping-file PATH` | No | `artifacts/manual_episode_mappings.json` when present | GUI-authored or hand-authored file mapping source files to TMDB episodes. |
| `--output-root PATH` | No | `data/runs` | Root directory for the new remap run folder. |

Read inputs from the source run:

- `artifacts/run_input.json`
- `artifacts/identity.json`
- `artifacts/extra_prompt.txt`
- `artifacts/tmdb_candidates.full.json`
- `artifacts/mapping_output.expanded.json` when applying manual episode
  mappings.

Manual episode mapping file shape:

```json
{
  "schema_version": "1.0",
  "tv_tmdb_id": "35753",
  "movie_tmdb_ids": [],
  "episode_mappings": [
    {
      "folder_path": "[VCB-Studio] Example Season",
      "file_name": "[VCB-Studio] Example [13(OVA)].mkv",
      "season_number": 0,
      "episode_number": 1,
      "reason": "Human review matched this file to TMDB S00E01."
    }
  ],
  "notes": ["Created from manual review."]
}
```

Implementation flow:

1. Load the original run inputs, identified work, and TMDB candidates.
2. Resolve manual TV/Movie IDs from CLI parameters or the mapping file.
3. Create a new timestamped run folder. The original run folder is not
   modified.
4. Rescan the original source folder with the current config.
5. Fetch TMDB details for the manual TV/Movie IDs.
6. If a manual episode mapping file exists, patch the previous
   `mapping_output.expanded.json` locally and skip the LLM mapping call.
7. If no mapping file exists, rerun only LLM stage `03-decide_mappings`.
8. Validate the resulting mapping with the same strict source/TMDB rules as a
   normal analysis run.
9. Write the same core analysis artifacts plus remap metadata.

Written files:

- New run folder under `--output-root`.
- Standard analysis artifacts listed in the Analysis CLI section.
- `artifacts/manual_remap_request.json`.
- `artifacts/manual_episode_mappings.json` when a mapping file was used.
- `analysis_status.json.source_run_dir` links the new run back to the old run.

Status and safety behavior:

- Performs no OpenList write operations.
- Does not mutate the source run.
- Still returns `needs_review` if TMDB episodes remain missing after manual
  mappings are applied.

## Organize CLI

Command:

```powershell
.\.venv\Scripts\python.exe tools\organize_run.py "data/runs/{run_id}"
```

Implementation entry points:

- CLI orchestration: `tools/organize_run.py`
- OpenList execution engine:
  `src/starlist_bangumi/services/executor.py`
- Analysis refresh helpers:
  `src/starlist_bangumi/services/plan_builder.py`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `run_dir` | Yes | - | Run folder containing `analysis_result.json` or `work_plan.json`. |
| `--config PATH` | No | `data/config.json` | Config JSON path. |
| `--allow-failed-analysis` | No | `false` | Confirm organizing a `needs_review` result. `failed` results are still blocked. |
| `--delete-target-before` | No | `false` | Delete existing same-name mapped media files before organizing. |
| `--overwrite-archive-target-before` | No | `false` | Delete the run-specific archive target folder before archiving. |
| `--delete-source-after` | No | `false` | Delete the source folder after archive and media verification succeeds. |
| `--resume-existing` | No | `false` | Resume an interrupted organize by skipping verified final targets and finishing staging renames. |

Read inputs:

- `artifacts/analysis_result.json` preferred.
- Fallback: `analysis_result.json` at run root.
- Fallback: `artifacts/work_plan.json` or `work_plan.json`.
- `artifacts/tmdb_season_details.raw.json` or `.prompt.json` for recalculating
  missing TMDB episodes when possible.
- OpenList source and target paths from the loaded plan.

Implementation flow:

1. Load and refresh the saved analysis result.
2. Recalculate status from the embedded `WorkPlan` and available TMDB season
   artifacts.
3. Block execution unless status is `succeeded`. A `needs_review` result may
   proceed only when `--allow-failed-analysis` is supplied; `failed` is always
   blocked.
4. Write `organize_status.json` with status `running`.
5. Run executor stages:
   `preflight -> archive copy -> media-library directory creation -> mapped
   file copy -> mapped file rename -> verify -> optional source deletion`.
6. Write human and structured logs during execution.
7. On success, write `organize_status.json` with status `succeeded`.
8. On failure, write `organize_status.json` with status `failed` and a
   structured error payload.

Written files:

- `organize_status.json`
- `organize_log.txt`
- `organize_log.jsonl`
- refreshed `artifacts/analysis_result.json`
- refreshed `analysis_status.json`
- refreshed `artifacts/organized_target_tree.txt`

OpenList write behavior:

- Archive target: copies the full source folder under
  `archive_path + archive_path_template + source folder name`.
- Media-library targets: copies only validated video/subtitle mappings.
- Source folder: deleted only after full verification and only with
  `--delete-source-after`.
- Existing mapped media files fail preflight unless `--delete-target-before` is set.
- Existing run-specific archive target fails preflight unless
  `--overwrite-archive-target-before` is set.
- `--resume-existing` cannot be combined with target cleanup options.

## Run Index CLI

Command:

```powershell
.\.venv\Scripts\python.exe tools\list_runs.py
```

Implementation entry point:

- `tools/list_runs.py`
- `src/starlist_bangumi/run_index.py`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--root PATH` | No | `data/runs` | Run folder root. |
| `--status VALUE` | No | empty | Filter by analysis status: `succeeded`, `needs_review`, `failed`, `unknown`. |
| `--organize-status VALUE` | No | empty | Filter by organize status: `not_started`, `running`, `succeeded`, `failed`. |
| `--source TEXT` | No | empty | Case-insensitive substring filter on source name/path. |
| `--latest-only` | No | `false` | Show only the newest run per source path/name. |
| `--limit N` | No | `50` | Maximum rows to show. |
| `--json` | No | `false` | Print JSON instead of a table. |

Read inputs:

- `manifest.json`
- `analysis_status.json`
- `organize_status.json`
- `artifacts/run_input.json`
- `artifacts/work_plan.json`
- `artifacts/analysis_result.json`
- `artifacts/manual_remap_request.json`

Implementation behavior:

- Scans immediate child directories under `--root`.
- Builds one `RunSummary` per run folder.
- Sorts by inferred creation time descending.
- Applies filters and optional latest-only grouping.
- Prints either a compact table or JSON.

Safety behavior:

- Read-only. Missing or malformed run files produce best-effort summaries
  instead of mutating the run.

## Markdown Report Export CLI

Command:

```powershell
.\.venv\Scripts\python.exe tools\export_report.py "data/runs/{run_id}"
```

Implementation entry point:

- `tools/export_report.py`
- `src/starlist_bangumi/report_exporter.py`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `run` | Yes | - | Run ID or run folder path. |
| `--run-root PATH` | No | `data/runs` | Root directory used when `run` is a run ID. |
| `--output-root PATH` | No | `data/reports` | Directory for Markdown reports. |
| `--overwrite` | No | `false` | Replace an existing report file. |

Read inputs:

- Run summary from `RunIndex`.
- `artifacts/analysis_result.json`.
- `artifacts/dry_run_result_tree.txt`.
- `artifacts/organized_target_tree.txt`.
- `organize_status.json` when present.

Implementation behavior:

1. Resolve `run` to a run ID.
2. Load run summary and full analysis result.
3. Render Markdown sections for status, source, targets, selected TMDB entries,
   validation counts, mappings, missing items, rejected mappings, unmapped
   files, warnings, preview trees, and organize status.
4. Write a report named:

```text
{timestamp}_{safe_title}_v{analysis_version}.md
```

Safety behavior:

- Read-only with respect to run folders and OpenList.
- Refuses to overwrite an existing report unless `--overwrite` is supplied.

## Run Cleanup CLI

Command:

```powershell
.\.venv\Scripts\python.exe tools\cleanup_runs.py --status failed
```

Implementation entry point:

- `tools/cleanup_runs.py`
- `src/starlist_bangumi/run_cleanup.py`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--root PATH` | No | `data/runs` | Run folder root. |
| `--status VALUE` | No | empty | Select runs by analysis status. |
| `--organize-status VALUE` | No | empty | Select runs by organize status. |
| `--older-than-days N` | No | empty | Select runs created at least this many days ago. |
| `--keep-latest-per-source N` | No | empty | Protect the newest N runs per source path/name. |
| `--include-manual` | No | `false` | Allow deleting runs containing `manual_episode_mappings.json`. |
| `--all` | No | `false` | Select every unprotected run. |
| `--execute` | No | `false` | Actually delete selected folders. Without this, dry-run only. |
| `--json` | No | `false` | Print JSON output. |

Filter rules:

- At least one filter or `--all` is required.
- Filters are combined with AND semantics.
- Runs with `artifacts/manual_episode_mappings.json` are protected unless
  `--include-manual` is supplied.
- `--keep-latest-per-source N` protects the newest N runs for each source and
  selects older matching runs when used with another filter or `--all`.

Implementation behavior:

1. Load run summaries from `RunIndex`.
2. Calculate protected latest runs per source.
3. Build cleanup candidates and reasons.
4. Print candidates as a table or JSON.
5. Delete candidate run folders only when `--execute` is supplied.

Safety behavior:

- Dry-run by default.
- Deletes local run folders only, never OpenList files.
- Refuses to delete paths outside the configured run root.
- Does not delete Markdown reports under `data/reports/`.

## Desktop/Web Entry CLI

Command:

```powershell
.\.venv\Scripts\python.exe -m starlist_bangumi
```

or, after installation:

```powershell
starlist-bangumi
```

Implementation entry points:

- `src/starlist_bangumi/main.py`
- `src/starlist_bangumi/api.py`
- `src/starlist_bangumi/static/`

Parameters:

| Parameter | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--host HOST` | No | `127.0.0.1` | Backend bind host. |
| `--port N` | No | `0` | Backend port. `0` means choose a free local port. |
| `--web` | No | `false` | Open in default browser instead of WebView. |

Implementation behavior:

1. Build the FastAPI app.
2. Start Uvicorn on the selected host/port in a background thread.
3. Wait for `/api/health`.
4. Open either a WebView window or the default browser.
5. Exit when the WebView closes or when interrupted in browser mode.

Current product status:

- The WebView exists for earlier experiments and manual inspection.
- The current MVP implementation surface is CLI-first. GUI task submission and
  retry endpoints are disabled or deferred while the file-based CLI workflow is
  stabilized.

## Stable Artifacts by Module

| Module | Stable local artifacts |
| --- | --- |
| Analysis | `requests/`, `responses/`, `artifacts/work_plan.json`, `artifacts/analysis_result.json`, `analysis_status.json`, preview trees |
| Remap | Same as analysis plus `artifacts/manual_remap_request.json` and optional `artifacts/manual_episode_mappings.json` |
| Organize | `organize_status.json`, `organize_log.txt`, `organize_log.jsonl`, refreshed analysis status |
| Run index | No writes |
| Report export | `data/reports/{timestamp}_{safe_title}_v{analysis_version}.md` |
| Cleanup | Deletes selected run folders only with `--execute` |
