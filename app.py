"""
app.py
======
FastAPI entrypoint. Handles the upload, runs parsing (+ optional JD
matching) off the event loop via a thread-pool executor, and persists a
record of each request to SQLite.
"""
import os
import tempfile
import shutil
import asyncio
import uuid
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI, File, UploadFile, HTTPException, status, Form
from fastapi.responses import JSONResponse

from parser import parse_resume
from matcher import ResumeJDMatcher
import db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at process startup: creates the SQLite tables if they don't exist yet."""
    db.init_db()
    yield


app = FastAPI(
    title="Universal Resume Parsing API",
    version="1.0.0",
    description="Extracts structured sections and entities from single/multi-column PDFs and DOCX files.",
    lifespan=lifespan,
)


def _save_upload_to_temp(file_obj, suffix: str) -> str:
    """Blocking: writes the uploaded stream to a temp file. Runs in a worker thread."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        shutil.copyfileobj(file_obj, temp_file)
        return temp_file.name


@app.post("/api/v1/analyze", status_code=status.HTTP_200_OK)
async def analyze_resume(file: UploadFile = File(...), job_description: str = Form("")):
    """
    Accepts a resume file (+ optional job description text). Saves the
    upload, parses it, optionally matches it against the JD, persists a
    session record (and any gap suggestions) to SQLite, and returns the
    combined result. All blocking work (file I/O, PDF/OCR parsing, LLM
    calls, SQLite writes) runs in the executor so the event loop stays free
    for other concurrent requests.
    """
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()

    if ext not in [".pdf", ".docx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only PDF and DOCX files are supported.",
        )

    loop = asyncio.get_running_loop()
    temp_path = None
    session_id = str(uuid.uuid4())

    try:
        temp_path = await loop.run_in_executor(
            None, _save_upload_to_temp, file.file, ext
        )

        result = await loop.run_in_executor(
            None, partial(parse_resume, temp_path, job_description)
        )

        response_data = {
            "status": "success",
            "filename": filename,
            "session_id": session_id,
            "data": result,
        }

        match_score = None
        gaps = []

        if job_description.strip():
            matcher = ResumeJDMatcher(result, job_description)
            match_output = await loop.run_in_executor(None, matcher.analyze)

            response_data.update({
                "match_score": match_output["match_score"],
                "sub_scores": match_output["sub_scores"],
                "explanations": match_output["explanations"],
                "parsed_resume": match_output["parsed_resume"],
                "gaps": match_output["gaps"],
            })
            match_score = match_output["match_score"]
            gaps = match_output["gaps"]

        # Persist the session (and any gap suggestions) - SQLite writes are
        # blocking, so these go through the executor too.
        await loop.run_in_executor(
            None,
            db.save_session,
            session_id,
            result.get("raw_normalized_text", ""),
            job_description,
            match_score,
        )
        if gaps:
            await loop.run_in_executor(None, db.save_suggestions, session_id, gaps)

        return JSONResponse(content=response_data)

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while parsing the document: {str(e)}",
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)