"""Tests for spec 13a — `get_transactions` MCP tool.

Derived from .claude/specs/13a-mcp-get-transactions.spec.md ONLY (not from the
implementation). One test file for this feature; new cases get appended here.

Determinism (spec behaviors 1-6): synthetic transaction rows are INSERTed inside
an uncommitted REPEATABLE READ transaction (see tests/conftest.py) and rolled
back afterwards. Distinctive merchant/gateway values isolate assertions from any
live stream data; the flexible nature of this tool (returning rows by ID or
filter) is tested comprehensively within the same transaction.
"""

import asyncio
import json
import uuid
from datetime import datetime

import psycopg
import pytest

from mcp_server.transactions import get_transactions

# --------------------------------------------------------------------------
# Synthetic dataset (distinctive values so live stream data cannot collide)
# --------------------------------------------------------------------------

MERCHANT = "TEST-MERCHANT-13A"
NO_SUCH_MERCHANT = "TEST-MERCHANT-13A-DOES-NOT-EXIST"
GW_A = "TEST-GW-ALPHA-13A"  # 4 rows in window
GW_B = "TEST-GW-BETA-13A"   # 3 rows in window
GW_C = "TEST-GW-GAMMA-13A"  # 2 rows in window

# label, minutes_ago, method, status, gateway, amount, error_text
_DATASET = [
    ("tx1", 5, "card", "failure", GW_A, 46.67, "connection timed out"),
    ("tx2", 5, "card", "failure", GW_A, 12.10, "card declined"),
    ("tx3", 5, "card", "failure", GW_A, 99.99, "timeout"),
    ("tx4", 5, "card", "success", GW_A, 25.00, None),
    ("tx5", 5, "ach", "success", GW_B, 100.00, None),
    ("tx6", 5, "ach", "success", GW_B, 50.00, None),
    ("tx7", 5, "card", "failure", GW_B, 75.50, "gateway error"),
    ("tx8", 5, "wallet", "success", GW_C, 10.00, None),
    ("tx9", 5, "wallet", "success", GW_C, 20.00, None),
    # Outside the default 30-minute window (for window boundary tests):
    ("tx10", 90, "card", "failure", GW_A, 5.00, "stale row"),
]

_INSERT_SQL = """
    INSERT INTO transactions
        (transaction_id, event_timestamp, merchant, method, amount,
         status, gateway, error_text, card_bin, ingested_at)
    VALUES
        (%(id)s, now() - make_interval(mins => %(minutes_ago)s), %(merchant)s,
         %(method)s, %(amount)s, %(status)s, %(gateway)s, %(error_text)s,
         %(card_bin)s, now() - make_interval(mins => %(minutes_ago)s))
"""


def _seed(conn: psycopg.Connection) -> dict[str, str]:
    """Insert the synthetic dataset inside the caller's open transaction.

    Returns label -> transaction_id mapping for easy reference in tests.
    """
    ids = {}
    with conn.cursor() as cur:
        for label, minutes_ago, method, status, gateway, amount, error_text in _DATASET:
            tx_id = str(uuid.uuid4())
            ids[label] = tx_id
            cur.execute(
                _INSERT_SQL,
                {
                    "id": tx_id,
                    "minutes_ago": minutes_ago,
                    "merchant": MERCHANT,
                    "method": method,
                    "amount": amount,
                    "status": status,
                    "gateway": gateway,
                    "error_text": error_text,
                    "card_bin": "411111" if method == "card" else None,
                },
            )
    return ids


@pytest.fixture()
def seeded_conn(db_conn):
    """(conn, ids) — conn has synthetic rows inserted, ids maps labels to UUIDs."""
    ids = _seed(db_conn)
    return db_conn, ids


class _ForbiddenConn:
    """Stand-in connection that fails loudly if touched at all.

    The spec requires validation ValueErrors to be raised *before any SQL
    executes* — so validation failures must never touch the connection.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f"get_transactions touched the connection (attribute {name!r}) "
            "before input validation raised ValueError"
        )


# --------------------------------------------------------------------------
# Behavior 1 — ID mode returns exactly the requested rows
# --------------------------------------------------------------------------


def test_id_mode_fetches_single_row_by_id(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    assert result["mode"] == "ids"
    assert result["transaction_ids"] == [ids["tx1"]]
    assert result["count"] == 1
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["transaction_id"] == ids["tx1"]
    assert row["merchant"] == MERCHANT
    assert row["method"] == "card"
    assert row["amount"] == 46.67
    assert row["status"] == "failure"
    assert row["gateway"] == GW_A
    assert row["error_text"] == "connection timed out"
    assert row["card_bin"] == "411111"
    assert isinstance(row["event_timestamp"], str)
    assert isinstance(row["ingested_at"], str)
    # ISO 8601 format check (no exception means valid format)
    datetime.fromisoformat(row["event_timestamp"])
    datetime.fromisoformat(row["ingested_at"])


def test_id_mode_fetches_multiple_rows_by_ids(seeded_conn):
    conn, ids = seeded_conn
    query_ids = [ids["tx1"], ids["tx5"], ids["tx8"]]
    result = get_transactions(conn, transaction_ids=query_ids)

    assert result["mode"] == "ids"
    assert set(result["transaction_ids"]) == set(query_ids)
    assert result["count"] == 3
    assert len(result["rows"]) == 3
    returned_ids = {row["transaction_id"] for row in result["rows"]}
    assert returned_ids == set(query_ids)


def test_id_mode_failure_with_nullable_fields(seeded_conn):
    conn, ids = seeded_conn
    # tx5 is ACH success, has no error_text or card_bin
    result = get_transactions(conn, transaction_ids=[ids["tx5"]])

    row = result["rows"][0]
    assert row["error_text"] is None
    assert row["card_bin"] is None
    assert row["method"] == "ach"


# --------------------------------------------------------------------------
# Behavior 2 — missing_ids reported when some IDs have no matching row
# --------------------------------------------------------------------------


def test_id_mode_reports_missing_ids(seeded_conn):
    conn, ids = seeded_conn
    fake_id = str(uuid.uuid4())
    query_ids = [ids["tx1"], fake_id, ids["tx2"]]
    result = get_transactions(conn, transaction_ids=query_ids)

    assert result["count"] == 2  # only 2 found
    assert len(result["rows"]) == 2
    assert result["missing_ids"] == [fake_id]
    returned_ids = {row["transaction_id"] for row in result["rows"]}
    assert returned_ids == {ids["tx1"], ids["tx2"]}


def test_id_mode_all_ids_missing(seeded_conn):
    conn, ids = seeded_conn
    fake_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    result = get_transactions(conn, transaction_ids=fake_ids)

    assert result["count"] == 0
    assert result["rows"] == []
    assert set(result["missing_ids"]) == set(fake_ids)


def test_id_mode_empty_missing_ids_when_all_found(seeded_conn):
    conn, ids = seeded_conn
    query_ids = [ids["tx1"], ids["tx2"]]
    result = get_transactions(conn, transaction_ids=query_ids)

    assert result["missing_ids"] == []


# --------------------------------------------------------------------------
# Behavior 3 — filter mode with default and custom windows
# --------------------------------------------------------------------------


def test_filter_mode_default_window_30_minutes(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn)

    assert result["mode"] == "filter"
    assert result["transaction_ids"] is None
    assert result["window_minutes"] == 30
    # 9 rows within 30 min window (tx1-tx9), tx10 is 90 min ago
    assert result["count"] == 9
    assert len(result["rows"]) == 9


def test_filter_mode_custom_window_includes_stale_row(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, window_minutes=120)

    assert result["window_minutes"] == 120
    # Now includes tx10 (90 min ago)
    assert result["count"] == 10
    assert len(result["rows"]) == 10


def test_filter_mode_narrow_window_excludes_stale_row(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, window_minutes=30)

    assert result["count"] == 9
    tx_ids = {row["transaction_id"] for row in result["rows"]}
    assert ids["tx10"] not in tx_ids


# --------------------------------------------------------------------------
# Behavior 3 & 4 — filter mode with filters (status, gateway, method)
# --------------------------------------------------------------------------


def test_filter_mode_filter_by_gateway(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway=GW_A)

    assert result["gateway"] == GW_A
    assert result["count"] == 4  # tx1, tx2, tx3, tx4 on GW_A
    for row in result["rows"]:
        assert row["gateway"] == GW_A


def test_filter_mode_filter_by_status(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, status="failure")

    assert result["status"] == "failure"
    assert result["count"] == 4  # tx1, tx2, tx3, tx7 are failures
    for row in result["rows"]:
        assert row["status"] == "failure"


def test_filter_mode_filter_by_method(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, method="card")

    assert result["method"] == "card"
    assert result["count"] == 5  # tx1, tx2, tx3, tx4, tx7
    for row in result["rows"]:
        assert row["method"] == "card"


def test_filter_mode_multiple_filters_combine_with_and(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway=GW_A, status="failure")

    assert result["gateway"] == GW_A
    assert result["status"] == "failure"
    assert result["count"] == 3  # tx1, tx2, tx3 on GW_A + failure
    for row in result["rows"]:
        assert row["gateway"] == GW_A
        assert row["status"] == "failure"


def test_filter_mode_all_three_filters(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(
        conn, gateway=GW_A, status="failure", method="card"
    )

    assert result["count"] == 3  # tx1, tx2, tx3
    for row in result["rows"]:
        assert row["gateway"] == GW_A
        assert row["status"] == "failure"
        assert row["method"] == "card"


# --------------------------------------------------------------------------
# Behavior 3 & 4 — filter mode row ordering (newest first) and limit
# --------------------------------------------------------------------------


def test_filter_mode_rows_ordered_by_event_timestamp_desc(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway=GW_A, limit=100)

    # All rows in GW_A have the same event_timestamp (5 min ago), so ordering
    # is deterministic within the group. At minimum, we can confirm rows are
    # returned (ordering by DESC at least doesn't crash).
    assert result["count"] >= 1
    assert len(result["rows"]) >= 1
    # Ensure they're sorted desc by checking timestamps don't go forward
    timestamps = [
        datetime.fromisoformat(row["event_timestamp"]) for row in result["rows"]
    ]
    for i in range(len(timestamps) - 1):
        assert timestamps[i] >= timestamps[i + 1]


def test_filter_mode_limit_truncates_rows(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, limit=5)

    assert result["limit"] == 5
    assert result["count"] == 5
    assert len(result["rows"]) == 5


def test_filter_mode_limit_with_fewer_rows_available(seeded_conn):
    conn, ids = seeded_conn
    # Filter to only gateway B (3 rows), request limit 10
    result = get_transactions(conn, gateway=GW_B, limit=10)

    assert result["count"] == 3
    assert len(result["rows"]) == 3


# --------------------------------------------------------------------------
# Behavior 4 & 5 — return shape: all 10 columns, amount as float, timestamps
# as ISO strings
# --------------------------------------------------------------------------


def test_return_shape_all_ten_columns(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    row = result["rows"][0]
    required_columns = {
        "transaction_id",
        "event_timestamp",
        "merchant",
        "method",
        "amount",
        "status",
        "gateway",
        "error_text",
        "card_bin",
        "ingested_at",
    }
    assert set(row.keys()) == required_columns


def test_amount_is_float_not_decimal(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    amount = result["rows"][0]["amount"]
    assert isinstance(amount, float)
    assert amount == 46.67


def test_timestamps_are_iso8601_strings(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    row = result["rows"][0]
    # Both must be valid ISO 8601
    _event_ts = datetime.fromisoformat(row["event_timestamp"])
    _ingested_ts = datetime.fromisoformat(row["ingested_at"])
    assert isinstance(row["event_timestamp"], str)
    assert isinstance(row["ingested_at"], str)


def test_transaction_id_is_string(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    tx_id = result["rows"][0]["transaction_id"]
    assert isinstance(tx_id, str)
    # Should be a valid UUID string
    uuid.UUID(tx_id)


# --------------------------------------------------------------------------
# Behavior 5 — count == len(rows) always
# --------------------------------------------------------------------------


def test_count_equals_len_rows_id_mode(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"], ids["tx2"]])
    assert result["count"] == len(result["rows"])


def test_count_equals_len_rows_filter_mode(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway=GW_A, limit=100)
    assert result["count"] == len(result["rows"])


def test_count_equals_len_rows_empty(seeded_conn):
    conn, ids = seeded_conn
    fake_id = str(uuid.uuid4())
    result = get_transactions(conn, transaction_ids=[fake_id])
    assert result["count"] == 0
    assert len(result["rows"]) == 0


# --------------------------------------------------------------------------
# Behavior 6 — filter mode with no matches returns empty rows, not error
# --------------------------------------------------------------------------


def test_filter_mode_no_matches_returns_empty(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway=NO_SUCH_MERCHANT)

    assert result["count"] == 0
    assert result["rows"] == []
    assert result["mode"] == "filter"


def test_filter_mode_filter_combines_with_no_results(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(
        conn, gateway=GW_A, method="wallet"
    )  # GW_A has no wallet rows

    assert result["count"] == 0
    assert result["rows"] == []


# --------------------------------------------------------------------------
# Edge cases & errors — ValueError before any SQL executes
# --------------------------------------------------------------------------


def test_id_mode_and_filter_param_raises_value_error():
    """Mutual exclusion: IDs + any filter param is an error."""
    fake_ids = [str(uuid.uuid4())]

    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, window_minutes=10)
    assert "window_minutes" in str(excinfo.value).lower() or "mode" in str(
        excinfo.value
    ).lower()

    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, status="success")

    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, gateway="test")

    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, method="card")


@pytest.mark.parametrize("too_many", [101, 200, 1000])
def test_id_mode_over_100_ids_raises_value_error(too_many):
    """IDs list > 100 raises ValueError."""
    many_ids = [str(uuid.uuid4()) for _ in range(too_many)]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=many_ids)
    message = str(excinfo.value).lower()
    assert "100" in message or "cap" in message or "limit" in message


@pytest.mark.parametrize("bad_window", [0, -1, -30])
def test_filter_mode_window_out_of_bounds_raises_value_error(bad_window):
    """window_minutes must be positive; > 1440 clamps instead of rejecting."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), window_minutes=bad_window)
    message = str(excinfo.value).lower()
    assert "window" in message or "positive" in message


@pytest.mark.parametrize("bad_limit", [0, -1, -10])
def test_filter_mode_limit_out_of_bounds_raises_value_error(bad_limit):
    """limit must be positive; > 100 clamps instead of rejecting."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), limit=bad_limit)
    message = str(excinfo.value).lower()
    assert "limit" in message or "positive" in message


# --------------------------------------------------------------------------
# Edge case — unmatched filter values with valid enums yield zero rows, not errors
# --------------------------------------------------------------------------


def test_unmatched_gateway_filter_yields_zero_rows(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway="nonexistent-gateway")
    assert result["count"] == 0
    assert result["rows"] == []


# --------------------------------------------------------------------------
# Behavior 8 — takes an existing psycopg connection; issues only SELECTs
# --------------------------------------------------------------------------


def test_issues_only_selects_in_read_only_transaction(connect_db_factory):
    """Any write would raise ReadOnlySqlTransaction."""
    conn = connect_db_factory()
    try:
        conn.read_only = True
        result = get_transactions(conn, gateway=NO_SUCH_MERCHANT)
        assert result["count"] == 0
    finally:
        conn.rollback()
        conn.close()


def test_uncommitted_transaction_rolls_back_cleanly(connect_db_factory):
    """Seed + query inside one uncommitted transaction, roll back, verify no persistence."""
    conn = connect_db_factory()
    try:
        ids = _seed(conn)
        result = get_transactions(conn, transaction_ids=[ids["tx1"]])
        assert result["count"] == 1
    finally:
        conn.rollback()
        conn.close()

    # Verify from a second connection that nothing persisted
    other = connect_db_factory(autocommit=True)
    try:
        with other.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM transactions WHERE merchant = %s",
                (MERCHANT,),
            )
            assert cur.fetchone()["n"] == 0
    finally:
        other.close()


# --------------------------------------------------------------------------
# Return shape — plain dict, JSON-serializable
# --------------------------------------------------------------------------


def test_return_shape_is_plain_json_serializable_dict(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    assert isinstance(result, dict)
    # json.dumps will raise TypeError if any value is not JSON-serializable
    # (e.g. UUID, Decimal, datetime objects)
    json.dumps(result)


def test_filter_mode_full_return_shape_metadata(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, gateway=GW_A, status="failure", limit=10)

    # Verify all metadata fields are echoed back
    assert result["mode"] == "filter"
    assert result["transaction_ids"] is None
    assert result["window_minutes"] == 30
    assert result["status"] == "failure"
    assert result["gateway"] == GW_A
    assert result["method"] is None  # not given
    assert result["limit"] == 10
    assert "count" in result
    assert "rows" in result
    # missing_ids should be [] in filter mode
    assert result["missing_ids"] == []


def test_id_mode_full_return_shape_metadata(seeded_conn):
    conn, ids = seeded_conn
    query_ids = [ids["tx1"], ids["tx2"]]
    result = get_transactions(conn, transaction_ids=query_ids)

    # Verify all metadata fields are echoed/set correctly
    assert result["mode"] == "ids"
    assert set(result["transaction_ids"]) == set(query_ids)
    assert result["window_minutes"] is None
    assert result["status"] is None
    assert result["gateway"] is None
    assert result["method"] is None
    assert result["limit"] == 10  # default
    assert "count" in result
    assert "rows" in result
    assert "missing_ids" in result


# --------------------------------------------------------------------------
# Edge case — empty transaction_ids list treated as filter mode
# --------------------------------------------------------------------------


def test_empty_transaction_ids_list_is_filter_mode(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[])

    # Empty list should be treated like None (filter mode)
    assert result["mode"] == "filter"
    assert result["window_minutes"] == 30


def test_empty_transaction_ids_with_filter_param_is_filter_mode(seeded_conn):
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[], gateway=GW_A)

    # Empty list + filter param should be OK (filter mode)
    assert result["mode"] == "filter"
    assert result["gateway"] == GW_A
    assert result["count"] == 4


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


def test_mcp_server_registers_get_transactions_tool():
    from fastmcp import Client

    from mcp_server.server import mcp

    assert mcp.name == "streaming-rag"

    async def _list():
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_list())
    by_name = {t.name: t for t in tools}
    assert "get_transactions" in by_name

    # Parameters mirror get_transactions minus conn.
    schema = by_name["get_transactions"].inputSchema
    properties = set(schema.get("properties", {}))
    expected = {
        "transaction_ids",
        "window_minutes",
        "status",
        "gateway",
        "method",
        "limit",
    }
    assert properties == expected


def test_mcp_tool_end_to_end_filter_mode():
    from fastmcp import Client

    from mcp_server.server import mcp

    # The tool opens its own DB connection, so it cannot see test transaction.
    # Use an impossible gateway filter for determinism against live stream.
    async def _call():
        async with Client(mcp) as client:
            return await client.call_tool(
                "get_transactions",
                {
                    "window_minutes": 30,
                    "gateway": NO_SUCH_MERCHANT,
                    "limit": 5,
                },
            )

    payload = _tool_result_dict(asyncio.run(_call()))

    assert payload["mode"] == "filter"
    assert payload["window_minutes"] == 30
    assert payload["gateway"] == NO_SUCH_MERCHANT
    assert payload["count"] == 0
    assert payload["rows"] == []
    assert payload["missing_ids"] == []
    # Should be JSON serializable
    json.dumps(payload)


# --------------------------------------------------------------------------
# Step 14: Input validation + limits — notes field + clamping behavior
# --------------------------------------------------------------------------


def test_valid_defaults_return_empty_notes(seeded_conn):
    """Regression: default params should return notes: []."""
    conn, ids = seeded_conn
    result = get_transactions(conn, transaction_ids=[ids["tx1"]])

    assert "notes" in result
    assert result["notes"] == []


def test_valid_filter_mode_return_empty_notes(seeded_conn):
    """Regression: in-range filter mode params should return notes: []."""
    conn, ids = seeded_conn
    result = get_transactions(conn, window_minutes=60, limit=50, gateway=GW_A)

    assert "notes" in result
    assert result["notes"] == []


def test_mode_exclusivity_ids_and_window_minutes_raises():
    """Behavior #22: transaction_ids + window_minutes is rejected with exact message."""
    fake_ids = [str(uuid.uuid4())]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, window_minutes=10)
    # Exact message from spec Behavior #22
    expected_msg = (
        "get_transactions accepts either transaction_ids OR filter params "
        "(window_minutes/status/gateway/method), not both. Pass IDs to look up specific "
        "rows, or filters to search."
    )
    assert expected_msg in str(excinfo.value)


def test_mode_exclusivity_ids_and_status_raises():
    """Behavior #22: transaction_ids + status is rejected with exact message."""
    fake_ids = [str(uuid.uuid4())]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, status="success")
    expected_msg = (
        "get_transactions accepts either transaction_ids OR filter params "
        "(window_minutes/status/gateway/method), not both. Pass IDs to look up specific "
        "rows, or filters to search."
    )
    assert expected_msg in str(excinfo.value)


def test_mode_exclusivity_ids_and_gateway_raises():
    """Behavior #22: transaction_ids + gateway is rejected with exact message."""
    fake_ids = [str(uuid.uuid4())]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, gateway="stripe-proxy")
    expected_msg = (
        "get_transactions accepts either transaction_ids OR filter params "
        "(window_minutes/status/gateway/method), not both. Pass IDs to look up specific "
        "rows, or filters to search."
    )
    assert expected_msg in str(excinfo.value)


def test_mode_exclusivity_ids_and_method_raises():
    """Behavior #22: transaction_ids + method is rejected with exact message."""
    fake_ids = [str(uuid.uuid4())]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=fake_ids, method="card")
    expected_msg = (
        "get_transactions accepts either transaction_ids OR filter params "
        "(window_minutes/status/gateway/method), not both. Pass IDs to look up specific "
        "rows, or filters to search."
    )
    assert expected_msg in str(excinfo.value)


def test_id_mode_too_many_ids_raises():
    """Behavior #24: transaction_ids > 100 rejects with message naming the cap."""
    many_ids = [str(uuid.uuid4()) for _ in range(101)]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=many_ids)
    message = str(excinfo.value).lower()
    assert "100" in message
    assert "reject" in message or "cap" in message or "too many" in message.lower()


def test_id_mode_500_ids_raises():
    """Behavior #24: transaction_ids with 500 entries raises (too many, not clamped)."""
    many_ids = [str(uuid.uuid4()) for _ in range(500)]
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=many_ids)
    message = str(excinfo.value).lower()
    # Should mention the cap (100) and that we're rejecting
    assert "100" in message


def test_id_mode_malformed_uuid_raises():
    """Behavior #24: malformed UUID in list is rejected, naming the malformed entry."""
    valid_id = str(uuid.uuid4())
    bad_id = "not-a-uuid"
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), transaction_ids=[bad_id, valid_id])
    message = str(excinfo.value)
    # Should name the malformed ID
    assert bad_id in message or "not-a-uuid" in message


def test_id_mode_valid_but_absent_uuid_returns_missing_ids(seeded_conn):
    """Behavior #24: valid-but-absent UUID returns found rows + missing_ids, not error."""
    conn, ids = seeded_conn
    absent_id = str(uuid.uuid4())
    # Pass one existing ID and one absent ID
    result = get_transactions(conn, transaction_ids=[ids["tx1"], absent_id])

    assert result["count"] == 1  # Only one found
    assert len(result["rows"]) == 1
    assert result["rows"][0]["transaction_id"] == ids["tx1"]
    assert absent_id in result["missing_ids"]


def test_filter_mode_no_args_defaults(seeded_conn):
    """Behavior #23: no args at all → filter mode with window 30, limit 10."""
    conn, ids = seeded_conn
    result = get_transactions(conn)

    assert result["mode"] == "filter"
    assert result["window_minutes"] == 30
    assert result["limit"] == 10
    assert result["status"] is None
    assert result["gateway"] is None
    assert result["method"] is None


def test_filter_mode_window_non_integer_raises():
    """Behavior #25: window_minutes must be a positive integer."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), window_minutes=3.5)
    assert "integer" in str(excinfo.value).lower()


def test_filter_mode_window_zero_raises():
    """Behavior #25: window_minutes=0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), window_minutes=0)
    assert "positive" in str(excinfo.value).lower()


def test_filter_mode_window_negative_raises():
    """Behavior #25: window_minutes<0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), window_minutes=-5)
    assert "positive" in str(excinfo.value).lower()


def test_filter_mode_window_boolean_raises():
    """Behavior #25: window_minutes=True/False rejects explicitly."""
    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), window_minutes=True)

    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), window_minutes=False)


def test_filter_mode_limit_non_integer_raises():
    """Behavior #25: limit must be a positive integer."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), limit=2.5)
    assert "integer" in str(excinfo.value).lower()


def test_filter_mode_limit_zero_raises():
    """Behavior #25: limit=0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), limit=0)
    assert "positive" in str(excinfo.value).lower()


def test_filter_mode_limit_negative_raises():
    """Behavior #25: limit<0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), limit=-10)
    assert "positive" in str(excinfo.value).lower()


def test_filter_mode_limit_boolean_raises():
    """Behavior #25: limit=True/False rejects explicitly."""
    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), limit=True)

    with pytest.raises(ValueError):
        get_transactions(_ForbiddenConn(), limit=False)


def test_filter_mode_status_invalid_raises():
    """Behavior #25: status value must be in {success, failure}."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), status="unknown")
    message = str(excinfo.value)
    assert "success" in message.lower()
    assert "failure" in message.lower()


def test_filter_mode_method_invalid_raises():
    """Behavior #25: method value must be in {card, ach, wallet}."""
    with pytest.raises(ValueError) as excinfo:
        get_transactions(_ForbiddenConn(), method="crypto")
    message = str(excinfo.value)
    assert "card" in message.lower()
    assert "ach" in message.lower()
    assert "wallet" in message.lower()


def test_filter_mode_window_clamped_to_1440_with_note(seeded_conn):
    """Behavior #25: window_minutes > 1440 clamps to 1440 with a note."""
    conn, ids = seeded_conn
    result = get_transactions(conn, window_minutes=5000)

    assert result["window_minutes"] == 1440
    assert len(result["notes"]) > 0
    # Note should mention the window was capped
    note_text = " ".join(result["notes"]).lower()
    assert "window" in note_text
    assert "capped" in note_text or "cap" in note_text or "exceeded" in note_text


def test_filter_mode_limit_clamped_to_100_with_note(seeded_conn):
    """Behavior #25: limit > 100 clamps to 100 with a note."""
    conn, ids = seeded_conn
    result = get_transactions(conn, limit=99999)

    assert result["limit"] == 100
    assert len(result["notes"]) > 0
    # Note should mention the limit was capped
    note_text = " ".join(result["notes"]).lower()
    assert "limit" in note_text
    assert "capped" in note_text or "cap" in note_text or "exceeded" in note_text


def test_filter_mode_window_boundary_1_is_valid(seeded_conn):
    """Edge case: window_minutes=1 is the minimum and should pass without clamping."""
    conn, ids = seeded_conn
    result = get_transactions(conn, window_minutes=1)

    assert result["window_minutes"] == 1
    assert result["notes"] == []


def test_filter_mode_window_boundary_1440_is_valid(seeded_conn):
    """Edge case: window_minutes=1440 is the maximum and should pass without clamping."""
    conn, ids = seeded_conn
    result = get_transactions(conn, window_minutes=1440)

    assert result["window_minutes"] == 1440
    assert result["notes"] == []


def test_filter_mode_limit_boundary_1_is_valid(seeded_conn):
    """Edge case: limit=1 is the minimum and should pass without clamping."""
    conn, ids = seeded_conn
    result = get_transactions(conn, limit=1)

    assert result["limit"] == 1
    assert result["notes"] == []


def test_filter_mode_limit_boundary_100_is_valid(seeded_conn):
    """Edge case: limit=100 is the maximum and should pass without clamping."""
    conn, ids = seeded_conn
    result = get_transactions(conn, limit=100)

    assert result["limit"] == 100
    assert result["notes"] == []
