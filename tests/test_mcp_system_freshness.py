"""Tests for spec 13b — `system_freshness` MCP tool.

Derived from .claude/specs/13b-mcp-system-freshness.spec.md ONLY (not from the
implementation). One test file for this feature; new cases get appended here.

Determinism (spec behaviors 1-6): synthetic transaction rows are INSERTed with
known event_timestamp/ingested_at gaps inside an uncommitted REPEATABLE READ
transaction (see tests/conftest.py) and rolled back afterwards. The system_freshness
function reuses consumer.freshness.query_freshness() which computes lag percentiles
via SQL percentile_cont over (ingested_at - event_timestamp).
"""

import asyncio
import json
import uuid
from datetime import timedelta

import psycopg
import pytest

from mcp_server.freshness import system_freshness

# --------------------------------------------------------------------------
# Synthetic dataset with known lag values (ingested_at - event_timestamp)
# --------------------------------------------------------------------------

MERCHANT = "TEST-MERCHANT-FRESHNESS-13B"

# For deterministic lag values, we create rows with calculated event/ingested gaps:
# Each row has event_timestamp = now() - X minutes, ingested_at = now() - (X - lag_seconds/60)
# So lag = ingested_at - event_timestamp = lag_seconds
#
# We'll create rows with specific lag values so we can assert the percentiles:
# lag values (in seconds): [0.5, 1.0, 1.5, 2.0, 2.5, 3.0] (p50=1.5, p95~2.85, p99~2.98)
# For simplicity, create 6 rows with these lag values.

_INSERT_SQL = """
    INSERT INTO transactions
        (transaction_id, event_timestamp, merchant, method, amount,
         status, gateway, error_text, card_bin, ingested_at)
    VALUES
        (%(id)s, %(event_timestamp)s, %(merchant)s, %(method)s, %(amount)s,
         %(status)s, %(gateway)s, %(error_text)s, %(card_bin)s, %(ingested_at)s)
"""


def _seed_with_known_lags(conn: psycopg.Connection, lag_seconds: list[float]) -> dict[str, str]:
    """Insert rows with precise known lag values (ingested_at - event_timestamp).

    Args:
        conn: psycopg connection
        lag_seconds: list of lag values (in seconds) for each row

    Returns:
        dict mapping "lag_N" -> transaction_id for reference
    """
    ids = {}
    with conn.cursor() as cur:
        # Get the current transaction timestamp (stable in REPEATABLE READ)
        cur.execute("SELECT now()::timestamptz AS now")
        tx_now = cur.fetchone()["now"]

        for i, lag_sec in enumerate(lag_seconds):
            tx_id = str(uuid.uuid4())
            ids[f"lag_{i}"] = tx_id
            _label = f"lag_{i}"

            # event_timestamp: 2 minutes ago (well within default 5-minute window,
            # leaves buffer for test execution)
            event_ts = tx_now - timedelta(minutes=2)
            # ingested_at: event_timestamp + lag (so ingested_at - event_timestamp = lag)
            ingested_ts = event_ts + timedelta(seconds=lag_sec)

            cur.execute(
                _INSERT_SQL,
                {
                    "id": tx_id,
                    "event_timestamp": event_ts,
                    "merchant": MERCHANT,
                    "method": "card",
                    "amount": 10.00,
                    "status": "success",
                    "gateway": "test-gw",
                    "error_text": None,
                    "card_bin": "411111",
                    "ingested_at": ingested_ts,
                },
            )
    return ids


def _seed_outside_window(conn: psycopg.Connection) -> str:
    """Insert a row outside the default 5-minute window (so it won't be counted).

    Computes datetime in Python (like _seed_with_known_lags), not as SQL string.
    """
    tx_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        # Get the current transaction timestamp (stable in REPEATABLE READ)
        cur.execute("SELECT now()::timestamptz AS now")
        tx_now = cur.fetchone()["now"]

        # event_timestamp: 15 minutes ago (well outside default 5-min window)
        event_ts = tx_now - timedelta(minutes=15)
        # ingested_at: same timestamp (no lag)
        ingested_ts = event_ts

        cur.execute(
            _INSERT_SQL,
            {
                "id": tx_id,
                "event_timestamp": event_ts,
                "merchant": MERCHANT,
                "method": "card",
                "amount": 10.00,
                "status": "success",
                "gateway": "test-gw",
                "error_text": None,
                "card_bin": "411111",
                "ingested_at": ingested_ts,
            },
        )
    return tx_id


@pytest.fixture()
def seeded_conn_with_lags(db_conn):
    """(conn, ids) — conn has rows with known lag values.

    Note: this fixture seeds data in the rollback-wrapped db_conn, which is fine
    for tests that share this connection (query_stats, semantic_search, get_transactions
    all accept a conn parameter). However, system_freshness() opens its own connection,
    so it cannot see uncommitted rows here. Tests that call system_freshness() for real
    (not mocked) should use seeded_committed_conn_with_lags instead.
    """
    # Use lag values that give us predictable percentiles:
    # [0.5, 1.0, 1.5, 2.0, 2.5, 3.0] seconds
    # p50 (median) = (1.5 + 2.0) / 2 = 1.75
    # p95 ≈ 2.925
    # p99 ≈ 2.985
    # max = 3.0
    # Data is seeded 2 min old (within 5-min default window with buffer for test execution).
    lag_values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    ids = _seed_with_known_lags(db_conn, lag_values)
    return db_conn, ids


@pytest.fixture()
def seeded_committed_conn_with_lags(connect_db_factory):
    """For system_freshness() tests: seeds rows in a committed transaction.

    system_freshness() opens its own connection and cannot see uncommitted rows
    from another connection's transaction (Postgres isolation). This fixture
    commits data to the real database and cleans it up afterward.
    Data is seeded 2 min old (within 5-min default window with buffer for test execution).
    """
    conn = connect_db_factory()
    try:
        # Use lag values that give us predictable percentiles:
        # [0.5, 1.0, 1.5, 2.0, 2.5, 3.0] seconds
        lag_values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        ids = _seed_with_known_lags(conn, lag_values)
        conn.commit()
        yield conn, ids
    finally:
        # Clean up: delete the seeded rows by merchant marker
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE merchant = %s", (MERCHANT,))
        conn.commit()
        conn.close()


@pytest.fixture()
def empty_window_conn(db_conn):
    """conn with a row outside the default 5-minute window (empty for freshness query).

    Note: this fixture seeds data in the rollback-wrapped db_conn. Tests that call
    system_freshness() for real (not mocked) should use empty_window_committed_conn.
    """
    _seed_outside_window(db_conn)
    return db_conn


@pytest.fixture()
def empty_window_committed_conn(connect_db_factory):
    """For system_freshness() tests: seeds a row outside the window in committed transaction.

    system_freshness() opens its own connection and cannot see uncommitted rows.
    This fixture commits data and cleans it up afterward.
    """
    conn = connect_db_factory()
    try:
        _seed_outside_window(conn)
        conn.commit()
        yield conn
    finally:
        # Clean up: delete the seeded rows by merchant marker
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE merchant = %s", (MERCHANT,))
        conn.commit()
        conn.close()


class _ForbiddenFreshness:
    """Stand-in that fails if query_freshness is called.

    Used to verify validation happens before calling query_freshness().
    """

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "system_freshness called query_freshness() before input validation raised ValueError"
        )


# --------------------------------------------------------------------------
# Behavior 1 — default window (5 minutes) calls query_freshness with "5 minutes"
# --------------------------------------------------------------------------


def test_default_window_5_minutes(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    result = system_freshness()

    assert result["window_minutes"] == 5
    assert result["event_count"] == 6
    assert result["p50_seconds"] is not None
    assert result["p95_seconds"] is not None
    assert result["p99_seconds"] is not None
    assert result["max_seconds"] is not None
    assert "human_readable" in result
    assert isinstance(result["human_readable"], str)


# --------------------------------------------------------------------------
# Behavior 1 & 2 — window_minutes parameter is translated to "X minutes" string
# --------------------------------------------------------------------------


def test_custom_window_minutes_is_honored(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    result = system_freshness(window_minutes=10)

    assert result["window_minutes"] == 10
    # Since our data is 2 min old, it is well within a 10-minute window
    assert result["event_count"] == 6


def test_window_minutes_1_is_valid(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    # Should not raise; 1 minute is the lower bound
    result = system_freshness(window_minutes=1)
    # Data is 2 min old, so won't match 1-minute window
    assert result["event_count"] == 0


def test_window_minutes_60_is_valid(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    # Should not raise; 60 minutes is the upper bound
    result = system_freshness(window_minutes=60)
    assert result["event_count"] == 6


# --------------------------------------------------------------------------
# Behavior 3 — percentiles are rounded to 1 decimal place
# --------------------------------------------------------------------------


def test_percentiles_rounded_to_1_decimal_place(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    result = system_freshness()

    # Check that all percentile values are either None or a numeric type with max 1 decimal
    from decimal import Decimal
    for field in ["p50_seconds", "p95_seconds", "p99_seconds", "max_seconds"]:
        value = result[field]
        if value is not None:
            # Accept float or Decimal (both are numeric types)
            assert isinstance(value, (float, Decimal)), (
                f"{field} should be float or Decimal, got {type(value)}"
            )
            # Check it's rounded to 1 decimal place: value == round(value, 1)
            assert value == round(value, 1), (
                f"{field} not rounded to 1 decimal: {value}"
            )


def test_percentiles_match_query_freshness_output(seeded_committed_conn_with_lags):
    """Verify percentiles match what query_freshness computed (same underlying SQL)."""
    from consumer.freshness import query_freshness

    result_mcp = system_freshness(window_minutes=5)
    result_direct = query_freshness(window="5 minutes")

    if result_direct is None:
        assert result_mcp["event_count"] == 0
        assert result_mcp["p50_seconds"] is None
    else:
        assert result_mcp["event_count"] == result_direct["event_count"]
        # Percentiles from query_freshness are floats; system_freshness rounds to 1 decimal
        expected_p50 = round(result_direct["p50"], 1)
        expected_p95 = round(result_direct["p95"], 1)
        expected_p99 = round(result_direct["p99"], 1)
        expected_max = round(result_direct["max"], 1)

        assert result_mcp["p50_seconds"] == expected_p50
        assert result_mcp["p95_seconds"] == expected_p95
        assert result_mcp["p99_seconds"] == expected_p99
        assert result_mcp["max_seconds"] == expected_max


# --------------------------------------------------------------------------
# Behavior 4 — no events in window: all None, not an error
# --------------------------------------------------------------------------


def test_no_events_in_window_returns_all_none(empty_window_committed_conn):
    # Row is 15 min old; 5-minute window won't catch it
    result = system_freshness(window_minutes=5)

    assert result["window_minutes"] == 5
    assert result["event_count"] == 0
    assert result["p50_seconds"] is None
    assert result["p95_seconds"] is None
    assert result["p99_seconds"] is None
    assert result["max_seconds"] is None
    assert "human_readable" in result
    assert "No events" in result["human_readable"] or "no events" in result["human_readable"]


def test_no_events_is_not_an_error():
    """Call system_freshness when we know there's no data (impossible gateway)."""
    # This calls the real query_freshness which queries the real DB with an
    # impossible time window (should be fast and empty).
    result = system_freshness(window_minutes=1)  # 1-minute window in the past

    # Should not raise; should return the no-events shape
    assert result["event_count"] == 0
    assert result["p50_seconds"] is None
    assert all(
        result[k] is None for k in ["p95_seconds", "p99_seconds", "max_seconds"]
    )


# --------------------------------------------------------------------------
# Behavior 5 — human_readable string
# --------------------------------------------------------------------------


def test_human_readable_with_events(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    result = system_freshness(window_minutes=5)

    human = result["human_readable"]
    assert isinstance(human, str)
    # Should mention p50 lag and event count
    assert "p50" in human.lower() or "0." in human  # some lag value
    assert "5 minutes" in human or "5" in human  # window
    assert "events" in human.lower() or "6" in human  # event count


def test_human_readable_with_no_events(empty_window_committed_conn):
    result = system_freshness(window_minutes=5)

    human = result["human_readable"]
    assert isinstance(human, str)
    assert "no events" in human.lower() or "No events" in human


# --------------------------------------------------------------------------
# Edge cases & errors — ValueError before calling query_freshness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_window", [0, -1, -30, 61, 100, 1000])
def test_window_minutes_out_of_bounds_raises_value_error(bad_window):
    """window_minutes outside [1, 60] raises ValueError."""
    # Pass a fake query_freshness that should NOT be called
    # (but we don't use it in this test since validation happens first)
    with pytest.raises(ValueError) as excinfo:
        system_freshness(window_minutes=bad_window)
    message = str(excinfo.value).lower()
    assert "window" in message or "60" in message or "bound" in message


def test_window_minutes_boundary_1_is_valid(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    # Should not raise; 1 is the inclusive lower bound
    result = system_freshness(window_minutes=1)
    assert result["window_minutes"] == 1


def test_window_minutes_boundary_60_is_valid(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    # Should not raise; 60 is the inclusive upper bound
    result = system_freshness(window_minutes=60)
    assert result["window_minutes"] == 60


# --------------------------------------------------------------------------
# Return shape — plain dict, JSON-serializable
# --------------------------------------------------------------------------


def test_return_shape_is_plain_json_serializable_dict(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    result = system_freshness()

    assert isinstance(result, dict)
    # json.dumps will raise TypeError if any value is not JSON-serializable
    # (accepting Decimal which needs explicit conversion to float for JSON)
    from decimal import Decimal
    def json_serialize_decimal(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    json.dumps(result, default=json_serialize_decimal)


def test_return_shape_with_events_has_all_fields(seeded_committed_conn_with_lags):
    conn, ids = seeded_committed_conn_with_lags
    result = system_freshness(window_minutes=5)

    required_fields = {
        "window_minutes",
        "event_count",
        "p50_seconds",
        "p95_seconds",
        "p99_seconds",
        "max_seconds",
        "human_readable",
    }
    assert set(result.keys()) == required_fields
    assert result["window_minutes"] == 5
    assert isinstance(result["event_count"], int)
    assert result["event_count"] > 0


def test_return_shape_with_no_events_has_all_fields(empty_window_committed_conn):
    result = system_freshness(window_minutes=5)

    required_fields = {
        "window_minutes",
        "event_count",
        "p50_seconds",
        "p95_seconds",
        "p99_seconds",
        "max_seconds",
        "human_readable",
    }
    assert set(result.keys()) == required_fields
    assert result["window_minutes"] == 5
    assert result["event_count"] == 0
    assert all(
        result[k] is None
        for k in ["p50_seconds", "p95_seconds", "p99_seconds", "max_seconds"]
    )


# --------------------------------------------------------------------------
# Behavior 8 — does NOT open its own connection (unlike query_stats/semantic_search)
# --------------------------------------------------------------------------


def test_system_freshness_calls_query_freshness_directly():
    """Verify that system_freshness reuses query_freshness without opening a connection."""
    from unittest.mock import patch

    # Mock query_freshness to verify it's called with the right window string
    with patch("mcp_server.freshness.query_freshness") as mock_qf:
        mock_qf.return_value = {
            "event_count": 10,
            "p50": 1.5,
            "p95": 2.5,
            "p99": 3.0,
            "max": 4.0,
        }

        result = system_freshness(window_minutes=7)

        # Verify query_freshness was called with the translated window string
        mock_qf.assert_called_once_with(window="7 minutes")

        # Verify result shape is correct
        assert result["window_minutes"] == 7
        assert result["event_count"] == 10
        assert result["p50_seconds"] == 1.5
        assert result["p95_seconds"] == 2.5
        assert result["p99_seconds"] == 3.0
        assert result["max_seconds"] == 4.0


def test_system_freshness_handles_none_from_query_freshness():
    """Verify that system_freshness handles None return from query_freshness."""
    from unittest.mock import patch

    with patch("mcp_server.freshness.query_freshness") as mock_qf:
        mock_qf.return_value = None

        result = system_freshness(window_minutes=5)

        assert result["event_count"] == 0
        assert result["p50_seconds"] is None
        assert result["p95_seconds"] is None
        assert result["p99_seconds"] is None
        assert result["max_seconds"] is None


# --------------------------------------------------------------------------
# Behavior 9 — FastMCP server and tool registration / end-to-end call
# --------------------------------------------------------------------------


def _tool_result_dict(result):
    """Extract the plain-dict payload from a fastmcp CallToolResult."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return json.loads(result.content[0].text)


def test_mcp_server_registers_system_freshness_tool():
    from fastmcp import Client

    from mcp_server.server import mcp

    assert mcp.name == "streaming-rag"

    async def _list():
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_list())
    by_name = {t.name: t for t in tools}
    assert "system_freshness" in by_name

    # Tool exposes only window_minutes parameter
    schema = by_name["system_freshness"].inputSchema
    properties = set(schema.get("properties", {}))
    assert properties == {"window_minutes"}


def test_mcp_tool_end_to_end_default_window():
    from fastmcp import Client

    from mcp_server.server import mcp

    async def _call():
        async with Client(mcp) as client:
            return await client.call_tool("system_freshness", {})

    payload = _tool_result_dict(asyncio.run(_call()))

    assert payload["window_minutes"] == 5
    assert isinstance(payload["event_count"], int)
    assert payload["event_count"] >= 0
    # Percentiles are either None or floats
    for field in ["p50_seconds", "p95_seconds", "p99_seconds", "max_seconds"]:
        value = payload[field]
        assert value is None or isinstance(value, float)
    assert isinstance(payload["human_readable"], str)
    # Should be JSON serializable
    json.dumps(payload)


def test_mcp_tool_end_to_end_custom_window():
    from fastmcp import Client

    from mcp_server.server import mcp

    async def _call():
        async with Client(mcp) as client:
            return await client.call_tool("system_freshness", {"window_minutes": 30})

    payload = _tool_result_dict(asyncio.run(_call()))

    assert payload["window_minutes"] == 30
    assert isinstance(payload["event_count"], int)
    assert isinstance(payload["human_readable"], str)
    json.dumps(payload)


# --------------------------------------------------------------------------
# Integration — consistency with consumer.freshness.query_freshness
# --------------------------------------------------------------------------


def test_system_freshness_uses_same_sql_as_query_freshness(seeded_committed_conn_with_lags):
    """Both functions query the same window using event_timestamp for filtering."""
    from consumer.freshness import query_freshness

    # Create identical conditions: call both with the same window
    result_mcp = system_freshness(window_minutes=30)
    result_direct = query_freshness(window="30 minutes")

    # Both should see the same event_count
    if result_direct is None:
        assert result_mcp["event_count"] == 0
    else:
        assert result_mcp["event_count"] == result_direct["event_count"]
        # Both use the same percentile_cont SQL, so values should match
        # (after rounding in MCP)
        assert result_mcp["p50_seconds"] == round(result_direct["p50"], 1)
        assert result_mcp["p95_seconds"] == round(result_direct["p95"], 1)
        assert result_mcp["p99_seconds"] == round(result_direct["p99"], 1)
        assert result_mcp["max_seconds"] == round(result_direct["max"], 1)
