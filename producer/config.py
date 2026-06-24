"""Value pools and distribution constants for the baseline event generator.

These are separated from the generation logic so Steps 5 and 6 can import
and reuse them without touching the producer loop.
"""

MERCHANTS = [
    "24 Hour Fitness",
    "Anthropic",
    "Fortnite",
    "Cornell University",
    "Foodland Hawaii",
    "Walmart",
    "Sam's Club",
    "Valorant",
    "Costco",
    "LeetCode Premium",
    "LinkedIn Premium",
    "Spotify Premium",
    "Safeway",
    "7-Eleven",
    "United Airlines",
    "Oishii Bowl",
    "2Hollis LLC",
    "Andrea & Co.",
]

GATEWAYS = [
    "stripe-proxy",
    "adyen-gw",
    "braintree-edge",
    "checkout-io",
]

# Realistic 6-digit Visa (4xxxxx) and Mastercard (5xxxxx) BINs.
# Small pool so each BIN has meaningful baseline volume — a fraud burst
# on one BIN will be statistically obvious against ~12% expected share.
CARD_BINS = [
    "411111",
    "424242",
    "453201",
    "471500",
    "510510",
    "522233",
    "540012",
    "556677",
]

# (choice, weight) — card dominates real-world payment mix.
METHOD_WEIGHTS = {
    "card": 0.70,
    "ach": 0.20,
    "wallet": 0.10,
}

# Amount ranges per method: (min, max).
# The generator uses log-uniform sampling within these to produce a
# realistic right-skewed distribution (many small, few large).
AMOUNT_RANGES = {
    "wallet": (1.00, 100.00),
    "card": (5.00, 500.00),
    "ach": (100.00, 10_000.00),
}

FAILURE_RATE = 0.04  # ~4% baseline failure rate

KAFKA_BOOTSTRAP = "localhost:29092"
KAFKA_TOPIC = "transactions"
