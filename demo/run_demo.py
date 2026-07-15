"""Scripted demo orchestrator (Step 18A).

Brings the stack up cold if needed, establishes a calm baseline, then walks
through all three incident types on a fixed, deterministic schedule -
injecting each one, waiting for its signature to become genuinely observable
through the MCP tools, then asking the agent (agent.loop.run_loop) a grounded
question about it. Reuses every existing piece end to end: producer.inject
for injection, agent.mcp_bridge.MCPBridge for the demo's own readiness
checks, agent.loop.run_loop for every question asked to the agent. No new
retrieval, injection, or tool-use logic is built here.

Two different kinds of "wait" appear below, and they are NOT the same thing:

  - The baseline wait (BASELINE_WAIT_SECONDS) is a blind, fixed sleep. There
    is no signal to poll for "nothing is wrong yet" - the absence of an
    anomaly isn't something you can detect early, only something you can
    wait long enough to be reasonably confident of. So this one just counts
    seconds.
  - Every incident-visibility wait below (poll_until, from demo.incident_control)
    is signal-gated: it
    polls a real MCP tool and only proceeds once the incident's actual
    signature shows up in the data (or a bounded max wait is exceeded, in
    which case it warns and proceeds anyway rather than hanging forever).
    This is the genuine distributed-systems detail - injecting a control-file
    entry is not the same instant as that bias becoming visible in Postgres,
    because of the producer's poll interval, ingest lag, and the query
    window itself. Treating that gap as real (not cosmetic) is the whole
    point of gating on it instead of guessing a sleep duration.

Run with: `python -m demo.run_demo [--pause]` or `make demo`.
"""

import argparse
import subprocess
import time
from pathlib import Path

from agent.loop import run_loop
from agent.mcp_bridge import MCPBridge
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
from producer.config import CARD_BINS, MERCHANTS
from producer.scenarios import NOVEL_ERROR_SIGNATURE

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(__file__).resolve().parent / "logs"

COMPOSE_CMD = ["docker", "compose", "--env-file", ".env", "-f", "infra/docker-compose.yml"]
CONTAINERS = ["kafka", "postgres", "pgweb"]
DOCKER_MAX_WAIT_SECONDS = 90
DOCKER_POLL_INTERVAL_SECONDS = 3

# A cold start needs time for the embedding model to load plus at least one
# consumer batch commit; a slightly stale environment (e.g. a Kafka topic
# with leftover backlog from an earlier abandoned producer run) needs more -
# 90s gives real cold starts headroom without hanging indefinitely.
FRESHNESS_MAX_WAIT_SECONDS = 90
FRESHNESS_POLL_INTERVAL_SECONDS = 3

# Fixed, narrated wait before the first question - not gated on anything.
# See module docstring for why this one is deliberately a blind sleep.
BASELINE_WAIT_SECONDS = 30

# Generous fixed duration so an incident can't expire mid-demo before it is
# explicitly cleared in step (e) of the incident loop.
INCIDENT_DURATION = "3m"
# GATE_POLL_INTERVAL_SECONDS/GATE_MAX_WAIT_SECONDS/POST_CLEAR_PAUSE_SECONDS
# moved to demo/incident_control.py (Step 19A) - imported above.

# gateway_degradation and novel_error_pattern each use a stronger-than-CLI-default
# severity/intensity so the injected signal dominates a couple minutes of
# aggregate data even against this long-lived dev environment's residue from
# earlier verification runs (repeated stripe-proxy gateway_degradation testing in
# particular) and even if the model's own tool call picks a wide window. At the
# stream's ~20 events/sec baseline (~5/sec per gateway, ~1/sec per merchant),
# these values push the incident well clear of both the demo's own gate
# threshold and plain baseline noise within the gate's normal poll window,
# rather than leaving it a coin flip. fraud_burst keeps the CLI's own default
# intensity - that beat was already reliable both prior runs.
#
# 0.6 was tried first and measured live: the gate passed, but by question time
# the incident had only been actively running for ~15-45s out of the 3-minute
# window the question asks about, so the *window-averaged* failure rate (~12%)
# came out statistically indistinguishable from this environment's own
# accumulated 60-minute "chronic" stripe-proxy baseline (also ~12%, itself a
# residue artifact of many earlier verification runs against the same fixed
# target). 0.7 plus a much longer POST_GATE_BUFFER_SECONDS (below) is sized so
# the *active* portion of the 3-minute window dominates that math even with a
# generously-estimated ~15% residual baseline (see the worked estimate next to
# POST_GATE_BUFFER_SECONDS).
GATEWAY_DEGRADATION_SEVERITY = "0.7"
NOVEL_ERROR_INTENSITY = "0.35"

# Extra fixed wait *after* the real visibility gate already passed, for these
# two incidents only. This is additive to (not a replacement for) the gate:
# the gate still decides *whether/when* to proceed based on real data; this
# buffer just gives the now-confirmed-visible signal meaningfully more running
# time before the highest-stakes question is asked, because a signal that just
# barely tripped the gate a moment ago is still a small minority of the
# multi-minute window the golden question inspects.
#
# Sized deliberately larger than a token "few more seconds" pause: with
# severity=0.7 and an assumed (generous) ~15% residual baseline on the target
# gateway (window-avg = (residual*(180-T) + severity*T)/180 over a 3-minute
# question window), reaching ~T=85s of *active* incident time inside that
# window pushes the window average to roughly (0.15*95 + 0.7*85)/180 ~= 0.41
# (~41%) - unambiguously separated from both the ~15% residual baseline and
# the demo's own 0.12 gate threshold, rather than the ~12%-vs-12% coin flip
# measured with the old 15s buffer. 75s buffer + the few seconds the gate
# itself typically takes to pass gets us to roughly that T.
# (POST_GATE_BUFFER_SECONDS itself now lives in demo/incident_control.py,
# imported above, since eval/run_eval.py (Step 19A) reuses the same value.)

# Fixed, deterministic incident targets (never random) so the demo is
# repeatable run to run.
TARGET_GATEWAY = "stripe-proxy"
TARGET_CARD_BIN = CARD_BINS[0]
TARGET_MERCHANT = MERCHANTS[0]

GATEWAY_FAILURE_RATE_THRESHOLD = 0.12
FRAUD_MIN_MATCHING_ROWS = 5
FRAUD_MAX_AMOUNT = 5.00

BASELINE_QUESTION = "What are the top payment methods and gateways in the last 5 minutes?"
# Scoped to "the last 2-3 minutes" (like the fraud question's "12 most recent"
# scoping) so the model checks a window the just-injected incident actually
# dominates, rather than an open-ended "right now" that a wide default window
# (query_stats/get_transactions both default window_minutes to 30) would dilute
# against minutes of pre-incident baseline. Also deliberately steers toward a
# *peer-gateway* comparison ("higher than the others") rather than a
# self-history comparison ("higher than usual for this gateway"): live
# verification showed a fair, grounded model checking stripe-proxy's own
# 60-minute history and correctly reporting the injected spike as
# "chronic, not new" because this dev environment's residual stripe-proxy
# baseline is itself elevated from many earlier verification runs against the
# same fixed target - a true reading of that (confounded) self-history
# comparison, but not the demo's intent. The other three gateways carry no such
# residue, so a peer comparison is the axis that stays genuinely discriminating
# regardless of this environment's accumulated history. Still a real
# investigation, not a leading question - it must report actual gateway names
# and rates, and the "if so" leaves room for "no, they're all comparable."
GATEWAY_QUESTION = (
    "Looking specifically at the last 2-3 minutes, does any one payment gateway have a "
    "meaningfully higher failure rate than the others right now? If so, name the gateway "
    "and quantify its failure rate versus the others."
)
# Deliberately points at a small, concrete sample ("12 most recent") rather than
# an open-ended "check recent card transactions": the fraud signature is only
# visible by eyeballing raw get_transactions rows (there's no aggregate tool
# for card_bin), and a broad/unbounded phrasing tends to make the model pull a
# large row dump (limit=30-100) to reason over - which, combined with this
# model's extended-thinking token usage, can exhaust the loop's per-turn
# MAX_TOKENS budget before it finishes writing an answer. A small, bounded
# sample keeps the same investigation grounded and correct while comfortably
# fitting the response budget - verified live (see coder's report).
FRAUD_QUESTION = (
    "Look at the 12 most recent successful card transactions. Do several of them "
    "share the same card BIN and have small amounts (under $5)? If so, name the "
    "BIN and cite the matching transaction IDs, briefly."
)
# Same window-tightening as GATEWAY_QUESTION above, plus one more deliberate
# reframe: live verification showed the fixed NOVEL_ERROR_SIGNATURE constant
# (producer/scenarios.py - out of scope to change) has itself now genuinely
# recurred multiple times in this dev environment's real history from earlier
# verification runs targeting the same fixed merchant, so a model that
# (reasonably) checks a wide history window can correctly find it "already
# seen" and conclude "not new" - a true reading of a now-confounded premise,
# not a model miss. "Occurring often enough to stand out, different in kind
# from the usual decline/timeout/fraud reasons" keeps the same investigation
# (find the distinctive structured error text among ordinary ones, via
# semantic_search or get_transactions, and cite real transaction_ids) without
# resting on literal all-time uniqueness, which this reused dev DB can no
# longer guarantee. Still genuine - it must find and quantify a real pattern,
# not agree with a leading premise.
#
# Bounded like FRAUD_QUESTION for the same reason: live verification showed an
# unbounded phrasing let the model escalate through several tool calls ending
# in a get_transactions(limit=100) dump, then exhaust MAX_TOKENS (thinking +
# visible text share one per-turn budget in agent/loop.py, out of scope to
# change) reasoning over it - producing a blank final answer, not a wrong one.
# Explicitly bounding the sample and asking for brevity keeps the same
# investigation (still has to find and quantify a real pattern, still can
# answer "no") while fitting comfortably in the response budget.
NOVEL_QUESTION = (
    "Check at most the 15 most recent failures (via get_transactions and/or "
    "semantic_search) from the last 2-3 minutes. Is there an error message among them "
    "that's different in kind from the usual decline/timeout/fraud reasons in this "
    "stream, and occurring often enough to stand out (not just an isolated one-off)? "
    "If so, describe the pattern briefly and cite up to 3 matching transaction IDs."
)
FRESHNESS_QUESTION = "How current is this data right now?"
HALLUCINATION_QUESTION = "How many crypto payments failed today?"


def _beat(pause: bool, message: str) -> None:
    """Print a narrated beat and, in --pause mode, wait for Enter before continuing."""
    print(f"\n{message}")
    if pause:
        input("  (press Enter to continue) ")


def _running_containers() -> set:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=False
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _container_health(name: str) -> str:
    """Return 'healthy'/'unhealthy'/'starting' for containers with a healthcheck,
    or the plain container status (e.g. 'running') for ones without (pgweb)."""
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _containers_ready() -> bool:
    running = _running_containers()
    if not all(name in running for name in CONTAINERS):
        return False
    return all(_container_health(name) in ("healthy", "running") for name in CONTAINERS)


def _ensure_containers_up() -> None:
    """Bring kafka/postgres/pgweb up (same command `make up` runs) if not already running."""
    running = _running_containers()
    missing = [name for name in CONTAINERS if name not in running]
    if not missing:
        print(f"[preflight] containers already up: {', '.join(CONTAINERS)}")
    else:
        print(f"[preflight] missing containers {missing} - running docker compose up -d ...")
        subprocess.run(COMPOSE_CMD + ["up", "-d"], cwd=REPO_ROOT, check=True)

    deadline = time.monotonic() + DOCKER_MAX_WAIT_SECONDS
    while True:
        if _containers_ready():
            print("[preflight] all containers running and healthy.")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Containers not healthy after {DOCKER_MAX_WAIT_SECONDS}s: "
                f"{[(n, _container_health(n)) for n in CONTAINERS]}"
            )
        time.sleep(DOCKER_POLL_INTERVAL_SECONDS)


def _check_freshness(bridge: MCPBridge, window_minutes: int = 1) -> dict:
    text = bridge.call_tool("system_freshness", {"window_minutes": window_minutes})
    return parse_tool_json(text)


def _ensure_data_flowing(bridge: MCPBridge) -> None:
    """Confirm events are actually landing in Postgres, starting producer/consumer if not.

    Left running after the demo ends (matches the existing dev workflow where
    these are long-lived processes) - this script never tears them down.
    """
    print("[preflight] checking whether data is flowing...")
    fresh = _check_freshness(bridge, window_minutes=1)
    if fresh.get("event_count", 0) > 0:
        print(f"[preflight] data already flowing: {fresh.get('human_readable', fresh)}")
        return

    print("[preflight] no recent events - launching producer and consumer in the background...")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Popen duplicates the file's OS-level handle into the child at launch time,
    # so it's safe to close these in the parent right after - the child's copy
    # keeps writing independently (true on both Windows and POSIX).
    with open(LOG_DIR / "producer.log", "a", encoding="utf-8") as producer_log:
        subprocess.Popen(
            ["uv", "run", "python", "-m", "producer.main"],
            cwd=REPO_ROOT,
            stdout=producer_log,
            stderr=subprocess.STDOUT,
        )
    with open(LOG_DIR / "consumer.log", "a", encoding="utf-8") as consumer_log:
        subprocess.Popen(
            ["uv", "run", "python", "-m", "consumer.main"],
            cwd=REPO_ROOT,
            stdout=consumer_log,
            stderr=subprocess.STDOUT,
        )
    print(f"[preflight] launched producer + consumer (logs under {LOG_DIR})")

    deadline = time.monotonic() + FRESHNESS_MAX_WAIT_SECONDS
    while True:
        time.sleep(FRESHNESS_POLL_INTERVAL_SECONDS)
        fresh = _check_freshness(bridge, window_minutes=1)
        if fresh.get("event_count", 0) > 0:
            print(f"[preflight] data flowing: {fresh.get('human_readable', fresh)}")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"No events flowing after starting producer/consumer and waiting "
                f"{FRESHNESS_MAX_WAIT_SECONDS}s - check {LOG_DIR}/producer.log and "
                f"{LOG_DIR}/consumer.log."
            )
        print("[preflight] still waiting for events...")


def _ask(pause: bool, label: str, question: str) -> str:
    _beat(pause, f"Question ({label}): {question}")
    print("  (asking the agent...)")
    answer = run_loop(question)
    print(f"\n--- Answer ({label}) ---\n{answer}\n")
    return answer


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the scripted streaming-RAG demo")
    parser.add_argument(
        "--pause",
        action="store_true",
        help="Wait for Enter before each narrated beat (for live/recorded narration)",
    )
    args = parser.parse_args()
    pause = args.pause

    print("=== Streaming RAG demo ===")

    print("\n--- Preflight ---")
    _ensure_containers_up()
    bridge = MCPBridge()
    _ensure_data_flowing(bridge)

    _beat(
        pause,
        f"Letting the stream run to establish a calm baseline ({BASELINE_WAIT_SECONDS}s, "
        "fixed wait - there's no signal to poll for 'nothing wrong yet', only elapsed "
        "time gives confidence)...",
    )
    time.sleep(BASELINE_WAIT_SECONDS)
    _ask(pause, "baseline", BASELINE_QUESTION)

    # --- Incident 1: gateway degradation ---
    _beat(
        pause,
        f"Injecting gateway_degradation targeting {TARGET_GATEWAY!r} for "
        f"{INCIDENT_DURATION} at severity={GATEWAY_DEGRADATION_SEVERITY} (tests SQL "
        "aggregation + time awareness)...",
    )
    inject(
        "gateway_degradation",
        "--gateway", TARGET_GATEWAY,
        "--duration", INCIDENT_DURATION,
        "--severity", GATEWAY_DEGRADATION_SEVERITY,
    )
    poll_until(
        lambda: gateway_failure_rate_elevated(
            bridge, TARGET_GATEWAY, threshold=GATEWAY_FAILURE_RATE_THRESHOLD
        ),
        f"{TARGET_GATEWAY} failure-rate spike",
    )
    _beat(
        pause,
        f"Giving the now-visible spike {POST_GATE_BUFFER_SECONDS}s more to build up a "
        "clearer recent-minutes majority before asking...",
    )
    time.sleep(POST_GATE_BUFFER_SECONDS)
    _ask(pause, "gateway_degradation", GATEWAY_QUESTION)
    clear_incident()
    time.sleep(POST_CLEAR_PAUSE_SECONDS)

    # --- Incident 2: fraud burst ---
    _beat(
        pause,
        f"Injecting fraud_burst on card_bin={TARGET_CARD_BIN!r} for {INCIDENT_DURATION} "
        "(tests structured pattern detection - shared BIN, tiny amounts)...",
    )
    inject("fraud_burst", "--card-bin", TARGET_CARD_BIN, "--duration", INCIDENT_DURATION)
    poll_until(
        lambda: fraud_pattern_visible(
            bridge,
            TARGET_CARD_BIN,
            min_rows=FRAUD_MIN_MATCHING_ROWS,
            max_amount=FRAUD_MAX_AMOUNT,
        ),
        f"fraud burst on BIN {TARGET_CARD_BIN}",
    )
    _ask(pause, "fraud_burst", FRAUD_QUESTION)
    clear_incident()
    time.sleep(POST_CLEAR_PAUSE_SECONDS)

    # --- Incident 3: novel error pattern ---
    _beat(
        pause,
        f"Injecting novel_error_pattern targeting {TARGET_MERCHANT!r} for "
        f"{INCIDENT_DURATION} at intensity={NOVEL_ERROR_INTENSITY} (tests semantic "
        "search - SQL can't find this one)...",
    )
    inject(
        "novel_error_pattern",
        "--merchant", TARGET_MERCHANT,
        "--duration", INCIDENT_DURATION,
        "--intensity", NOVEL_ERROR_INTENSITY,
    )
    poll_until(
        lambda: novel_error_visible(bridge, NOVEL_ERROR_SIGNATURE), "novel error signature"
    )
    _beat(
        pause,
        f"Giving the now-visible novel error {POST_GATE_BUFFER_SECONDS}s more to "
        "accumulate more matching rows before asking...",
    )
    time.sleep(POST_GATE_BUFFER_SECONDS)
    _ask(pause, "novel_error_pattern", NOVEL_QUESTION)
    clear_incident()
    time.sleep(POST_CLEAR_PAUSE_SECONDS)

    print("\n--- Wrap-up ---")
    _ask(pause, "freshness", FRESHNESS_QUESTION)
    _ask(pause, "hallucination-control", HALLUCINATION_QUESTION)

    print("\n=== Demo complete ===")


if __name__ == "__main__":
    main()
