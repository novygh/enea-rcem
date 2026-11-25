import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from subscription_migration_plan import classify, month_hours, DEFAULTS


class MigrationPlanTest(unittest.TestCase):
    def test_dst_october(self):
        hour = datetime(2026, 10, 25, tzinfo=timezone.utc)
        self.assertEqual(month_hours(hour), 745)

    def test_classify(self):
        hour = datetime(2026, 11, 10, tzinfo=timezone.utc)
        _, old, new = classify(0, 1, hour, DEFAULTS, 0.0001)
        self.assertEqual(classify(old, 1, hour, DEFAULTS, 0.0001)[0], "legacy")
        self.assertEqual(classify(new, 1, hour, DEFAULTS, 0.0001)[0], "correct")
        self.assertEqual(classify(None, 1, hour, DEFAULTS, 0.0001)[0], "missing")
        self.assertEqual(classify(100, 1, hour, DEFAULTS, 0.0001)[0], "unknown")
        self.assertAlmostEqual((old-new)*720, 0.32*1.23)
