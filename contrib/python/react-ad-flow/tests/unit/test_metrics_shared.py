"""Unit tests for metrics_shared.py — the single home of RPI math.

These are the phase's core validation: known pairs, the zero case, and the
equal-vs-unequal daily-values cases that distinguish ratio-of-sums from
sum-of-ratios (ratio-of-sums rule).
"""

from app.tools.metrics_shared import compute_rpi, compute_weighted_average


class TestComputeRpi:
    def test_known_value_pair(self):
        assert compute_rpi(50.0, 1000) == 0.05

    def test_rounding_to_four_places(self):
        assert compute_rpi(1.0, 3) == 0.3333

    def test_zero_impressions_returns_zero(self):
        assert compute_rpi(100.0, 0) == 0.0

    def test_none_and_negative_impressions_return_zero(self):
        assert compute_rpi(100.0, None) == 0.0
        assert compute_rpi(100.0, -5) == 0.0

    def test_none_revenue_treated_as_zero(self):
        assert compute_rpi(None, 1000) == 0.0

    def test_equal_daily_values_coincide_with_mean_of_ratios(self):
        # Two identical days: ratio-of-sums == mean of daily ratios.
        # This agreement is coincidental and NOT probative of correctness —
        # kept to document the trap (see the unequal case below).
        days = [(10.0, 100), (10.0, 100)]
        ratio_of_sums = compute_rpi(
            sum(r for r, _ in days), sum(i for _, i in days)
        )
        mean_of_ratios = round(
            sum(compute_rpi(r, i) for r, i in days) / len(days), 4
        )
        assert ratio_of_sums == mean_of_ratios == 0.1

    def test_unequal_daily_values_must_differ_from_sum_of_ratios(self):
        # THE regression-catcher: with unequal days, sum (and mean) of daily
        # ratios diverge from ratio-of-sums. Weekly RPI must be the latter.
        days = [(10.0, 1000), (50.0, 500)]  # daily RPIs 0.01 and 0.1
        ratio_of_sums = compute_rpi(60.0, 1500)
        assert ratio_of_sums == 0.04
        sum_of_ratios = sum(compute_rpi(r, i) for r, i in days)  # 0.11
        mean_of_ratios = sum_of_ratios / len(days)  # 0.055
        assert ratio_of_sums != round(sum_of_ratios, 4)
        assert ratio_of_sums != round(mean_of_ratios, 4)


class TestComputeWeightedAverage:
    def test_unequal_weights_differ_from_naive_mean(self):
        rows = [
            {"dwell_time": 10.0, "impressions": 900},
            {"dwell_time": 2.0, "impressions": 100},
        ]
        weighted = compute_weighted_average(rows, "dwell_time", "impressions")
        assert weighted == 9.2  # (10*900 + 2*100) / 1000
        naive_mean = (10.0 + 2.0) / 2  # 6.0
        assert weighted != naive_mean

    def test_equal_weights_match_naive_mean(self):
        rows = [
            {"dwell_time": 4.0, "impressions": 500},
            {"dwell_time": 6.0, "impressions": 500},
        ]
        assert (
            compute_weighted_average(rows, "dwell_time", "impressions") == 5.0
        )

    def test_zero_total_weight_returns_zero(self):
        rows = [{"dwell_time": 4.0, "impressions": 0}]
        assert (
            compute_weighted_average(rows, "dwell_time", "impressions") == 0.0
        )

    def test_missing_fields_treated_as_zero(self):
        rows = [{"dwell_time": 4.0, "impressions": 100}, {"impressions": 100}]
        assert (
            compute_weighted_average(rows, "dwell_time", "impressions") == 2.0
        )
