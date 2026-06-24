"""CLI for injecting incidents into the running producer.

Writes to the control file that the producer polls. The producer detects
new entries and activates biasing + ground-truth logging.

Usage:
    python -m producer.inject gateway_degradation --gateway stripe-proxy --duration 2m
    python -m producer.inject status
    python -m producer.inject clear
"""

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from producer.config import GATEWAYS
from producer.scenarios import CONTROL_FILE


def _parse_duration(s: str) -> int:
    """Parse '2m', '30s', '120' into seconds."""
    s = s.strip().lower()
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("s"):
        return int(s[:-1])
    return int(s)


def _read_control() -> list:
    try:
        if CONTROL_FILE.exists():
            return json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _write_control(incidents: list):
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(incidents, indent=2), encoding="utf-8")
    tmp.replace(CONTROL_FILE)


def _cmd_gateway_degradation(args):
    now = datetime.now(timezone.utc)
    duration_sec = _parse_duration(args.duration)

    incident = {
        "id": f"gw-deg-{uuid.uuid4().hex[:8]}",
        "type": "gateway_degradation",
        "params": {
            "gateway": args.gateway,
            "severity": args.severity,
        },
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=duration_sec)).isoformat(),
    }

    incidents = _read_control()
    incidents.append(incident)
    _write_control(incidents)
    print(f"Injected: gateway_degradation targeting {args.gateway} for {duration_sec}s (severity={args.severity})")


def _cmd_status(args):
    incidents = _read_control()
    now = datetime.now(timezone.utc)
    if not incidents:
        print("No incidents in control file.")
        return
    for inc in incidents:
        expires_at = datetime.fromisoformat(inc["expires_at"])
        if expires_at > now:
            remaining = int((expires_at - now).total_seconds())
            print(f"  ACTIVE  {inc['type']:<25} {inc['params']}  ({remaining}s remaining)")
        else:
            print(f"  EXPIRED {inc['type']:<25} {inc['params']}")


def _cmd_clear(args):
    _write_control([])
    print("All incidents cleared.")


def main():
    parser = argparse.ArgumentParser(description="Inject incidents into the running producer")
    sub = parser.add_subparsers(dest="command")

    gw = sub.add_parser("gateway_degradation", help="Spike failures for one gateway")
    gw.add_argument("--gateway", required=True, choices=GATEWAYS)
    gw.add_argument("--duration", required=True, help="e.g. 2m, 30s, 120")
    gw.add_argument("--severity", type=float, default=0.35, help="Failure probability (default: 0.35)")
    gw.set_defaults(func=_cmd_gateway_degradation)

    sub.add_parser("status", help="Show active incidents").set_defaults(func=_cmd_status)
    sub.add_parser("clear", help="Clear all incidents").set_defaults(func=_cmd_clear)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
