"""Shared, pure validation/clamping helpers for the MCP tool delegates.

All four tool delegate modules (stats.py, semantic.py, transactions.py,
freshness.py) call into this module for bounds checks, enum/allowlist
membership, UUID format checks, and query-text length capping, instead of
repeating inline checks with slightly different messages and behavior.

Two families of numeric validation:
  - Reject-only (require_positive_int): non-integer or non-positive input has
    no sensible auto-fix, so it always raises.
  - Clamp (clamp_positive_int): an in-range-but-too-large positive integer is
    silently useful information from the caller (e.g. "give me as much as you
    can, up to a lot") — clamping it to the max and noting the clamp keeps the
    call working instead of forcing a retry.

This module has no dependency on psycopg or fastmcp — it is pure validation
logic, importable and testable standalone.
"""

import uuid
from collections.abc import Iterable

# Column allowlist for query_stats' group_by/filter keys.
ALLOWED_COLUMNS = {"method", "status", "gateway", "merchant"}

# Enum allowlists for filter/argument values.
ALLOWED_STATUS = {"success", "failure"}
ALLOWED_METHOD = {"card", "ach", "wallet"}
ALLOWED_METRIC = {"count", "failure_rate"}


def require_positive_int(name: str, value: object) -> int:
    """Reject anything that is not a positive int.

    Bools are rejected explicitly even though `bool` is a subclass of `int`
    in Python — `True`/`False` are never a sensible window/limit/k value.
    Floats (even whole ones like 3.0) are rejected too: the caller should
    pass an int, not something that merely looks numeric.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")
    return value


def clamp_positive_int(
    name: str, value: int | None, default: int, max_value: int
) -> tuple[int, str | None]:
    """Resolve an optional positive-int argument to (effective_value, note).

    - `value` is None -> (default, None); the default itself is trusted and
      not re-validated.
    - `value` fails require_positive_int (non-integer, non-positive, or a
      bool) -> propagates the ValueError (reject, never clamp).
    - `value` > max_value -> (max_value, "<name> capped at <max_value>
      (requested <value>)").
    - otherwise -> (value, None), unchanged and no note.
    """
    if value is None:
        return default, None
    value = require_positive_int(name, value)
    if value > max_value:
        return max_value, f"{name} capped at {max_value} (requested {value})"
    return value, None


def check_enum(name: str, value: object, allowed: set[str]) -> None:
    """Raise ValueError if `value` is not a member of `allowed`.

    Message names the field, the invalid value, and the full sorted list of
    valid options, so a retrying caller knows exactly what to pass instead.
    """
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")


def check_allowed_keys(kind: str, keys: Iterable[str], allowed: set[str]) -> None:
    """Raise ValueError if any of `keys` is not in `allowed`.

    Used for both a single key (e.g. group_by, passed as a one-item iterable)
    and a set of keys (e.g. filters.keys()). Message names the bad key(s) and
    lists valid options, same shape as check_enum.
    """
    bad = sorted(set(keys) - set(allowed))
    if bad:
        raise ValueError(f"{kind} keys must be among {sorted(allowed)}, got {bad}")


def find_invalid_uuids(ids: Iterable[str]) -> list[str]:
    """Return the subset of `ids` that are not valid UUID strings.

    Uses uuid.UUID(x) parsing (not a regex) so any RFC 4122 representation is
    accepted. Does not raise itself — callers decide reject vs. partial-accept
    per tool.
    """
    invalid = []
    for value in ids:
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError):
            invalid.append(value)
    return invalid


def check_query_text(query: str, max_len: int) -> tuple[str, str | None]:
    """Validate free-text query input.

    Empty or whitespace-only raises ValueError (no sensible auto-fix — there
    is nothing to search for). Longer than `max_len` is truncated to
    `max_len` characters and a note describing the truncation is returned
    alongside the truncated string; the caller attaches the note to its own
    `notes` list.
    """
    if not query or not query.strip():
        raise ValueError("query must not be empty or whitespace-only")
    if len(query) > max_len:
        note = f"query truncated to {max_len} characters (was {len(query)})"
        return query[:max_len], note
    return query, None
