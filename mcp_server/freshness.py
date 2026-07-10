"""Ingest-lag freshness for the MCP system_freshness tool.

system_freshness is the pure core of the MCP tool: unlike stats.py/semantic.py
it does not accept (or open) a psycopg connection at all. The percentile math
already lives in consumer/freshness.py::query_freshness(), which opens and
closes its own connection internally — this module simply translates
window_minutes into the interval string that function expects, calls it, and
reshapes the result into a plain JSON-serializable dict. No SQL and no
percentile computation happens here; that would duplicate consumer/freshness.py
rather than reuse it.
"""

from consumer.freshness import query_freshness

# Basic sanity cap only (Step 13 scope) — exhaustive validation is Step 14.
# Kept tight (1h) because this tool describes *recent* ingest lag, not a
# long historical window.
_MAX_WINDOW_MINUTES = 60


def system_freshness(window_minutes: int = 5) -> dict:
    """Report ingest-lag percentiles over the last window_minutes.

    Calls consumer.freshness.query_freshness() with window_minutes translated
    to an interval string (e.g. "5 minutes") and reshapes its result: p50/p95/
    p99/max become p50_seconds/p95_seconds/p99_seconds/max_seconds, each
    rounded to 1 decimal place, plus event_count and a short human_readable
    summary line. When there are no events in the window, query_freshness()
    returns None — that's not an error, so this returns event_count 0 and all
    four percentile fields as None with an explanatory human_readable line.
    """
    # Basic sanity check only — exhaustive validation is Step 14.
    if window_minutes <= 0 or window_minutes > _MAX_WINDOW_MINUTES:
        raise ValueError(
            f"window_minutes must be > 0 and <= {_MAX_WINDOW_MINUTES}, got {window_minutes}"
        )

    stats = query_freshness(window=f"{window_minutes} minutes")

    if stats is None:
        return {
            "window_minutes": window_minutes,
            "event_count": 0,
            "p50_seconds": None,
            "p95_seconds": None,
            "p99_seconds": None,
            "max_seconds": None,
            "human_readable": (
                f"No events in the last {window_minutes} minutes — "
                "freshness cannot be computed."
            ),
        }

    p50 = round(stats["p50"], 1)
    p95 = round(stats["p95"], 1)
    p99 = round(stats["p99"], 1)
    max_seconds = round(stats["max"], 1)
    event_count = stats["event_count"]

    return {
        "window_minutes": window_minutes,
        "event_count": event_count,
        "p50_seconds": p50,
        "p95_seconds": p95,
        "p99_seconds": p99,
        "max_seconds": max_seconds,
        "human_readable": (
            f"Data is current as of ~{p50}s (p50) over the last "
            f"{window_minutes} minutes ({event_count:,} events)."
        ),
    }
