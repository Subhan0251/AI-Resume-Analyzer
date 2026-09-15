"""Profession-independent JD criterion coverage with inspectable source evidence."""
from jd import JDRequirements, validate_job_description
from scorecard import assess_criteria, gap_suggestions
from skills import source_backed_skills


class ResumeJDMatcher:
    def __init__(self, parsed_resume, job_description, *, validated_requirements=None):
        self.resume = dict(parsed_resume.get("parsed_fields", {}))
        self.raw_resume_text = parsed_resume.get("raw_normalized_text", "")
        self.resume["skills"] = source_backed_skills(self.resume.get("skills", []), self.raw_resume_text)
        self.jd = job_description
        self.validated_requirements = validated_requirements
        self.llm = None

    def _parse_jd(self):
        return validate_job_description(self.jd, llm=self.llm)

    def analyze(self):
        requirements = self.validated_requirements
        if requirements is None:
            requirements = self._parse_jd()
            self.validated_requirements = requirements
        if self.llm is None:
            from config import get_llm_client
            self.llm = get_llm_client()
        scorecard = assess_criteria(requirements, self.jd, self.raw_resume_text, self.llm)
        return {
            "match_score": scorecard["required"]["coverage"],
            "score_type": "required_criteria_coverage",
            "scorecard": scorecard,
            "jd_requirements": {"criteria": [item.model_dump() for item in requirements.criteria]},
            "parsed_resume": {
                "skills": self.resume.get("skills", []),
                "years_experience": self.resume.get("total_years_experience", 0),
                "seniority_detected": self.resume.get("seniority_level", "unknown"),
            },
            "gaps": gap_suggestions(scorecard),
        }
