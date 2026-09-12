"""
parser.py
=========
Extracts structured data from a resume file (PDF or DOCX).

Pipeline: file -> raw text (rule-based extraction) -> structured fields
(LLM) -> total_years_experience / seniority_level (rule-based, computed in
code - see compute_experience_stats).
"""
import os
import io
import re
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

import docx
import pymupdf  # PyMuPDF
import pdfplumber
import pytesseract
from PIL import Image
from dateutil import parser as date_parser

from langsmith import traceable
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate

from config import llm_client

# ==========================================
# Structured output schema
# ==========================================
# NOTE: total_years_experience and seniority_level are intentionally NOT
# fields the LLM fills in. Date-range arithmetic (parsing messy date
# formats, handling "Present", merging overlapping/concurrent jobs so they
# aren't double-counted) is exact, rule-based work - LLMs are unreliable at
# precise arithmetic like this, and getting it wrong directly skews
# downstream JD-matching scores. Both are computed deterministically in
# compute_experience_stats() below, after the LLM call returns. This also
# shortens the prompt/schema the LLM has to fill, which helps latency.
class ContactInfo(BaseModel):
    name: Optional[str] = Field(None, description="Full name of the candidate")
    email: Optional[str] = Field(None, description="Email address")
    phone: Optional[str] = Field(None, description="Phone number")


class ExperienceEntry(BaseModel):
    role: Optional[str] = Field(None, description="Job title / role")
    company: Optional[str] = Field(None, description="Company or organization name")
    dates: Optional[str] = Field(None, description="Employment date range, exactly as written in the resume")
    bullets: List[str] = Field(default_factory=list, description="Responsibility/achievement bullet points")


class ResumeSchema(BaseModel):
    contact: ContactInfo
    summary: str = Field("", description="Professional summary or objective, if present")
    skills: List[str] = Field(default_factory=list, description="Flat list of skills")
    experience: List[ExperienceEntry] = Field(default_factory=list, description="Work experience entries, in the order they appear")
    education: str = Field("", description="Education section, summarized as plain text")
    certifications: str = Field("", description="Certifications section, summarized as plain text")


# ==========================================
# Deterministic years-of-experience / seniority calculation
# ==========================================
_PRESENT_WORDS = {"present", "current", "currently", "now", "ongoing", "till date", "to date"}
_RANGE_SPLIT_RE = re.compile(r"\s*(?:-|\u2013|\u2014|\bto\b)\s*", re.IGNORECASE)


def _parse_single_date(token: str, *, is_end: bool) -> Optional[datetime]:
    """
    Parse one side of a date range (e.g. "Jan 2020" or "2022") into a
    datetime. Missing month/day are filled from `default`: start dates
    default to Jan 1 (earliest reasonable start), end dates default to
    Dec 31 (latest reasonable end) - this avoids systematically
    under-counting duration when a resume only gives a year.
    "Present"/"Current"/etc. resolve to today. Returns None (rather than
    raising) on anything unparseable, so one bad date string in one resume
    entry never crashes the whole calculation - that entry is just skipped.
    """
    token = token.strip().rstrip(".,")
    if not token:
        return None
    if token.lower() in _PRESENT_WORDS:
        return datetime.utcnow()
    default = datetime(1900, 12, 31) if is_end else datetime(1900, 1, 1)
    try:
        return date_parser.parse(token, default=default, fuzzy=True)
    except (ValueError, OverflowError):
        return None


def _parse_date_range(raw: str) -> Optional[Tuple[datetime, datetime]]:
    """Split a raw 'dates' string like 'Jun 2020 - Present' into (start, end) datetimes, or None if it can't be parsed."""
    if not raw:
        return None
    parts = _RANGE_SPLIT_RE.split(raw.strip(), maxsplit=1)
    if len(parts) != 2:
        return None  # no recognizable range separator - can't compute a duration from this entry
    start = _parse_single_date(parts[0], is_end=False)
    end = _parse_single_date(parts[1], is_end=True)
    if not start or not end or end < start:
        return None
    return start, end


def _merge_intervals(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    """Merge overlapping/adjacent date ranges so concurrent jobs (e.g. a freelance gig alongside a full-time role) aren't double-counted toward total experience."""
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda iv: iv[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def compute_experience_stats(experience: List[Dict[str, Any]]) -> Tuple[float, str]:
    """
    Deterministically compute (total_years_experience, seniority_level) from
    the 'dates' string on each experience entry the LLM already extracted.

    Seniority thresholds below are a starting heuristic (<2y entry-level,
    2-5y mid-level, 5-8y senior, 8y+ lead) - adjust these if your target
    roles use different bands; they're intentionally simple constants so
    they're easy to find and tune later.
    """
    intervals = []
    for entry in experience:
        date_range = _parse_date_range(entry.get("dates") or "")
        if date_range:
            intervals.append(date_range)

    merged = _merge_intervals(intervals)
    total_days = sum((end - start).days for start, end in merged)
    total_years = round(total_days / 365.25, 1)

    if total_years < 2:
        seniority = "entry-level"
    elif total_years < 5:
        seniority = "mid-level"
    elif total_years < 8:
        seniority = "senior"
    else:
        seniority = "lead"

    return total_years, seniority


class DynamicResumeParser:
    """Runs the full resume -> structured-data pipeline for a single uploaded file."""

    def __init__(self, file_path: str, job_description: Optional[str] = None):
        self.file_path = file_path
        self.job_description = job_description or ""
        self.file_ext = os.path.splitext(file_path)[1].lower()
        # Reuses the single shared client from config.py instead of building
        # a new ChatOpenAI instance per request (see review point 7).
        self.structured_llm = llm_client.with_structured_output(ResumeSchema)

    def process(self) -> Dict[str, Any]:
        """Entry point: extract raw text, run LLM structuring, attach deterministic experience stats, and return the full result dict."""
        if self.file_ext == ".docx":
            raw_text = self._parse_docx()
        elif self.file_ext == ".pdf":
            raw_text = self._parse_pdf()
        else:
            raise ValueError(f"Unsupported file format: {self.file_ext}")

        parsed_fields = self._extract_with_llm(raw_text)
        fields_dict = parsed_fields.model_dump()

        total_years, seniority = compute_experience_stats(fields_dict.get("experience", []))
        fields_dict["total_years_experience"] = total_years
        fields_dict["seniority_level"] = seniority

        return {
            "parsed_fields": fields_dict,
            "raw_normalized_text": raw_text,
            "job_description": self.job_description,
        }

    # ==========================================
    # STEP 1: DOCX Parsing (rule-based - text layout extraction is a
    # geometry/format problem, not a language problem, so no LLM involved)
    # ==========================================
    def _parse_docx(self) -> str:
        """Reads all paragraph and table-cell text from a .docx file, in document order, into a single normalized text blob."""
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
        """Routes a PDF to OCR (if it's image-based) or to per-page single/two-column text extraction, and joins the result into one text blob."""
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
        """Heuristic: builds a horizontal word-density histogram and checks for a whitespace 'gutter' between 25%-75% of page width, which indicates a two-column layout."""
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
        """Fast-path direct text extraction for standard single-column resume pages."""
        text = page.extract_text(layout=False)
        return text if text else "No text extracted for single column PDF."

    @traceable(name="Parsing two column CV")
    def _extract_two_column_text(self, page) -> str:
        """Finds the vertical gutter x-coordinate, splits words into left/right columns, sorts each column top-to-bottom, and returns left column text followed by right column text."""
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
        """Rasterizes each page at 300dpi and runs Tesseract OCR - fallback path for scanned/image-only PDFs with no extractable text layer."""
        doc = pymupdf.open(self.file_path)
        ocr_text = []
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_text.append(pytesseract.image_to_string(img))
        doc.close()
        return "\n".join(ocr_text)

    # ==========================================
    # STEP 4: LLM-based structuring
    # ==========================================
    @traceable(name="Extraction of structured data from LLM using raw text of the CV")
    def _extract_with_llm(self, raw_text: str) -> ResumeSchema:
        """
        Single LLM call that turns the raw extracted text into a validated
        ResumeSchema object. Note the prompt deliberately does NOT ask for
        years-of-experience or seniority anymore - those are computed in
        code from the 'dates' field this call still extracts (see
        compute_experience_stats). Dropping those two fields from the
        prompt/schema also shortens the call, which helps latency.
        """
        prompt = ChatPromptTemplate.from_messages([
            (
                "system",
                "You are a precise resume-parsing assistant. Extract structured "
                "information from the raw resume text below. Preserve dates and "
                "company/role names exactly as written. If a field is not present "
                "in the resume, leave it empty rather than guessing. Keep the order "
                "of work experience entries the same as in the original resume.",
            ),
            ("human", "Resume text:\n\n{raw_text}"),
        ])

        chain = prompt | self.structured_llm
        result: ResumeSchema = chain.invoke({"raw_text": raw_text})
        return result


def parse_resume(file_path: str, job_description: Optional[str] = None) -> Dict[str, Any]:
    """Convenience wrapper: runs the full pipeline for one file and returns the result dict."""
    return DynamicResumeParser(file_path, job_description=job_description).process()