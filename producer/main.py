"""Baseline transaction event producer.

Generates realistic payment events and sends them to Kafka.
Run directly or via `make produce`.
"""

import argparse
import json
import signal
import sys
import time

from confluent_kafka import KafkaError, Producer

from producer.config import KAFKA_BOOTSTRAP, KAFKA_TOPIC
from producer.event import generate_event
from producer.scenarios import ScenarioEngine

_shutdown = False


def _on_sigint(sig, frame):
    global _shutdown
    _shutdown = True


def _delivery_callback(err, msg):
    if err is not None:
        print(f"  delivery failed: {err}", file=sys.stderr)


def _format_summary(event: dict) -> str:
    ts = event["event_timestamp"][11:19]  # HH:MM:SS from ISO string
    method = event["method"]
    amount = f"${event['amount']:,.2f}"
    status = event["status"]
    gateway = event["gateway"]
    line = f"{ts}  {method:<6} {amount:>10}  {status:<7}  {gateway}"
    if event.get("error_text"):
        line += f"  | {event['error_text']}"
    return line


def run(rate: int, count: int | None, duration: float | None, log_every: int):
    signal.signal(signal.SIGINT, _on_sigint)

    producer = Producer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "linger.ms": 5,
        "batch.num.messages": 100,
    })

    engine = ScenarioEngine()

    interval = 1.0 / rate
    sent = 0
    start = time.monotonic()

    print(f"Producing to '{KAFKA_TOPIC}' at ~{rate} events/sec  (Ctrl-C to stop)")
    print(f"{'time':>8}  {'method':<6} {'amount':>10}  {'status':<7}  gateway")
    print("-" * 58)

    try:
        while not _shutdown:
            if count is not None and sent >= count:
                break
            if duration is not None and (time.monotonic() - start) >= duration:
                break

            engine.poll()
            event = generate_event()
            event = engine.apply(event)
            payload = json.dumps(event).encode("utf-8")

            producer.produce(
                KAFKA_TOPIC,
                value=payload,
                key=event["transaction_id"].encode("utf-8"),
                callback=_delivery_callback,
            )
            producer.poll(0)

            sent += 1
            if sent % log_every == 0:
                print(_format_summary(event))

            # Simple rate limiting: sleep the remainder of the interval.
            # Good enough for 20 events/sec; not meant for high-throughput.
            elapsed = time.monotonic() - start
            expected = sent * interval
            if expected > elapsed:
                time.sleep(expected - elapsed)

    finally:
        print(f"\nFlushing... ({sent} events produced)")
        producer.flush(timeout=10)
        print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Baseline transaction event producer")
    parser.add_argument("--rate", type=int, default=20, help="Events per second (default: 20)")
    parser.add_argument("--count", type=int, default=None, help="Stop after N events")
    parser.add_argument("--duration", type=float, default=None, help="Stop after N seconds")
    parser.add_argument("--log-every", type=int, default=20, help="Log every Nth event (default: 20)")
    args = parser.parse_args()
    run(args.rate, args.count, args.duration, args.log_every)


if __name__ == "__main__":
    main()
