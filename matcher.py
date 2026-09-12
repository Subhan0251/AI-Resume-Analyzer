"""
matcher.py
==========
Compares a parsed resume against a job description and produces a match
score, sub-scores with explanations, and grounded gap-closing suggestions.

Latency note: this module makes at most 2 reasoning-model calls in the
common case (1 to parse the JD, 1 to generate all gap suggestions in a
single batched call - see _generate_gap_suggestions), plus 2 embedding calls
(fast, non-reasoning). It used to make up to 6 sequential reasoning calls
(1 JD parse + up to 5 individual gap-suggestion calls) - that loop was the
single biggest latency cost in the whole pipeline.
"""
import re
from typing import Dict, List, Any, Tuple

import numpy as np
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable
from rapidfuzz import fuzz

from config import llm_client, embedding_client


class JDRequirements(BaseModel):
    required_skills: List[str] = Field(default_factory=list, description="Must-have explicit technical/functional skills")
    nice_to_have_skills: List[str] = Field(default_factory=list, description="Preferred/optional skills")
    target_seniority: str = Field("mid-level", description="Required seniority: entry-level, mid-level, senior, or lead")
    min_years_experience: float = Field(0.0, description="Minimum years of experience requested in JD")
    core_responsibilities: List[str] = Field(default_factory=list, description="Key job responsibilities")


class GapSuggestion(BaseModel):
    missing_skill: str
    importance: str
    suggestion: str


class GapSuggestionList(BaseModel):
    """Wrapper so the LLM can return suggestions for every missing skill in one structured-output call, instead of one call per skill."""
    suggestions: List[GapSuggestion] = Field(default_factory=list)


# ==========================================
# Grounding check helpers (point 4)
# ==========================================
# Heuristic, zero-latency first pass: pull out proper-noun-like phrases
# (potential company names, tools, certifications) from a generated
# suggestion and fuzzy-match each one against the actual resume text. This
# catches the common failure mode (the model inventing a specific employer,
# tool, or credential the candidate never had) without an extra network
# call for the vast majority of suggestions, which will already be clean.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z0-9&+.]*(?:\s+[A-Z][a-zA-Z0-9&+.]*){0,3}\b")
_SENTENCE_STARTERS = {
    "The", "This", "Consider", "You", "Candidate", "Given", "Based",
    "While", "Although", "In", "For", "To", "Highlight", "Emphasize",
}


def _extract_candidate_entities(text: str) -> List[str]:
    """Pulls capitalized phrases (2+ chars, not a common sentence-starter word) out of a suggestion as candidates to verify against the resume."""
    matches = _PROPER_NOUN_RE.findall(text)
    return [m for m in matches if m not in _SENTENCE_STARTERS and len(m) > 2]


def _is_grounded_fuzzy(entity: str, source_text: str, threshold: int = 85) -> bool:
    """Fuzzy (not exact) substring check so minor spacing/case differences don't cause false positives on real, grounded mentions."""
    if not source_text:
        return False
    return fuzz.partial_ratio(entity.lower(), source_text.lower()) >= threshold


class ResumeJDMatcher:
    """Runs the resume-vs-JD comparison pipeline for a single (resume, job description) pair."""

    def __init__(self, parsed_resume: Dict[str, Any], job_description: str):
        self.resume = parsed_resume.get("parsed_fields", {})
        self.raw_resume_text = parsed_resume.get("raw_normalized_text", "")
        self.jd = job_description

        # Reuses the shared clients from config.py instead of building new
        # ChatOpenAI/OpenAIEmbeddings instances per request (review point 7).
        self.embeddings = embedding_client
        self.llm = llm_client

    def analyze(self) -> Dict[str, Any]:
        """Entry point: parses the JD, computes each sub-score, combines them into an overall weighted score, and generates grounded gap suggestions."""
        jd_schema = self._parse_jd()

        kw_score, kw_reason, missing_reqs, missing_nice = self._calc_keyword_overlap(jd_schema)
        sem_score, sem_reason = self._calc_semantic_similarity(jd_schema)
        sen_score, sen_reason = self._calc_seniority_match(jd_schema)

        overall_score = int(round(0.40 * kw_score + 0.40 * sem_score + 0.20 * sen_score))

        gaps = self._generate_gap_suggestions(missing_reqs, missing_nice)

        return {
            "match_score": overall_score,
            "sub_scores": {
                "keyword_overlap": kw_score,
                "semantic_similarity": sem_score,
                "seniority_match": sen_score
            },
            "explanations": {
                "keyword_overlap": kw_reason,
                "semantic_similarity": sem_reason,
                "seniority_match": sen_reason
            },
            "parsed_resume": {
                "skills": self.resume.get("skills", []),
                "years_experience": self.resume.get("total_years_experience", 0.0),
                "seniority_detected": self.resume.get("seniority_level", "junior")
            },
            "gaps": gaps
        }

    @traceable(name="Parsing raw JD into structured JSON.")
    def _parse_jd(self) -> JDRequirements:
        """One LLM call: turns the free-text job description into structured required/nice-to-have skills, target seniority, min years, and responsibilities."""
        structured_llm = self.llm.with_structured_output(JDRequirements)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Extract structured requirements from this Job Description. Categorize skills into required vs nice-to-have."),
            ("human", "Job Description:\n\n{jd}")
        ])
        chain = prompt | structured_llm
        return chain.invoke({"jd": self.jd})

    @traceable(name="Keyword overlapping calculation")
    def _calc_keyword_overlap(self, jd: JDRequirements) -> Tuple[int, str, List[str], List[str]]:
        """Deterministic (no LLM): substring-overlap match of JD required/nice-to-have skills against the resume's skill list, weighted 75/25."""
        resume_skills = [s.lower().strip() for s in self.resume.get("skills", [])]

        missing_required = []
        found_required = 0
        for s in jd.required_skills:
            if any(s.lower() in r or r in s.lower() for r in resume_skills):
                found_required += 1
            else:
                missing_required.append(s)

        missing_nice = []
        found_nice = 0
        for s in jd.nice_to_have_skills:
            if any(s.lower() in r or r in s.lower() for r in resume_skills):
                found_nice += 1
            else:
                missing_nice.append(s)

        req_score = (found_required / len(jd.required_skills)) * 100 if jd.required_skills else 100
        nice_score = (found_nice / len(jd.nice_to_have_skills)) * 100 if jd.nice_to_have_skills else 100

        final_kw_score = int(round(0.75 * req_score + 0.25 * nice_score))
        reason = f"Candidate matched {found_required}/{len(jd.required_skills)} required skills and {found_nice}/{len(jd.nice_to_have_skills)} preferred skills."

        return final_kw_score, reason, missing_required, missing_nice

    @traceable(name="Semantic similarity computation")
    def _calc_semantic_similarity(self, jd: JDRequirements) -> Tuple[int, str]:
        """Deterministic (embeddings, not a reasoning call): cosine-similarity between resume experience bullets and JD core responsibilities, averaged over each responsibility's best-matching bullet."""
        exp_bullets = []
        for exp in self.resume.get("experience", []):
            exp_bullets.extend(exp.get("bullets", []))

        if not exp_bullets or not jd.core_responsibilities:
            return 50, "Insufficient experience bullet points or JD responsibilities to perform semantic comparison."

        bullet_embeds = np.array(self.embeddings.embed_documents(exp_bullets))
        resp_embeds = np.array(self.embeddings.embed_documents(jd.core_responsibilities))

        norm_b = bullet_embeds / np.linalg.norm(bullet_embeds, axis=1, keepdims=True)
        norm_r = resp_embeds / np.linalg.norm(resp_embeds, axis=1, keepdims=True)
        sim_matrix = np.dot(norm_b, norm_r.T)

        max_sims = np.max(sim_matrix, axis=0)
        avg_sim = float(np.mean(max_sims))

        sem_score = int(round(max(0.0, min(1.0, avg_sim)) * 100))
        reason = f"Candidate experience bullets show a {sem_score}% semantic alignment with core job responsibilities."

        return sem_score, reason

    @traceable(name="Calculate seniority match")
    def _calc_seniority_match(self, jd: JDRequirements) -> Tuple[int, str]:
        """Deterministic (no LLM): compares the resume's code-computed total_years_experience against the JD's min_years_experience on a simple tiered scale."""
        candidate_years = float(self.resume.get("total_years_experience", 0.0))
        target_years = float(jd.min_years_experience)

        if candidate_years >= target_years:
            score = 100
            reason = f"Candidate exceeds experience requirement ({candidate_years} yrs vs {target_years} yrs required)."
        elif candidate_years >= target_years * 0.75:
            score = 80
            reason = f"Candidate closely approaches experience requirement ({candidate_years} yrs vs {target_years} yrs required)."
        else:
            diff = max(1.0, target_years - candidate_years)
            score = max(20, int(round(100 - (diff * 20))))
            reason = f"Candidate falls short of required experience ({candidate_years} yrs vs {target_years} yrs required)."

        return score, reason

    @traceable(name="Gap suggestion generator")
    def _generate_gap_suggestions(self, missing_reqs: List[str], missing_nice: List[str]) -> List[Dict[str, Any]]:
        """
        Generates a grounded suggestion for each of the top 5 missing skills
        in a SINGLE structured-output call (was previously one call per
        skill, up to 5 sequential network round-trips - the biggest latency
        cost in the old pipeline). Each returned suggestion then goes
        through the two-tier grounding check below.
        """
        all_gaps = [(s, "required") for s in missing_reqs] + [(s, "nice to have") for s in missing_nice]
        top_gaps = all_gaps[:5]
        if not top_gaps:
            return []

        gap_list_str = "\n".join(f"- {skill} ({importance})" for skill, importance in top_gaps)

        structured_llm = self.llm.with_structured_output(GapSuggestionList)
        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "For each missing skill listed below, write one grounded suggestion "
                "for how the candidate could reframe or highlight existing experience "
                "to partially address the gap. Never invent employers, tools, "
                "certifications, or skills that aren't supported by the resume text. "
                "Return exactly one suggestion per listed skill, in the same order.",
            ),
            ("human", "Candidate Resume:\n{raw_resume}\n\nMissing Skills:\n{gaps}"),
        ])
        chain = prompt | structured_llm
        result: GapSuggestionList = chain.invoke({
            "raw_resume": self.raw_resume_text,
            "gaps": gap_list_str,
        })

        return self._validate_grounding_batch(result.suggestions)

    @traceable(name="Validation of grounding")
    def _validate_grounding_batch(self, suggestions: List[GapSuggestion]) -> List[Dict[str, Any]]:
        """
        Two-tier grounding check (point 4):
          1. Fast path, no extra latency: fuzzy-match proper-noun-like
             phrases in each suggestion against the resume text (see
             _is_grounded_fuzzy). Suggestions that pass are trusted as-is.
          2. Slow path, only if the fast path flags something: ONE extra
             batched LLM call asking it to rewrite just the flagged
             suggestions so they only reference what's actually in the
             resume. This keeps the common case (nothing flagged) at zero
             extra latency, and even the escalation path is a single call
             regardless of how many suggestions were flagged.
        """
        clean: List[Dict[str, Any]] = []
        flagged_indices: List[int] = []

        for i, s in enumerate(suggestions):
            text = s.suggestion or "Consider highlighting transferable experience related to this skill."
            entities = _extract_candidate_entities(text)
            unverified = [e for e in entities if not _is_grounded_fuzzy(e, self.raw_resume_text)]
            clean.append({"missing_skill": s.missing_skill, "importance": s.importance, "suggestion": text})
            if unverified:
                flagged_indices.append(i)

        if not flagged_indices:
            return clean

        # Escalate only the flagged suggestions, in one batched call.
        flagged_str = "\n".join(
            f"{i}. Missing skill: {clean[i]['missing_skill']}\n   Suggestion: {clean[i]['suggestion']}"
            for i in flagged_indices
        )
        rewrite_prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "The suggestions below may reference an employer, tool, or "
                "credential that does not actually appear in the candidate's "
                "resume. Rewrite each one so it strictly only references what "
                "is present in the resume text, keeping the same numbering.",
            ),
            ("human", "Resume:\n{raw_resume}\n\nSuggestions to check/rewrite:\n{flagged}"),
        ])
        structured_llm = self.llm.with_structured_output(GapSuggestionList)
        chain = rewrite_prompt | structured_llm
        rewritten = chain.invoke({"raw_resume": self.raw_resume_text, "flagged": flagged_str})

        for idx, new_s in zip(flagged_indices, rewritten.suggestions):
            clean[idx]["suggestion"] = new_s.suggestion or clean[idx]["suggestion"]

        return clean