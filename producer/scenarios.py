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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from producer.config import CARD_BINS, GATEWAYS, MERCHANTS
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


def _generate_fraud_event(params: dict) -> dict | None:
    """Maybe generate an extra fraud-shaped event. Returns None if not this tick.

    Fraud events are small card charges ($1-$5) all sharing one BIN, spread
    across many merchants, mostly succeeding. No single event is suspicious —
    only the aggregate pattern (shared BIN, tiny amounts, high velocity).
    """
    intensity = params.get("intensity", 0.25)
    if random.random() > intensity:
        return None

    return {
        "transaction_id": str(uuid.uuid4()),
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "merchant": random.choice(MERCHANTS),
        "method": "card",
        "amount": round(random.uniform(1.00, 5.00), 2),
        "status": "success",
        "gateway": random.choice(GATEWAYS),
        "error_text": None,
        "card_bin": params["card_bin"],
    }


NOVEL_ERROR_SIGNATURE = (
    "FATAL: currency_mismatch expected=USD got=JPY merchant_cfg_v2 "
    "locale_override=true fallback_denied"
)


def _apply_novel_error(event: dict, params: dict) -> dict:
    """Inject a never-before-seen error string on failures from the target merchant."""
    if event["merchant"] != params["merchant"]:
        return event

    intensity = params.get("intensity", 0.15)
    if random.random() < intensity:
        event["status"] = "failure"
        event["error_text"] = NOVEL_ERROR_SIGNATURE

    return event


INCIDENT_APPLIERS = {
    "gateway_degradation": _apply_gateway_degradation,
    "novel_error_pattern": _apply_novel_error,
}

# Generators return extra events (or None to skip). Unlike appliers, these
# produce NEW events rather than modifying existing ones.
INCIDENT_GENERATORS = {
    "fraud_burst": _generate_fraud_event,
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

    def generate_extra_events(self) -> list[dict]:
        """Generate extra events from active incidents (e.g. fraud burst)."""
        extras = []
        for inc in self._active.values():
            generator = INCIDENT_GENERATORS.get(inc["type"])
            if generator:
                event = generator(inc["params"])
                if event is not None:
                    extras.append(event)
        return extras

    @staticmethod
    def _read_control_file() -> list:
        try:
            if CONTROL_FILE.exists():
                return json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError):
            pass
        return []
