CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE transactions (
    transaction_id uuid PRIMARY KEY,
    event_timestamp timestamptz NOT NULL, -- When producer creates the event
    ingested_at timestamptz NOT NULL DEFAULT now(), -- When the row was written to Postgres
    merchant text NOT NULL,
    method text NOT NULL CHECK (method IN ('card', 'ach', 'wallet')), -- CHECK over ENUM for easier migration
    amount numeric(12, 2) NOT NULL, -- Financial data
    status text NOT NULL CHECK (status IN ('success', 'failure')),
    gateway text NOT NULL,
    error_text text, -- Null when status = 'success'
    card_bin text -- Only present for card method
);

CREATE INDEX idx_transactions_event_timestamp ON transactions (event_timestamp);
CREATE INDEX idx_transactions_gateway ON transactions (gateway);
CREATE INDEX idx_transactions_failures ON transactions (event_timestamp) WHERE status = 'failure'; -- For semantic search and aggregation queries

CREATE TABLE embeddings (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id uuid NOT NULL REFERENCES transactions(transaction_id) ON DELETE CASCADE,
    embedded_text text NOT NULL, -- Exact text that was embedded
    embedding vector(384) NOT NULL, -- intentional coupling with all-MiniLM-L6-v2
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_embeddings_transaction_id ON embeddings (transaction_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_embedding_hnsw ON embeddings USING hnsw (embedding vector_cosine_ops);