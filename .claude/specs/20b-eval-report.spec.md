# 20B — Eval metrics report (rebuilt)

## Feature

`eval/report.py` (+ `make eval-report`) reads a graded results file
(`eval/results/graded_<timestamp>.json`, Step 19B's output) plus the raw
capture file it references, and emits a Markdown report: headline metrics
with sample sizes, methodology, judged-vs-measured separation, tolerance
rationale, failure analysis, and reproduction commands. This is a rebuild of
a prior attempt that was reverted for two structural defects — see
"Design requirements (the fixes)" below, which are non-negotiable, not
suggestions.

## Context

- **What changed since the reverted attempt**: `eval/run_eval.py` (Step 20A,
  already committed) now writes a `run_metadata` block into every new
  `eval/results/run_<timestamp>.json` — see `eval/run_eval.py`'s results
  dict assembly (end of `main()`) for the exact shape:
  `{model, started_at, finished_at, repeats_per_question, golden_set_path,
  golden_set_sha256, git_commit, eval_config: {post_clear_pause_seconds,
  post_gate_buffer_seconds, ground_truth_windows_source}}`. This report
  must read every provenance fact from that block — it must never import
  `agent.loop`, never regex `agent/loop.py`'s source, never call
  `git rev-parse` itself. All of that is the runner's job now, done once,
  at run time.
- **Real data available for verification**: `eval/results/graded_20260715_011631.json`
  references `eval/results/run_20260715_004828.json`, which **predates**
  the Step 20A patch and therefore has **no `run_metadata` key at all**
  (confirmed: `json.loads(...)['runs']`'s sibling keys are
  `run_started_at`/`repeats_per_question`/`golden_questions_source`/`runs`
  only). This is the real-data test case for the "not recorded" fallback
  path — every provenance field must degrade to "not recorded" against
  this exact file, not guess or fall back to a different source.
  A second real capture, `eval/results/run_20260716_021745.json` (1
  question, 1 repeat, captured during 20A's own verification), **does**
  have a `run_metadata` block and can be used to hand-check the "recorded"
  path if useful, though it has never been graded (no matching
  `graded_*.json` exists for it, and per process rules 20B does not grade
  it — grading is optional, out of scope here).
- `eval/grade.py:124-217` (`grade_run()`) — shape of one `graded_runs`
  entry: `question_id`, `repeat_index`, `citation` (`cited_ids`,
  `valid_ids`, `fabricated_ids`, `ungrounded_but_real_ids`, `db_checked`,
  `passed`), `numeric_accuracy` (`attempted`, plus `categories` list for
  `aggregation` or a single value for `gateway_rate`, or just
  `{attempted: false}` otherwise), `routing` (`expected`, `used`,
  `passed`), `assertions` (list of `{assertion, tier, passed, detail|reason}`),
  `overall_mechanical_pass`, `failure_reasons`. Top-level graded file:
  `source_run_file`, `graded_at`, `config` (`count_tolerance_pct`,
  `rate_tolerance_abs`, `freshness_stale_threshold_seconds`), `graded_runs`,
  `summary` (`per_question_pass_rate`, `semantic_note`). **Unchanged from
  before** — `grade.py` itself is out of scope for this patch.
- `eval/mechanical_checks.py:431-438` (`ASSERTION_CHECKS`) — assertion
  order per question id. Index 0 is the "names/identifies the incident"
  assertion for `gateway_rate` and `fraud_pattern` (mechanical); for
  `novel_error`, index 0 is the one semantic (LLM-judged) assertion, index
  2 is mechanical ("cites at least one real transaction_id matching the
  novel error" — overlaps `ground_truth.sql.matching_transaction_ids`).
- `eval/ground_truth_queries.py:113-134` (`freshness()`) — the run file's
  `ground_truth.sql` for the `freshness` question already carries
  `{event_count, p50, p95, p99, max, window}`, captured via
  `consumer/freshness.py::query_freshness()` at eval-run time. This report
  reads that back — it makes no live DB call of its own.
- `demo/golden_questions.json` — `id`, `category`, `question`, `assertions`
  text, used only for display labels.
- `Makefile` — add `eval-report` alongside the existing `eval-run`/
  `eval-grade` targets (same `uv run python -m eval.X` style), and to the
  `.PHONY` line.

## Design requirements (the fixes — non-negotiable)

1. **`generate_report(graded_path, output_path=None) -> Path`** is the real
   entry point, importable from `eval.report`. `output_path` defaults to
   `eval/REPORT.md` only inside the CLI wrapper (`main()`), never inside
   the function's own default-argument logic in a way that hides a
   hardcoded write target — the function signature itself must make the
   write destination an explicit, caller-supplied value. No code path
   inside `generate_report` (or any helper it calls) may write to a
   filesystem path that isn't the `output_path` parameter it was given.
2. **Tests must be structurally incapable of touching the real
   `eval/REPORT.md`.** Every test calls `generate_report(graded_path,
   output_path=tmp_path / "REPORT.md")` (or invokes the CLI with an
   explicit output-path argument that maps to the same parameter — see
   Inputs/Outputs below for the CLI shape) and asserts against that
   `tmp_path` file. This is a structural guarantee, not a cleanup
   discipline: **no autouse save/restore fixture, no backing up and
   restoring the real file, no test that calls the CLI with zero arguments
   and relies on undoing the damage afterward.** If a test crashes mid-run,
   the real `eval/REPORT.md` must be unaffected, full stop — because the
   test never had a path to it in the first place.
3. **All provenance comes from `run_metadata` in the run file.** No
   `import agent.loop`, no reading `agent/loop.py`'s source (regex or
   otherwise), no `subprocess` call to git from inside `report.py` — the
   commit hash, model name, and dates are all read from
   `run_metadata.git_commit` / `.model` / `.started_at` / `.finished_at`.
   If `run_metadata` is entirely absent from the run file, or a specific
   field inside it is missing/null, print `not recorded` for that specific
   field — never fall back to a different source, never guess, never
   raise.

## Behavior

1. CLI: `python -m eval.report [graded_file] [--output PATH]` (mirrors
   `eval/grade.py`'s positional-arg pattern for `graded_file`; `--output`
   is new, defaulting to `eval/REPORT.md` if omitted). If no `graded_file`
   arg, pick the latest `eval/results/graded_*.json` by mtime, resolved
   relative to repo root like `grade.py` does. `make eval-report` runs it
   with no args (default graded file, default output path).
2. Load the graded file, then resolve and load its `source_run_file`
   (relative to repo root). If either file is missing/unparseable, exit
   with a clear message naming the missing file — don't crash with a raw
   traceback.
3. Headline metrics table, every row carrying an explicit `n`:
   - **Citation validity / hallucination rate** — aggregate `citation`
     across all `graded_runs`: total cited, valid, fabricated (the
     "want zero" count), ungrounded-but-real. Hallucination rate =
     `(fabricated + ungrounded_but_real) / total_cited`; if
     `total_cited == 0`, print "not measured (no citations issued)"
     instead of dividing by zero. Note if any run had `db_checked: false`.
   - **Aggregation accuracy** — split by unit, never blended: counts
     (`aggregation`'s `numeric_accuracy.categories`, `extracted is not None`)
     vs. rates (`gateway_rate`'s `numeric_accuracy`, `extracted is not None`).
     Each: % within tolerance and mean absolute error in that unit's own
     terms. Tolerance constants read from the graded file's own `config`
     block.
   - **Incident detection rate**, per incident type, never blended:
     `gateway_degradation` → `gateway_rate` assertion index 0;
     `fraud_burst` → `fraud_pattern` assertion index 0; `novel_error_pattern`
     → `novel_error` assertion index 2 (the mechanical proxy — index 0 for
     that question is semantic and belongs only in the judged subsection).
     State which assertion each row measures.
   - **Tool routing accuracy** — overall % of `routing.passed`, plus a
     per-question-id breakdown.
   - **Negative control** — `hallucination_control` runs': % with
     `overall_mechanical_pass == True`, plus explicit confirmation
     `citation.fabricated_ids` was empty for all of them.
   - **Freshness** — read from the run file's `ground_truth.sql` for the
     `freshness` question entries (`p50`/`p95`/`p99`/`max`/`event_count`/
     `window`). State the window and that it reflects the live stream at
     capture time, not a backlog drain. Show each repeat if more than one.
4. **Methodology** section, entirely from `run_metadata` (with "not
   recorded" per missing field, per Design requirement #3): model, run
   start/finish dates, repeats per question, golden set path + sha256,
   git commit, eval config (gating thresholds). Also: how ground truth was
   derived (independent SQL + `ground_truth.incident_records`), and the
   `asked_at`→`ground_truth.captured_at` gap across runs (computed from the
   run file's own per-run timestamps, not from `run_metadata` — this part
   is unchanged from before, it was never the problem).
5. **Measured vs judged** subsection, separate from the headline table:
   every `tier: "semantic"` assertion result (currently `novel_error`
   index 0) with its `reason`, labeled "LLM-judged — weaker evidence than
   the mechanical checks above." Never counted into any headline
   percentage.
6. **Tolerance rationale**: why numeric comparison uses a tolerance band —
   the stream advances between the agent's tool call and the grader's
   independent SQL snapshot. Cite the actual constants from `config`.
7. **Failure analysis**: every `graded_runs` entry with
   `overall_mechanical_pass == False` (question_id, repeat_index,
   `failure_reasons` verbatim). Surface `summary.per_question_pass_rate`
   as-is. **Known limitations**: sample size (read `repeats_per_question`
   from `run_metadata` if present, else the top-level `repeats_per_question`
   field, else "not recorded" — the top-level field still exists on every
   run file regardless of `run_metadata` presence, so this one has a
   working fallback chain rather than going straight to "not recorded"),
   single-environment caveat, stochastic-behavior caveat, and the
   timing-drift mechanism (general terms, not just an assertion it's fine).
8. **Reproduction**: the three commands (`make eval-run` → `make eval-grade`
   → `make eval-report`), and note `--repeats N` on `eval-run`.
9. Write the assembled Markdown to `output_path` (the parameter — never a
   hardcoded path inside the writing logic), and print its resolved path
   on success.

## Inputs / Outputs

- Python API: `eval.report.generate_report(graded_path: Path | str,
  output_path: Path | str | None = None) -> Path` — returns the path
  actually written to. This is what tests call.
- CLI: `python -m eval.report [graded_file] [--output PATH]`. No args =
  latest graded file, `eval/REPORT.md`. `make eval-report` = no args.
- No return/exit contract beyond: nonzero exit + clear message on a
  missing/unparseable graded file or missing referenced run file.

## Edge cases & errors

- No `eval/results/graded_*.json` files exist (no arg given) → clear
  message pointing at `make eval-run` / `make eval-grade`.
- Referenced `source_run_file` doesn't exist → clear message naming both
  files.
- `run_metadata` key entirely absent from the run file → every provenance
  line in Methodology prints "not recorded" (this is the real, current
  state of `eval/results/run_20260715_004828.json` — the primary
  real-data verification case).
- A specific field inside `run_metadata` is present but `null`/missing →
  that one field prints "not recorded", the rest of the block still
  renders normally.
- A metric's inputs are entirely absent for every run → "not measured" for
  that row, never a fabricated 0% or a silently omitted row.
- `total_cited == 0` → "not measured (no citations issued)", not `0/0`.
- `numeric_accuracy.extracted is None` for a category/rate → excluded from
  that metric's % and MAE denominator (extraction failure ≠ fail).
- Multiple `graded_*.json` files present, no arg given → latest by mtime.

## Out of scope

- Re-running or re-grading the agent, or running `eval/grade.py` against
  the new `run_20260716_021745.json` capture to manufacture a
  "run_metadata present" real graded fixture — consume existing graded
  files only, per process rules.
- The README (Step 21), demo video (Step 22).
- 20C (HNSW recall measurement) — separate sub-commit.
- Any change to `eval/grade.py`, `eval/mechanical_checks.py`, or
  `eval/run_eval.py` (already patched in 20A, committed).
- Historical trending across multiple graded files.
- Any autouse pytest fixture that saves/restores `eval/REPORT.md`, or any
  test that invokes the CLI/function without an explicit output path —
  both are exactly the pattern being designed out (Design requirement #2).

## Acceptance criteria

- `make eval-report` runs against the real
  `eval/results/graded_20260715_011631.json` and writes `eval/REPORT.md`.
- Every headline number has a visible `n`.
- The judged (`novel_error` semantic) result appears only in "Measured vs
  judged", never in the headline table or folded into a percentage.
- Freshness section shows the real p50/p95/p99 from the run file's
  captured `ground_truth.sql` — no live DB call from `report.py`.
- Against the real graded file (whose run file predates `run_metadata`),
  the Methodology section shows "not recorded" for model/commit/golden-set
  fields — this is the correct, expected output for that file, not a bug.
- Failure analysis lists the real failing runs (`gateway_rate`,
  `fraud_pattern`) with their actual `failure_reasons`.
- Reproduction section's three commands are correct.
- Running the full test suite (`uv run pytest tests/`) leaves the real
  `eval/REPORT.md` **byte-identical** to its state before the run — not
  "restored to be identical," genuinely never written during the test run.
  Verify this directly: hash or diff the file before and after running
  `uv run pytest tests/`.
- Works on Windows.
