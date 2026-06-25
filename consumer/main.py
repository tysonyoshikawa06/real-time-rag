"""Kafka consumer: reads transaction events and writes them to Postgres.

Run directly or via `make consume`.
"""

import json
import signal
import sys

from confluent_kafka import Consumer, KafkaError

from consumer.config import CONSUMER_GROUP, KAFKA_BOOTSTRAP, KAFKA_TOPIC

_shutdown = False


def _on_sigint(sig, frame):
    global _shutdown
    _shutdown = True


def _format_event(event: dict) -> str:
    ts = event.get("event_timestamp", "")[11:19]
    method = event.get("method", "?")
    amount = event.get("amount", 0)
    status = event.get("status", "?")
    gateway = event.get("gateway", "?")
    line = f"{ts}  {method:<6} ${amount:>10,.2f}  {status:<7}  {gateway}"
    if event.get("error_text"):
        line += f"  | {event['error_text']}"
    return line


def run():
    signal.signal(signal.SIGINT, _on_sigint)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": CONSUMER_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([KAFKA_TOPIC])

    received = 0

    print(f"Consuming '{KAFKA_TOPIC}' (group={CONSUMER_GROUP}, Ctrl-C to stop)")
    print(f"{'time':>8}  {'method':<6} {'amount':>11}  {'status':<7}  gateway")
    print("-" * 58)

    try:
        while not _shutdown:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"  consumer error: {msg.error()}", file=sys.stderr)
                continue

            try:
                event = json.loads(msg.value().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            if "transaction_id" not in event:
                continue

            received += 1
            if received % 20 == 0:
                print(_format_event(event))

    finally:
        print(f"\nClosing consumer... ({received} events received)")
        consumer.close()
        print("Done.")


def main():
    run()


if __name__ == "__main__":
    main()
