"""Bounded, authenticated resume analysis API."""
import hmac
import logging
import os
import tempfile
import time
import uuid
import json
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import anyio
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

from config import validate_configuration
from errors import (AUTHENTIC_DOCUMENT_MESSAGE, InvalidDocumentError, InputLimitError,
                    InvalidJobDescriptionError, JDValidationUnavailableError)
from jd import validate_job_description
from parser import parse_resume
from matcher import ResumeJDMatcher
import db
from reports import render_pdf
from reproducibility import comparison_key, comparison_lock, analysis_version

logger = logging.getLogger(__name__)


class ReportRequest(BaseModel):
    session_id: str = Field(default="", max_length=100)
    sample: bool = False


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    max_upload_bytes: int = 10 * 1024 * 1024
    max_body_bytes: int = 11 * 1024 * 1024
    max_jd_chars: int = 20_000
    max_concurrent: int = 2
    requests_per_minute: int = 10

    @classmethod
    def from_env(cls):
        return cls(api_key=os.getenv("ATS_API_KEY", "").strip())

    def validate(self):
        if len(self.api_key) < 32:
            raise RuntimeError("Set ATS_API_KEY to a random secret of at least 32 characters (see .env.example).")


class RequestGuard:
    """Authenticate and bound request bodies before multipart parsing starts.

    The single-key rate budget and concurrency budget are per application process.
    """
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.active = 0
        self.recent = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        async def reject(code, message):
            await JSONResponse({"detail": message}, status_code=code)(scope, receive, send)
        key = headers.get(b"x-api-key", b"")
        if not self.settings.api_key or not hmac.compare_digest(key, self.settings.api_key.encode()):
            return await reject(401, "A valid X-API-Key header is required.")
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await reject(400, "Invalid Content-Length header.")
        if length < 0:
            return await reject(400, "Invalid Content-Length header.")
        if length > self.settings.max_body_bytes:
            return await reject(413, "Request exceeds the 11 MB limit.")
        now = time.monotonic()
        while self.recent and self.recent[0] <= now - 60:
            self.recent.popleft()
        if len(self.recent) >= self.settings.requests_per_minute:
            return await reject(429, "Request limit reached. Try again in one minute.")
        if self.active >= self.settings.max_concurrent:
            return await reject(503, "Analysis capacity is full. Try again shortly.")
        self.recent.append(now)
        self.active += 1
        received = 0
        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.settings.max_body_bytes:
                    raise HTTPException(413, "Request exceeds the 11 MB limit.")
            return message
        try:
            await self.app(scope, limited_receive, send)
        finally:
            self.active -= 1


def _save_upload_to_temp(file_obj, suffix, max_bytes):
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            path = temp_file.name
            total = 0
            while chunk := file_obj.read(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise InputLimitError("Resume file exceeds the 10 MB limit.")
                temp_file.write(chunk)
            if total == 0:
                raise InvalidDocumentError(AUTHENTIC_DOCUMENT_MESSAGE)
        return path
    except BaseException:
        if path:
            os.unlink(path)
        raise


def _analyze_upload(file_obj, filename, ext, job_description, settings):
    """The worker owns its temporary file through parsing, persistence, and cleanup."""
    key = comparison_key(file_obj, ext, job_description, settings.max_upload_bytes)
    with comparison_lock(key):
        cached = db.get_cached_analysis(key)
        if cached is not None:
            return cached
        requirements = validate_job_description(job_description)
        path = _save_upload_to_temp(file_obj, ext, settings.max_upload_bytes)
        try:
            session_id = str(uuid.uuid4())
            result = parse_resume(path, job_description)
            response = {"status": "success", "filename": filename,
                        "session_id": session_id, "data": result,
                        "analysis_version": analysis_version(),
                        "analysis_as_of": datetime.now(timezone.utc).date().isoformat()}
            output = ResumeJDMatcher(result, job_description, validated_requirements=requirements).analyze()
            response.update(output)
            return db.save_canonical_analysis(key, response)
        finally:
            os.unlink(path)


def create_app(settings=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        settings.validate()
        validate_configuration()
        await anyio.to_thread.run_sync(db.init_db)
        yield

    application = FastAPI(title="Resume Relevance Analysis API", version="1.1.0", lifespan=lifespan,
                          description="Compare a PDF or DOCX CV against a required job description and suggest improvements. Scans are not supported.")
    application.add_middleware(RequestGuard, settings=settings)
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
    frontend_directory = Path(__file__).resolve().parent / "frontend"
    application.mount("/static", StaticFiles(directory=frontend_directory), name="frontend")

    @application.get("/", include_in_schema=False)
    async def frontend():
        return FileResponse(frontend_directory / "index.html")

    @application.post("/api/v1/report/pdf")
    async def download_report(request: ReportRequest, api_key: str | None = Security(api_key_header)):
        if request.sample:
            report = json.loads((frontend_directory / "sample-report.json").read_text(encoding="utf-8"))
        else:
            report = await anyio.to_thread.run_sync(db.get_report, request.session_id)
            if report is None:
                raise HTTPException(404, "This report was not found. Run a new comparison.")
        if not report.get("scorecard"):
            raise HTTPException(409, "This report uses an older scoring method. Run a new comparison for evidence insights.")
        try:
            content = await anyio.to_thread.run_sync(render_pdf, report)
        except Exception as exc:
            logger.error("PDF export failed (%s)", type(exc).__name__)
            raise HTTPException(503, "PDF export is unavailable. Please try again shortly.") from exc
        return Response(content, media_type="application/pdf", headers={
            "Content-Disposition": 'attachment; filename="matchpoint-report.pdf"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    @application.post("/api/v1/analyze")
    async def analyze_resume(file: UploadFile = File(...), job_description: str = Form(..., min_length=1, description="Required job description to compare the CV against."),
                             api_key: str | None = Security(api_key_header)):
        try:
            if not job_description.strip():
                raise HTTPException(422, "Job description is required to compare your CV against the job.")
            filename = file.filename or ""
            ext = os.path.splitext(filename)[1].lower()
            if ext not in {".pdf", ".docx"}:
                raise InvalidDocumentError(AUTHENTIC_DOCUMENT_MESSAGE)
            if len(job_description) > settings.max_jd_chars:
                raise InputLimitError("Job description exceeds the 20,000 character limit.")
            job_description = job_description.strip()
            # Do not abandon a worker on cancellation while it owns an upload stream.
            result = await anyio.to_thread.run_sync(
                _analyze_upload, file.file, filename, ext, job_description, settings,
                abandon_on_cancel=False,
            )
            return JSONResponse(result)
        except HTTPException:
            raise
        except InvalidJobDescriptionError as exc:
            raise HTTPException(422, str(exc)) from exc
        except JDValidationUnavailableError as exc:
            raise HTTPException(503, str(exc)) from exc
        except InputLimitError as exc:
            raise HTTPException(413, str(exc)) from exc
        except InvalidDocumentError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            # Log an incident ID and exception class, never raw resume/provider payloads.
            incident_id = str(uuid.uuid4())
            logger.error("Analysis failed: incident=%s type=%s", incident_id, type(exc).__name__)
            raise HTTPException(500, {"message": "Unable to analyze the resume. Please try again.",
                                      "incident_id": incident_id}) from exc
        finally:
            await file.close()

    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000)
