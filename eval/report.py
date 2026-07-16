"""Eval metrics report generator (Step 20B) — rebuilt from scratch.

`python -m eval.report [graded_file] [--output PATH]` (or `make eval-report`,
no args, defaults to the latest `eval/results/graded_*.json` and writes
`eval/REPORT.md`) reads one graded file (Step 19B's output) plus the raw
capture file it references (`source_run_file`, Step 19A/20A's output) and
writes a Markdown report — pure aggregation/presentation over what those two
files already recorded, no new grading logic, no live DB or agent calls, no
git/subprocess calls of its own.

Two structural fixes vs. a prior, reverted attempt at this feature:

1. `generate_report(graded_path, output_path=None) -> Path` takes the write
   destination as an explicit parameter. `main()` (the CLI wrapper) is the
   only place that resolves an omitted `--output` to the real
   `eval/REPORT.md` (via `DEFAULT_OUTPUT_PATH`, also reused as
   `generate_report`'s own fallback for direct Python-API callers who pass
   none — the same single constant either way, never a second hardcoded
   literal). No helper below ever writes to disk; only `generate_report`
   itself does, and only to the `output_path` it resolved.
2. Every provenance fact (model, run start/finish, golden-set path+hash,
   git commit, gating config) is read from the run file's `run_metadata`
   block, written once by `eval/run_eval.py` at capture time — never
   `import agent.loop`, never regex `agent/loop.py`'s source, never a
   `subprocess` call to git from in here. A run file predating that patch
   (or missing an individual field) degrades to "not recorded" per field,
   never a guess and never a crash.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "eval" / "REPORT.md"

# incident_context["type"] -> (question_id, assertion index) of the one
# mechanical assertion that measures "did the agent detect this incident",
# per question. novel_error's index 0 is the LLM-judged assertion for that
# question, so its mechanical proxy is index 2 (cites a real matching
# transaction_id) — see Measured vs judged for index 0's separate treatment.
INCIDENT_DETECTION_MAP: dict[str, tuple[str, int]] = {
    "gateway_degradation": ("gateway_rate", 0),
    "fraud_burst": ("fraud_pattern", 0),
    "novel_error_pattern": ("novel_error", 2),
}


# --- loading -----------------------------------------------------------


def _load_graded_file(path: Path) -> dict:
    # Regular exceptions (not SystemExit) here on purpose: generate_report is
    # the documented Python API ("This is what tests call" per the spec's
    # Inputs/Outputs section), so its errors must be catchable with a plain
    # `except Exception` like any other library call — SystemExit deliberately
    # does not subclass Exception. main() (the CLI wrapper) is what turns
    # these into a clean, traceback-free exit for command-line users.
    if not path.exists():
        raise FileNotFoundError(f"Graded file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Graded file is not valid JSON: {path} ({exc})") from exc


def _resolve_run_path(graded: dict, graded_path: Path) -> Path:
    source = graded.get("source_run_file")
    if not source:
        raise ValueError(f"Graded file has no 'source_run_file' field: {graded_path}")
    # Stored relative path may carry Windows backslashes (this project's own
    # graded files are written on Windows) — normalize before joining.
    candidate = Path(str(source).replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate


def _load_run_file(run_path: Path, graded_path: Path) -> dict:
    if not run_path.exists():
        raise FileNotFoundError(
            f"Source run file not found: {run_path} (referenced by graded file {graded_path})"
        )
    try:
        return json.loads(run_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Source run file is not valid JSON: {run_path} ({exc})") from exc


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _field(d: dict | None, key: str):
    """`d[key]` if present and non-empty, else the literal "not recorded" —
    Design requirement #3: every provenance field degrades independently,
    never falls back to a different source, never guesses."""
    if not d:
        return "not recorded"
    value = d.get(key)
    if value is None or value == "":
        return "not recorded"
    return value


def _fmt_num(value, digits: int = 2) -> str:
    if isinstance(value, int | float):
        return f"{value:.{digits}f}"
    return "n/a"


def _asked_to_captured_gaps(run_data: dict) -> list[float]:
    gaps = []
    for r in run_data.get("runs", []):
        try:
            asked = datetime.fromisoformat(r["asked_at"])
            captured = datetime.fromisoformat(r["ground_truth"]["captured_at"])
        except (KeyError, TypeError, ValueError):
            continue
        gaps.append((captured - asked).total_seconds())
    return gaps


# --- headline metrics ----------------------------------------------------


def _citation_section(graded_runs: list[dict]) -> str:
    total_cited = total_valid = total_fabricated = total_ungrounded = 0
    any_unchecked = False
    for gr in graded_runs:
        c = gr["citation"]
        total_cited += len(c["cited_ids"])
        total_valid += len(c["valid_ids"])
        total_fabricated += len(c["fabricated_ids"])
        total_ungrounded += len(c["ungrounded_but_real_ids"])
        if not c["db_checked"]:
            any_unchecked = True

    lines = ["### Citation validity / hallucination rate", ""]
    lines.append(f"- n = {total_cited} citation(s) across {len(graded_runs)} run(s)")
    if total_cited == 0:
        lines.append("- Hallucination rate: not measured (no citations issued)")
    else:
        rate = (total_fabricated + total_ungrounded) / total_cited
        lines.append(f"- Valid: {total_valid}/{total_cited}")
        lines.append(f"- Fabricated (want zero): {total_fabricated}/{total_cited}")
        lines.append(f"- Ungrounded but real: {total_ungrounded}/{total_cited}")
        lines.append(
            f"- Hallucination rate: {rate:.1%} "
            "((fabricated + ungrounded_but_real) / total cited)"
        )
    if any_unchecked:
        lines.append(
            "- Note: at least one run had `db_checked: false` (DB existence "
            "corroboration was unavailable for that run's citations)."
        )
    lines.append("")
    return "\n".join(lines)


def _aggregation_accuracy_section(graded_runs: list[dict], config: dict) -> str:
    count_tol = config.get("count_tolerance_pct")
    rate_tol = config.get("rate_tolerance_abs")

    count_entries = [
        cat
        for gr in graded_runs
        if gr["question_id"] == "aggregation"
        for cat in gr["numeric_accuracy"].get("categories", [])
        if cat.get("extracted") is not None and cat.get("ground_truth") is not None
    ]
    rate_entries = [
        gr["numeric_accuracy"]
        for gr in graded_runs
        if gr["question_id"] == "gateway_rate"
        and gr["numeric_accuracy"].get("extracted") is not None
        and gr["numeric_accuracy"].get("ground_truth") is not None
    ]

    lines = ["### Aggregation accuracy", ""]
    lines.append(
        f"Tolerance (from this graded file's own `config`): counts within "
        f"{count_tol!r} relative, rates within {rate_tol!r} absolute. "
        "Counts and rates are never blended into one number."
    )
    lines.append("")
    if count_entries:
        n = len(count_entries)
        pct = sum(1 for c in count_entries if c["within_tolerance"]) / n
        mae = sum(abs(c["extracted"] - c["ground_truth"]) for c in count_entries) / n
        lines.append(
            f"- **Counts** (n={n} extracted category value(s), `aggregation` question): "
            f"{pct:.1%} within tolerance, mean absolute error {mae:.1f} (count units)"
        )
    else:
        lines.append("- **Counts**: not measured (no count values extracted)")
    if rate_entries:
        n = len(rate_entries)
        pct = sum(1 for r in rate_entries if r["within_tolerance"]) / n
        mae = sum(abs(r["extracted"] - r["ground_truth"]) for r in rate_entries) / n
        lines.append(
            f"- **Rates** (n={n} extracted rate value(s), `gateway_rate` question): "
            f"{pct:.1%} within tolerance, mean absolute error {mae:.1%} (percentage points)"
        )
    else:
        lines.append("- **Rates**: not measured (no rate values extracted)")
    lines.append("")
    return "\n".join(lines)


def _incident_detection_section(graded_runs: list[dict]) -> str:
    lines = ["### Incident detection rate", ""]
    lines.append(
        "One mechanical assertion per incident type, never blended across types "
        "(state which assertion each row measures):"
    )
    lines.append("")
    for incident_type, (qid, idx) in INCIDENT_DETECTION_MAP.items():
        runs = [gr for gr in graded_runs if gr["question_id"] == qid]
        if not runs:
            lines.append(
                f"- **{incident_type}** (via `{qid}` assertion #{idx}): "
                f"not measured (no `{qid}` runs)"
            )
            continue
        n = len(runs)
        assertions = runs[0]["assertions"]
        if len(assertions) <= idx:
            lines.append(
                f"- **{incident_type}** (via `{qid}` assertion #{idx}): "
                "not measured (assertion index out of range)"
            )
            continue
        assertion_text = assertions[idx]["assertion"]
        passed = sum(1 for gr in runs if gr["assertions"][idx]["passed"] is True)
        lines.append(
            f"- **{incident_type}** (n={n}, via `{qid}` assertion #{idx} — "
            f"{assertion_text!r}): {passed}/{n} passed ({passed / n:.0%})"
        )
    lines.append("")
    return "\n".join(lines)


def _routing_section(graded_runs: list[dict]) -> str:
    lines = ["### Tool routing accuracy", ""]
    if not graded_runs:
        lines.append("- not measured (no runs)")
        lines.append("")
        return "\n".join(lines)

    n_total = len(graded_runs)
    passed_total = sum(1 for gr in graded_runs if gr["routing"]["passed"])
    lines.append(
        f"- Overall (n={n_total}): {passed_total}/{n_total} ({passed_total / n_total:.0%})"
    )
    lines.append("")
    lines.append("| question_id | n | passed | rate |")
    lines.append("|---|---|---|---|")
    by_qid: dict[str, list[dict]] = {}
    for gr in graded_runs:
        by_qid.setdefault(gr["question_id"], []).append(gr)
    for qid, runs in by_qid.items():
        n = len(runs)
        passed = sum(1 for gr in runs if gr["routing"]["passed"])
        lines.append(f"| {qid} | {n} | {passed} | {passed / n:.0%} |")
    lines.append("")
    return "\n".join(lines)


def _negative_control_section(graded_runs: list[dict]) -> str:
    runs = [gr for gr in graded_runs if gr["question_id"] == "hallucination_control"]
    lines = ["### Negative control (`hallucination_control`)", ""]
    if not runs:
        lines.append("- not measured (no `hallucination_control` runs)")
        lines.append("")
        return "\n".join(lines)
    n = len(runs)
    passed = sum(1 for gr in runs if gr["overall_mechanical_pass"])
    all_no_fabrication = all(not gr["citation"]["fabricated_ids"] for gr in runs)
    lines.append(f"- n = {n}")
    lines.append(f"- Overall mechanical pass: {passed}/{n} ({passed / n:.0%})")
    lines.append(f"- `citation.fabricated_ids` empty for all runs: {all_no_fabrication}")
    lines.append("")
    return "\n".join(lines)


def _freshness_section(run_data: dict) -> str:
    runs = [r for r in run_data.get("runs", []) if r.get("question_id") == "freshness"]
    lines = ["### Freshness", ""]
    if not runs:
        lines.append("- not measured (no `freshness` runs in the source run file)")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "Read from the run file's captured `ground_truth.sql` for the `freshness` "
        "question — reflects the live stream at capture time, not a backlog drain. "
        "This report makes no live DB call of its own."
    )
    lines.append("")
    lines.append(f"n = {len(runs)} repeat(s).")
    lines.append("")
    lines.append("| repeat | window | event_count | p50 (s) | p95 (s) | p99 (s) | max (s) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in runs:
        sql = r.get("ground_truth", {}).get("sql") or {}
        if not sql:
            lines.append(f"| {r.get('repeat_index')} | not measured | - | - | - | - | - |")
            continue
        lines.append(
            f"| {r.get('repeat_index')} | {sql.get('window', 'n/a')} "
            f"| {sql.get('event_count', 'n/a')} | {_fmt_num(sql.get('p50'))} "
            f"| {_fmt_num(sql.get('p95'))} | {_fmt_num(sql.get('p99'))} "
            f"| {_fmt_num(sql.get('max'))} |"
        )
    lines.append("")
    return "\n".join(lines)


# --- other sections --------------------------------------------------------


def _methodology_section(run_data: dict, graded: dict) -> str:
    rm = run_data.get("run_metadata")
    eval_config = (rm or {}).get("eval_config")

    lines = ["## Methodology", ""]
    lines.append(f"- Model: {_field(rm, 'model')}")
    lines.append(f"- Run started: {_field(rm, 'started_at')}")
    lines.append(f"- Run finished: {_field(rm, 'finished_at')}")
    lines.append(f"- Repeats per question: {_field(rm, 'repeats_per_question')}")
    lines.append(f"- Golden set path: {_field(rm, 'golden_set_path')}")
    lines.append(f"- Golden set sha256: {_field(rm, 'golden_set_sha256')}")
    lines.append(f"- Git commit: {_field(rm, 'git_commit')}")
    lines.append(f"- Post-clear pause (s): {_field(eval_config, 'post_clear_pause_seconds')}")
    lines.append(f"- Post-gate buffer (s): {_field(eval_config, 'post_gate_buffer_seconds')}")
    lines.append(
        f"- Ground-truth windows source: {_field(eval_config, 'ground_truth_windows_source')}"
    )
    if rm is None:
        lines.append(
            "\n_This run file predates the `run_metadata` patch (Step 20A), so every "
            "field above is \"not recorded\" — this is correct, not a bug._"
        )
    lines.append("")
    lines.append(
        "Ground truth is derived independently of the agent's own tool calls: each "
        "golden question has a dedicated function in `eval/ground_truth_queries.py` "
        "that re-runs the equivalent query directly against Postgres, and incident "
        "questions additionally cross-check the scenario engine's own "
        "`ground_truth.incident_records` log — captured immediately after the agent "
        "answered, never re-derived from the agent's own tool output."
    )
    lines.append("")
    gaps = _asked_to_captured_gaps(run_data)
    if gaps:
        lines.append(
            f"- `asked_at` → `ground_truth.captured_at` gap across {len(gaps)} run(s): "
            f"min {min(gaps):.1f}s, mean {sum(gaps) / len(gaps):.1f}s, max {max(gaps):.1f}s "
            "(the delay between the agent's tool call and the grader's independent "
            "snapshot; see Tolerance rationale below)."
        )
    else:
        lines.append("- `asked_at` → `ground_truth.captured_at` gap: not measured (no runs)")
    lines.append("")
    return "\n".join(lines)


def _measured_vs_judged_section(graded_runs: list[dict]) -> str:
    lines = ["## Measured vs judged", ""]
    semantic_entries = [
        (gr, a) for gr in graded_runs for a in gr["assertions"] if a["tier"] == "semantic"
    ]
    if not semantic_entries:
        lines.append("No semantic (LLM-judged) assertions were present in this graded file.")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "Every semantic-tier assertion below was judged by an LLM, not a mechanical "
        "check — LLM-judged, weaker evidence than the mechanical checks above. Never "
        "counted into any headline percentage."
    )
    lines.append("")
    for gr, a in semantic_entries:
        lines.append(f"- **{gr['question_id']}** (repeat {gr['repeat_index']}): {a['assertion']}")
        lines.append(f"  - passed: {a['passed']}")
        lines.append(f"  - reason: {a.get('reason', 'n/a')}")
    lines.append("")
    return "\n".join(lines)


def _tolerance_rationale_section(config: dict) -> str:
    lines = ["## Tolerance rationale", ""]
    lines.append(
        "Numeric comparisons use a tolerance band, not exact equality, because the "
        "stream keeps advancing between the moment the agent makes its tool call and "
        "the moment the grader's independent SQL snapshot runs a few seconds later — "
        "on a live stream that gap alone can shift raw counts and rates measurably, "
        "especially mid-incident when a rate is actively ramping."
    )
    lines.append("")
    lines.append(f"- Count tolerance: {config.get('count_tolerance_pct', 'not recorded')} relative")
    lines.append(
        f"- Rate tolerance: {config.get('rate_tolerance_abs', 'not recorded')} absolute "
        "(percentage points)"
    )
    lines.append(
        "- Freshness staleness threshold: "
        f"{config.get('freshness_stale_threshold_seconds', 'not recorded')} seconds"
    )
    lines.append("")
    return "\n".join(lines)


def _failure_analysis_section(graded: dict, run_data: dict) -> str:
    graded_runs = graded.get("graded_runs", [])
    failing = [gr for gr in graded_runs if not gr["overall_mechanical_pass"]]

    lines = ["## Failure analysis", ""]
    if not failing:
        lines.append("All runs passed mechanical grading.")
        lines.append("")
    else:
        lines.append(f"{len(failing)} failing run(s):")
        lines.append("")
        for gr in failing:
            lines.append(f"### {gr['question_id']} (repeat {gr['repeat_index']})")
            for reason in gr["failure_reasons"]:
                lines.append(
                    f"- **{reason['check']}**: expected {reason['expected']!r}, "
                    f"actual {reason['actual']!r}"
                )
            lines.append("")

    lines.append("### Per-question pass rate")
    lines.append("")
    per_question = graded.get("summary", {}).get("per_question_pass_rate", {})
    for qid, rate in per_question.items():
        lines.append(f"- {qid}: {rate}")
    lines.append("")

    lines.append("### Known limitations")
    lines.append("")
    rm = run_data.get("run_metadata")
    if rm and rm.get("repeats_per_question") is not None:
        repeats_display = rm["repeats_per_question"]
    elif run_data.get("repeats_per_question") is not None:
        repeats_display = run_data["repeats_per_question"]
    else:
        repeats_display = "not recorded"
    lines.append(f"- Sample size: {repeats_display} repeat(s) per question.")
    lines.append(
        "- Single-environment caveat: all runs come from one local docker-compose "
        "environment, not a fleet of independent trials."
    )
    lines.append(
        "- Stochastic-behavior caveat: the agent's tool-use path and phrasing can vary "
        "run to run for the same question, so a single failing repeat is not proof of "
        "a systemic bug (and a single passing repeat is not proof of robustness)."
    )
    lines.append(
        "- Timing-drift mechanism: the agent's tool call and the grader's independent "
        "ground-truth snapshot happen seconds apart on a live stream, so any "
        "comparison against a moving target can drift outside tolerance even when the "
        "agent's underlying reasoning and tool use were both correct — most visible "
        "during actively-ramping incidents."
    )
    lines.append("")
    return "\n".join(lines)


def _reproduction_section() -> str:
    lines = ["## Reproduction", ""]
    lines.append("```")
    lines.append("make eval-run       # capture a fresh run (add --repeats N for more trials)")
    lines.append("make eval-grade     # grade the latest run against independent ground truth")
    lines.append("make eval-report    # regenerate this report from the latest graded file")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


# --- assembly + entry points ------------------------------------------------


def _build_report(graded: dict, run_data: dict, graded_path: Path, run_path: Path) -> str:
    graded_runs = graded.get("graded_runs", [])
    config = graded.get("config", {})

    parts = [
        "# Eval Metrics Report",
        "",
        f"Generated from `{_rel(graded_path)}` (source run: `{_rel(run_path)}`).",
        "",
        "## Headline metrics",
        "",
        _citation_section(graded_runs),
        _aggregation_accuracy_section(graded_runs, config),
        _incident_detection_section(graded_runs),
        _routing_section(graded_runs),
        _negative_control_section(graded_runs),
        _freshness_section(run_data),
        _methodology_section(run_data, graded),
        _measured_vs_judged_section(graded_runs),
        _tolerance_rationale_section(config),
        _failure_analysis_section(graded, run_data),
        _reproduction_section(),
    ]
    return "\n".join(parts).rstrip() + "\n"


def generate_report(graded_path: Path | str, output_path: Path | str | None = None) -> Path:
    """Read `graded_path` (+ the run file it references) and write a Markdown
    report to `output_path`, returning the path actually written.

    `output_path` is an explicit, caller-supplied value — the only default
    (`DEFAULT_OUTPUT_PATH`, i.e. `eval/REPORT.md`) is the same module-level
    constant `main()` uses, applied here only when a direct Python-API
    caller passes none at all. Every test must pass its own `output_path`
    (e.g. `tmp_path / "REPORT.md"`) so it never reaches this fallback.
    """
    graded_path = Path(graded_path)
    graded = _load_graded_file(graded_path)
    run_path = _resolve_run_path(graded, graded_path)
    run_data = _load_run_file(run_path, graded_path)

    markdown = _build_report(graded, run_data, graded_path, run_path)

    resolved_output = Path(output_path) if output_path is not None else DEFAULT_OUTPUT_PATH
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(markdown, encoding="utf-8")
    return resolved_output


def _find_latest_graded_file() -> Path:
    candidates = sorted(RESULTS_DIR.glob("graded_*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(
            f"No graded files found in {RESULTS_DIR} — run `make eval-run` then "
            "`make eval-grade` first."
        )
    return candidates[-1]


def _resolve_graded_path(arg: str | None) -> Path:
    if not arg:
        return _find_latest_graded_file()
    path = Path(arg)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Markdown eval metrics report from a graded results file"
    )
    parser.add_argument(
        "graded_file",
        nargs="?",
        default=None,
        help="Path to a graded_<timestamp>.json file (default: latest in eval/results/)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help=f"Output Markdown path (default: {DEFAULT_OUTPUT_PATH})",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    graded_path = _resolve_graded_path(args.graded_file)

    if args.output is None:
        output_path = DEFAULT_OUTPUT_PATH
    else:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = REPO_ROOT / output_path

    try:
        written = generate_report(graded_path, output_path)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Wrote {written}")


if __name__ == "__main__":
    main()
