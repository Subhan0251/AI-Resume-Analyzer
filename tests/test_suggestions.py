import unittest
from scorecard import gap_suggestions, RUBRIC


class SuggestionTests(unittest.TestCase):
    def test_all_gaps_are_returned_without_inventing_skills(self):
        rows = [{"id": str(i), "requirement": f"Exact JD requirement {i}", "importance": "required" if i % 2 else "preferred",
                 "status": "not_evidenced", "cv_evidence": []} for i in range(17)]
        gaps = gap_suggestions({"criteria": rows})
        self.assertEqual(len(gaps), 17)
        self.assertEqual({gap["missing_skill"] for gap in gaps}, {row["requirement"] for row in rows})
        self.assertEqual(gaps[0]["importance"], "required")
        self.assertTrue(all("If you meet this requirement" in gap["suggestion"] for gap in gaps))

    def test_supported_criteria_do_not_create_gaps(self):
        self.assertEqual(gap_suggestions({"criteria": [{"status": "supported", "importance": "required"}]}), [])
