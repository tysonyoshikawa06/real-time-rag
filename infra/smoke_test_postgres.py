"""Smoke test: connect to Postgres, verify pgvector, insert/query/clean up.

Run with: uv run python infra/smoke_test_postgres.py
Requires: Postgres running on localhost:5433 (start with `make up`)
          .env file with POSTGRES_PASSWORD set
"""

import sys
import uuid

import psycopg
from dotenv import dotenv_values
from pgvector.psycopg import register_vector

# Load password from .env in the project root (not infra/)
config = dotenv_values(".env")
password = config.get("POSTGRES_PASSWORD")
if not password:
    print("FAIL: POSTGRES_PASSWORD not set in .env")
    sys.exit(1)

DSN = f"host=localhost port=5433 dbname=streaming_rag user=rag password={password}"


def main():
    print("Connecting to Postgres at localhost:5433...")
    conn = psycopg.connect(DSN)

    # Register the pgvector type so psycopg knows how to handle vector columns
    register_vector(conn)

    cur = conn.cursor()

    # 1. Verify pgvector extension is loaded
    cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    row = cur.fetchone()
    if not row:
        print("FAIL: pgvector extension not found")
        sys.exit(1)
    print(f"pgvector extension loaded (v{row[0]})")

    # 2. Insert a test transaction
    txn_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO transactions
            (transaction_id, event_timestamp, merchant, method, amount,
             status, gateway, error_text, card_bin)
        VALUES
            (%s, now(), %s, %s, %s, %s, %s, %s, %s)
        """,
        (str(txn_id), "test_merchant", "card", 42.50,
         "failure", "stripe", "timeout error", "411111"),
    )
    print(f"Inserted transaction {str(txn_id)[:8]}...")

    # 3. Insert a test embedding with a dummy 384-dim vector (all zeros)
    dummy_vector = [0.0] * 384
    dummy_vector[0] = 1.0  # set one component nonzero so similarity isn't degenerate
    cur.execute(
        """
        INSERT INTO embeddings (transaction_id, embedded_text, embedding)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (str(txn_id), "timeout error", dummy_vector),
    )
    embedding_id = cur.fetchone()[0]
    print(f"Inserted embedding (id={embedding_id})")

    # 4. Run a vector similarity query
    # The <-> operator computes L2 (Euclidean) distance between two vectors.
    # Lower distance = more similar. ORDER BY ... ASC returns the closest match.
    # Other operators: <#> for negative inner product, <=> for cosine distance.
    # We use cosine distance (<=>), which is what most sentence-transformer
    # models are trained for and what we'll use in the real semantic search.
    query_vector = [0.0] * 384
    query_vector[0] = 1.0
    cur.execute(
        """
        SELECT e.id, e.embedded_text, e.embedding <=> %s::vector AS cosine_distance,
               t.merchant, t.amount
        FROM embeddings e
        JOIN transactions t ON t.transaction_id = e.transaction_id
        ORDER BY e.embedding <=> %s::vector ASC
        LIMIT 1
        """,
        (query_vector, query_vector),
    )
    result = cur.fetchone()
    if not result:
        print("FAIL: similarity query returned no results")
        sys.exit(1)

    result_id, text, distance, merchant, amount = result
    print(f"Similarity query returned: id={result_id}, text='{text}', "
          f"cosine_distance={distance:.4f}, merchant={merchant}, amount={amount}")

    if result_id != embedding_id:
        print("FAIL: returned embedding id doesn't match inserted id")
        sys.exit(1)

    # 5. Clean up test rows (embedding cascades from transaction delete)
    cur.execute("DELETE FROM transactions WHERE transaction_id = %s", (str(txn_id),))
    conn.commit()
    print("Cleaned up test rows")

    # Verify cascade worked
    cur.execute("SELECT count(*) FROM embeddings WHERE transaction_id = %s", (str(txn_id),))
    orphans = cur.fetchone()[0]
    if orphans > 0:
        print(f"FAIL: CASCADE delete didn't clean up {orphans} embedding(s)")
        sys.exit(1)

    conn.close()
    print("\nPASS — pgvector extension loaded, insert/query/cascade all working")


if __name__ == "__main__":
    main()
