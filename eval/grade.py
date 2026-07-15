"""Eval grader (Step 19B) — offline scoring of a 19A capture file.

`python -m eval.grade [results_file]` (or `make eval-grade`, no-arg, latest
capture) reads `eval/results/run_<timestamp>.json`, grades every run across
three explicitly separated tiers — citation validity, numeric accuracy,
assertion checks (15 mechanical + 1 semantic) — plus tool routing, and writes
`eval/results/graded_<timestamp>.json` with a console pass-rate summary.

No agent calls here except the one LLM-judge completion (eval/llm_judge.py);
everything else is pure Python over what 19A already captured, plus exactly
one batched, parameterized DB lookup for citation existence
(consumer.db.connect()) that degrades to db_checked=False if unreachable
rather than crashing the run.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from eval.mechanical_checks import (
    ASSERTION_CHECKS,
    COUNT_TOLERANCE_PCT,
    FRESHNESS_STALE_THRESHOLD_SECONDS,
    NUMERIC_ACCURACY_FUNCS,
    RATE_TOLERANCE_ABS,
    check_citations,
    check_routing,
    extract_uuids,
    numeric_accuracy_not_attempted,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_QUESTIONS_PATH = REPO_ROOT / "demo" / "golden_questions.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _load_golden_questions() -> dict[str, dict]:
    questions = json.loads(GOLDEN_QUESTIONS_PATH.read_text(encoding="utf-8"))
    return {q["id"]: q for q in questions}


def _validate_assertion_counts(golden_questions: dict[str, dict]) -> None:
    """Catches silent drift between golden_questions.json and ASSERTION_CHECKS
    (Context section's requirement) — raised before any grading happens."""
    for qid, question in golden_questions.items():
        expected_len = len(question.get("assertions", []))
        actual_len = len(ASSERTION_CHECKS.get(qid, []))
        if expected_len != actual_len:
            raise RuntimeError(
                f"Assertion-checker count mismatch for question id {qid!r}: "
                f"demo/golden_questions.json has {expected_len} assertion(s) but "
                f"eval.mechanical_checks.ASSERTION_CHECKS has {actual_len} checker(s) "
                "registered for it. Update ASSERTION_CHECKS to match before grading."
            )


def _find_latest_run_file() -> Path:
    candidates = sorted(RESULTS_DIR.glob("run_*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit(
            f"No capture files found in {RESULTS_DIR} — run `make eval-run` "
            "(or `python -m eval.run_eval`) first."
        )
    return candidates[-1]


def _resolve_results_path(argv: list[str]) -> Path:
    if not argv:
        return _find_latest_run_file()
    path = Path(argv[0])
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        raise SystemExit(f"Results file not found: {path}")
    return path


def _connect_db():
    """Best-effort DB connection for the citation existence sub-check.

    Returns None (never raises) if the DB is unreachable — every run's
    citation result then reports db_checked: false instead of crashing the
    whole grade pass (per spec Edge cases).
    """
    try:
        from consumer.db import connect as db_connect

        return db_connect()
    except Exception as exc:  # noqa: BLE001 - deliberate blanket degrade, not a bug
        print(
            f"[grade] warning: could not connect to the DB for citation existence "
            f"corroboration ({exc}); every run will report db_checked=false and rely "
            "on the offline grounding check alone."
        )
        return None


def _db_existing_ids(conn, cited_ids: list[str]) -> set[str] | None:
    """Batched, parameterized existence lookup for one run's cited ids.

    Returns a lowercased set of transaction_id strings found in `transactions`,
    or None if the lookup itself failed (caller treats that as db_checked=false
    for this run, without tearing down the connection for subsequent runs).
    """
    if conn is None:
        return None
    if not cited_ids:
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT transaction_id FROM transactions "
                "WHERE transaction_id = ANY(%(ids)s::uuid[])",
                {"ids": cited_ids},
            )
            rows = cur.fetchall()
        return {str(row["transaction_id"]).lower() for row in rows}
    except Exception as exc:  # noqa: BLE001 - one bad lookup must not crash grading
        print(f"[grade] warning: citation existence lookup failed ({exc}); db_checked=false")
        return None


def grade_run(run: dict, golden_question: dict, conn) -> dict:
    qid = run["question_id"]
    answer_text = run.get("answer_text") or ""
    tool_calls = run.get("tool_calls", [])
    ground_truth = run.get("ground_truth") or {"sql": {}}
    incident_context = run.get("incident_context")

    cited_ids = extract_uuids(answer_text)
    existing_ids = _db_existing_ids(conn, cited_ids)
    db_checked = existing_ids is not None
    citation = check_citations(answer_text, tool_calls, existing_ids, db_checked)

    numeric_func = NUMERIC_ACCURACY_FUNCS.get(qid)
    numeric_accuracy = (
        numeric_func(answer_text, ground_truth.get("sql", {}))
        if numeric_func is not None
        else numeric_accuracy_not_attempted()
    )

    routing = check_routing(tool_calls, golden_question.get("tools_expected", []))

    ctx_base = {
        "answer_text": answer_text,
        "question_id": qid,
        "incident_context": incident_context,
        "ground_truth": ground_truth,
        "citation": citation,
        "numeric_accuracy": numeric_accuracy,
    }

    checkers = ASSERTION_CHECKS[qid]
    assertion_texts = golden_question["assertions"]
    assertions = []
    for checker, assertion_text in zip(checkers, assertion_texts, strict=True):
        ctx = {**ctx_base, "assertion_text": assertion_text}
        result = checker(ctx)
        assertions.append({"assertion": assertion_text, **result})

    mechanical_assertions_ok = all(
        a["passed"] is not False for a in assertions if a["tier"] == "mechanical"
    )
    numeric_ok = (not numeric_accuracy.get("attempted")) or numeric_accuracy.get("passed", True)
    overall_mechanical_pass = bool(
        citation["passed"] and numeric_ok and mechanical_assertions_ok and routing["passed"]
    )

    failure_reasons = []
    if not citation["passed"]:
        failure_reasons.append(
            {
                "check": "citation",
                "expected": "every cited transaction_id grounded in this run's tool results",
                "actual": {
                    "fabricated_ids": citation["fabricated_ids"],
                    "ungrounded_but_real_ids": citation["ungrounded_but_real_ids"],
                },
            }
        )
    if numeric_accuracy.get("attempted") and not numeric_accuracy.get("passed", True):
        failure_reasons.append(
            {
                "check": "numeric_accuracy",
                "expected": "extracted value(s) within tolerance of ground truth",
                "actual": numeric_accuracy,
            }
        )
    if not routing["passed"]:
        failure_reasons.append(
            {
                "check": "routing",
                "expected": routing["expected"],
                "actual": routing["used"],
            }
        )
    for assertion in assertions:
        if assertion["tier"] == "mechanical" and assertion["passed"] is False:
            failure_reasons.append(
                {
                    "check": f"assertion: {assertion['assertion']}",
                    "expected": "passed",
                    "actual": assertion.get("detail"),
                }
            )

    return {
        "question_id": qid,
        "repeat_index": run.get("repeat_index"),
        "citation": citation,
        "numeric_accuracy": numeric_accuracy,
        "routing": routing,
        "assertions": assertions,
        "overall_mechanical_pass": overall_mechanical_pass,
        "failure_reasons": failure_reasons,
    }


def _print_console_summary(output: dict, db_was_checked: bool) -> None:
    print("\n=== Per-question mechanical pass rate ===")
    for qid, rate in output["summary"]["per_question_pass_rate"].items():
        print(f"  {qid}: {rate}")
    print(f"\n{output['summary']['semantic_note']}")
    if not db_was_checked:
        print(
            "[grade] note: DB existence corroboration was skipped this run "
            "(db unreachable) — fabricated_ids/valid_ids relied on offline grounding only."
        )

    failing = [gr for gr in output["graded_runs"] if not gr["overall_mechanical_pass"]]
    if not failing:
        print("\nAll runs passed mechanical grading.")
        return
    print(f"\n=== Failing runs ({len(failing)}) ===")
    for gr in failing:
        print(f"  {gr['question_id']} (repeat {gr['repeat_index']}):")
        for reason in gr["failure_reasons"]:
            print(
                f"    - {reason['check']}: "
                f"expected {reason['expected']!r}, actual {reason['actual']!r}"
            )


def main() -> None:
    results_path = _resolve_results_path(sys.argv[1:])

    golden_questions = _load_golden_questions()
    _validate_assertion_counts(golden_questions)

    results = json.loads(results_path.read_text(encoding="utf-8"))
    runs = results.get("runs", [])

    conn = _connect_db()
    db_was_checked = conn is not None
    try:
        graded_runs = []
        for run in runs:
            golden_question = golden_questions.get(run["question_id"])
            if golden_question is None:
                # Defensive only — 19A only ever writes ids sourced from
                # golden_questions.json, so this shouldn't happen in practice.
                continue
            graded_runs.append(grade_run(run, golden_question, conn))
    finally:
        if conn is not None:
            conn.close()

    per_question_totals: dict[str, list[int]] = {}
    for graded in graded_runs:
        totals = per_question_totals.setdefault(graded["question_id"], [0, 0])
        totals[1] += 1
        if graded["overall_mechanical_pass"]:
            totals[0] += 1
    per_question_pass_rate = {
        qid: f"{passed}/{total}" for qid, (passed, total) in per_question_totals.items()
    }

    semantic_count = sum(
        1 for graded in graded_runs for a in graded["assertions"] if a["tier"] == "semantic"
    )
    semantic_note = (
        f"{semantic_count} semantic (LLM-judged) assertion(s) across {len(graded_runs)} run(s) "
        "— reported separately, never counted toward overall_mechanical_pass above."
    )

    try:
        source_run_file = str(results_path.relative_to(REPO_ROOT))
    except ValueError:
        source_run_file = str(results_path)

    graded_at = datetime.now(UTC)
    timestamp = graded_at.strftime("%Y%m%d_%H%M%S")
    output = {
        "source_run_file": source_run_file,
        "graded_at": graded_at.isoformat(),
        "config": {
            "count_tolerance_pct": COUNT_TOLERANCE_PCT,
            "rate_tolerance_abs": RATE_TOLERANCE_ABS,
            "freshness_stale_threshold_seconds": FRESHNESS_STALE_THRESHOLD_SECONDS,
        },
        "graded_runs": graded_runs,
        "summary": {
            "per_question_pass_rate": per_question_pass_rate,
            "semantic_note": semantic_note,
        },
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"graded_{timestamp}.json"
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

    _print_console_summary(output, db_was_checked)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
