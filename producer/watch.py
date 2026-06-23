"""Console consumer that reads and pretty-prints events from the transactions topic.

Run via `make watch` in a second terminal while the producer is running.
"""

import json
import sys

from confluent_kafka import Consumer, KafkaError

from producer.config import KAFKA_BOOTSTRAP, KAFKA_TOPIC


def main():
    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": "watch-console",
        "auto.offset.reset": "latest",
    })
    consumer.subscribe([KAFKA_TOPIC])

    print(f"Watching '{KAFKA_TOPIC}' (latest offsets, Ctrl-C to stop)...\n")

    try:
        while True:
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"Consumer error: {msg.error()}", file=sys.stderr)
                continue

            event = json.loads(msg.value().decode("utf-8"))
            ts = event["event_timestamp"][11:19]
            method = event["method"]
            amount = f"${event['amount']:,.2f}"
            status = event["status"]
            gateway = event["gateway"]
            merchant = event["merchant"]
            card_bin = event.get("card_bin") or ""

            print(
                f"{ts}  {method:<6} {amount:>10}  {status:<7}  {gateway:<16}  "
                f"{merchant:<22}  {card_bin}"
            )
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()
        print("\nDone.")


if __name__ == "__main__":
    main()
