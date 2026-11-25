"""Regression checks without a Home Assistant installation."""
import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "custom_components/enea_rcem/tariffs.py"
spec = importlib.util.spec_from_file_location("enea_tariffs_under_test", path)
tariffs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tariffs
spec.loader.exec_module(tariffs)


class SubscriptionTariffTest(unittest.TestCase):
    def test_boundaries(self):
        for day, expected in (
            (date(2026, 9, 30), 0.32),
            (date(2026, 10, 1), 0.0),
            (date(2026, 12, 1), 0.0),
            (date(2026, 12, 2), 7.94),
        ):
            with self.subTest(day=day):
                self.assertEqual(tariffs.subscription_fee_for_date(day, 0.32), expected)
                self.assertEqual(tariffs.tariff_for_date(day).subscription_fee_net, expected)

    def test_no_other_rate_change(self):
        before = tariffs.tariff_for_date(date(2026, 9, 30))
        after = tariffs.tariff_for_date(date(2026, 12, 2))
        self.assertEqual(before.variable_rate_gross, after.variable_rate_gross)
        self.assertAlmostEqual(after.fixed_monthly_net - before.fixed_monthly_net, 7.62)
        self.assertEqual(round(after.subscription_fee_net * 1.23, 2), 9.77)
        self.assertEqual(tariffs.prosumer_factor_for_date(date(2026, 12, 2)), 1.23)

    def test_legacy_override(self):
        self.assertEqual(tariffs.subscription_fee_for_date(date(2026, 9, 30), 1.92), 1.92)
        self.assertEqual(tariffs.subscription_fee_for_date(date(2026, 12, 2), 1.92), 7.94)


if __name__ == "__main__":
    unittest.main()
