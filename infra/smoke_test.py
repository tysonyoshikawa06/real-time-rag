"""Smoke test: produce 5 JSON messages to Kafka, consume them back, verify round-trip integrity.

Run with: uv run python infra/smoke_test.py
Requires: Kafka running on localhost:29092 (start with `make up`)
"""

import json
import sys
import time
import uuid

from confluent_kafka import Consumer, Producer
from confluent_kafka.admin import AdminClient

BOOTSTRAP_SERVERS = "localhost:29092"
TOPIC = "transactions"


def wait_for_kafka(timeout=30):
    """Block until Kafka is reachable or timeout expires."""
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP_SERVERS})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            metadata = admin.list_topics(timeout=5)
            if metadata.topics is not None:
                return True
        except Exception:
            time.sleep(1)
    return False


def make_messages(n=5):
    """Generate n sample transaction messages with unique IDs."""
    return [
        {
            "transaction_id": str(uuid.uuid4()),
            "amount": round(10.0 + i * 25.50, 2),
            "currency": "USD",
            "merchant": f"merchant_{i}",
            "status": "completed",
        }
        for i in range(n)
    ]


def produce(messages):
    """Send each message as JSON to the transactions topic.

    producer.produce() is asynchronous — it queues the message in an internal
    buffer. producer.flush() blocks until all queued messages are delivered
    (or the timeout expires). The return value is the number of messages still
    in the queue — 0 means everything was delivered.
    """
    producer = Producer({"bootstrap.servers": BOOTSTRAP_SERVERS})

    for msg in messages:
        producer.produce(
            topic=TOPIC,
            value=json.dumps(msg).encode("utf-8"),
        )

    remaining = producer.flush(timeout=10)
    if remaining > 0:
        print(f"FAIL: {remaining} messages were not delivered")
        sys.exit(1)

    print(f"Produced {len(messages)} messages to '{TOPIC}'")


def consume(expected_count, timeout=15):
    """Read messages from the topic and return them as parsed dicts.

    Key settings:
    - group.id: each consumer group tracks its own offsets. We use a random ID
      so this test always reads from scratch (no leftover offset state).
    - auto.offset.reset: "earliest" means start from the first message in the
      topic (not just new ones arriving after we subscribe).
    """
    consumer = Consumer(
        {
            "bootstrap.servers": BOOTSTRAP_SERVERS,
            "group.id": f"smoke-test-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([TOPIC])

    received = []
    deadline = time.time() + timeout
    while len(received) < expected_count and time.time() < deadline:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            continue
        if msg.error():
            print(f"Consumer error: {msg.error()}")
            continue
        received.append(json.loads(msg.value().decode("utf-8")))

    consumer.close()
    return received


def main():
    print(f"Connecting to Kafka at {BOOTSTRAP_SERVERS}...")
    if not wait_for_kafka():
        print("FAIL: Kafka not reachable within 30s")
        sys.exit(1)
    print("Kafka is ready.\n")

    # Produce
    messages = make_messages(5)
    produce(messages)

    # Consume
    print("Consuming messages...")
    received = consume(expected_count=5)
    print(f"Received {len(received)} messages\n")

    # Verify: every sent transaction_id came back
    sent_ids = {m["transaction_id"] for m in messages}
    recv_ids = {m["transaction_id"] for m in received}

    if sent_ids == recv_ids:
        print("PASS — all 5 messages round-tripped successfully:")
        for msg in received:
            print(f"  {msg['transaction_id'][:8]}... {msg['merchant']:>12}  ${msg['amount']:.2f}")
    else:
        missing = sent_ids - recv_ids
        extra = recv_ids - sent_ids
        print("FAIL — message mismatch:")
        if missing:
            print(f"  Not received: {missing}")
        if extra:
            print(f"  Unexpected:   {extra}")
        sys.exit(1)


if __name__ == "__main__":
    main()
