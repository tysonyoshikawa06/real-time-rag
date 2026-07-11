"""Tests for spec 11B — MCP server skeleton + `query_stats` tool.

Derived from .claude/specs/11b-mcp-query-stats.spec.md ONLY (not from the
implementation). One test file for this feature; new cases get appended here.

Determinism (spec behavior 8): synthetic rows are INSERTed inside an
uncommitted REPEATABLE READ transaction (see tests/conftest.py) and rolled
back afterwards. A distinctive merchant value isolates assertions from any
live stream data; the one unavoidable unfiltered assertion (behavior 1)
compares against a baseline count taken inside the same snapshot.
"""

import json
import uuid

import psycopg
import pytest

from mcp_server.stats import query_stats

# --------------------------------------------------------------------------
# Synthetic dataset (distinctive values so live stream data cannot collide)
# --------------------------------------------------------------------------

MERCHANT = "TEST-MERCHANT-11B"
NO_SUCH_MERCHANT = "TEST-MERCHANT-11B-DOES-NOT-EXIST"
GW_A = "TEST-GW-ALPHA-11B"  # 4 rows in window: 3 card failures + 1 card success
GW_B = "TEST-GW-BETA-11B"  # 3 rows in window: 2 ach successes + 1 card failure
GW_C = "TEST-GW-GAMMA-11B"  # 2 rows in window: 2 wallet successes

# (minutes_ago, method, status, gateway)
_DATASET = [
    (5, "card", "failure", GW_A),
    (5, "card", "failure", GW_A),
    (5, "card", "failure", GW_A),
    (5, "card", "success", GW_A),
    (5, "ach", "success", GW_B),
    (5, "ach", "success", GW_B),
    (5, "card", "failure", GW_B),
    (5, "wallet", "success", GW_C),
    (5, "wallet", "success", GW_C),
    # Outside the default 30-minute window (behavior 6):
    (90, "card", "failure", GW_A),
]

IN_WINDOW = 9  # rows above with minutes_ago < 30
IN_WINDOW_CARD = 5  # 4 on GW_A + 1 on GW_B
IN_WINDOW_CARD_FAILURES = 4  # 3 on GW_A + 1 on GW_B
IN_WINDOW_FAILURES = 4  # 3 on GW_A + 1 on GW_B

_INSERT_SQL = """
    INSERT INTO transactions
        (transaction_id, event_timestamp, merchant, method, amount,
         status, gateway, error_text, card_bin)
    VALUES
        (%(id)s, now() - make_interval(mins => %(minutes_ago)s), %(merchant)s,
         %(method)s, %(amount)s, %(status)s, %(gateway)s, %(error_text)s,
         %(card_bin)s)
"""


def _seed(conn: psycopg.Connection) -> None:
    """Insert the synthetic dataset inside the caller's open transaction."""
    with conn.cursor() as cur:
        for minutes_ago, method, status, gateway in _DATASET:
            cur.execute(
                _INSERT_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "minutes_ago": minutes_ago,
                    "merchant": MERCHANT,
                    "method": method,
                    "amount": 10.00,
                    "status": status,
                    "gateway": gateway,
                    "error_text": "TEST 11B synthetic failure" if status == "failure" else None,
                    "card_bin": "411111",
                },
            )


def _window_baseline(conn: psycopg.Connection, minutes: int = 30) -> int:
    """Count of live (pre-existing) rows visible in the window snapshot."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM transactions "
            "WHERE event_timestamp >= now() - make_interval(mins => %s)",
            (minutes,),
        )
        return cur.fetchone()["n"]


@pytest.fixture()
def seeded_conn(db_conn):
    """(conn, baseline) — baseline is the live in-window row count taken in the
    same REPEATABLE READ snapshot, before the synthetic rows are inserted."""
    baseline = _window_baseline(db_conn)
    _seed(db_conn)
    return db_conn, baseline


class _ForbiddenConn:
    """Stand-in connection that fails loudly if touched at all.

    The spec requires whitelist/sanity ValueErrors to be raised *before any
    SQL executes* — so validation failures must never touch the connection.
    """

    def __getattr__(self, name):
        raise AssertionError(
            f"query_stats touched the connection (attribute {name!r}) "
            "before input validation raised ValueError"
        )


# --------------------------------------------------------------------------
# Behavior 1 — ungrouped, unfiltered window count
# --------------------------------------------------------------------------


def test_ungrouped_unfiltered_window_count(seeded_conn):
    conn, baseline = seeded_conn
    result = query_stats(conn)

    assert result["total_events"] == baseline + IN_WINDOW
    assert result["rows"] == [{"count": result["total_events"]}]
    # Echoed metadata per the return shape.
    assert result["window_minutes"] == 30
    assert result["metric"] == "count"
    assert result["group_by"] is None
    assert result["filters"] == {}  # {} when none given
    # Plain dict, JSON-serializable.
    assert isinstance(result, dict)
    json.dumps(result)


# --------------------------------------------------------------------------
# Behavior 2 — group_by counts, ordered desc, truncated to limit
# --------------------------------------------------------------------------


def test_group_by_gateway_counts_ordered_desc(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(conn, group_by="gateway", filters={"merchant": MERCHANT})

    assert result["total_events"] == IN_WINDOW
    assert result["group_by"] == "gateway"
    # Exact rows: counts DESC, and count-metric rows carry no failure_rate key.
    assert result["rows"] == [
        {"group": GW_A, "count": 4},
        {"group": GW_B, "count": 3},
        {"group": GW_C, "count": 2},
    ]


def test_limit_truncates_rows_but_not_total(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(conn, group_by="gateway", filters={"merchant": MERCHANT}, limit=2)

    # total_events counts all matching rows in the window, ignoring limit.
    assert result["total_events"] == IN_WINDOW
    assert result["rows"] == [
        {"group": GW_A, "count": 4},
        {"group": GW_B, "count": 3},
    ]


# --------------------------------------------------------------------------
# Behavior 3 — failure_rate metric
# --------------------------------------------------------------------------


def test_failure_rate_grouped_by_gateway(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(
        conn, group_by="gateway", filters={"merchant": MERCHANT}, metric="failure_rate"
    )

    assert result["metric"] == "failure_rate"
    rows = result["rows"]
    # Ordered by failure_rate DESC; every row has both count and failure_rate.
    assert [r["group"] for r in rows] == [GW_A, GW_B, GW_C]
    assert [r["count"] for r in rows] == [4, 3, 2]
    for r in rows:
        assert isinstance(r["failure_rate"], float)
        assert 0.0 <= r["failure_rate"] <= 1.0
    assert rows[0]["failure_rate"] == 0.75
    assert rows[1]["failure_rate"] == 0.3333  # 1/3 rounded to 4 decimal places
    assert rows[2]["failure_rate"] == 0.0
    json.dumps(result)  # Decimals from avg() must have been converted


def test_failure_rate_ungrouped_single_row(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(conn, filters={"merchant": MERCHANT}, metric="failure_rate")

    assert result["total_events"] == IN_WINDOW
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["count"] == IN_WINDOW
    assert row["failure_rate"] == round(IN_WINDOW_FAILURES / IN_WINDOW, 4)  # 0.4444
    assert "group" not in row  # group only when group_by is given


# --------------------------------------------------------------------------
# Behavior 4 — a single equality filter narrows rows AND total_events
# --------------------------------------------------------------------------


def test_filter_method_card_counts_only_card(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(
        conn, group_by="gateway", filters={"merchant": MERCHANT, "method": "card"}
    )

    assert result["total_events"] == IN_WINDOW_CARD
    assert result["rows"] == [
        {"group": GW_A, "count": 4},
        {"group": GW_B, "count": 1},
    ]
    assert result["filters"] == {"merchant": MERCHANT, "method": "card"}


# --------------------------------------------------------------------------
# Behavior 5 — multiple filters combine with AND
# --------------------------------------------------------------------------


def test_multiple_filters_combine_with_and(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(
        conn,
        filters={"merchant": MERCHANT, "method": "card", "status": "failure"},
    )

    assert result["total_events"] == IN_WINDOW_CARD_FAILURES
    assert result["rows"] == [{"count": IN_WINDOW_CARD_FAILURES}]


# --------------------------------------------------------------------------
# Behavior 6 — rows older than the window are excluded
# --------------------------------------------------------------------------


def test_window_excludes_rows_older_than_window(seeded_conn):
    conn, _ = seeded_conn
    # Default 30-minute window: the 90-minutes-ago row is excluded.
    narrow = query_stats(conn, filters={"merchant": MERCHANT})
    assert narrow["total_events"] == IN_WINDOW

    # Positive control: a 120-minute window includes it.
    wide = query_stats(conn, window_minutes=120, filters={"merchant": MERCHANT})
    assert wide["total_events"] == IN_WINDOW + 1
    assert wide["window_minutes"] == 120


# --------------------------------------------------------------------------
# Behavior 7 — filters matching nothing yield zeros, not errors
# --------------------------------------------------------------------------


def test_unmatched_filter_ungrouped_returns_zero_row(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(conn, filters={"merchant": NO_SUCH_MERCHANT})

    assert result["total_events"] == 0
    assert result["rows"] == [{"count": 0}]  # single row even when count is 0


def test_unmatched_filter_ungrouped_failure_rate_is_none(seeded_conn):
    # Spec row-shape rule: with metric="failure_rate" and zero matching rows
    # (ungrouped), the single row reports failure_rate None — a rate over zero
    # events is undefined, and 0.0 would falsely report health.
    conn, _ = seeded_conn
    result = query_stats(conn, filters={"merchant": NO_SUCH_MERCHANT}, metric="failure_rate")

    assert result["total_events"] == 0
    assert result["rows"] == [{"count": 0, "failure_rate": None}]
    json.dumps(result)  # None must serialize as JSON null


def test_unmatched_filter_grouped_returns_empty_rows(seeded_conn):
    conn, _ = seeded_conn
    result = query_stats(conn, group_by="gateway", filters={"merchant": NO_SUCH_MERCHANT})

    assert result["total_events"] == 0
    assert result["rows"] == []


# --------------------------------------------------------------------------
# Behavior 8 — takes an existing psycopg connection; issues only SELECTs
# --------------------------------------------------------------------------


def test_issues_only_selects_in_read_only_transaction(connect_db_factory):
    conn = connect_db_factory()
    try:
        conn.read_only = True  # any write would raise ReadOnlySqlTransaction
        result = query_stats(conn, filters={"merchant": NO_SUCH_MERCHANT})
        assert result["total_events"] == 0
    finally:
        conn.rollback()
        conn.close()


def test_uncommitted_transaction_rolls_back_cleanly(connect_db_factory):
    # Seed + query inside one uncommitted transaction, roll back, then verify
    # from a second connection that nothing persisted (query_stats must not
    # have committed anything).
    conn = connect_db_factory()
    try:
        _seed(conn)
        result = query_stats(conn, filters={"merchant": MERCHANT})
        assert result["total_events"] == IN_WINDOW
    finally:
        conn.rollback()
        conn.close()

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
# Edge cases & errors — ValueError before any SQL executes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_group_by", ["amount", "transaction_id; DROP", "card_bin", ""])
def test_group_by_outside_whitelist_raises_value_error(bad_group_by):
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), group_by=bad_group_by)
    # The error names the allowed values.
    message = str(excinfo.value)
    for allowed in ("method", "status", "gateway", "merchant"):
        assert allowed in message


@pytest.mark.parametrize(
    "bad_filters",
    [
        {"amount": "10"},
        {"transaction_id; DROP": "x"},
        {"merchant": "ok", "card_bin": "411111"},  # one bad key among good ones
    ],
)
def test_filters_key_outside_whitelist_raises_value_error(bad_filters):
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), filters=bad_filters)
    # The error names the allowed keys.
    message = str(excinfo.value)
    for allowed in ("method", "status", "gateway", "merchant"):
        assert allowed in message


@pytest.mark.parametrize("bad_metric", ["median", "sum", "COUNT; DROP", ""])
def test_metric_outside_whitelist_raises_value_error(bad_metric):
    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), metric=bad_metric)


@pytest.mark.parametrize("bad_window", [0, -1, -30])
def test_nonpositive_window_minutes_raises_value_error(bad_window):
    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), window_minutes=bad_window)


@pytest.mark.parametrize("bad_limit", [0, -1, -10])
def test_nonpositive_limit_raises_value_error(bad_limit):
    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), limit=bad_limit)


def test_sql_injection_probe_in_filter_value_matches_zero_rows(seeded_conn):
    conn, _ = seeded_conn
    probe = "x' OR '1'='1"

    # Ungrouped: parameterized value simply matches nothing — no error.
    result = query_stats(conn, filters={"merchant": probe})
    assert result["total_events"] == 0
    assert result["rows"] == [{"count": 0}]

    # Grouped, via a different filter column (values are open-ended, never
    # whitelisted, always parameterized).
    grouped = query_stats(conn, group_by="gateway", filters={"gateway": probe})
    assert grouped["total_events"] == 0
    assert grouped["rows"] == []

    # The transaction is still healthy afterwards (no aborted-transaction
    # state, no injected side effects): our synthetic rows are still visible.
    again = query_stats(conn, filters={"merchant": MERCHANT})
    assert again["total_events"] == IN_WINDOW


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


def test_mcp_server_name_and_tool_registration():
    import asyncio

    from fastmcp import Client

    from mcp_server.server import mcp

    assert mcp.name == "streaming-rag"

    async def _list():
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_list())
    by_name = {t.name: t for t in tools}
    assert "query_stats" in by_name

    # Parameters mirror query_stats minus conn.
    schema = by_name["query_stats"].inputSchema
    properties = set(schema.get("properties", {}))
    assert properties == {"window_minutes", "group_by", "filters", "metric", "limit"}
    assert "conn" not in properties


def test_mcp_tool_end_to_end_returns_documented_shape():
    import asyncio

    from fastmcp import Client

    from mcp_server.server import mcp

    # The tool opens its own DB connection, so it cannot see any test
    # transaction. Use an impossible merchant filter for determinism against
    # live stream data.
    async def _call():
        async with Client(mcp) as client:
            return await client.call_tool(
                "query_stats",
                {"window_minutes": 15, "filters": {"merchant": NO_SUCH_MERCHANT}, "limit": 5},
            )

    payload = _tool_result_dict(asyncio.run(_call()))

    assert payload["window_minutes"] == 15
    assert payload["metric"] == "count"
    assert payload["group_by"] is None
    assert payload["filters"] == {"merchant": NO_SUCH_MERCHANT}
    assert payload["total_events"] == 0
    assert payload["rows"] == [{"count": 0}]


# --------------------------------------------------------------------------
# Step 14: Input validation + limits — notes field + clamping behavior
# --------------------------------------------------------------------------


def test_valid_defaults_return_empty_notes(seeded_conn):
    """Regression: default params should return notes: []."""
    conn, _ = seeded_conn
    result = query_stats(conn, filters={"merchant": MERCHANT})

    assert "notes" in result
    assert result["notes"] == []


def test_valid_in_range_values_return_empty_notes(seeded_conn):
    """Regression: in-range window_minutes and limit should return notes: []."""
    conn, _ = seeded_conn
    result = query_stats(
        conn,
        window_minutes=60,
        limit=50,
        filters={"merchant": MERCHANT},
    )

    assert "notes" in result
    assert result["notes"] == []


def test_group_by_invalid_column_raises_value_error():
    """Behavior #12: group_by must be in allowed columns."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), group_by="banana")
    message = str(excinfo.value)
    # Message should list valid columns
    for allowed in ("method", "status", "gateway", "merchant"):
        assert allowed in message


def test_filter_key_invalid_raises_value_error():
    """Behavior #12: filter keys must be in allowed columns."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), filters={"amount": "10"})
    message = str(excinfo.value)
    # Message should list valid keys
    for allowed in ("method", "status", "gateway", "merchant"):
        assert allowed in message


def test_filter_status_invalid_value_raises_value_error():
    """Behavior #13: status filter value must be in {success, failure}."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), filters={"status": "maybe"})
    message = str(excinfo.value)
    # Message should list valid statuses
    assert "success" in message.lower()
    assert "failure" in message.lower()


def test_filter_method_invalid_value_raises_value_error():
    """Behavior #13: method filter value must be in {card, ach, wallet}."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), filters={"method": "crypto"})
    message = str(excinfo.value)
    # Message should list valid methods
    assert "card" in message.lower()
    assert "ach" in message.lower()
    assert "wallet" in message.lower()


def test_metric_invalid_raises_value_error():
    """Behavior #14: metric must be in {count, failure_rate}."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), metric="median")
    message = str(excinfo.value)
    assert "count" in message.lower()
    assert "failure_rate" in message.lower()


def test_window_minutes_non_integer_raises_value_error():
    """Behavior #11: window_minutes must be a positive integer."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), window_minutes=3.5)
    assert "integer" in str(excinfo.value).lower()


def test_window_minutes_zero_raises_value_error():
    """Behavior #11: window_minutes=0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), window_minutes=0)
    assert "positive" in str(excinfo.value).lower()


def test_window_minutes_negative_raises_value_error():
    """Behavior #11: window_minutes<0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), window_minutes=-5)
    assert "positive" in str(excinfo.value).lower()


def test_window_minutes_boolean_raises_value_error():
    """Behavior #11: window_minutes=True/False rejects explicitly."""
    # In Python, bool is a subclass of int, so True==1 and False==0.
    # Spec: "Booleans are not accepted as integers... reject True/False explicitly."
    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), window_minutes=True)

    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), window_minutes=False)


def test_limit_non_integer_raises_value_error():
    """Behavior #15: limit must be a positive integer."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), limit=2.5)
    assert "integer" in str(excinfo.value).lower()


def test_limit_zero_raises_value_error():
    """Behavior #15: limit=0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), limit=0)
    assert "positive" in str(excinfo.value).lower()


def test_limit_negative_raises_value_error():
    """Behavior #15: limit<0 rejects (never clamps)."""
    with pytest.raises(ValueError) as excinfo:
        query_stats(_ForbiddenConn(), limit=-10)
    assert "positive" in str(excinfo.value).lower()


def test_limit_boolean_raises_value_error():
    """Behavior #15: limit=True/False rejects explicitly."""
    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), limit=True)

    with pytest.raises(ValueError):
        query_stats(_ForbiddenConn(), limit=False)


def test_limit_clamped_to_100_with_note(seeded_conn):
    """Behavior #15: limit > 100 clamps to 100 with a note."""
    conn, _ = seeded_conn
    result = query_stats(conn, limit=99999, filters={"merchant": MERCHANT})

    assert result["limit"] == 100
    assert len(result["notes"]) > 0
    # Note should mention the limit was capped
    note_text = " ".join(result["notes"]).lower()
    assert "limit" in note_text
    assert "capped" in note_text or "cap" in note_text or "exceeded" in note_text


def test_window_minutes_clamped_to_1440_with_note(seeded_conn):
    """Behavior #11: window_minutes > 1440 clamps to 1440 with a note."""
    conn, _ = seeded_conn
    result = query_stats(conn, window_minutes=5000, filters={"merchant": MERCHANT})

    assert result["window_minutes"] == 1440
    assert len(result["notes"]) > 0
    # Note should mention the window was capped
    note_text = " ".join(result["notes"]).lower()
    assert "window" in note_text
    assert "capped" in note_text or "cap" in note_text or "exceeded" in note_text


def test_window_minutes_boundary_1_is_valid(seeded_conn):
    """Edge case: window_minutes=1 is the minimum and should pass without clamping."""
    conn, _ = seeded_conn
    result = query_stats(conn, window_minutes=1, filters={"merchant": MERCHANT})

    assert result["window_minutes"] == 1
    assert result["notes"] == []


def test_window_minutes_boundary_1440_is_valid(seeded_conn):
    """Edge case: window_minutes=1440 is the maximum and should pass without clamping."""
    conn, _ = seeded_conn
    result = query_stats(conn, window_minutes=1440, filters={"merchant": MERCHANT})

    assert result["window_minutes"] == 1440
    assert result["notes"] == []


def test_limit_boundary_1_is_valid(seeded_conn):
    """Edge case: limit=1 is the minimum and should pass without clamping."""
    conn, _ = seeded_conn
    result = query_stats(conn, limit=1, filters={"merchant": MERCHANT})

    assert result["limit"] == 1
    assert result["notes"] == []


def test_limit_boundary_100_is_valid(seeded_conn):
    """Edge case: limit=100 is the maximum and should pass without clamping."""
    conn, _ = seeded_conn
    result = query_stats(conn, limit=100, filters={"merchant": MERCHANT})

    assert result["limit"] == 100
    assert result["notes"] == []
