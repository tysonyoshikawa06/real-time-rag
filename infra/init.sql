-- pgvector extension must be created before we can use the vector type.
-- CREATE EXTENSION is idempotent with IF NOT EXISTS.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE transactions (
    -- UUID is the natural type for distributed event IDs: 128-bit, globally unique,
    -- no coordination needed between producers. Stored as 16 bytes internally —
    -- much smaller than the 36-char text representation.
    transaction_id  uuid PRIMARY KEY,

    -- Always use TIMESTAMPTZ, never TIMESTAMP. Without a timezone, you're storing
    -- wall-clock time with no way to know WHICH wall clock — this leads to silent
    -- bugs when servers span timezones or DST changes. TIMESTAMPTZ stores the
    -- absolute moment in UTC internally, then displays in the session timezone.
    event_timestamp timestamptz NOT NULL,

    -- When the row was written to Postgres, not when the event occurred. The gap
    -- between event_timestamp and ingested_at is the ingest lag — measured by the
    -- system_freshness tool in Step 8. DEFAULT now() captures it automatically.
    ingested_at     timestamptz NOT NULL DEFAULT now(),

    -- TEXT over VARCHAR(N): in Postgres they are the same internal type with
    -- identical performance. VARCHAR just adds a length check that creates
    -- migration headaches when a merchant name exceeds your guess.
    merchant        text NOT NULL,

    -- CHECK constraint over ENUM: Postgres ENUMs are awkward to alter (can't
    -- remove values, adding requires ALTER TYPE ... ADD VALUE which can't run
    -- inside a transaction before PG14). A CHECK constraint is equally enforcing
    -- and trivial to modify with ALTER TABLE.
    method          text NOT NULL CHECK (method IN ('card', 'ach', 'wallet')),

    -- NUMERIC (aka DECIMAL) stores exact decimal values — never use FLOAT/DOUBLE
    -- for money. Floats use binary fractions and cannot represent 0.10 exactly
    -- (try 0.1 + 0.2 in Python: 0.30000000000000004). NUMERIC(12,2) means up to
    -- 10 digits before the decimal and 2 after — handles amounts up to
    -- $9,999,999,999.99 with zero rounding error.
    -- Why not the MONEY type? It's locale-dependent (display changes with
    -- LC_MONETARY), making it fragile across environments.
    amount          numeric(12, 2) NOT NULL,

    status          text NOT NULL CHECK (status IN ('success', 'failure')),

    -- Free text, no constraint — gateway names come from external payment
    -- processors and we don't want to enumerate them upfront.
    gateway         text NOT NULL,

    -- Nullable: only present on failures. This is the messy free-text field that
    -- later gets embedded into pgvector for semantic search.
    error_text      text,

    -- Nullable: only present for card method. Stored as TEXT, not INTEGER, because
    -- BINs are 6-8 digit identifiers where leading zeros are meaningful (e.g.
    -- "004321" is not the same as 4321).
    card_bin        text
);

-- Time-range filtering is the most common query pattern ("what happened in the
-- last hour/day"). B-tree on timestamptz enables efficient range scans.
CREATE INDEX idx_transactions_event_timestamp ON transactions (event_timestamp);

-- The gateway-degradation scenario queries "failures for gateway X in time
-- range". An index on gateway makes that fast.
CREATE INDEX idx_transactions_gateway ON transactions (gateway);

-- Most semantic search and aggregation queries filter for failures specifically
-- (that's where the interesting error_text lives). A partial index is smaller
-- and faster than a full index since it only includes the ~5-10% of rows that
-- are failures.
CREATE INDEX idx_transactions_failures ON transactions (event_timestamp)
    WHERE status = 'failure';


CREATE TABLE embeddings (
    -- GENERATED ALWAYS AS IDENTITY is the modern Postgres way to auto-increment.
    -- Replaces the older SERIAL type, which creates a separate sequence with
    -- subtle ownership issues (e.g. dropping the column doesn't drop the sequence).
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- CASCADE: an embedding is meaningless without its parent transaction. If you
    -- delete a transaction, the orphaned embedding is garbage — cascade removes
    -- it automatically. RESTRICT would be wrong (it would block transaction
    -- cleanup). SET NULL would leave orphaned rows with no parent.
    transaction_id  uuid NOT NULL
                    REFERENCES transactions(transaction_id) ON DELETE CASCADE,

    -- Store the exact text that was embedded so the table is self-documenting:
    -- you can inspect what went into the vector without re-deriving it from the
    -- source transaction. Essential for debugging retrieval quality and for
    -- showing citations in the agent's answers.
    embedded_text   text NOT NULL,

    -- vector(384) is a pgvector column type storing a fixed-length float array.
    -- *** 384 is coupled to the embedding model: all-MiniLM-L6-v2 outputs 384
    -- dimensions. Changing the model (e.g. to a 768-dim model) requires:
    --   1. ALTER this column's dimension
    --   2. Re-embed every row
    --   3. Rebuild any vector index
    -- This is an intentional coupling — the alternative (storing variable-length
    -- vectors) loses pgvector's ability to do fixed-size SIMD-optimized math. ***
    embedding       vector(384) NOT NULL,

    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Postgres does NOT auto-create indexes on foreign key columns (unlike primary
-- keys). Without this index, CASCADE deletes and JOINs between transactions and
-- embeddings would do sequential scans of the embeddings table.
CREATE INDEX idx_embeddings_transaction_id ON embeddings (transaction_id);
