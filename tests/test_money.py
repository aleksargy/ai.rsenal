"""Sell-on fee arithmetic.

Getting this wrong inflates the apparent budget, which produces a plan the FPL
server rejects at the deadline — the worst possible moment to discover it.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from arsenal.money import format_money, parse_money, selling_price


class TestSellingPrice:
    @pytest.mark.parametrize(
        ("purchase", "now", "expected", "why"),
        [
            (70, 75, 72, "rose 0.5, keep half rounded down -> 0.2"),
            (40, 43, 41, "rose 0.3, keep 0.1"),
            (40, 42, 41, "rose 0.2, keep 0.1"),
            (40, 41, 40, "rose 0.1, keep nothing — half of 0.1 rounds down to 0"),
            (100, 110, 105, "rose 1.0, keep 0.5"),
            (100, 111, 105, "rose 1.1, keep 0.5 — the odd tenth is lost"),
            (70, 70, 70, "unchanged"),
            (90, 86, 86, "fell — losses are taken in full"),
            (50, 40, 40, "large fall, full loss"),
        ],
    )
    def test_known_values(self, purchase: int, now: int, expected: int, why: str) -> None:
        assert selling_price(purchase, now) == expected, why

    def test_never_exceeds_current_price(self) -> None:
        """You can never sell for more than the player is currently worth."""
        for purchase in range(38, 150):
            for now in range(38, 150):
                assert selling_price(purchase, now) <= now

    def test_never_below_purchase_when_price_rose(self) -> None:
        """A rise must never leave you worse off than you bought at."""
        for purchase in range(38, 150):
            for now in range(purchase, 150):
                assert selling_price(purchase, now) >= purchase

    def test_monotonic_in_current_price(self) -> None:
        """A higher current price never reduces what you can sell for."""
        purchase = 70
        values = [selling_price(purchase, now) for now in range(60, 120)]
        assert all(b >= a for a, b in pairwise(values))

    def test_returns_int_always(self) -> None:
        """No float may ever escape this function — see the module docstring."""
        assert isinstance(selling_price(70, 75), int)
        assert isinstance(selling_price(70, 65), int)


class TestFormatting:
    @pytest.mark.parametrize(
        ("tenths", "text"),
        [(75, "£7.5m"), (100, "£10.0m"), (1000, "£100.0m"), (38, "£3.8m"), (0, "£0.0m")],
    )
    def test_format(self, tenths: int, text: str) -> None:
        assert format_money(tenths) == text

    def test_format_negative(self) -> None:
        assert format_money(-5) == "-£0.5m"

    @pytest.mark.parametrize("text", ["7.5", "£7.5m", " £7.5m ", "7.5m"])
    def test_parse_accepts_common_forms(self, text: str) -> None:
        assert parse_money(text) == 75

    def test_round_trips(self) -> None:
        for tenths in range(0, 1001, 7):
            assert parse_money(format_money(tenths)) == tenths
