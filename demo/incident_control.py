"""Shared incident-injection and visibility-gating mechanics (Step 19A).

Extracted verbatim from demo/run_demo.py (Step 18A) and parameterized: targets
and thresholds that run_demo.py always read from its own fixed module
constants are now function arguments, so eval/run_eval.py can drive arbitrary
incident targets read from demo/golden_questions.json instead of run_demo.py's
hardcoded demo targets. The logic itself - polling cadence, threshold
comparisons, subprocess invocation - is unchanged from run_demo.py; this is a
pure, behavior-preserving extraction. demo/run_demo.py imports and calls into
this module with its own existing fixed constants (TARGET_GATEWAY,
GATEWAY_FAILURE_RATE_THRESHOLD, etc.) so `make demo`'s observable behavior is
unchanged.
"""

import json
import subprocess
import time
from pathlib import Path

from agent.mcp_bridge import MCPBridge

REPO_ROOT = Path(__file__).resolve().parent.parent

GATE_POLL_INTERVAL_SECONDS = 5
GATE_MAX_WAIT_SECONDS = 90

# Extra fixed wait *after* the real visibility gate already passed, for
# gateway_degradation and novel_error_pattern. Additive to (not a replacement
# for) the gate: the gate still decides *whether/when* to proceed based on
# real data; this buffer just gives the now-confirmed-visible signal
# meaningfully more running time before the highest-stakes question is asked.
# See demo/run_demo.py's module docstring / comments (Step 18A) for the full
# worked reasoning behind this value.
POST_GATE_BUFFER_SECONDS = 75

# Short settle pause after clearing an incident, before the next reset/inject.
POST_CLEAR_PAUSE_SECONDS = 2


def parse_tool_json(text: str) -> dict:
    """Best-effort parse of an MCP tool's text result back into the dict it wraps.

    call_tool() never raises, so a genuine connection failure or a validation
    error comes back as plain (non-JSON) text here - treated as "no data yet"
    by callers rather than crashing.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def inject(*args: str) -> None:
    """Invoke producer.inject's real CLI (control-file writing lives there, not here)."""
    cmd = ["uv", "run", "python", "-m", "producer.inject", *args]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def clear_incident() -> None:
    inject("clear")


def poll_until(
    check_fn,
    label: str,
    poll_interval: int = GATE_POLL_INTERVAL_SECONDS,
    max_wait: int = GATE_MAX_WAIT_SECONDS,
) -> bool:
    """Poll check_fn every poll_interval seconds until it returns True or max_wait
    is exceeded. Never hangs forever - a late/missing signal is reported as a
    warning and the caller proceeds anyway."""
    print(f"  waiting for {label} to become visible...")
    deadline = time.monotonic() + max_wait
    while True:
        if check_fn():
            print(f"  {label} is visible.")
            return True
        if time.monotonic() >= deadline:
            print(
                f"  WARNING: {label} did not become visible within "
                f"{max_wait}s - proceeding anyway."
            )
            return False
        time.sleep(poll_interval)


def gateway_failure_rate_elevated(
    bridge: MCPBridge,
    gateway: str,
    window_minutes: int = 2,
    threshold: float = 0.12,
) -> bool:
    """query_stats can group by gateway directly - this is the one incident an
    aggregate tool can see on its own."""
    text = bridge.call_tool(
        "query_stats",
        {"metric": "failure_rate", "group_by": "gateway", "window_minutes": window_minutes},
    )
    data = parse_tool_json(text)
    for row in data.get("rows", []):
        if row.get("group") == gateway:
            return (row.get("failure_rate") or 0) >= threshold
    return False


def fraud_pattern_visible(
    bridge: MCPBridge,
    card_bin: str,
    window_minutes: int = 2,
    min_rows: int = 5,
    max_amount: float = 5.00,
) -> bool:
    """query_stats has no card_bin dimension, so the fraud burst has to be found
    by inspecting get_transactions rows client-side, exactly like the agent
    itself must do when asked the fraud question."""
    text = bridge.call_tool(
        "get_transactions", {"method": "card", "window_minutes": window_minutes, "limit": 100}
    )
    data = parse_tool_json(text)
    matches = [
        row
        for row in data.get("rows", [])
        if row.get("card_bin") == card_bin and (row.get("amount") or 0) < max_amount
    ]
    return len(matches) >= min_rows


def novel_error_visible(bridge: MCPBridge, signature: str, window_minutes: int = 2) -> bool:
    """Client-side string containment on error_text - internal plumbing only.
    The agent itself is never told this signature; it must find it via
    semantic_search's meaning-based match."""
    text = bridge.call_tool(
        "get_transactions", {"status": "failure", "window_minutes": window_minutes, "limit": 100}
    )
    data = parse_tool_json(text)
    return any(signature in (row.get("error_text") or "") for row in data.get("rows", []))
