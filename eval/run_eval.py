"""Golden-question eval runner - capture only, no scoring (Step 19A).

Runs every golden question from demo/golden_questions.json N times each
(default 3) against the live agent, resetting stream state and gating on
real incident-visibility between runs exactly like Step 18A's demo does, and
writes one fully self-contained JSON transcript file per invocation to
eval/results/. This step captures only - grading against the saved file is a
later step, not built here.

Reuses (never duplicates) every existing piece: demo.incident_control for
injection/clearing/gating mechanics (Step 19A's own refactor of Step 18A's
demo logic), agent.loop.run_turn for asking (not run_loop - this needs the
mutated `messages` list back for the transcript), mcp_server.stats.query_stats
and consumer.freshness.query_freshness via eval.ground_truth_queries for
independent ground truth, and demo/golden_questions.json as the only question
source (question text and incident params are never duplicated inline here).

Run with: `python -m eval.run_eval [--repeats N] [--questions id1,id2,...]`
or `make eval-run`.
"""

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from agent.loop import MODEL, run_turn
from agent.mcp_bridge import MCPBridge
from consumer.db import connect as db_connect
from demo.incident_control import (
    POST_CLEAR_PAUSE_SECONDS,
    POST_GATE_BUFFER_SECONDS,
    clear_incident,
    fraud_pattern_visible,
    gateway_failure_rate_elevated,
    inject,
    novel_error_visible,
    parse_tool_json,
    poll_until,
)
from eval.ground_truth_queries import GROUND_TRUTH_FUNCS
from producer.scenarios import NOVEL_ERROR_SIGNATURE

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_QUESTIONS_PATH = REPO_ROOT / "demo" / "golden_questions.json"
INCIDENTS_LOG_PATH = REPO_ROOT / "eval" / "ground_truth" / "incidents.jsonl"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

DEFAULT_REPEATS = 3

# incident_context["type"] -> which field in that dict is the incident's
# target, and how it maps onto producer.inject's real CLI args. Mirrors
# producer/inject.py's subcommands exactly (gateway_degradation --gateway
# --duration --severity, fraud_burst --duration --card-bin [--intensity],
# novel_error_pattern --duration --merchant --intensity).
_INCIDENT_TARGET_KEY = {
    "gateway_degradation": "gateway",
    "fraud_burst": "card_bin",
    "novel_error_pattern": "merchant",
}

# gateway_degradation and novel_error_pattern get the extra settle buffer
# after their gate passes (matching 18A/18B's proven timing); fraud_burst
# asks immediately once gated.
_POST_GATE_BUFFER_TYPES = {"gateway_degradation", "novel_error_pattern"}


def _inject_args(incident_type: str, ctx: dict) -> tuple[str, ...]:
    if incident_type == "gateway_degradation":
        return (
            "gateway_degradation",
            "--gateway", ctx["gateway"],
            "--duration", ctx["duration"],
            "--severity", ctx["severity"],
        )
    if incident_type == "fraud_burst":
        return (
            "fraud_burst",
            "--card-bin", ctx["card_bin"],
            "--duration", ctx["duration"],
        )
    if incident_type == "novel_error_pattern":
        return (
            "novel_error_pattern",
            "--merchant", ctx["merchant"],
            "--duration", ctx["duration"],
            "--intensity", ctx["intensity"],
        )
    raise ValueError(f"Unknown incident type: {incident_type!r}")


def _gate_check(incident_type: str, ctx: dict, bridge: MCPBridge):
    if incident_type == "gateway_degradation":
        return lambda: gateway_failure_rate_elevated(bridge, ctx["gateway"])
    if incident_type == "fraud_burst":
        return lambda: fraud_pattern_visible(bridge, ctx["card_bin"])
    if incident_type == "novel_error_pattern":
        return lambda: novel_error_visible(bridge, NOVEL_ERROR_SIGNATURE)
    raise ValueError(f"Unknown incident type: {incident_type!r}")


def _gate_label(incident_type: str, ctx: dict) -> str:
    if incident_type == "gateway_degradation":
        return f"{ctx['gateway']} failure-rate spike"
    if incident_type == "fraud_burst":
        return f"fraud burst on BIN {ctx['card_bin']}"
    if incident_type == "novel_error_pattern":
        return "novel error signature"
    raise ValueError(f"Unknown incident type: {incident_type!r}")


def _incident_records_since(
    inject_started_at: datetime, incident_type: str, target_value: str
) -> list[dict]:
    """Snapshot eval/ground_truth/incidents.jsonl records confirming this run's
    injection actually activated - independent confirmation from the scenario
    engine itself, not just that the CLI call returned. Missing/unreadable
    file or bad lines yield an empty list rather than raising."""
    if not INCIDENTS_LOG_PATH.exists():
        return []
    target_key = _INCIDENT_TARGET_KEY[incident_type]
    records = []
    try:
        lines = INCIDENTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != incident_type:
            continue
        if rec.get("params", {}).get(target_key) != target_value:
            continue
        try:
            ts = datetime.fromisoformat(rec["timestamp"])
        except (KeyError, ValueError):
            continue
        if ts >= inject_started_at:
            records.append(rec)
    return records


def _serialize_messages(messages: list) -> list[dict]:
    """Convert a run_turn `messages` list into JSON-native dicts.

    User messages' content is already plain (a string, or a list of plain
    tool_result dicts). Assistant messages' content is a list of Anthropic
    SDK Pydantic block objects (TextBlock/ToolUseBlock/...) - model_dump()
    each of those; anything already a plain dict passes through unchanged.
    """
    serialized = []
    for msg in messages:
        content = msg["content"]
        if isinstance(content, list):
            content = [
                block.model_dump() if hasattr(block, "model_dump") else block
                for block in content
            ]
        serialized.append({"role": msg["role"], "content": content})
    return serialized


def _extract_tool_calls(serialized_messages: list[dict]) -> list[dict]:
    """Flatten the transcript's tool_use/tool_result pairs into a convenience
    list of {name, arguments, result_text} - transcript.messages stays the
    full source of truth, this just saves later steps a raw-block walk."""
    calls: dict[str, dict] = {}
    order: list[str] = []
    for msg in serialized_messages:
        content = msg["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block["id"]] = {"name": block["name"], "arguments": block.get("input", {})}
                order.append(block["id"])
            elif block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if tool_use_id in calls:
                    calls[tool_use_id]["result_text"] = block.get("content")
    for call in calls.values():
        call.setdefault("result_text", None)
    return [calls[tid] for tid in order]


def _preflight() -> None:
    """Lightweight check that data is actually flowing - never launches
    producer/consumer itself (that's demo/run_demo.py's job, out of scope
    here)."""
    bridge = MCPBridge()
    text = bridge.call_tool("system_freshness", {"window_minutes": 1})
    data = parse_tool_json(text)
    if data.get("event_count", 0) <= 0:
        raise RuntimeError(
            "No recent events flowing - start the stack first (`make up` + "
            "`make produce` + `make consume`, or `make demo`) before running "
            "`make eval-run` / `python -m eval.run_eval`."
        )
    print(f"[preflight] data flowing: {data.get('human_readable', data)}")


def _git_commit() -> str:
    """Short commit SHA for run provenance, captured once at run time (not
    re-derived later by a report). Degrades to "unknown" rather than raising
    - a missing git binary or a non-repo checkout must never abort a real
    (API-cost-incurring) eval run over a metadata nicety."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _load_golden_questions() -> list[dict]:
    return json.loads(GOLDEN_QUESTIONS_PATH.read_text(encoding="utf-8"))


def _select_questions(all_questions: list[dict], questions_arg: str | None) -> list[dict]:
    if not questions_arg:
        return all_questions
    wanted = [qid.strip() for qid in questions_arg.split(",") if qid.strip()]
    by_id = {q["id"]: q for q in all_questions}
    unknown = [qid for qid in wanted if qid not in by_id]
    if unknown:
        raise SystemExit(f"Unknown question id(s): {unknown}. Valid ids: {list(by_id)}")
    return [by_id[qid] for qid in wanted]


def _run_one(
    question: dict, repeat_index: int, repeats: int, run_number: int, total_runs: int
) -> dict:
    qid = question["id"]
    print(f"[{run_number}/{total_runs}] {qid} repeat {repeat_index}/{repeats} ...")
    run_start = time.monotonic()

    # (a) Reset to a known state before anything else - clears whatever the
    # previous question/repeat may have left active.
    clear_incident()
    time.sleep(POST_CLEAR_PAUSE_SECONDS)

    incident_context = question.get("incident_context")
    inject_started_at = None

    # (b) Inject + gate on real visibility, if this question is incident-tagged.
    if incident_context is not None:
        incident_type = incident_context["type"]
        inject_started_at = datetime.now(UTC)
        gate_bridge = MCPBridge()
        inject(*_inject_args(incident_type, incident_context))
        poll_until(
            _gate_check(incident_type, incident_context, gate_bridge),
            _gate_label(incident_type, incident_context),
        )
        if incident_type in _POST_GATE_BUFFER_TYPES:
            time.sleep(POST_GATE_BUFFER_SECONDS)

    # (c) Ask - fresh bridge/tools/messages per run, timed.
    bridge = MCPBridge()
    tools = bridge.list_tools()
    messages: list = []
    asked_at = datetime.now(UTC)
    ask_start = time.monotonic()
    answer_text, response = run_turn(messages, question["question"], bridge, tools)
    wall_clock_seconds = time.monotonic() - ask_start

    # (d) Snapshot ground truth immediately after asking - independent SQL
    # plus (for incident questions) the scenario engine's own confirmation.
    captured_at = datetime.now(UTC)
    if incident_context is not None:
        incident_type = incident_context["type"]
        target_value = incident_context[_INCIDENT_TARGET_KEY[incident_type]]
        incident_records = _incident_records_since(inject_started_at, incident_type, target_value)
    else:
        incident_records = []

    gt_conn = db_connect()
    try:
        sql_ground_truth = GROUND_TRUTH_FUNCS[qid](gt_conn, incident_context)
    finally:
        gt_conn.close()

    serialized_messages = _serialize_messages(messages)
    tool_calls = _extract_tool_calls(serialized_messages)

    usage = None
    if response is not None:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    record = {
        "question_id": qid,
        "question_text": question["question"],
        "repeat_index": repeat_index,
        "incident_context": incident_context,
        "asked_at": asked_at.isoformat(),
        "wall_clock_seconds": round(wall_clock_seconds, 2),
        "final_turn_usage": usage,
        "answer_text": answer_text,
        "tool_calls": tool_calls,
        "transcript": {"messages": serialized_messages},
        "ground_truth": {
            "captured_at": captured_at.isoformat(),
            "incident_records": incident_records,
            "sql": sql_ground_truth,
        },
    }

    # (f) Clear before the next repeat/question - each repeat is an
    # independent trial and must not inherit this one's still-active incident.
    if incident_context is not None:
        clear_incident()
        time.sleep(POST_CLEAR_PAUSE_SECONDS)

    print(f"  done ({time.monotonic() - run_start:.1f}s)")
    return record


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the golden-question eval harness (capture only, no scoring)"
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help=f"Repeats per question (default: {DEFAULT_REPEATS})",
    )
    parser.add_argument(
        "--questions",
        type=str,
        default=None,
        help="Comma-separated golden-question ids to run (default: all, in file order)",
    )
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be a positive integer")
    return args


def main() -> None:
    args = _parse_args()

    print("=== Eval runner (capture only) ===")
    _preflight()

    # Captured now (after preflight, before the run loop) so a preflight
    # failure never produces a partial run_metadata block, and so provenance
    # reflects this run's actual state rather than being reconstructed later
    # from present-day source (see spec 20a-run-metadata for why that's wrong).
    run_started_at = datetime.now(UTC)
    golden_set_sha256 = hashlib.sha256(GOLDEN_QUESTIONS_PATH.read_bytes()).hexdigest()
    git_commit = _git_commit()

    all_questions = _load_golden_questions()
    questions = _select_questions(all_questions, args.questions)
    repeats = args.repeats
    total_runs = len(questions) * repeats

    runs = []
    run_number = 0
    for question in questions:
        for repeat_index in range(1, repeats + 1):
            run_number += 1
            runs.append(_run_one(question, repeat_index, repeats, run_number, total_runs))

    finished_at = datetime.now(UTC)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"run_{timestamp}.json"
    results = {
        "run_started_at": run_started_at.isoformat(),
        "repeats_per_question": repeats,
        "golden_questions_source": "demo/golden_questions.json",
        "runs": runs,
        "run_metadata": {
            "model": MODEL,
            "started_at": run_started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "repeats_per_question": repeats,
            "golden_set_path": "demo/golden_questions.json",
            "golden_set_sha256": golden_set_sha256,
            "git_commit": git_commit,
            "eval_config": {
                "post_clear_pause_seconds": POST_CLEAR_PAUSE_SECONDS,
                "post_gate_buffer_seconds": POST_GATE_BUFFER_SECONDS,
                "ground_truth_windows_source": "eval/ground_truth_queries.py",
            },
        },
    }
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\nWrote {out_path}")
    print(f"{total_runs} runs captured across {len(questions)} questions")


if __name__ == "__main__":
    main()
