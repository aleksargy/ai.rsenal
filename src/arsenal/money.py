"""Money arithmetic for FPL.

Every price in this codebase is an ``int`` in **tenths of a million**:
``now_cost = 75`` means £7.5m. Nothing here returns a float, and no other module
should introduce one.

This is not fussiness. FPL budgets bind exactly — a squad may cost precisely
£100.0m — so a float representation eventually yields a bank of ``-1e-9``, an
infeasible optimiser model, and an aborted run at the deadline. Integers make
that class of bug impossible rather than unlikely.
"""

from __future__ import annotations

# FPL stores prices in tenths; `bootstrap-static.game_settings.ui_currency_multiplier`
# is 10 and is the authority for this.
TENTHS_PER_MILLION = 10


def selling_price(purchase_price: int, now_cost: int) -> int:
    """Return what a held player actually sells for, in tenths.

    FPL applies a 50% sell-on fee to any *profit*, rounded down to £0.1m
    (``transfers_sell_on_fee = 0.5``, ``element_sell_at_purchase_price = false``).
    Losses are taken in full.

    Integer floor division implements the "rounded down" rule exactly::

        >>> selling_price(70, 75)   # rose 0.5, keep 0.2
        72
        >>> selling_price(40, 43)   # rose 0.3, keep 0.1
        41
        >>> selling_price(90, 86)   # fell, full loss
        86
        >>> selling_price(70, 70)
        70

    The consequence for budgeting: **squad value is not selling value.** The
    optimiser must budget against selling prices, which requires each player's
    original purchase price — available only from the authenticated
    ``/api/my-team/{id}/`` endpoint.
    """
    if now_cost <= purchase_price:
        return now_cost
    return purchase_price + (now_cost - purchase_price) // 2


def format_money(tenths: int) -> str:
    """Render tenths as a human-readable price: ``75`` -> ``'£7.5m'``."""
    sign = "-" if tenths < 0 else ""
    whole, frac = divmod(abs(tenths), TENTHS_PER_MILLION)
    return f"{sign}£{whole}.{frac}m"


def parse_money(text: str) -> int:
    """Parse ``'7.5'`` or ``'£7.5m'`` into tenths. Inverse of :func:`format_money`."""
    cleaned = text.strip().lstrip("£").rstrip("m").strip()
    return round(float(cleaned) * TENTHS_PER_MILLION)
