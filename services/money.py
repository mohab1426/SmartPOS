"""Monetary conversion helpers.

Rounding policy (single, project-wide)
--------------------------------------
All money is stored and computed as **integer piasters** (1 EGP = 100 piasters),
which is exact: there is no floating-point drift in sums, discounts or ledgers.
Wherever a division is unavoidable (discount allocation, partial-return refunds,
weighted-average cost) the result is rounded **half-up to the nearest piaster**
at that single boundary, and remainders are distributed so line values always
sum exactly to the document total (largest-remainder method).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def to_piasters(amount) -> int:
    """Convert a user-entered pound amount (str/int/float/Decimal) to integer piasters.

    Floats are converted through ``str`` so 1.005 is treated as the typed
    value 1.005 (-> 101 piasters half-up), not the binary approximation.
    """
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid money amount: {amount!r}") from exc
    if not value.is_finite():
        raise ValueError(f"Invalid money amount: {amount!r}")
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
