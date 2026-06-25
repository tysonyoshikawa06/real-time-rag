"""Consumer configuration — Kafka and Postgres connection defaults.

Defaults target host access (localhost + mapped ports) for dev-velocity.
In a container deployment, override via environment variables to use the
internal Docker DNS names and ports (kafka:9092, postgres:5432).
"""

import os

from dotenv import dotenv_values

_env = dotenv_values(".env")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:29092")
KAFKA_TOPIC = "transactions"
CONSUMER_GROUP = "rag-consumer"

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5433"))
POSTGRES_DB = os.environ.get("POSTGRES_DB", "streaming_rag")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "rag")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", _env.get("POSTGRES_PASSWORD", ""))

POSTGRES_DSN = (
    f"host={POSTGRES_HOST} port={POSTGRES_PORT} dbname={POSTGRES_DB} "
    f"user={POSTGRES_USER} password={POSTGRES_PASSWORD}"
)

BATCH_SIZE = 100
BATCH_TIMEOUT_SEC = 1.0
