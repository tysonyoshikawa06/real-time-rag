"""Scenario engine: control-file polling, incident biasing, ground-truth logging.

The producer polls a control file each tick. A separate CLI (inject.py) writes
incidents to the file. The producer detects starts/ends/clears and applies
biasing to generated events. Ground-truth records are written by the producer
(the source of truth for what was actually emitted).

Incident types are registered in INCIDENT_APPLIERS. Each is a function that
takes (event, params) and returns the (possibly modified) event.
"""

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from producer.errors import GATEWAY_TIMEOUT, NETWORK_ERROR

CONTROL_FILE = Path(__file__).parent / "control.json"

GROUND_TRUTH_DIR = Path(__file__).resolve().parent.parent / "eval" / "ground_truth"
GROUND_TRUTH_FILE = GROUND_TRUTH_DIR / "incidents.jsonl"

_POLL_INTERVAL = 0.5  # seconds between control-file reads


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log_ground_truth(record: dict):
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    with open(GROUND_TRUTH_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Incident appliers: each takes (event, params) → event.
# Register new types in INCIDENT_APPLIERS at the bottom.
# ---------------------------------------------------------------------------

def _apply_gateway_degradation(event: dict, params: dict) -> dict:
    """Spike failures for the target gateway with timeout/network errors."""
    if event["gateway"] != params["gateway"]:
        return event

    severity = params.get("severity", 0.35)
    if random.random() < severity:
        event["status"] = "failure"
        templates = GATEWAY_TIMEOUT + NETWORK_ERROR
        template = random.choice(templates)
        event["error_text"] = template.format(gateway=event["gateway"])

    return event


INCIDENT_APPLIERS = {
    "gateway_degradation": _apply_gateway_degradation,
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ScenarioEngine:
    """Polls the control file, tracks active incidents, applies biasing."""

    def __init__(self):
        self._active: dict[str, dict] = {}
        self._last_poll: float = 0.0

    def poll(self):
        """Read control file if enough time has passed. Detect starts/ends/clears."""
        now_mono = time.monotonic()
        if now_mono - self._last_poll < _POLL_INTERVAL:
            return
        self._last_poll = now_mono

        now = _now()
        file_incidents = self._read_control_file()

        # Detect new or still-active incidents from the file
        seen_ids = set()
        for inc in file_incidents:
            inc_id = inc.get("id")
            if not inc_id:
                continue
            seen_ids.add(inc_id)

            expires_at = datetime.fromisoformat(inc["expires_at"])

            if expires_at <= now:
                # Expired — if we were tracking it, log end
                if inc_id in self._active:
                    self._end_incident(inc_id)
                continue

            if inc_id not in self._active:
                # New incident — activate
                self._active[inc_id] = inc
                _log_ground_truth({
                    "incident_id": inc_id,
                    "type": inc["type"],
                    "action": "start",
                    "timestamp": now.isoformat(),
                    "params": inc["params"],
                })
                print(f"  [scenario] ACTIVATED: {inc['type']} {inc['params']}")

        # Detect cleared incidents (in memory but gone from file)
        cleared = [iid for iid in self._active if iid not in seen_ids]
        for inc_id in cleared:
            self._end_incident(inc_id)

    def _end_incident(self, inc_id: str):
        inc = self._active.pop(inc_id)
        _log_ground_truth({
            "incident_id": inc_id,
            "type": inc["type"],
            "action": "end",
            "timestamp": _now().isoformat(),
            "params": inc["params"],
        })
        print(f"  [scenario] EXPIRED: {inc['type']} {inc['params']}")

    def apply(self, event: dict) -> dict:
        """Apply all active incident biases to an event."""
        for inc in self._active.values():
            applier = INCIDENT_APPLIERS.get(inc["type"])
            if applier:
                event = applier(event, inc["params"])
        return event

    @staticmethod
    def _read_control_file() -> list:
        try:
            if CONTROL_FILE.exists():
                return json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError):
            pass
        return []
