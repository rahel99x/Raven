"""An empty cohort must not silently accept invalid scientific preferences."""
import pytest

from raven.planning import analyze


@pytest.mark.parametrize("coverage", [True, False, None, "0.8", 0, 1, -1, float("inf"), float("nan"), 1 << 5000])
def test_empty_forecast_rejects_invalid_coverage(coverage):
    with pytest.raises(ValueError, match="coverage"):
        analyze("forecast", {"jobs": [], "coverage": coverage})
