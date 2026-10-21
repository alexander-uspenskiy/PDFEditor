"""Universal find-and-replace service: REST API plus a step-by-step web UI.

Run locally (the default host keeps uploads on this machine):

    uvicorn main:app --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000 for the UI, or http://127.0.0.1:8000/docs for the API.

Steps for a client:

1. POST /api/v1/uploads              upload a file, get an id and its detected format
2. POST /api/v1/uploads/{id}/find    count a string and get excerpts
3. POST /api/v1/uploads/{id}/replace replace it, get the result excerpts and a new file
4. GET  /api/v1/uploads/{id}/download?version=original|result
   GET  /api/v1/uploads/{id}/render?version=original|result&page=N   (PDF pages as PNG)

The original single-purpose endpoint POST /api/replace-name is kept for PDFs.
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import formats
import pdf_tools
import replace_name as legacy
from formats import (
    FormatError,
    InvalidInput,
    NotFound,
    OutputInvalid,
    UnsupportedFormat,
)
from jobs import JobStore

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
EDITOR_DIR = STATIC_DIR / "app"
MAX_STRING = formats.MAX_STRING_LENGTH

app = FastAPI(
    title="Edit PDF",
    version="3.0.0",
    description=(
        "Editor tools for PDFs: search, clean, inspect, and find and replace."
    ),
)
JOBS = JobStore()

if EDITOR_DIR.exists():
    # The editor is built from backend/frontend (npm run build) into static/app.
    app.mount("/app", StaticFiles(directory=EDITOR_DIR, html=True), name="editor")


class FindBody(BaseModel):
    find: str = Field("", max_length=MAX_STRING, description="The text to look for (case-sensitive).")


class ReplaceBody(BaseModel):
    find: str = Field("", max_length=MAX_STRING, description="The text to look for (case-sensitive).")
    replace: str = Field("", max_length=MAX_STRING, description="The text to write in its place.")


def _http(exc: FormatError) -> HTTPException:
    if isinstance(exc, InvalidInput):
        return HTTPException(400, str(exc))
    if isinstance(exc, NotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, UnsupportedFormat):
        return HTTPException(415, str(exc))
    if isinstance(exc, OutputInvalid):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


def _read_upload(file: UploadFile) -> bytes:
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "the file is larger than 25 MB")
    if not data:
        raise HTTPException(400, "the file is empty")
    return data


def _job_or_404(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "upload not found or expired; upload the file again")
    return job


def _summary(job) -> dict:
    return {
        "id": job.id,
        "filename": job.filename,
        "format": job.format,
        "size": job.size,
        "details": job.details,
        "has_result": job.has_result,
    }


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/app/")


@app.get("/find-replace", include_in_schema=False)
def find_replace_page() -> RedirectResponse:
    return RedirectResponse("/app/")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}




@app.post("/api/v1/uploads", status_code=201)
def upload(file: UploadFile = File(...)) -> dict:
    data = _read_upload(file)
    try:
        format_name = formats.detect(data)
        details = formats.HANDLERS[format_name].info(data)
    except FormatError as exc:
        raise _http(exc) from exc
    job = JOBS.create(file.filename or "document", data, format_name, details)
    return _summary(job)


@app.get("/api/v1/uploads/{job_id}")
def get_upload(job_id: str) -> dict:
    return _summary(_job_or_404(job_id))


@app.post("/api/v1/uploads/{job_id}/find")
def find(job_id: str, body: FindBody) -> dict:
    job = _job_or_404(job_id)
    handler = formats.HANDLERS[job.format]
    try:
        result = handler.find(job.original_path.read_bytes(), body.find)
    except FormatError as exc:
        raise _http(exc) from exc
    return {
        "count": result.count,
        "excerpts": [excerpt.as_dict() for excerpt in result.excerpts],
    }


@app.post("/api/v1/uploads/{job_id}/replace")
def replace(job_id: str, body: ReplaceBody) -> dict:
    job = _job_or_404(job_id)
    handler = formats.HANDLERS[job.format]
    try:
        result = handler.replace(job.original_path.read_bytes(), body.find, body.replace)
    except FormatError as exc:
        raise _http(exc) from exc

    job.result_path.write_bytes(result.data)
    job.has_result = True
    return {
        "id": job.id,
        "format": job.format,
        "replacements": result.replacements,
        "remaining": result.remaining,
        "touched": result.touched,
        "touched_label": result.touched_label,
        "excerpts": [excerpt.as_dict() for excerpt in result.excerpts],
        "download_url": f"/api/v1/uploads/{job.id}/download?version=result",
        "render_url": f"/api/v1/uploads/{job.id}/render" if job.format == "pdf" else None,
        "pages": job.details.get("pages"),
    }


@app.get("/api/v1/uploads/{job_id}/inspect")
def inspect_upload(job_id: str) -> dict:
    job = _job_or_404(job_id)
    if job.format != "pdf":
        raise HTTPException(400, "inspection is only available for PDF files")
    try:
        report = pdf_tools.inspect_pdf(job.original_path.read_bytes())
    except FormatError as exc:
        raise _http(exc) from exc
    report["size"] = job.size
    return report


class SearchBody(BaseModel):
    find: str = Field("", max_length=MAX_STRING, description="Text to locate (case-sensitive).")


@app.post("/api/v1/uploads/{job_id}/search")
def search_upload(job_id: str, body: SearchBody) -> dict:
    job = _job_or_404(job_id)
    if job.format != "pdf":
        raise HTTPException(400, "search with positions is only available for PDF files")
    try:
        return pdf_tools.search_pdf(job.original_path.read_bytes(), body.find)
    except FormatError as exc:
        raise _http(exc) from exc


class CleanBody(BaseModel):
    strip_metadata: bool = True
    strip_javascript: bool = True
    strip_embedded_files: bool = True
    compress: bool = True


@app.post("/api/v1/uploads/{job_id}/clean")
def clean_upload(job_id: str, body: CleanBody) -> dict:
    job = _job_or_404(job_id)
    if job.format != "pdf":
        raise HTTPException(400, "cleaning is only available for PDF files")
    try:
        output, report = pdf_tools.clean_pdf(
            job.original_path.read_bytes(),
            strip_metadata=body.strip_metadata,
            strip_javascript=body.strip_javascript,
            strip_embedded_files=body.strip_embedded_files,
            compress=body.compress,
        )
    except FormatError as exc:
        raise _http(exc) from exc

    job.result_path.write_bytes(output)
    job.has_result = True
    return {
        "id": job.id,
        **report,
        "download_url": f"/api/v1/uploads/{job.id}/download?version=result",
    }


class OccurrenceBody(BaseModel):
    find: str = Field("", max_length=MAX_STRING, description="The text of the match to change.")
    replace: str = Field("", max_length=MAX_STRING, description="The new text. Empty removes the match.")
    page: int = Field(1, ge=1, description="Page of the match, from 1.")
    index: int = Field(0, ge=0, description="Position of the match on that page, from /occurrences.")


@app.post("/api/v1/uploads/{job_id}/occurrences")
def occurrences_upload(job_id: str, body: FindBody) -> dict:
    """Every match of the text, with its page, index and box in PDF points. Nothing is changed."""
    job = _job_or_404(job_id)
    if job.format != "pdf":
        raise HTTPException(400, "occurrences are only available for PDF files")
    try:
        found = legacy.list_occurrences(job.original_path.read_bytes(), body.find)
    except legacy.InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except legacy.ReplacementError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"count": len(found), "occurrences": found}


@app.post("/api/v1/uploads/{job_id}/replace-occurrence")
def replace_occurrence_upload(job_id: str, body: OccurrenceBody) -> dict:
    """Replace or remove one match. The other matches of the same text are left as they are."""
    job = _job_or_404(job_id)
    if job.format != "pdf":
        raise HTTPException(400, "occurrences are only available for PDF files")
    try:
        output, report = legacy.replace_occurrence(
            job.original_path.read_bytes(), body.find, body.replace, body.page, body.index
        )
    except legacy.InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except legacy.NameNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except legacy.VerificationError as exc:
        raise HTTPException(409, f"verification failed, no file returned: {exc}") from exc
    except legacy.ReplacementError as exc:
        raise HTTPException(422, str(exc)) from exc

    job.result_path.write_bytes(output)
    job.has_result = True
    return {
        "id": job.id,
        **report.as_dict(),
        "download_url": f"/api/v1/uploads/{job.id}/download?version=result",
    }


@app.get("/api/v1/uploads/{job_id}/download")
def download(
    job_id: str,
    version: Literal["original", "result"] = Query("result"),
) -> Response:
    job = _job_or_404(job_id)
    path, name = _version_file(job, version)
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(
        content=path.read_bytes(),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/v1/uploads/{job_id}/render")
def render(
    job_id: str,
    version: Literal["original", "result"] = Query("result"),
    page: int = Query(1, ge=1),
) -> Response:
    job = _job_or_404(job_id)
    if job.format != "pdf":
        raise HTTPException(400, "page images are only available for PDF files")
    path, _ = _version_file(job, version)
    try:
        png = formats.HANDLERS["pdf"].render_png(path.read_bytes(), page)
    except FormatError as exc:
        raise _http(exc) from exc
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


def _version_file(job, version: str) -> tuple[Path, str]:
    if version == "original":
        return job.original_path, job.filename
    if not job.has_result:
        raise HTTPException(404, "no result yet; run the replace step first")
    return job.result_path, f"{job.stem}-edited{job.suffix}"


# --- Original PDF-only endpoint, kept for existing clients --------------------

@app.post(
    "/api/replace-name",
    response_class=Response,
    responses={
        200: {
            "description": "The rewritten PDF. The X-Replacement-Report header holds a JSON summary.",
            "content": {"application/pdf": {}},
        },
        400: {"description": "Empty, too long, or unusable names."},
        404: {"description": "The name is not in a single text run of the document."},
        409: {"description": "Verification failed, so no file was returned."},
        413: {"description": "The upload is larger than 25 MB."},
        415: {"description": "The upload is not a PDF."},
        422: {"description": "The PDF uses a feature this engine does not support."},
    },
)
def replace_name_endpoint(
    file: UploadFile = File(..., description="The PDF to edit."),
    old_name: str = Form("", description="The exact name to find (case-sensitive)."),
    new_name: str = Form("", description="The name to write in its place."),
) -> Response:
    data = _read_upload(file)
    if not data.startswith(b"%PDF-"):
        raise HTTPException(415, "the upload is not a PDF")

    try:
        output, report = legacy.replace_name(data, old_name, new_name)
    except legacy.InvalidInput as exc:
        raise HTTPException(400, str(exc)) from exc
    except legacy.NameNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except legacy.VerificationError as exc:
        raise HTTPException(409, f"verification failed, no file returned: {exc}") from exc
    except legacy.ReplacementError as exc:
        raise HTTPException(422, str(exc)) from exc

    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "document").stem)[:80] or "document"
    return Response(
        content=output,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}-renamed.pdf"',
            "X-Replacement-Report": json.dumps(report.as_dict()),
            "Access-Control-Expose-Headers": "X-Replacement-Report, Content-Disposition",
        },
    )
