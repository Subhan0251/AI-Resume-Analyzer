import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from matcher import ResumeJDMatcher, JDRequirements
from parser import compute_experience_stats, _parse_date_range


class ScoringTests(unittest.TestCase):
    def matcher(self, **fields):
        return ResumeJDMatcher({"parsed_fields": fields}, "test JD")

    def test_date_formats(self):
        for dates in ("2020-01 - 2022-12", "2020-01-01 to 2022-12-31",
                      "Jan 2020 - Dec 2022", "2020–2022", "2020-2022",
                      "01/2020 - 12/2022"):
            with self.subTest(dates=dates):
                self.assertEqual(compute_experience_stats([{"dates": dates}]), (3.0, "mid-level"))

    def test_future_and_invalid_ranges(self):
        for dates in ("Jan 2990 - Dec 2995", "unknown", "2022 - 2020", "Jan - Dec"):
            self.assertEqual(compute_experience_stats([{"dates": dates}])[0], 0)
        _, end = _parse_date_range("2020 - 2999")
        self.assertLessEqual(end, datetime.now(timezone.utc).replace(tzinfo=None))

    def test_overlap_not_double_counted(self):
        self.assertEqual(compute_experience_stats([
            {"dates": "2020 - 2022"}, {"dates": "2021 - 2022"}])[0], 3.0)

