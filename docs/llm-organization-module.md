# LLM Organization Module

This document describes the LLM/TMDB dry-run analysis module. The current focus
is CLI-first: one command analyzes one OpenList inbox folder and writes all run
records to that command's run folder. The module turns the folder into a
validated `WorkPlan`; it does not copy, move, archive, or delete anything.

## Responsibility

`LlmPlanBuilder` contains the shared validation and planning rules. A top-level
source folder is treated as one work collection. The module may map TV episodes,
Season 0 specials, and movies into media-library targets, but only when the
mapping can be validated against TMDB.

The debug script `tools/llm_debug_pipeline.py` mirrors the same business flow
and writes all intermediate artifacts to disk for prompt and schema inspection.

## Production Flow

1. `identify_work`
   - Input: source folder name and optional user hint.
   - Output: canonical search title, aliases, expected components, and a short
     reason.
   - The schema accepts only `tv`, `movie`, and `special`; common anime aliases
     such as `ova`, `oad`, `ona`, and `sp` are normalized to `special`.

2. TMDB candidate search
   - Search both TV and Movie candidates by canonical title and aliases.
   - Deduplicate candidates by media type and TMDB ID.

3. Source tree scan
   - Scan the configured OpenList source path with `scan.tree_max_depth` and
     `scan.tree_max_nodes`.
   - Ignore configured folder names case-insensitively.
   - Build a candidate list containing video files only. Subtitle files are kept
     in the full tree snapshot for later program-side binding.

4. `select_candidates`
   - Input: identified work plus TMDB TV/Movie candidates.
   - Output: at most one selected TV series and zero or more selected movies.
   - Season numbers from the LLM are ignored for detail fetching. The program
     fetches season numbers from TMDB TV details.
   - If a TV series is selected, OVA/OAD/OAV/ONA/SP-like Movie candidates for
     that same series are pruned and left for TMDB Season 0 mapping.
   - Unknown or invalid candidate IDs make the analysis require review.

5. TMDB details fetch
   - Fetch selected TV details, then every TMDB season with an episode count
     greater than zero.
   - Fetch each selected movie detail.
   - TMDB details are the source of truth for seasons, episodes, titles, years,
     and external IDs.

6. `decide_mappings`
   - Input: identified work, compact TMDB details, compact season details,
     compact movie details, and grouped candidate files.
   - The full text tree is not sent to this stage; the grouped candidate list is
     the allowed source universe. It contains videos plus subtitles that could
     not be matched to a video by filename.
   - Output is strict JSON with grouped decisions:

```json
{
  "schema_version": "1.0",
  "coverage_scope": [
    {"type": "tv_season", "season_number": 1, "complete": true, "note": ""}
  ],
  "decisions": [
    {
      "folder_path": "Season Folder",
      "target_kind": "tv_episode",
      "season_number": 1,
      "tmdb_movie_id": "",
      "confidence": 0.98,
      "reason": "Folder is Season 1.",
      "file_infos": [
        {
          "file_name": "Example [01].mkv",
          "episode_number": 1,
          "confidence": 0.98,
          "reason": "Episode number matches."
        }
      ]
    }
  ],
  "ignored_files": [
    {
      "folder_path": "Extras",
      "file_name": "NCOP.mkv",
      "reason": "No concrete TMDB episode match."
    }
  ],
  "notes": []
}
```

7. Program validation
   - Expand grouped decisions back to concrete OpenList source paths.
   - Reject any file not present in the candidate list.
   - Reject subtitle files if the LLM tries to use them as primary media
     mappings.
   - Validate TV decisions against concrete TMDB season/episode pairs.
   - Validate Movie decisions against selected TMDB movie IDs.
   - Reject duplicate source mappings and duplicate target paths.
   - Bind subtitles after video validation by same-directory/same-prefix match.
   - Build missing TMDB episodes from TMDB's fetched episode list directly. Every
     TMDB season/episode, including Season 0, must have a mapped source file.
   - Mark selected movies with no mapped source file as missing.

## Status Rules

An analysis is `succeeded` when it produces validated mappings and has no review
reason. It becomes `needs_review` for candidate uncertainty, missing selected
movies, any missing TMDB episode, rejected mappings, no coverage scope, or no
validated mappings. Extra source videos that do not map to TMDB are allowed and
remain in `unmapped_files`; they do not require review by themselves. External
service errors, retry exhaustion, and strict schema parse failures fail the
analysis task.

## Output

The module returns an `AnalysisResult` containing:

- `work_plan`: the structured plan persisted with `plan_version: "1.0"`.
- `mappings`: validated file copy targets for the executor.
- `report_tree`: a dry-run text tree suitable for UI preview.
- `warnings`: concise user-visible analysis notes.

The executor consumes only validated `mappings`. Source files are never moved by
analysis. Organize still requires an explicit follow-up task.

## File-Based Run Records

The CLI path saves business request/response diagnostics to the run folder:

- `requests/01-identify_work.json`
- `requests/02-select_candidates.json`
- `requests/03-decide_mappings.json`
- matching extracted `.txt` prompt files
- `responses/01-identify_work.json`
- `responses/02-select_candidates.json`
- `responses/03-decide_mappings.json`

The CLI also writes artifacts under `data/runs/` by default:

- request JSON plus extracted readable message content files
- response JSON
- TMDB candidate/detail prompt inputs
- candidate file lists
- validated `work_plan.json`
- `dry_run_result_tree.txt`
- `organized_target_tree.txt`
- `analysis_result.json`
- `analysis_status.json`
- `timings.json` and `manifest.json`

Example:

```powershell
.\.venv\Scripts\python.exe tools\llm_debug_pipeline.py "[VCB-Studio] To LOVE-Ru"
```

Manual TMDB overrides remain available for diagnosis:

```powershell
.\.venv\Scripts\python.exe tools\llm_debug_pipeline.py "[Folder]" --tv-id 34742
```

Manual TMDB remapping is available after a run already exists:

```powershell
.\.venv\Scripts\python.exe tools\remap_run.py "data/runs/{old_run}" --tv-id 34742
```

The remap command creates a new run folder and links it to the original run via
`artifacts/manual_remap_request.json` and `analysis_status.json.source_run_dir`.
It reuses the original `identify_work` output and prompt, rescans the source
folder for current files, fetches details for the manually supplied TMDB IDs,
reruns only `decide_mappings`, then performs the same program validation. The
old run folder is kept immutable for diagnosis.

When the TMDB entry is correct but specific missing episodes need human review,
the remap command can consume `manual_episode_mappings.json` from the source run:

```json
{
  "schema_version": "1.0",
  "tv_tmdb_id": "35753",
  "movie_tmdb_ids": [],
  "episode_mappings": [
    {
      "folder_path": "Season Folder",
      "file_name": "Example [13(OVA)].mkv",
      "season_number": 0,
      "episode_number": 1,
      "reason": "Human review matched this file to TMDB S00E01."
    }
  ],
  "notes": []
}
```

This file is applied as a patch over the previous strict LLM mapping output.
The program removes any old decision or ignored-file entry for the same
`folder_path + file_name`, inserts the manual TV episode decision, and then runs
normal validation. Manual mappings are therefore still rejected if the file is
not in the candidate list or the target TMDB episode does not exist. Each remap
run stores its own copy of the mapping file for later diagnosis.

## Key Invariants

- TMDB is the source of truth for seasons and episode existence.
- Only validated video files and program-bound external subtitles enter the
  media library.
- Files that cannot be mapped to a concrete TMDB episode/movie stay out of the
  media library and are recorded as `unmapped_files`.
- Analyze is dry-run only; archive and media-library copy operations are outside
  this module.
