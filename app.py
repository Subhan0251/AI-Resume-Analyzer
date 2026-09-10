import os
import tempfile
import shutil
import asyncio
from functools import partial
from fastapi import FastAPI, File, UploadFile, HTTPException, status, Form
from fastapi.responses import JSONResponse
from parser import parse_resume
from matcher import ResumeJDMatcher
import uuid

app = FastAPI(
    title="Universal Resume Parsing API",
    version="1.0.0",
    description="Extracts structured sections and entities from single/multi-column PDFs and DOCX files.",
)


def _save_upload_to_temp(file_obj, suffix: str) -> str:
    """Blocking: writes the uploaded stream to a temp file. Runs in a worker thread."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        shutil.copyfileobj(file_obj, temp_file)
        return temp_file.name


@app.post("/api/v1/analyze", status_code=status.HTTP_200_OK)
async def analyze_resume(file: UploadFile = File(...),job_description:str=Form("")):
    filename = file.filename
    ext = os.path.splitext(filename)[1].lower()

    if ext not in [".pdf", ".docx"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file format. Only PDF and DOCX files are supported.",
        )

    loop = asyncio.get_running_loop()
    temp_path = None

    try:
        # file.file is a sync SpooledTemporaryFile; copying it is blocking I/O,
        # so it goes to the executor rather than running on the event loop.
        temp_path = await loop.run_in_executor(
            None, _save_upload_to_temp, file.file, ext
        )

        # parse_resume() does blocking PDF/OCR work plus a blocking network
        # call to the LLM - also offloaded so concurrent requests don't queue
        # behind each other on the event loop.
        result = await loop.run_in_executor(None, partial(parse_resume, temp_path,job_description))

        
        response_data={
            "status": "success",
            "filename": filename,
            "session_id":str(uuid.uuid4()),
            "data": result,
        }
        if job_description.strip():
            matcher = ResumeJDMatcher(result, job_description)
            match_output = await loop.run_in_executor(None, matcher.analyze)
            
            response_data.update({
                "match_score": match_output["match_score"],
                "sub_scores": match_output["sub_scores"],
                "explanations": match_output["explanations"],
                "parsed_resume": match_output["parsed_resume"],
                "gaps": match_output["gaps"]
            })

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