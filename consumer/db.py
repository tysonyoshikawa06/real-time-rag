"""Database writer: batched inserts into the transactions table.

Uses psycopg3's executemany with a parameterized INSERT ... ON CONFLICT
DO NOTHING, so re-delivered events are harmlessly ignored (idempotent).

ingested_at is set explicitly by the consumer (host clock), not by the
Postgres default (container clock). Both event_timestamp and ingested_at
then share one clock, making the lag measurement accurate.
"""

from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from consumer.config import POSTGRES_DSN

_INSERT_SQL = """
    INSERT INTO transactions
        (transaction_id, event_timestamp, merchant, method, amount,
         status, gateway, error_text, card_bin, ingested_at)
    VALUES
        (%(transaction_id)s, %(event_timestamp)s, %(merchant)s, %(method)s,
         %(amount)s, %(status)s, %(gateway)s, %(error_text)s, %(card_bin)s,
         %(ingested_at)s)
    ON CONFLICT (transaction_id) DO NOTHING
"""


def connect():
    return psycopg.connect(POSTGRES_DSN, row_factory=dict_row)


def write_batch(conn: psycopg.Connection, events: list[dict]) -> int:
    """Insert a batch of events in a single transaction. Returns rows written."""
    now = datetime.now(timezone.utc).isoformat()
    for event in events:
        event["ingested_at"] = now

    with conn.transaction():
        cur = conn.cursor()
        cur.executemany(_INSERT_SQL, events)
    return len(events)
