"""Tests for the name replacement engine and REST API.

Run from the backend folder:  python -m pytest tests
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pikepdf
import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from pdfminer.high_level import extract_pages, extract_text
from pdfminer.layout import LTChar, LTTextLine

import replace_name as engine
from main import app

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "samples" / "sample_before.pdf"
PAGE_HEIGHT = 792.0


def build_pdf(content: bytes) -> bytes:
    """A one-page Helvetica document with the given content stream."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name.Font,
                "/Subtype": pikepdf.Name.Type1,
                "/BaseFont": pikepdf.Name.Helvetica,
                "/Encoding": pikepdf.Name.WinAnsiEncoding,
            }
        )
    )
    page = pdf.add_blank_page(page_size=(612, 792))
    page.obj["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
    page.obj["/Contents"] = pdf.make_stream(content)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def chars(data: bytes):
    for page in extract_pages(io.BytesIO(data)):
        stack = list(page)
        while stack:
            element = stack.pop()
            if isinstance(element, LTChar):
                yield element
            elif hasattr(element, "__iter__"):
                stack.extend(element)


def line_boxes(data: bytes, text: str):
    boxes = []
    for page in extract_pages(io.BytesIO(data)):
        stack = list(page)
        while stack:
            element = stack.pop()
            if isinstance(element, LTTextLine) and text in element.get_text():
                boxes.append(element.bbox)
            elif hasattr(element, "__iter__") and not isinstance(element, str):
                stack.extend(element)
    return boxes


def all_bytes_contain(data: bytes, needle: bytes) -> bool:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return engine._find_bytes_in_objects(pdf, needle) is not None


@pytest.fixture(scope="module")
def sample_bytes() -> bytes:
    if not SAMPLE.exists():
        pytest.skip("samples/sample_before.pdf is not present")
    return SAMPLE.read_bytes()


def test_sample_name_is_replaced_and_verified(sample_bytes):
    output, report = engine.replace_name(sample_bytes, "Jane Doe", "Alex")

    assert report.replacements == 1
    assert report.pages_changed == 1
    assert report.new_name_occurrences == 1
    text = extract_text(io.BytesIO(output))
    assert "Alex" in text
    assert "Jane" not in text


def test_no_original_bytes_remain_in_any_object(sample_bytes):
    output, _ = engine.replace_name(sample_bytes, "Jane Doe", "Alex")

    assert not all_bytes_contain(output, b"Jane Doe")
    assert not all_bytes_contain(output, b"Jane")


def test_metadata_thumbnails_and_xmp_are_removed(sample_bytes):
    with pikepdf.open(io.BytesIO(sample_bytes)) as pdf:
        assert "/Creator" in pdf.docinfo
    output, _ = engine.replace_name(sample_bytes, "Jane Doe", "Alex")

    with pikepdf.open(io.BytesIO(output)) as pdf:
        assert len(pdf.docinfo) == 0
        assert "/Metadata" not in pdf.Root
        assert all("/Thumb" not in page.obj for page in pdf.pages)


def test_background_pixels_are_unchanged_outside_the_name(sample_bytes):
    output, _ = engine.replace_name(sample_bytes, "Jane Doe", "Alex")

    scale = 2.0
    before = pdfium.PdfDocument(sample_bytes)[0].render(scale=scale).to_numpy()
    after = pdfium.PdfDocument(output)[0].render(scale=scale).to_numpy()
    assert before.shape == after.shape

    changed = np.argwhere(np.any(before != after, axis=2))
    assert len(changed) > 0, "the name should change the rendered page"

    # Only the areas where the old and new names are drawn may differ.
    allowed = line_boxes(sample_bytes, "Jane Doe") + line_boxes(output, "Alex")
    pad = 3.0
    for row, col in changed:
        x = col / scale
        y = PAGE_HEIGHT - row / scale
        assert any(
            x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad for x0, y0, x1, y1 in allowed
        ), f"pixel ({col}, {row}) changed outside the name"


def test_text_after_a_partial_match_keeps_its_position():
    content = (
        b"BT /F1 12 Tf 1 0 0 1 50 400 Tm (Hello Jane Doe world) Tj ET"
    )
    original = build_pdf(content)

    output, report = engine.replace_name(original, "Jane Doe", "Al")

    assert report.replacements == 1
    before_w = next(c for c in chars(original) if c.get_text() == "w")
    after_w = next(c for c in chars(output) if c.get_text() == "w")
    assert after_w.x0 == pytest.approx(before_w.x0, abs=0.05)
    assert "Al" in extract_text(io.BytesIO(output))


def test_every_occurrence_in_one_run_is_replaced():
    original = build_pdf(b"BT /F1 12 Tf 1 0 0 1 50 400 Tm (Jane Doe and Jane Doe) Tj ET")

    output, report = engine.replace_name(original, "Jane Doe", "Alex")

    assert report.replacements == 2
    assert extract_text(io.BytesIO(output)).count("Alex") == 2


def test_name_split_across_runs_is_not_matched():
    original = build_pdf(
        b"BT /F1 12 Tf 1 0 0 1 50 400 Tm (Jane) Tj ET BT /F1 12 Tf 1 0 0 1 50 380 Tm (Doe) Tj ET"
    )

    with pytest.raises(engine.NameNotFound):
        engine.replace_name(original, "Jane Doe", "Alex")


def test_missing_name_raises_not_found(sample_bytes):
    with pytest.raises(engine.NameNotFound):
        engine.replace_name(sample_bytes, "Nobody Here", "Alex")


def test_invalid_names_are_rejected(sample_bytes):
    with pytest.raises(engine.InvalidInput):
        engine.replace_name(sample_bytes, "  ", "Alex")
    with pytest.raises(engine.InvalidInput):
        engine.replace_name(sample_bytes, "Jane Doe", "Jane Doe")
    with pytest.raises(engine.InvalidInput):
        engine.replace_name(sample_bytes, "Jane Doe", "Алекс")


def test_api_replace_returns_pdf_and_report(sample_bytes):
    client = TestClient(app)
    response = client.post(
        "/api/replace-name",
        files={"file": ("sample record.pdf", sample_bytes, "application/pdf")},
        data={"old_name": "Jane Doe", "new_name": "Alex"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert response.headers["content-disposition"] == 'attachment; filename="sample_record-renamed.pdf"'
    assert '"replacements": 1' in response.headers["x-replacement-report"]
    assert "Alex" in extract_text(io.BytesIO(response.content))


@pytest.mark.parametrize(
    "payload, status",
    [
        ({"old_name": "Nobody Here", "new_name": "Alex"}, 404),
        ({"old_name": "", "new_name": "Alex"}, 400),
    ],
)
def test_api_error_codes(sample_bytes, payload, status):
    client = TestClient(app)
    response = client.post(
        "/api/replace-name",
        files={"file": ("sample.pdf", sample_bytes, "application/pdf")},
        data=payload,
    )
    assert response.status_code == status
    assert "detail" in response.json()


def test_api_rejects_non_pdf_upload():
    client = TestClient(app)
    response = client.post(
        "/api/replace-name",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"old_name": "Jane Doe", "new_name": "Alex"},
    )
    assert response.status_code == 415


def test_api_serves_the_ui_and_health():
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok"}
    page = client.get("/find-replace", follow_redirects=False)
    assert page.headers["location"] == "/app/"
