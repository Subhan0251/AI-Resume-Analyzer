import os
import io
import docx
import pymupdf  # PyMuPDF
import pdfplumber
import pytesseract
from PIL import Image
from typing import Dict, List, Any, Optional

from langsmith import traceable
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RESUME_PARSER_MODEL = os.getenv("RESUME_PARSER_MODEL")

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY is not set. Add it to your .env file "
        "(see .env.example)."
    )


# ==========================================
# Structured output schema
# ==========================================
class ContactInfo(BaseModel):
    name: Optional[str] = Field(None, description="Full name of the candidate")
    email: Optional[str] = Field(None, description="Email address")
    phone: Optional[str] = Field(None, description="Phone number")


class ExperienceEntry(BaseModel):
    role: Optional[str] = Field(None, description="Job title / role")
    company: Optional[str] = Field(None, description="Company or organization name")
    dates: Optional[str] = Field(None, description="Employment date range, as written in the resume")
    bullets: List[str] = Field(default_factory=list, description="Responsibility/achievement bullet points")


class ResumeSchema(BaseModel):
    contact: ContactInfo
    summary: str = Field("", description="Professional summary or objective, if present")
    skills: List[str] = Field(default_factory=list, description="Flat list of skills")
    experience: List[ExperienceEntry] = Field(default_factory=list, description="Work experience entries, in the order they appear")
    education: str = Field("", description="Education section, summarized as plain text")
    certifications: str = Field("", description="Certifications section, summarized as plain text")
    total_years_experience: float = Field(0.0, description="Estimated total years of professional work experience derived from dates.")
    seniority_level: str = Field("junior", description="Detected seniority: entry-level, mid-level, senior, or lead")


class DynamicResumeParser:
    def __init__(self, file_path: str, job_description: Optional[str] = None, model: Optional[str] = None):
        self.file_path = file_path
        self.job_description=job_description or ""
        self.file_ext = os.path.splitext(file_path)[1].lower()
        self.model = RESUME_PARSER_MODEL
        self.llm = ChatOpenAI(model=self.model, api_key=OPENAI_API_KEY, temperature=0)
        self.structured_llm = self.llm.with_structured_output(ResumeSchema)

    def process(self) -> Dict[str, Any]:
        if self.file_ext == ".docx":
            raw_text = self._parse_docx()
        elif self.file_ext == ".pdf":
            raw_text = self._parse_pdf()
        else:
            raise ValueError(f"Unsupported file format: {self.file_ext}")

        parsed_fields = self._extract_with_llm(raw_text)

        return {
            "parsed_fields": parsed_fields.model_dump(),
            "raw_normalized_text": raw_text,
            "job_description":self.job_description
        }

    # ==========================================
    # STEP 1: DOCX Parsing (rule-based, unchanged)
    # ==========================================
    def _parse_docx(self) -> str:
        doc = docx.Document(self.file_path)
        lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        if p.text.strip():
                            lines.append(p.text.strip())
        return "\n".join(lines)

    # ==========================================
    # STEP 2 & 3: Layout Detection & PDF Extraction (rule-based, unchanged)
    # ==========================================
    @traceable(name="Parsing PDF")
    def _parse_pdf(self) -> str:
        doc = pymupdf.open(self.file_path)
        total_chars = sum(len(page.get_text()) for page in doc)
        doc.close()

        if total_chars < 50:
            return self._parse_image_pdf()

        extracted_pages = []
        with pdfplumber.open(self.file_path) as pdf:
            for page in pdf.pages:
                if self._is_two_column_layout(page):
                    
                    extracted_pages.append(self._extract_two_column_text(page))
                else:
                    extracted_pages.append(self._extract_single_column_text(page))

        return "\n\n".join(extracted_pages)

    def _is_two_column_layout(self, page) -> bool:
        words = page.extract_words(x_tolerance=3, y_tolerance=3)
        if not words or len(words) < 20:
            return False

        page_width = float(page.width)
        bucket_size = 10
        num_buckets = int(page_width // bucket_size) + 1
        histogram = [0] * num_buckets

        for w in words:
            start_b = int(w["x0"] // bucket_size)
            end_b = int(w["x1"] // bucket_size)
            for b in range(max(0, start_b), min(num_buckets, end_b + 1)):
                histogram[b] += 1

        min_b = int((page_width * 0.25) // bucket_size)
        max_b = int((page_width * 0.75) // bucket_size)

        min_density = min(histogram[min_b:max_b]) if min_b < max_b else 100
        return min_density == 0

    @traceable(name="Parsing one column CV")
    def _extract_single_column_text(self, page) -> str:
        text = page.extract_text(layout=False)
        return text if text else "No text extracted for single column PDF."

    @traceable(name="Parsing two column CV")
    def _extract_two_column_text(self, page) -> str:
        words = page.extract_words(x_tolerance=3, y_tolerance=3)
        page_width = float(page.width)

        bucket_size = 10
        histogram = [0] * (int(page_width // bucket_size) + 1)
        for w in words:
            for b in range(int(w["x0"] // bucket_size), int(w["x1"] // bucket_size) + 1):
                histogram[b] += 1

        min_b = int((page_width * 0.25) // bucket_size)
        max_b = int((page_width * 0.75) // bucket_size)
        gutter_b = min_b + histogram[min_b:max_b].index(min(histogram[min_b:max_b]))
        gutter_x = gutter_b * bucket_size

        left_words = [w for w in words if w["x1"] <= gutter_x]
        right_words = [w for w in words if w["x0"] > gutter_x]

        left_words.sort(key=lambda w: (round(w["top"], -1), w["x0"]))
        right_words.sort(key=lambda w: (round(w["top"], -1), w["x0"]))

        left_text = " ".join([w["text"] for w in left_words])
        right_text = " ".join([w["text"] for w in right_words])

        return f"{left_text}\n\n{right_text}"

    @traceable(name="Image PDF extraction")
    def _parse_image_pdf(self) -> str:
        doc = pymupdf.open(self.file_path)
        ocr_text = []
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_text.append(pytesseract.image_to_string(img))
        doc.close()
        return "\n".join(ocr_text)

    # ==========================================
    # STEP 4: LLM-based structuring (replaces old regex segmentation)
    # ==========================================
    @traceable(name="Extraction of structued data from LLM using raw text of the CV")
    def _extract_with_llm(self, raw_text: str) -> ResumeSchema:
        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                """You are a precise resume-parsing assistant. Extract structured information from the raw resume text below. Preserve dates and company/role names exactly as written. Calculate total_years_experience accurately based on the employement date ranges. Set seniority_level as entry_level, mid_level, senior or Lead based on the number of years of experience candidate have. If a field is not present in the resume, leave it empty rather than guessing. Keep the order 
                of work experience entries the same as in the original resume."""
            ),
            ("human", "Resume text:\n\n{raw_text}"),
        ])

        chain = prompt | self.structured_llm
        result: ResumeSchema = chain.invoke({"raw_text": raw_text})
        return result


def parse_resume(file_path: str,job_description:Optional[str]=None, model: Optional[str] = None) -> Dict[str, Any]:
    return DynamicResumeParser(file_path, job_description=job_description,model=model).process()