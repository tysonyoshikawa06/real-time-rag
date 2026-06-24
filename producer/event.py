"""Single-event generation logic for the baseline producer.

This module owns the shape of one transaction event. Steps 5 and 6 will
layer on error text and distribution biases by wrapping or post-processing
generate_event() — the function signature stays stable.
"""

import math
import random
import uuid
from datetime import datetime, timezone

from producer.config import (
    AMOUNT_RANGES,
    CARD_BINS,
    FAILURE_RATE,
    GATEWAYS,
    MERCHANTS,
    METHOD_WEIGHTS,
)
from producer.errors import generate_error_text

_METHODS = list(METHOD_WEIGHTS.keys())
_METHOD_CUM_WEIGHTS = list(METHOD_WEIGHTS.values())


def generate_event() -> dict:
    """Generate one realistic baseline transaction event.

    Returns a dict matching the transactions table schema (minus ingested_at).
    """
    method = random.choices(_METHODS, weights=_METHOD_CUM_WEIGHTS, k=1)[0]

    lo, hi = AMOUNT_RANGES[method]
    amount = _log_uniform(lo, hi)

    status = "failure" if random.random() < FAILURE_RATE else "success"

    card_bin = random.choice(CARD_BINS) if method == "card" else None
    gateway = random.choice(GATEWAYS)
    error_text = generate_error_text(method, gateway) if status == "failure" else None

    return {
        "transaction_id": str(uuid.uuid4()),
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "merchant": random.choice(MERCHANTS),
        "method": method,
        "amount": round(amount, 2),
        "status": status,
        "gateway": gateway,
        "error_text": error_text,
        "card_bin": card_bin,
    }


def _log_uniform(lo: float, hi: float) -> float:
    """Sample from a log-uniform distribution between lo and hi.

    Log-uniform produces a right-skewed distribution: most values cluster
    near the low end with a long tail toward the high end. This matches
    real payment amounts — many $10-$30 charges, few $400+ charges.
    """
    return math.exp(random.uniform(math.log(lo), math.log(hi)))
