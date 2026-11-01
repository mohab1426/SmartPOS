"""GUI money entry -> integer piasters (single rounding boundary). Previously untested (0% coverage)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from services.money import to_piasters


@pytest.mark.parametrize("raw,expected", [
    ("10", 1000), (10, 1000), (10.5, 1050), ("0.01", 1), ("0", 0),
    (1.005, 101),                       # typed value 1.005 rounds half-up, not the binary 1.00499999...
    ("2.675", 268), ("19.99", 1999), (Decimal("3.333"), 333), (" 12.30 ", 1230),
    (0.1 + 0.2, 30),                    # float noise (0.30000000000000004) must not leak into money
    ("10000000.00", 1_000_000_000),
])
def test_to_piasters_values(raw, expected):
    assert to_piasters(raw) == expected


@pytest.mark.parametrize("bad", ["abc", "", "1,5", "NaN", "Infinity", None, "12.3.4"])
def test_to_piasters_rejects_garbage(bad):
    with pytest.raises(ValueError, match="Invalid money amount"):
        to_piasters(bad)
