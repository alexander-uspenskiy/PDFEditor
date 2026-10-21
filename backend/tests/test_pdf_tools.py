"""Tests for the editor tools: inspect, clean, search with positions, and their API routes.

Run from the backend folder:  python -m pytest tests
"""

from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from fastapi.testclient import TestClient

import formats
import pdf_tools
from main import app

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "sample_before.pdf"
client = TestClient(app)


def make_pdf(pages: list[str], size=(400, 300)) -> bytes:
    """A plain Helvetica document. Each entry is the text drawn near the top of one page."""
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
    for text in pages:
        page = pdf.add_blank_page(page_size=size)
        page.obj["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
        page.obj["/Contents"] = pdf.make_stream(f"BT /F1 14 Tf 40 200 Td ({text}) Tj ET".encode())
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def make_risky_pdf() -> bytes:
    """A PDF with JavaScript (open action, annotation), an attachment, XMP, metadata and a form field."""
    pdf = pikepdf.new()
    font = pdf.make_indirect(
        pikepdf.Dictionary({"/Type": pikepdf.Name.Font, "/Subtype": pikepdf.Name.Type1, "/BaseFont": pikepdf.Name.Helvetica})
    )
    page = pdf.add_blank_page(page_size=(300, 300))
    page.obj["/Resources"] = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": font})})
    page.obj["/Contents"] = pdf.make_stream(b"BT /F1 12 Tf 20 200 Td (Secret Name) Tj ET")

    js = pikepdf.Dictionary({"/S": pikepdf.Name.JavaScript, "/JS": pikepdf.String("app.alert('hi')")})
    pdf.Root["/OpenAction"] = js
    link = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name.Annot,
                "/Subtype": pikepdf.Name.Link,
                "/Rect": pikepdf.Array([0, 0, 10, 10]),
                "/A": pikepdf.Dictionary({"/S": pikepdf.Name.JavaScript, "/JS": pikepdf.String("x()")}),
            }
        )
    )
    attachment = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name.Annot,
                "/Subtype": pikepdf.Name("/FileAttachment"),
                "/Rect": pikepdf.Array([10, 10, 20, 20]),
                "/FS": pikepdf.Dictionary({"/Type": pikepdf.Name.Filespec, "/F": pikepdf.String("note.txt")}),
            }
        )
    )
    page.obj["/Annots"] = pikepdf.Array([link, attachment])

    filespec = pdf.make_indirect(
        pikepdf.Dictionary({"/Type": pikepdf.Name.Filespec, "/F": pikepdf.String("embedded.txt")})
    )
    pdf.Root["/Names"] = pikepdf.Dictionary(
        {"/EmbeddedFiles": pikepdf.Dictionary({"/Names": pikepdf.Array([pikepdf.String("embedded.txt"), filespec])})}
    )
    field = pdf.make_indirect(
        pikepdf.Dictionary({"/FT": pikepdf.Name.Tx, "/T": pikepdf.String("name"), "/V": pikepdf.String("Ann")})
    )
    pdf.Root["/AcroForm"] = pikepdf.Dictionary({"/Fields": pikepdf.Array([field])})
    xmp = (
        b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>'
        b'<?xpacket end="w"?>'
    )
    pdf.Root["/Metadata"] = pdf.make_stream(xmp)
    pdf.docinfo["/Author"] = pikepdf.String("Alex")
    pdf.docinfo["/Title"] = pikepdf.String("Risky")
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


# --- Inspect -------------------------------------------------------------------

def test_inspect_reports_risky_content():
    report = pdf_tools.inspect_pdf(make_risky_pdf())

    assert report["pages"] == 1
    assert report["has_xmp"] is True
    assert report["javascript_actions"] == 2  # open action and the link annotation's action
    assert report["embedded_files"] == 2  # name tree entry plus the file attachment annotation
    assert report["form_fields"] == 1
    assert report["metadata"]["Author"] == "Alex"


def test_inspect_plain_document_is_clean():
    report = pdf_tools.inspect_pdf(make_pdf(["Hello"]))
    assert report["javascript_actions"] == 0
    assert report["embedded_files"] == 0
    assert report["has_xmp"] is False


def test_inspect_rejects_non_pdf():
    with pytest.raises(pdf_tools.ToolError):
        pdf_tools.inspect_pdf(b"not a pdf at all")


# --- Clean ---------------------------------------------------------------------

def test_clean_removes_metadata_javascript_and_attachments():
    original = make_risky_pdf()

    output, report = pdf_tools.clean_pdf(original)

    after = pdf_tools.inspect_pdf(output)
    assert after["javascript_actions"] == 0
    assert after["embedded_files"] == 0
    assert after["has_xmp"] is False
    assert after["metadata"] == {}
    assert after["pages"] == 1
    assert after["form_fields"] == 1, "form fields are not removed by cleaning"
    assert report["size_before"] == len(original)
    assert report["size_after"] == len(output)
    assert any("JavaScript" in item for item in report["removed"])
    assert any("embedded" in item for item in report["removed"])


def test_clean_options_are_respected():
    original = make_risky_pdf()

    output, _ = pdf_tools.clean_pdf(
        original, strip_metadata=False, strip_javascript=False, strip_embedded_files=False, compress=False
    )

    after = pdf_tools.inspect_pdf(output)
    assert after["javascript_actions"] == 2
    assert after["embedded_files"] == 2
    assert after["metadata"]["Author"] == "Alex"


def test_clean_keeps_text_searchable():
    output, _ = pdf_tools.clean_pdf(make_risky_pdf())
    assert pdf_tools.search_pdf(output, "Secret Name")["count"] == 1


# --- Search --------------------------------------------------------------------

def test_search_returns_pages_and_boxes():
    data = make_pdf(["Jane Doe filed", "Nothing here", "Jane Doe again"])

    result = pdf_tools.search_pdf(data, "Jane Doe")

    assert result["count"] == 2
    assert [hit["page"] for hit in result["hits"]] == [1, 3]
    first = result["hits"][0]["box"]
    assert first[0] == pytest.approx(40, abs=2)
    assert first[2] > first[0] + 40
    assert first[3] > first[1] > 0
    assert result["truncated"] is False


def test_search_is_case_sensitive_and_handles_missing():
    data = make_pdf(["Jane Doe"])
    assert pdf_tools.search_pdf(data, "jane doe")["count"] == 0
    assert pdf_tools.search_pdf(data, "Nobody")["hits"] == []


def test_rotated_page_is_searched_and_replaced():
    """Rotated text comes out one character per line in pdfminer, so matching ignores spacing."""
    pdf = pikepdf.open(io.BytesIO(make_pdf(["Jane Doe filed"])))
    pdf.pages[0].obj["/Rotate"] = 90
    buffer = io.BytesIO()
    pdf.save(buffer)
    rotated = buffer.getvalue()

    assert pdf_tools.search_pdf(rotated, "Jane Doe")["count"] == 1

    import replace_name

    output, report = replace_name.replace_name(rotated, "Jane Doe", "Alex")
    assert report.replacements == 1
    assert pdf_tools.search_pdf(output, "Alex")["count"] == 1
    assert pdf_tools.search_pdf(output, "Jane Doe")["count"] == 0


def test_search_rejects_empty_text():
    with pytest.raises(formats.InvalidInput):
        pdf_tools.search_pdf(make_pdf(["x"]), "")


# --- API -----------------------------------------------------------------------

def upload_pdf(data: bytes):
    return client.post("/api/v1/uploads", files={"file": ("doc.pdf", data, "application/pdf")}).json()


def test_api_inspect_search_clean_flow():
    info = upload_pdf(make_risky_pdf())
    uploads = f"/api/v1/uploads/{info['id']}"

    inspected = client.get(f"{uploads}/inspect").json()
    assert inspected["javascript_actions"] == 2
    assert inspected["size"] == info["size"]

    found = client.post(f"{uploads}/search", json={"find": "Secret Name"}).json()
    assert found["count"] == 1 and found["hits"][0]["page"] == 1

    cleaned = client.post(f"{uploads}/clean", json={}).json()
    assert cleaned["download_url"] == f"{uploads}/download?version=result"
    assert "JavaScript" in " ".join(cleaned["removed"])

    downloaded = client.get(cleaned["download_url"])
    assert downloaded.status_code == 200
    assert pdf_tools.inspect_pdf(downloaded.content)["javascript_actions"] == 0


def test_api_tools_reject_non_pdf():
    info = client.post(
        "/api/v1/uploads", files={"file": ("notes.txt", b"plain text", "text/plain")}
    ).json()
    uploads = f"/api/v1/uploads/{info['id']}"

    assert client.get(f"{uploads}/inspect").status_code == 400
    assert client.post(f"{uploads}/search", json={"find": "x"}).status_code == 400
    assert client.post(f"{uploads}/clean", json={}).status_code == 400


def test_api_search_empty_text_is_rejected():
    info = upload_pdf(make_pdf(["x"]))
    assert client.post(f"/api/v1/uploads/{info['id']}/search", json={"find": ""}).status_code == 400


def test_api_pages_are_served():
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code in (302, 307)
    assert redirect.headers["location"] == "/app/"
    assert client.get("/find-replace", follow_redirects=False).headers["location"] == "/app/"


@pytest.mark.skipif(not SAMPLE.exists(), reason="samples/sample_before.pdf is not present")
def test_api_clean_sample_keeps_pages():
    info = upload_pdf(SAMPLE.read_bytes())
    cleaned = client.post(f"/api/v1/uploads/{info['id']}/clean", json={"compress": True}).json()
    assert cleaned["pages"] == 1
    assert client.get(cleaned["download_url"]).content.startswith(b"%PDF-")
