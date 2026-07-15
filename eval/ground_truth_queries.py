"""Independent ground-truth SQL for each golden question (Step 19A).

One function per `demo/golden_questions.json` id, dispatched via
GROUND_TRUTH_FUNCS. Each function issues fresh, independent SQL against its
own connection, scoped to the window the question itself concerns - never the
window the agent's own tool calls happened to use, and never a replay of the
agent's own tool output. This is what makes the captured number genuine
ground truth rather than a self-check against the thing being evaluated.

`aggregation`/`gateway_rate` reuse mcp_server.stats.query_stats(conn, ...)
directly (the same pure aggregation core the MCP tool wraps) since that IS
independent ground truth for a counting/rate question - calling it here with
our own chosen window/group_by/metric, not through the MCP transport and not
by reading the agent's tool call. The rest issue direct parameterized SQL.

Every query is parameterized (no f-string SQL values), matching the existing
codebase's injection-safety convention (see mcp_server/stats.py,
mcp_server/transactions.py).
"""

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from consumer.freshness import query_freshness
from mcp_server.stats import query_stats
from producer.scenarios import NOVEL_ERROR_SIGNATURE

_FRAUD_MAX_AMOUNT = 5.00

_FRAUD_SQL = """
    SELECT transaction_id, card_bin, amount::float8 AS amount, event_timestamp
    FROM transactions
    WHERE method = 'card' AND status = 'success'
    ORDER BY event_timestamp DESC
    LIMIT 12
"""

_NOVEL_ERROR_SQL = """
    SELECT transaction_id, error_text, event_timestamp
    FROM transactions
    WHERE status = 'failure'
      AND event_timestamp >= now() - make_interval(mins => 3)
    ORDER BY event_timestamp DESC
    LIMIT 15
"""

_HALLUCINATION_SQL = """
    SELECT count(*) AS count FROM transactions WHERE method = %(method)s
"""


def _row_dict(cur) -> list[dict]:
    return [dict(row) for row in cur.fetchall()]


def aggregation(conn: psycopg.Connection, incident_context: dict | None) -> dict:
    """No incident - independent counts by method and by gateway over 5 minutes."""
    return {
        "by_method": query_stats(conn, window_minutes=5, group_by="method", metric="count"),
        "by_gateway": query_stats(conn, window_minutes=5, group_by="gateway", metric="count"),
    }


def gateway_rate(conn: psycopg.Connection, incident_context: dict | None) -> dict:
    """gateway_degradation - independent failure_rate by gateway over 3 minutes."""
    return query_stats(conn, window_minutes=3, group_by="gateway", metric="failure_rate")


def fraud_pattern(conn: psycopg.Connection, incident_context: dict | None) -> dict:
    """fraud_burst - the 12 most recent successful card transactions, plus which
    rows match this run's target card_bin under $5."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(_FRAUD_SQL)
    rows = _row_dict(cur)
    for row in rows:
        row["transaction_id"] = str(row["transaction_id"])
        row["event_timestamp"] = row["event_timestamp"].isoformat()

    target_bin = (incident_context or {}).get("card_bin")
    matches = [
        row
        for row in rows
        if row["card_bin"] == target_bin and row["amount"] < _FRAUD_MAX_AMOUNT
    ]
    return {
        "rows": rows,
        "target_card_bin": target_bin,
        "max_amount": _FRAUD_MAX_AMOUNT,
        "matching_transaction_ids": [row["transaction_id"] for row in matches],
    }


def novel_error(conn: psycopg.Connection, incident_context: dict | None) -> dict:
    """novel_error_pattern - the 15 most recent failures in the last 3 minutes,
    plus which rows contain the fixed novel-error signature string."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(_NOVEL_ERROR_SQL)
    rows = _row_dict(cur)
    for row in rows:
        row["transaction_id"] = str(row["transaction_id"])
        row["event_timestamp"] = row["event_timestamp"].isoformat()

    matches = [row for row in rows if NOVEL_ERROR_SIGNATURE in (row["error_text"] or "")]
    return {
        "rows": rows,
        "signature": NOVEL_ERROR_SIGNATURE,
        "matching_transaction_ids": [row["transaction_id"] for row in matches],
    }


def freshness(conn: psycopg.Connection, incident_context: dict | None) -> dict:
    """No incident - independent freshness stats over 5 minutes.

    query_freshness() opens/closes its own connection (see
    consumer/freshness.py), so the `conn` passed here is unused - kept only
    so this function matches GROUND_TRUTH_FUNCS' uniform call signature.

    query_freshness()'s "max" field comes back from Postgres as a Decimal
    (unlike p50/p95/p99, which percentile_cont returns as plain floats) -
    cast every numeric field to float here so this dict is safely
    JSON-serializable by plain json.dumps, matching p50/p95/p99's type.
    """
    stats = query_freshness(window="5 minutes")
    if stats is None:
        return {}
    return {
        **stats,
        "p50": float(stats["p50"]),
        "p95": float(stats["p95"]),
        "p99": float(stats["p99"]),
        "max": float(stats["max"]),
    }


def hallucination_control(conn: psycopg.Connection, incident_context: dict | None) -> dict:
    """No incident - independent count of the (nonexistent) 'crypto' method."""
    cur = conn.cursor(row_factory=dict_row)
    cur.execute(_HALLUCINATION_SQL, {"method": "crypto"})
    return dict(cur.fetchone())


GROUND_TRUTH_FUNCS: dict[str, Callable[[psycopg.Connection, dict | None], dict]] = {
    "aggregation": aggregation,
    "gateway_rate": gateway_rate,
    "fraud_pattern": fraud_pattern,
    "novel_error": novel_error,
    "freshness": freshness,
    "hallucination_control": hallucination_control,
}
