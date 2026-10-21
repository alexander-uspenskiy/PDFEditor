"""Tests for per-spot editing: listing matches with positions, and replacing one match.

Run from the backend folder:  python -m pytest tests
"""

from __future__ import annotations

import io

import pikepdf
import pytest
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar

import pdf_tools
import replace_name as engine


def build(content: bytes, size=(400, 300), rotate: int | None = None) -> bytes:
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
    page = pdf.add_blank_page(page_size=size)
    page.obj["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
    page.obj["/Contents"] = pdf.make_stream(content)
    if rotate is not None:
        page.obj["/Rotate"] = rotate
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def line(text: str, y: int, x: int = 50) -> bytes:
    return f"BT /F1 12 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET ".encode()


def page_text(data: bytes) -> bytes:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return pdf.pages[0].Contents.read_bytes()


THREE = line("Jane Doe", 400) + line("Jane Doe", 300) + line("Jane Doe", 200)


def test_matches_are_listed_with_page_positions():
    found = engine.list_occurrences(build(THREE), "Jane Doe")

    assert [entry["index"] for entry in found] == [0, 1, 2]
    assert all(entry["page"] == 1 for entry in found)
    # Box starts at the text origin and runs from 25% below the baseline to 85% above.
    x0, y0, x1, y1 = found[0]["box"]
    assert x0 == pytest.approx(50, abs=0.01)
    assert y0 == pytest.approx(400 - 0.25 * 12, abs=0.01)
    assert y1 == pytest.approx(400 + 0.85 * 12, abs=0.01)
    assert x1 > x0 + 40


def test_box_agrees_with_pdfminer_origin():
    data = build(THREE)
    origin = next(
        char.matrix[4]
        for page in extract_pages(io.BytesIO(data))
        for char in _chars(page)
        if char.get_text() == "J"
    )
    assert engine.list_occurrences(data, "Jane Doe")[0]["box"][0] == pytest.approx(origin, abs=0.2)


def _chars(element):
    if isinstance(element, LTChar):
        yield element
    elif hasattr(element, "__iter__"):
        for child in element:
            yield from _chars(child)


def test_replacing_one_match_keeps_the_others():
    data = build(THREE)
    before = engine.list_occurrences(data, "Jane Doe")

    output, report = engine.replace_occurrence(data, "Jane Doe", "Alex", 1, 1)

    after = engine.list_occurrences(output, "Jane Doe")
    assert report.other_copies == 2
    assert len(after) == 2
    assert after[0]["box"] == before[0]["box"]
    assert after[1]["box"] == before[2]["box"]
    assert pdf_tools.search_pdf(output, "Alex")["count"] == 1


def test_replacement_takes_the_colour_of_the_original():
    data = build(b"0 0 1 rg " + line("Jane Doe", 200))

    output, _ = engine.replace_occurrence(data, "Jane Doe", "Alex", 1, 0)

    content = page_text(output)
    assert b"0 0 1 rg" in content
    assert content.index(b"0 0 1 rg") < content.index(b"(Alex)")


def test_partial_match_keeps_the_text_after_it_in_place():
    data = build(b"BT /F1 12 Tf 1 0 0 1 50 400 Tm (Hello Jane Doe world) Tj ET")
    before = next(c for page in extract_pages(io.BytesIO(data)) for c in _chars(page) if c.get_text() == "w")

    output, _ = engine.replace_occurrence(data, "Jane Doe", "Al", 1, 0)

    after = next(c for page in extract_pages(io.BytesIO(output)) for c in _chars(page) if c.get_text() == "w")
    assert after.x0 == pytest.approx(before.x0, abs=0.05)


def test_same_position_on_another_page_is_not_mistaken_for_the_removed_match():
    two_pages = pikepdf.Pdf.new()
    font = two_pages.make_indirect(pikepdf.Dictionary({"/Type": pikepdf.Name.Font, "/Subtype": pikepdf.Name.Type1, "/BaseFont": pikepdf.Name.Helvetica}))
    for _ in range(2):
        page = two_pages.add_blank_page(page_size=(400, 300))
        page.obj["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
        page.obj["/Contents"] = two_pages.make_stream(line("Jane Doe", 200))
    buffer = io.BytesIO()
    two_pages.save(buffer)
    data = buffer.getvalue()

    output, report = engine.replace_occurrence(data, "Jane Doe", "Alex", 1, 0)

    assert report.other_copies == 1
    assert [entry["page"] for entry in engine.list_occurrences(output, "Jane Doe")] == [2]


def test_deleting_one_match_removes_only_that_text():
    data = build(THREE)

    output, _ = engine.replace_occurrence(data, "Jane Doe", "", 1, 0)

    assert len(engine.list_occurrences(output, "Jane Doe")) == 2


def test_rotated_page_is_supported():
    data = build(line("Jane Doe", 200), rotate=90)
    assert len(engine.list_occurrences(data, "Jane Doe")) == 1

    output, report = engine.replace_occurrence(data, "Jane Doe", "Alex", 1, 0)

    assert report.other_copies == 0
    assert pdf_tools.search_pdf(output, "Alex")["count"] == 1


def test_text_split_across_runs_is_not_listed():
    data = build(line("Jane", 400) + line("Doe", 380))
    assert engine.list_occurrences(data, "Jane Doe") == []
    with pytest.raises(engine.NameNotFound):
        engine.replace_occurrence(data, "Jane Doe", "Alex", 1, 0)


def test_unknown_position_raises_not_found():
    with pytest.raises(engine.NameNotFound):
        engine.replace_occurrence(build(THREE), "Jane Doe", "Alex", 1, 7)


def test_metadata_is_not_changed_by_a_single_edit():
    data = build(THREE)
    with pikepdf.open(io.BytesIO(data)) as pdf:
        pdf.docinfo["/Author"] = pikepdf.String("Alex")
        buffer = io.BytesIO()
        pdf.save(buffer)
    data = buffer.getvalue()

    output, _ = engine.replace_occurrence(data, "Jane Doe", "Alex", 1, 0)

    with pikepdf.open(io.BytesIO(output)) as pdf:
        assert str(pdf.docinfo["/Author"]) == "Alex"


def test_empty_or_identical_texts_are_rejected():
    with pytest.raises(engine.InvalidInput):
        engine.list_occurrences(build(THREE), "")
    with pytest.raises(engine.InvalidInput):
        engine.replace_occurrence(build(THREE), "Jane Doe", "Jane Doe", 1, 0)


# --- API ----------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

client = TestClient(app)


def _upload(data: bytes) -> str:
    return client.post("/api/v1/uploads", files={"file": ("doc.pdf", data, "application/pdf")}).json()["id"]


def test_api_lists_then_replaces_one_match():
    job = _upload(build(THREE))

    listed = client.post(f"/api/v1/uploads/{job}/occurrences", json={"find": "Jane Doe"}).json()
    assert listed["count"] == 3
    first = listed["occurrences"][1]
    assert first["page"] == 1 and first["index"] == 1

    replaced = client.post(
        f"/api/v1/uploads/{job}/replace-occurrence",
        json={"find": "Jane Doe", "replace": "Alex", "page": first["page"], "index": first["index"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["other_copies"] == 2
    download = client.get(replaced.json()["download_url"])
    assert download.content.startswith(b"%PDF-")
    assert len(engine.list_occurrences(download.content, "Jane Doe")) == 2


def test_api_error_codes_for_occurrences():
    job = _upload(build(THREE))
    url = f"/api/v1/uploads/{job}/replace-occurrence"

    assert client.post(f"/api/v1/uploads/{job}/occurrences", json={"find": ""}).status_code == 400
    assert client.post(url, json={"find": "Jane Doe", "replace": "x", "page": 1, "index": 9}).status_code == 404
    assert client.post(url, json={"find": "Jane Doe", "replace": "Jane Doe", "page": 1, "index": 0}).status_code == 400
    assert client.post("/api/v1/uploads/unknown/occurrences", json={"find": "x"}).status_code == 404
