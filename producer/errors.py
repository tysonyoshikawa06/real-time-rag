"""Error families for the failure-text layer.

Each family represents one logical failure reason with multiple textual
variants — inconsistent casing, codes vs prose, abbreviations. This
inconsistency is what makes semantic search valuable: the same meaning
surfaces in different words.

The set of families is closed and enumerable. Step 6 defines a "novel error"
as any string outside ALL_FAMILIES.
"""

import random

# ---------------------------------------------------------------------------
# Error families: each is a list of template strings.
#
# Templates may contain {gateway} — filled from the event's gateway field.
# Families without {gateway} ignore it.
# ---------------------------------------------------------------------------

INSUFFICIENT_FUNDS = [
    "insufficient_funds",
    "NSF",
    "ERR_51 insufficient funds",
    "Declined - insufficient funds available",
    "51: INSUFFICIENT FUNDS",
]

DO_NOT_HONOR = [
    "do_not_honor",
    "ERR_05 DO NOT HONOR",
    "05: Do Not Honor",
    "Declined - do not honor",
]

EXPIRED_CARD = [
    "card_expired",
    "ERR_54 expired card",
    "Declined: card expired",
    "54: CARD EXPIRED",
]

INVALID_CVV = [
    "cvv_mismatch",
    "ERR_82 invalid CVC",
    "security code incorrect",
    "CVV2 verification failed",
]

GATEWAY_TIMEOUT = [
    "Gateway timeout after 30000ms upstream={gateway}-7",
    "504 upstream timeout ({gateway})",
    "{gateway}: connection timed out",
    "ETIMEDOUT connecting to {gateway}.internal:443",
    "upstream {gateway} did not respond within 30s",
]

NETWORK_ERROR = [
    "ECONNRESET",
    "upstream connect error, reset before headers. {gateway}:443",
    "network unreachable ({gateway})",
    "TLS handshake failed: {gateway}.internal:443",
]

FRAUD_SUSPECTED = [
    "fraud_suspected",
    "ERR_59 suspected fraud",
    "blocked by risk engine",
    "transaction flagged: high risk score",
]

# Master list — Step 6 imports this to define "novel" as outside all families.
ALL_FAMILIES = [
    INSUFFICIENT_FUNDS,
    DO_NOT_HONOR,
    EXPIRED_CARD,
    INVALID_CVV,
    GATEWAY_TIMEOUT,
    NETWORK_ERROR,
    FRAUD_SUSPECTED,
]

# Card-only families (expired card, invalid CVV don't apply to ach/wallet).
_CARD_ONLY = {id(EXPIRED_CARD), id(INVALID_CVV)}

# (family, weight) — weights reflect real-world decline frequency.
# Everyday declines dominate; timeouts and fraud are rarer.
_FAMILY_WEIGHTS_ALL = [
    (INSUFFICIENT_FUNDS, 30),
    (DO_NOT_HONOR, 20),
    (GATEWAY_TIMEOUT, 10),
    (NETWORK_ERROR, 8),
    (FRAUD_SUSPECTED, 7),
]

_FAMILY_WEIGHTS_CARD = _FAMILY_WEIGHTS_ALL + [
    (EXPIRED_CARD, 15),
    (INVALID_CVV, 10),
]


def generate_error_text(method: str, gateway: str) -> str:
    """Pick a random error family and variant, filling in gateway if templated."""
    if method == "card":
        families, weights = zip(*_FAMILY_WEIGHTS_CARD)
    else:
        families, weights = zip(*_FAMILY_WEIGHTS_ALL)

    family = random.choices(families, weights=weights, k=1)[0]
    template = random.choice(family)
    return template.format(gateway=gateway)
