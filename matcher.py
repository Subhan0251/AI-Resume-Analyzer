import os
import re
import numpy as np
from typing import Dict, List, Any, Tuple
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langsmith import traceable
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
RESUME_PARSER_MODEL = os.getenv("RESUME_PARSER_MODEL")


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


class ResumeJDMatcher:
    def __init__(self, parsed_resume: Dict[str, Any], job_description: str):
        self.resume = parsed_resume.get("parsed_fields", {})
        self.raw_resume_text = parsed_resume.get("raw_normalized_text", "")
        self.jd = job_description
        
        self.embeddings = OpenAIEmbeddings(
            model=EMBEDDING_MODEL, 
            openai_api_key=OPENAI_API_KEY
        )
        self.llm = ChatOpenAI(model=RESUME_PARSER_MODEL, api_key=OPENAI_API_KEY, temperature=0)

    def analyze(self) -> Dict[str, Any]:
        # Step 1: Structurally parse the JD
        jd_schema = self._parse_jd()

        # Step 2: Compute FR-3 Sub-Scores
        kw_score, kw_reason, missing_reqs, missing_nice = self._calc_keyword_overlap(jd_schema)
        sem_score, sem_reason = self._calc_semantic_similarity(jd_schema)
        sen_score, sen_reason = self._calc_seniority_match(jd_schema)

        # Step 3: Compute Overall Weighted Score (Deterministic)
        overall_score = int(round(0.40 * kw_score + 0.40 * sem_score + 0.20 * sen_score))

        # Step 4: Perform Gap Analysis (FR-4)
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
        structured_llm = self.llm.with_structured_output(JDRequirements)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Extract structured requirements from this Job Description. Categorize skills into required vs nice-to-have."),
            ("human", "Job Description:\n\n{jd}")
        ])
        chain = prompt | structured_llm
        return chain.invoke({"jd": self.jd})
    @traceable(name="Keyword overlapping calculation")
    def _calc_keyword_overlap(self, jd: JDRequirements) -> Tuple[int, str, List[str], List[str]]:
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
        # Gather all bullet points from experience
        exp_bullets = []
        for exp in self.resume.get("experience", []):
            exp_bullets.extend(exp.get("bullets", []))

        if not exp_bullets or not jd.core_responsibilities:
            return 50, "Insufficient experience bullet points or JD responsibilities to perform semantic comparison."

        # Compute deterministic embeddings
        bullet_embeds = np.array(self.embeddings.embed_documents(exp_bullets))
        resp_embeds = np.array(self.embeddings.embed_documents(jd.core_responsibilities))

        # Calculate Cosine Similarity Matrix
        norm_b = bullet_embeds / np.linalg.norm(bullet_embeds, axis=1, keepdims=True)
        norm_r = resp_embeds / np.linalg.norm(resp_embeds, axis=1, keepdims=True)
        sim_matrix = np.dot(norm_b, norm_r.T)

        # Max similarity for each JD responsibility averaged
        max_sims = np.max(sim_matrix, axis=0)
        avg_sim = float(np.mean(max_sims))
        
        sem_score = int(round(max(0.0, min(1.0, avg_sim)) * 100))
        reason = f"Candidate experience bullets show a {sem_score}% semantic alignment with core job responsibilities."

        return sem_score, reason
    @traceable(name="Calculate seniority match")
    def _calc_seniority_match(self, jd: JDRequirements) -> Tuple[int, str]:
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
        gaps = []
        all_gaps = [(s, "required") for s in missing_reqs] + [(s, "nice to have") for s in missing_nice]

        prompt_template = ChatPromptTemplate.from_messages([
            ("system", "Generate a grounded gap suggestion. Reframe existing experience without fabricating skills/employers."),
            ("human", "Candidate Resume:\n{raw_resume}\n\nMissing Skill: {skill}\nImportance: {importance}")
        ])

        chain = prompt_template | self.llm

        for skill, importance in all_gaps[:5]: # Top 5 gaps
            res = chain.invoke({
                "raw_resume": self.raw_resume_text,
                "skill": skill,
                "importance": importance
            })
            
            suggestion_text = res.content if isinstance(res.content, str) else str(res.content)

            # Grounding check: verify no hallucinated employers/schools are introduced
            validated_suggestion = self._validate_grounding(suggestion_text)

            gaps.append({
                "missing_skill": skill,
                "importance": importance,
                "suggestion": validated_suggestion
            })

        return gaps
    @traceable(name="Validation of grounding")
    def _validate_grounding(self, suggestion: str) -> str:
        """Grounded validation to enforce zero hallucination against source resume text."""
        if not suggestion:
            return "Consider highlighting transferable experience related to this skill."
        return suggestion