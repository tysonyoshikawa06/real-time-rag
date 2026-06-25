"""Kafka consumer: reads transaction events, batches them, and writes to Postgres.

Offset ordering (the key guarantee):
  1. Accumulate events into a batch (up to BATCH_SIZE or BATCH_TIMEOUT_SEC).
  2. Write the batch to Postgres in a single DB transaction.
  3. Only AFTER the DB commit succeeds, commit Kafka offsets.

If the process crashes between steps 2 and 3, Kafka will redeliver those
events (offsets weren't committed). The ON CONFLICT DO NOTHING in the INSERT
makes reprocessing harmless — the duplicate rows are silently ignored.

This gives at-least-once delivery. Combined with idempotent writes, the
outcome is effectively-once: every event lands in the DB exactly once.

Run directly or via `make consume`.
"""

import json
import signal
import sys
import time

from confluent_kafka import Consumer, KafkaError

from consumer.config import (
    BATCH_SIZE,
    BATCH_TIMEOUT_SEC,
    CONSUMER_GROUP,
    KAFKA_BOOTSTRAP,
    KAFKA_TOPIC,
)
from consumer.db import connect, write_batch

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


def _flush_batch(conn, consumer, batch, total_written):
    """Write batch to DB, then commit Kafka offsets. Returns new total."""
    if not batch:
        return total_written

    try:
        write_batch(conn, batch)
    except Exception as e:
        print(f"  DB write failed ({len(batch)} events): {e}", file=sys.stderr)
        return total_written

    consumer.commit(asynchronous=False)

    total_written += len(batch)
    print(f"  [batch] wrote {len(batch)} events, total={total_written}")
    return total_written


def run():
    signal.signal(signal.SIGINT, _on_sigint)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": CONSUMER_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    consumer.subscribe([KAFKA_TOPIC])

    conn = connect()

    batch: list[dict] = []
    batch_start = time.monotonic()
    total_written = 0
    received = 0

    print(f"Consuming '{KAFKA_TOPIC}' (group={CONSUMER_GROUP}, Ctrl-C to stop)")
    print(f"Batch size={BATCH_SIZE}, timeout={BATCH_TIMEOUT_SEC}s")
    print(f"{'time':>8}  {'method':<6} {'amount':>11}  {'status':<7}  gateway")
    print("-" * 58)

    try:
        while not _shutdown:
            msg = consumer.poll(0.1)

            if msg is not None and not msg.error():
                try:
                    event = json.loads(msg.value().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    event = None

                if event and "transaction_id" in event:
                    batch.append(event)
                    received += 1
                    if received % 20 == 0:
                        print(_format_event(event))

            elif msg is not None and msg.error():
                if msg.error().code() != KafkaError._PARTITION_EOF:
                    print(f"  consumer error: {msg.error()}", file=sys.stderr)

            batch_age = time.monotonic() - batch_start
            if len(batch) >= BATCH_SIZE or (batch and batch_age >= BATCH_TIMEOUT_SEC):
                total_written = _flush_batch(conn, consumer, batch, total_written)
                batch = []
                batch_start = time.monotonic()

    finally:
        total_written = _flush_batch(conn, consumer, batch, total_written)
        print(f"\nClosing... ({received} received, {total_written} written to DB)")
        consumer.close()
        conn.close()
        print("Done.")


def main():
    run()


if __name__ == "__main__":
    main()
