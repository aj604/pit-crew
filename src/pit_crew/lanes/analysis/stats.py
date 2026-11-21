"""Summary statistics shared by the analysis modules."""

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile: always a value that was actually observed. 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(math.ceil(pct / 100 * len(ordered)) - 1, 0)]
