import unittest
from unittest.mock import Mock
from langchain_core.runnables import RunnableLambda
from jd import JobCriterion, JDRequirements
from scorecard import Assessment, AssessmentBatch, Evidence, assess_criteria, summarize
from errors import JDValidationUnavailableError
from matcher import ResumeJDMatcher
from skills import source_backed_skills


class ScorecardTests(unittest.TestCase):
    def test_invented_extracted_skills_are_not_displayed(self):
        self.assertEqual(source_backed_skills(["Python", "Kubernetes", "Java", "JavaScript", "C"], "Python and JavaScript; C++"), ["javascript", "python"])
    def assess(self, text, requirement, assessment, category="skill"):
        requirements = JDRequirements(criteria=[JobCriterion(source_quote=requirement, importance="required", category=category)])
        llm = Mock()
        llm.with_structured_output.return_value = RunnableLambda(lambda _: AssessmentBatch(assessments=assessment))
        return assess_criteria(requirements, requirement, text, llm)

    def test_evidence_across_professions(self):
        examples = [
            ("Administer prescribed medication", "Administered prescribed medication on a hospital ward.", "responsibility", "work"),
            ("Teach primary mathematics", "Taught primary mathematics to a class of 25 pupils.", "responsibility", "work"),
            ("Prepare financial statements", "Prepared quarterly financial statements.", "responsibility", "work"),
            ("Commercial driving license", "Commercial driving license, valid through 2028.", "credential", "credential"),
            ("Build Python APIs", "Built Python APIs for reporting.", "skill", "work"),
        ]
        for req, quote, category, kind in examples:
            with self.subTest(profession=req):
                result = self.assess(quote, req, [Assessment(criterion_id="criterion-1", status="supported", evidence=[Evidence(quote=quote, kind=kind)])], category)
                self.assertEqual(result["required"]["coverage"], 100)
                self.assertEqual(result["criteria"][0]["cv_evidence"][0]["quote"], quote)

    def test_fabricated_quote_cannot_earn_credit(self):
        result = self.assess("Skills: nursing", "Provide patient care", [Assessment(criterion_id="criterion-1", status="supported", evidence=[Evidence(quote="Provided patient care for ten years.", kind="work")])])
        self.assertEqual(result["required"]["coverage"], 0)
        self.assertEqual(result["criteria"][0]["status"], "unclear")
        self.assertEqual(result["criteria"][0]["cv_evidence"], [])

    def test_listing_alone_cannot_earn_supported_credit(self):
        result = self.assess("Skills: Excel", "Prepare Excel reports", [Assessment(criterion_id="criterion-1", status="supported", evidence=[Evidence(quote="Skills: Excel", kind="listing")])])
        self.assertEqual(result["criteria"][0]["status"], "mentioned_only")

    def test_missing_duplicate_and_empty_evidence_are_unclear(self):
        item = Assessment(criterion_id="criterion-1", status="supported", evidence=[])
        for items in ([], [item], [item, item]):
            self.assertEqual(self.assess("CV", "Requirement", items)["criteria"][0]["status"], "unclear")

    def test_invented_unresolved_requirement_is_rejected(self):
        item = Assessment(criterion_id="criterion-1", status="partially_supported", evidence=[Evidence(quote="Worked as a nurse", kind="work")], unresolved_parts=["Python certification"])
        result = self.assess("Worked as a nurse", "Patient care", [item])
        self.assertEqual(result["criteria"][0]["status"], "unclear")
        self.assertEqual(result["criteria"][0]["unresolved_parts"], [])

    def test_no_preferred_compensation_or_partial_credit(self):
        rows = [{"importance": "required", "status": status} for status in ("supported", "partially_supported", "mentioned_only", "not_evidenced")]
        rows.append({"importance": "preferred", "status": "supported"})
        self.assertEqual(summarize(rows, "required")["coverage"], 25)
        self.assertEqual(summarize(rows, "preferred")["coverage"], 100)
        self.assertIsNone(summarize(rows, "unspecified")["coverage"])

    def test_whitespace_quotes_have_original_source_offsets(self):
        source = "Contact\nManaged\n   ward schedules."
        item = Assessment(criterion_id="criterion-1", status="supported", evidence=[Evidence(quote="Managed ward schedules.", kind="work")])
        evidence = self.assess(source, "Manage schedules", [item])["criteria"][0]["cv_evidence"][0]
        self.assertEqual(source[evidence["start"]:evidence["end"]], evidence["quote"])
        self.assertEqual(evidence["line"], 2)

    def test_provider_failure_stops_analysis(self):
        llm = Mock()
        llm.with_structured_output.side_effect = RuntimeError("unavailable")
        req = JDRequirements(criteria=[JobCriterion(source_quote="Teach mathematics", importance="required", category="responsibility")])
        matcher = ResumeJDMatcher({"raw_normalized_text": "Taught mathematics."}, "Teach mathematics", validated_requirements=req)
        matcher.llm = llm
        with self.assertRaises(JDValidationUnavailableError):
            matcher.analyze()
