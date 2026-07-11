"""Tests for spec 12 — `semantic_search` MCP tool.

Derived from .claude/specs/12-mcp-semantic-search.spec.md ONLY (not from the
mcp_server/semantic.py or consumer/search.py implementations, which were being
written in parallel). One test file for this feature; new cases get appended
here as the feature grows.

Determinism strategy (same discipline as tests/test_mcp_query_stats.py, spec
11B): synthetic `transactions` AND matching `embeddings` rows are INSERTed
inside an uncommitted REPEATABLE READ transaction (see tests/conftest.py) and
rolled back afterwards. Distinctive gateway values ("TEST-GW-SEM-12*") isolate
assertions from any concurrent live producer/consumer traffic.

Fake embedder: `semantic_search` takes the embedder as a dependency, so tests
never load the real ~80MB sentence-transformer model. `FakeEmbedder` below
hashes each whitespace/alnum token of the input text into one of 384 fixed
dimensions (sha256(token) -> index, and a sign bit from the same hash), sums
contributions per text, then L2-normalizes. Two texts sharing literal words
get a reliably smaller cosine distance than texts sharing none (verified
empirically: unrelated vocab here always lands at cosine distance == 1.0,
exact word overlap lands well below that) -- enough to assert exact top-match
identity deterministically without needing real semantic understanding. It
does NOT capture paraphrase-level meaning (no shared words -> orthogonal by
construction) -- that gap is exactly why the spec's paraphrase check is a
manual/live acceptance step, not a pytest case here.
"""

import hashlib
import re
import uuid
from datetime import datetime

import numpy as np
import psycopg
import pytest
from pgvector.psycopg import register_vector

from consumer.embedder import Embedder
from mcp_server.semantic import semantic_search

# --------------------------------------------------------------------------
# Deterministic fake Embedder (conforms to consumer.embedder.Embedder)
# --------------------------------------------------------------------------

_DIM = 384


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class FakeEmbedder(Embedder):
    """Deterministic, model-free stand-in for LocalEmbedder.

    Produces 384-dim, L2-normalized vectors (matching the embeddings.embedding
    column and the codebase's normalized-vector convention) via a simple
    hashed bag-of-words scheme: reproducible across processes/runs (uses
    hashlib, not Python's salted `hash()`), so cosine distance ordering is
    predictable and top-match identity is assertable exactly.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vec = np.zeros(_DIM, dtype=np.float64)
            for tok in _tokenize(text):
                digest = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
                idx = digest % _DIM
                sign = 1.0 if (digest // _DIM) % 2 == 0 else -1.0
                vec[idx] += sign
            norm = np.linalg.norm(vec)
            if norm == 0:
                vec[0] = 1.0
                norm = 1.0
            vectors.append((vec / norm).tolist())
        return vectors


def _cosine_distance(a: list[float], b: list[float]) -> float:
    av, bv = np.array(a), np.array(b)
    return 1.0 - float(np.dot(av, bv) / (np.linalg.norm(av) * np.linalg.norm(bv)))


# --------------------------------------------------------------------------
# Synthetic dataset (distinctive gateway values so live stream data cannot
# collide with assertions)
# --------------------------------------------------------------------------

MERCHANT = "TEST-MERCHANT-SEM-12"
GATEWAY = "TEST-GW-SEM-12"
GATEWAY_OTHER = "TEST-GW-SEM-12-OTHER"
NO_SUCH_GATEWAY = "TEST-GW-SEM-12-DOES-NOT-EXIST"

QUERY = "gateway connection timed out"

# Distances from QUERY (computed with FakeEmbedder, verified empirically):
#   TEXT_NEAR ~ 0.4226 (shares many words with QUERY)
#   TEXT_MID  ~ 0.4697 (shares fewer words)
#   TEXT_FAR, TEXT_FAR2 == 1.0 (share no words -> orthogonal by construction)
TEXT_NEAR = "card payment via stripe-proxy failed: gateway connection timed out after 30s"
TEXT_MID = "wallet payment declined: gateway timed out during handshake"
TEXT_FAR = "ach transfer rejected: invalid routing number provided"
TEXT_FAR2 = "card payment via legacy-gw failed: card number invalid checksum"

# label, minutes_ago, gateway, embedded_text, method, status, amount
_DATASET = [
    ("near", 5, GATEWAY, TEXT_NEAR, "card", "failure", 46.67),
    ("mid", 5, GATEWAY, TEXT_MID, "wallet", "failure", 12.10),
    ("far", 5, GATEWAY, TEXT_FAR, "ach", "failure", 99.99),
    ("far2", 5, GATEWAY, TEXT_FAR2, "card", "failure", 5.00),
    ("stale", 90, GATEWAY, TEXT_NEAR, "card", "failure", 46.67),  # outside default 30-min window
    ("other_gw", 5, GATEWAY_OTHER, TEXT_NEAR, "card", "failure", 46.67),
]

_INSERT_TX_SQL = """
    INSERT INTO transactions
        (transaction_id, event_timestamp, merchant, method, amount,
         status, gateway, error_text, card_bin)
    VALUES
        (%(id)s, now() - make_interval(mins => %(minutes_ago)s), %(merchant)s,
         %(method)s, %(amount)s, %(status)s, %(gateway)s, %(error_text)s, NULL)
"""

_INSERT_EMB_SQL = """
    INSERT INTO embeddings (transaction_id, embedded_text, embedding, created_at)
    VALUES (%(id)s, %(embedded_text)s, %(embedding)s,
            now() - make_interval(mins => %(minutes_ago)s))
"""


def _seed(conn: psycopg.Connection) -> dict[str, str]:
    """Insert synthetic transactions + matching embeddings rows.

    Returns label -> transaction_id so tests can assert exact match identity.
    """
    embedder = FakeEmbedder()
    ids: dict[str, str] = {}
    with conn.cursor() as cur:
        for label, minutes_ago, gateway, text, method, status, amount in _DATASET:
            tx_id = str(uuid.uuid4())
            ids[label] = tx_id
            cur.execute(
                _INSERT_TX_SQL,
                {
                    "id": tx_id,
                    "minutes_ago": minutes_ago,
                    "merchant": MERCHANT,
                    "method": method,
                    "amount": amount,
                    "status": status,
                    "gateway": gateway,
                    "error_text": "TEST SEM-12 synthetic failure",
                },
            )
            vector = np.array(embedder.embed([text])[0])
            cur.execute(
                _INSERT_EMB_SQL,
                {"id": tx_id, "embedded_text": text, "embedding": vector, "minutes_ago": minutes_ago},
            )
    return ids


@pytest.fixture()
def seeded(db_conn):
    """(conn, ids) -- conn has pgvector adaptation registered and the
    synthetic dataset inserted inside the caller's open (uncommitted) txn."""
    register_vector(db_conn)
    ids = _seed(db_conn)
    return db_conn, ids


class _ForbiddenConn:
    """Stand-in connection that fails loudly if touched at all.

    The spec requires bound-check ValueErrors to be raised before search()
    (and therefore before any SQL executes) -- validation must never touch
    the connection.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f"semantic_search touched the connection (attribute {name!r}) "
            "before input validation raised ValueError"
        )


class _ForbiddenEmbedder:
    """Stand-in embedder that fails loudly if embed() is ever called.

    The spec requires empty/whitespace query to raise ValueError "before
    embedding or calling search()".
    """

    def embed(self, texts):
        raise AssertionError(
            "semantic_search called embedder.embed() before input validation "
            "raised ValueError"
        )


# --------------------------------------------------------------------------
# Behaviors 1, 2, 3, 5, 8 -- happy path shape, fields, ordering, rounding
# --------------------------------------------------------------------------


def test_happy_path_header_and_top_match_shape(seeded):
    conn, ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY)

    # Header (behavior 1): echoes query params, plus count/path/matches.
    assert result["query"] == QUERY
    assert result["window_minutes"] == 30
    assert result["gateway"] == GATEWAY
    assert result["k"] == 10
    assert result["path"] in ("exact", "hnsw")
    assert result["count"] == len(result["matches"])
    assert result["count"] <= result["k"]

    # 4 rows for GATEWAY fall inside the default 30-minute window (behavior 1
    # window conversion + behavior 6 gateway filter): near, mid, far, far2.
    assert result["count"] == 4

    # Return shape is a plain, JSON-serializable dict (no UUID/Decimal/
    # datetime objects) -- per the spec's explicit "every value already a
    # JSON-native type" requirement.
    import json

    json.dumps(result)

    top = result["matches"][0]
    # Behavior 2: every documented field present.
    for key in (
        "transaction_id",
        "similarity",
        "embedded_text",
        "event_timestamp",
        "gateway",
        "method",
        "amount",
        "status",
    ):
        assert key in top
    assert isinstance(top["transaction_id"], str)  # not a raw uuid.UUID

    # Nearest match is the "near" row (behavior 5: ascending distance first).
    assert top["transaction_id"] == ids["near"]
    assert top["embedded_text"] == TEXT_NEAR
    assert top["gateway"] == GATEWAY
    assert top["method"] == "card"
    assert top["status"] == "failure"
    assert top["amount"] == 46.67
    assert isinstance(top["amount"], float)  # not Decimal

    # event_timestamp is an ISO 8601 string, not a datetime object.
    assert isinstance(top["event_timestamp"], str)
    datetime.fromisoformat(top["event_timestamp"])  # must not raise

    # Behavior 3: similarity = round(1 - distance, 4); higher = more similar.
    # Loose tolerance: pgvector stores float4, so exact float64 match with our
    # own numpy computation isn't guaranteed, only closeness.
    expected_sim = round(1 - _cosine_distance(
        FakeEmbedder().embed([QUERY])[0], FakeEmbedder().embed([TEXT_NEAR])[0]
    ), 4)
    assert isinstance(top["similarity"], float)
    assert top["similarity"] == round(top["similarity"], 4)
    assert abs(top["similarity"] - expected_sim) < 0.01


def test_matches_ordered_nearest_first(seeded):
    conn, ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY, k=2)

    # Behavior 5 + 8: top-2 by ascending distance == descending similarity;
    # count == len(matches) == k here since >= k rows exist.
    assert result["count"] == 2
    assert [m["transaction_id"] for m in result["matches"]] == [ids["near"], ids["mid"]]
    assert result["matches"][0]["similarity"] > result["matches"][1]["similarity"]


# --------------------------------------------------------------------------
# Behavior 1 -- window_minutes converts to search()'s window and is honored
# --------------------------------------------------------------------------


def test_window_minutes_excludes_and_includes_stale_row(seeded):
    conn, ids = seeded

    narrow = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY, window_minutes=30, k=10)
    assert narrow["count"] == 4  # "stale" (90 min ago) excluded
    assert ids["stale"] not in [m["transaction_id"] for m in narrow["matches"]]

    wide = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY, window_minutes=120, k=10)
    assert wide["count"] == 5  # "stale" now included
    assert wide["window_minutes"] == 120
    assert ids["stale"] in [m["transaction_id"] for m in wide["matches"]]


# --------------------------------------------------------------------------
# Behavior 6 -- gateway filter narrows results (entirely search()'s filter)
# --------------------------------------------------------------------------


def test_gateway_filter_narrows_to_one_gateway(seeded):
    conn, ids = seeded

    other = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY_OTHER, k=10)
    assert other["count"] == 1
    assert other["matches"][0]["transaction_id"] == ids["other_gw"]
    assert other["matches"][0]["gateway"] == GATEWAY_OTHER

    mine = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY, window_minutes=120, k=10)
    assert mine["count"] == 5
    assert all(m["gateway"] == GATEWAY for m in mine["matches"])


def test_gateway_none_echoed_when_not_given(seeded):
    conn, ids = seeded
    # No gateway filter: just confirm the header echoes gateway=None and the
    # call succeeds without error (unfiltered results may include live data,
    # so no count assertion here).
    result = semantic_search(conn, FakeEmbedder(), QUERY, k=1)
    assert result["gateway"] is None


# --------------------------------------------------------------------------
# Behavior 7 -- zero matches is not an error
# --------------------------------------------------------------------------


def test_no_matching_gateway_returns_empty_not_error(seeded):
    conn, _ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, gateway=NO_SUCH_GATEWAY)

    assert result["count"] == 0
    assert result["matches"] == []
    assert result["path"] in ("exact", "hnsw")  # header still reports path


# --------------------------------------------------------------------------
# Behavior 4 -- exact_scan_threshold passthrough forces both paths
# --------------------------------------------------------------------------


def test_exact_scan_threshold_forces_exact_path(seeded):
    conn, ids = seeded
    # 4 candidate rows for GATEWAY in the default window; default threshold
    # (50_000, not passed) keeps this well within the exact-scan branch.
    result = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY)
    assert result["path"] == "exact"
    assert result["count"] == 4


def test_exact_scan_threshold_forces_hnsw_path(seeded):
    conn, ids = seeded
    # Forcing a tiny threshold (1) with 4 candidate rows for GATEWAY pushes
    # search() onto the hnsw branch deterministically, without needing to
    # seed 50,000+ rows.
    result = semantic_search(
        conn, FakeEmbedder(), QUERY, gateway=GATEWAY, exact_scan_threshold=1
    )
    assert result["path"] == "hnsw"
    # Same underlying data -- header/count shape still holds regardless of
    # which scan strategy search() used (behavior 4: this module makes no
    # scan-strategy decisions of its own).
    assert result["count"] == 4
    assert result["matches"][0]["transaction_id"] == ids["near"]


# --------------------------------------------------------------------------
# Edge cases & errors -- ValueError before embedding or any SQL executes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_query", ["", "   ", "\t\n"])
def test_empty_or_whitespace_query_raises_before_embed_or_search(bad_query):
    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), bad_query)


@pytest.mark.parametrize("bad_window", [0, -1, -100])
def test_window_minutes_out_of_bounds_raises_before_embed_or_search(bad_window):
    """window_minutes must be positive; > 1440 clamps instead of rejecting."""
    with pytest.raises(ValueError):
        semantic_search(
            _ForbiddenConn(), _ForbiddenEmbedder(), QUERY, window_minutes=bad_window
        )


@pytest.mark.parametrize("bad_k", [0, -1, -50])
def test_k_out_of_bounds_raises_before_embed_or_search(bad_k):
    """k must be positive; > 50 clamps instead of rejecting."""
    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, k=bad_k)


def test_window_minutes_boundary_values_are_valid(seeded):
    # 1 and 1440 are the inclusive boundary values -- must NOT raise.
    conn, _ids = seeded
    semantic_search(conn, FakeEmbedder(), QUERY, gateway=NO_SUCH_GATEWAY, window_minutes=1)
    semantic_search(conn, FakeEmbedder(), QUERY, gateway=NO_SUCH_GATEWAY, window_minutes=1440)


def test_k_boundary_values_are_valid(seeded):
    # 1 and 50 are the inclusive boundary values -- must NOT raise.
    conn, _ids = seeded
    semantic_search(conn, FakeEmbedder(), QUERY, gateway=NO_SUCH_GATEWAY, k=1)
    semantic_search(conn, FakeEmbedder(), QUERY, gateway=NO_SUCH_GATEWAY, k=50)


# --------------------------------------------------------------------------
# Behavior 9 -- FastMCP tool registration + end-to-end call
# --------------------------------------------------------------------------


def _tool_result_dict(result):
    """Extract the plain-dict payload from a fastmcp CallToolResult."""
    import json

    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


def test_mcp_server_registers_semantic_search_tool():
    import asyncio

    from fastmcp import Client

    from mcp_server.server import mcp

    assert mcp.name == "streaming-rag"

    async def _list():
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_list())
    by_name = {t.name: t for t in tools}
    assert "semantic_search" in by_name
    assert "query_stats" in by_name  # registered alongside the existing tool

    # Tool exposes exactly the four caller-facing parameters -- not conn,
    # embedder, or the test-only exact_scan_threshold passthrough.
    schema = by_name["semantic_search"].inputSchema
    properties = set(schema.get("properties", {}))
    assert properties == {"query", "window_minutes", "gateway", "k"}


def test_mcp_tool_end_to_end_returns_documented_shape():
    import asyncio

    from fastmcp import Client

    from mcp_server.server import mcp

    # The tool opens its own DB connection (consumer.db.connect()), so it
    # cannot see our test transaction's synthetic rows. Use an impossible
    # gateway for a deterministic zero-match result regardless of live data,
    # exercising the full path (real embedder, real connection) end-to-end.
    async def _call():
        async with Client(mcp) as client:
            return await client.call_tool(
                "semantic_search",
                {"query": "connection timed out", "gateway": NO_SUCH_GATEWAY, "k": 5},
            )

    payload = _tool_result_dict(asyncio.run(_call()))

    assert payload["query"] == "connection timed out"
    assert payload["window_minutes"] == 30
    assert payload["gateway"] == NO_SUCH_GATEWAY
    assert payload["k"] == 5
    assert payload["count"] == 0
    assert payload["matches"] == []
    assert payload["path"] in ("exact", "hnsw")


# --------------------------------------------------------------------------
# Step 14: Input validation + limits — notes field + clamping behavior
# --------------------------------------------------------------------------


def test_valid_defaults_return_empty_notes(seeded):
    """Regression: default params should return notes: []."""
    conn, _ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, gateway=GATEWAY)

    assert "notes" in result
    assert result["notes"] == []


def test_valid_in_range_values_return_empty_notes(seeded):
    """Regression: in-range window_minutes and k should return notes: []."""
    conn, _ids = seeded
    result = semantic_search(
        conn,
        FakeEmbedder(),
        QUERY,
        window_minutes=100,
        k=20,
        gateway=GATEWAY,
    )

    assert "notes" in result
    assert result["notes"] == []


def test_query_empty_string_raises_value_error():
    """Behavior #17: empty query raises ValueError."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), "")
    assert "query" in str(excinfo.value).lower() or "empty" in str(excinfo.value).lower()


def test_query_whitespace_raises_value_error():
    """Behavior #17: whitespace-only query raises ValueError."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), "   ")
    assert "query" in str(excinfo.value).lower() or "empty" in str(excinfo.value).lower()


def test_query_tab_newline_raises_value_error():
    """Behavior #17: tab/newline-only query raises ValueError."""
    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), "\t\n")


def test_query_truncated_to_2000_chars_with_note(seeded):
    """Behavior #17: query > 2000 chars truncated to 2000 with a note."""
    conn, _ids = seeded
    long_query = "a" * 2500  # 2500 chars
    result = semantic_search(conn, FakeEmbedder(), long_query, gateway=NO_SUCH_GATEWAY)

    assert len(result["query"]) == 2000
    assert len(result["notes"]) > 0
    # Note should mention truncation
    note_text = " ".join(result["notes"]).lower()
    assert "truncat" in note_text or "limit" in note_text


def test_window_minutes_non_integer_raises_value_error():
    """Behavior #18: window_minutes must be a positive integer."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, window_minutes=3.5)
    assert "integer" in str(excinfo.value).lower()


def test_window_minutes_zero_raises_value_error():
    """Behavior #18: window_minutes=0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, window_minutes=0)
    assert "positive" in str(excinfo.value).lower()


def test_window_minutes_negative_raises_value_error():
    """Behavior #18: window_minutes<0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, window_minutes=-5)
    assert "positive" in str(excinfo.value).lower()


def test_window_minutes_boolean_raises_value_error():
    """Behavior #18: window_minutes=True/False rejects explicitly."""
    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, window_minutes=True)

    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, window_minutes=False)


def test_k_non_integer_raises_value_error():
    """Behavior #19: k must be a positive integer."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, k=2.5)
    assert "integer" in str(excinfo.value).lower()


def test_k_zero_raises_value_error():
    """Behavior #19: k=0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, k=0)
    assert "positive" in str(excinfo.value).lower()


def test_k_negative_raises_value_error():
    """Behavior #19: k<0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, k=-5)
    assert "positive" in str(excinfo.value).lower()


def test_k_boolean_raises_value_error():
    """Behavior #19: k=True/False rejects explicitly."""
    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, k=True)

    with pytest.raises(ValueError):
        semantic_search(_ForbiddenConn(), _ForbiddenEmbedder(), QUERY, k=False)


def test_k_clamped_to_50_with_note(seeded):
    """Behavior #19: k > 50 clamps to 50 with a note."""
    conn, _ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, k=1000, gateway=NO_SUCH_GATEWAY)

    assert result["k"] == 50
    assert len(result["notes"]) > 0
    # Note should mention k was capped
    note_text = " ".join(result["notes"]).lower()
    assert "k" in note_text
    assert "capped" in note_text or "cap" in note_text or "exceeded" in note_text


def test_window_minutes_clamped_to_1440_with_note(seeded):
    """Behavior #18: window_minutes > 1440 clamps to 1440 with a note (changed from reject)."""
    conn, _ids = seeded
    result = semantic_search(
        conn, FakeEmbedder(), QUERY, window_minutes=5000, gateway=NO_SUCH_GATEWAY
    )

    assert result["window_minutes"] == 1440
    assert len(result["notes"]) > 0
    # Note should mention the window was capped
    note_text = " ".join(result["notes"]).lower()
    assert "window" in note_text
    assert "capped" in note_text or "cap" in note_text or "exceeded" in note_text


def test_window_minutes_boundary_1_is_valid(seeded):
    """Edge case: window_minutes=1 is the minimum and should pass without clamping."""
    conn, _ids = seeded
    result = semantic_search(
        conn, FakeEmbedder(), QUERY, window_minutes=1, gateway=NO_SUCH_GATEWAY
    )

    assert result["window_minutes"] == 1
    assert result["notes"] == []


def test_window_minutes_boundary_1440_is_valid(seeded):
    """Edge case: window_minutes=1440 is the maximum and should pass without clamping."""
    conn, _ids = seeded
    result = semantic_search(
        conn, FakeEmbedder(), QUERY, window_minutes=1440, gateway=NO_SUCH_GATEWAY
    )

    assert result["window_minutes"] == 1440
    assert result["notes"] == []


def test_k_boundary_1_is_valid(seeded):
    """Edge case: k=1 is the minimum and should pass without clamping."""
    conn, _ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, k=1, gateway=NO_SUCH_GATEWAY)

    assert result["k"] == 1
    assert result["notes"] == []


def test_k_boundary_50_is_valid(seeded):
    """Edge case: k=50 is the maximum and should pass without clamping."""
    conn, _ids = seeded
    result = semantic_search(conn, FakeEmbedder(), QUERY, k=50, gateway=NO_SUCH_GATEWAY)

    assert result["k"] == 50
    assert result["notes"] == []


def test_query_exactly_2000_chars_not_truncated(seeded):
    """Edge case: query exactly 2000 chars should not be truncated or noted."""
    conn, _ids = seeded
    query_2000 = "a" * 2000
    result = semantic_search(conn, FakeEmbedder(), query_2000, gateway=NO_SUCH_GATEWAY)

    assert result["query"] == query_2000
    # Notes may exist for other reasons, but should not have truncation note
    note_text = " ".join(result["notes"]).lower()
    assert "truncat" not in note_text
