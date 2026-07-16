# 20A (rebuilt) — Capture run metadata in the runner

## Feature

Patch `eval/run_eval.py` to write a `run_metadata` block into every
`eval/results/run_<timestamp>.json` it produces, so a later report can cite
real run-time provenance (model, commit, dates, config) instead of reading
present-day source or guessing. Purely additive — no existing top-level key
is removed or renamed, so old results files still load.

## Context

- `eval/run_eval.py:28` already does `from agent.loop import run_turn` —
  `agent.loop` (and its `MODEL = "claude-sonnet-5"` constant, line 31 of
  `agent/loop.py`) is therefore already imported into `run_eval.py`'s
  process for functional reasons (running the agent) before `main()` ever
  runs. Reading `agent.loop.MODEL` here costs nothing extra — this is
  different from `eval/report.py` (a separate, later process that has no
  functional need to import `agent.loop` at all, which is why *that*
  module must not import it or regex its source; it must read
  `run_metadata` instead).
- `eval/run_eval.py:358-366` (`main()`, end) — current top-level output
  shape: `run_started_at`, `repeats_per_question`, `golden_questions_source`
  (a fixed string, not derived from anything), `runs`. Leave all three
  exactly as they are; add a new sibling top-level key `run_metadata`.
- `eval/run_eval.py:31-41` — `demo.incident_control` imports:
  `POST_CLEAR_PAUSE_SECONDS`, `POST_GATE_BUFFER_SECONDS` are the runner's
  real numeric gating-config constants (the "gating thresholds" the eval
  config should record). Per-question time windows live in
  `eval/ground_truth_queries.py` (`aggregation`'s 5-minute window,
  `gateway_rate`'s 3-minute window, etc.) — record a pointer to that module
  rather than duplicating the numbers inline, since they vary per question
  and any inline copy would drift the moment that file changes.
- `GOLDEN_QUESTIONS_PATH` (`eval/run_eval.py:46`) is the file whose content
  a "golden set version" should hash — `demo/golden_questions.json`.

## Behavior

1. At the start of `main()` (after `_preflight()`, before the run loop, so
   a preflight failure doesn't produce a partial metadata block), capture:
   `finished_at` is set after the run loop completes, immediately before
   writing the results file — it is genuinely "when the run finished," not
   copied from `run_started_at`.
2. `run_metadata` dict, added as a new top-level key alongside the existing
   `run_started_at`/`repeats_per_question`/`golden_questions_source`/`runs`:
   - `model`: `agent.loop.MODEL`, read via the already-imported module
     (`from agent.loop import MODEL` at the top of the file, next to the
     existing `from agent.loop import run_turn`) — not a new import, not a
     regex, not hand-copied.
   - `started_at` / `finished_at`: UTC ISO timestamps (reuse the existing
     `run_started_at` value for `started_at` rather than capturing it
     twice independently).
   - `repeats_per_question`: same value as the existing top-level field.
   - `golden_set_path`: `"demo/golden_questions.json"` (same string as the
     existing `golden_questions_source` field).
   - `golden_set_sha256`: SHA-256 hex digest of `GOLDEN_QUESTIONS_PATH`'s
     raw bytes at run time — a real version fingerprint, cheap to compute,
     detects drift the path string alone can't.
   - `git_commit`: short SHA via `subprocess.run(["git", "rev-parse",
     "--short", "HEAD"], ...)`, captured at run time (not read later by a
     report). Degrade to `"unknown"` if git is unavailable or the call
     fails — never raise, never abort a real eval run over this.
   - `eval_config`: `{"post_clear_pause_seconds": POST_CLEAR_PAUSE_SECONDS,
     "post_gate_buffer_seconds": POST_GATE_BUFFER_SECONDS,
     "ground_truth_windows_source": "eval/ground_truth_queries.py"}`.
3. Write `run_metadata` into the results dict exactly once, at the same
   point the file is currently assembled (`eval/run_eval.py:360-365`).
4. Backward compatibility is the consuming side's job (a later report must
   print "not recorded" for missing fields), not this patch's — this patch
   only starts writing the block into *new* files. Existing
   `run_<timestamp>.json` files in `eval/results/` are untouched (never
   rewritten in place).

## Inputs / Outputs

- No CLI surface change — `eval/run_eval.py`'s existing arguments
  (`--repeats`, `--questions`) are unchanged.
- Output: same `eval/results/run_<timestamp>.json` file, now with one
  additional top-level key, `run_metadata`, shaped as in Behavior #2.

## Edge cases & errors

- `git rev-parse` unavailable/fails (not a git repo, git not on PATH,
  nonzero exit) → `git_commit: "unknown"`, the run still completes and
  writes its file normally.
- `GOLDEN_QUESTIONS_PATH` somehow unreadable at the metadata-capture point
  → this would already have failed earlier in `main()` (the file is loaded
  once via `_load_golden_questions()` before any runs execute), so no new
  failure mode is introduced; the hash step re-reads the same path the
  runner already successfully read.

## Out of scope

- Any change to `eval/grade.py`'s output shape or grading logic.
- Building `eval/report.py` (that's the next sub-commit, and it must read
  `run_metadata` from the *run* file — check whether `grade.py`'s output
  needs to forward/echo it, or whether the report reads the run file
  directly via `source_run_file` as before; either is fine as long as the
  report never falls back to importing `agent.loop` or reading source).
- Rewriting or migrating old `run_<timestamp>.json` files to add a
  synthetic `run_metadata` block — they simply lack one, and downstream
  code must handle that (not retrofitted here).

## Acceptance criteria

- A fresh `python -m eval.run_eval --repeats 1 --questions <small subset>`
  run produces a results file whose `run_metadata` block has all fields
  populated (or `"unknown"` for `git_commit` only if git is genuinely
  unavailable — it should not be in this environment).
- `model` matches `agent.loop.MODEL`'s actual value.
- `golden_set_sha256` is a 64-character hex string.
- Loading an existing pre-patch results file (e.g.
  `eval/results/run_20260715_004828.json`, which lacks `run_metadata`
  entirely) with plain `json.loads` still succeeds without error — this
  patch adds a key, it doesn't require one.
- Works on Windows (subprocess call for git, path handling, no POSIX-only
  assumptions).
