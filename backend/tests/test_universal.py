"""Tests for the universal find-and-replace service: format handlers and the /api/v1 flow.

Run from the backend folder:  python -m pytest tests
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import formats
from main import app

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "sample_before.pdf"
W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    '<w:p w:name="Jane &amp; Doe"><w:r><w:t>Hello Jane &amp; Doe</w:t></w:r></w:p>'
    "<w:p><w:r><w:t>Jane &amp; Doe again</w:t></w:r></w:p>"
    "</w:body></w:document>"
)


def make_package(parts: dict[str, str], markers: tuple[str, ...]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        for name in markers:
            package.writestr(name, "<root/>")
        for name, content in parts.items():
            package.writestr(name, content)
    return buffer.getvalue()


def docx() -> bytes:
    return make_package({"word/document.xml": DOCUMENT_XML}, ())


def xlsx() -> bytes:
    shared = '<?xml version="1.0"?><sst xmlns="x"><si><t>Jane Doe</t></si><si><t>Other</t></si></sst>'
    return make_package(
        {"xl/sharedStrings.xml": shared},
        ("xl/workbook.xml",),
    )


def word_texts(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        root = ET.fromstring(package.read("word/document.xml"))
    return [node.text or "" for node in root.iter(f"{W_NS}t")]


# --- Detection -----------------------------------------------------------------

def test_detection_is_by_content():
    assert formats.detect(b"%PDF-1.7 rest") == "pdf"
    assert formats.detect(docx()) == "ooxml"
    assert formats.detect(xlsx()) == "ooxml"
    assert formats.detect("plain text".encode()) == "text"
    assert formats.detect(b"\xef\xbb\xbfwith bom") == "text"


def test_binary_data_is_rejected():
    with pytest.raises(formats.UnsupportedFormat):
        formats.detect(bytes(range(256)) * 4)


def test_unknown_zip_is_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("readme.bin", b"\x00\x01")
    with pytest.raises(formats.UnsupportedFormat):
        formats.detect(buffer.getvalue())


# --- Plain text ----------------------------------------------------------------

def test_text_replace_counts_lines_and_keeps_newlines():
    data = b"Dear Jane Doe,\nJane Doe is here.\n"
    result = formats.HANDLERS["text"].replace(data, "Jane Doe", "Alex")

    assert result.replacements == 2
    assert result.remaining == 0
    assert result.data == b"Dear Alex,\nAlex is here.\n"
    assert result.excerpts[0].location == "line 1"
    assert result.excerpts[1].location == "line 2"


def test_text_bom_and_cp1252_are_preserved():
    with_bom = formats.HANDLERS["text"].replace(b"\xef\xbb\xbfJane", "Jane", "Alex")
    assert with_bom.data == b"\xef\xbb\xbfAlex"

    legacy = "Café Jane".encode("cp1252")
    result = formats.HANDLERS["text"].replace(legacy, "Jane", "Zoë")
    assert result.data == "Café Zoë".encode("cp1252")


def test_text_replacement_outside_encoding_is_rejected():
    with pytest.raises(formats.InvalidInput):
        formats.HANDLERS["text"].replace("Café Jane".encode("cp1252"), "Jane", "中文")


def test_text_find_counts_without_changing_anything():
    result = formats.HANDLERS["text"].find(b"a Jane b\nJane c", "Jane")
    assert result.count == 2
    assert result.excerpts[0].after is None


def test_missing_text_raises_not_found():
    with pytest.raises(formats.NotFound):
        formats.HANDLERS["text"].replace(b"nothing here", "Jane", "Alex")


# --- Office packages -----------------------------------------------------------

def test_docx_replaces_text_nodes_only():
    result = formats.HANDLERS["ooxml"].replace(docx(), "Jane & Doe", "Alex <Admin>")

    # The attribute value "Jane &amp; Doe" is not text, so only the two text nodes count.
    assert result.replacements == 2
    assert result.remaining == 0
    assert result.touched == 1
    assert word_texts(result.data) == ["Hello Alex <Admin>", "Alex <Admin> again"]
    with zipfile.ZipFile(io.BytesIO(result.data)) as package:
        xml = package.read("word/document.xml").decode("utf-8")
    assert 'w:name="Jane &amp; Doe"' in xml, "attribute values must not change"


def test_xlsx_shared_strings_are_replaced():
    result = formats.HANDLERS["ooxml"].replace(xlsx(), "Jane Doe", "Alex")

    assert result.replacements == 1
    with zipfile.ZipFile(io.BytesIO(result.data)) as package:
        assert b"<t>Alex</t>" in package.read("xl/sharedStrings.xml")


def test_ooxml_find_reports_excerpts_with_part_names():
    result = formats.HANDLERS["ooxml"].find(docx(), "Jane & Doe")
    assert result.count == 2
    assert result.excerpts[0].location == "word/document.xml"


def test_ooxml_output_is_a_valid_package():
    result = formats.HANDLERS["ooxml"].replace(docx(), "Jane", "Al")
    with zipfile.ZipFile(io.BytesIO(result.data)) as package:
        assert package.testzip() is None
        assert "[Content_Types].xml" in package.namelist()


# --- PDF -----------------------------------------------------------------------

@pytest.mark.skipif(not SAMPLE.exists(), reason="samples/sample_before.pdf is not present")
def test_pdf_handler_find_and_replace():
    data = SAMPLE.read_bytes()
    found = formats.HANDLERS["pdf"].find(data, "Jane Doe")
    assert found.count == 1

    result = formats.HANDLERS["pdf"].replace(data, "Jane Doe", "Alex")
    assert result.replacements == 1
    assert result.remaining == 0
    assert result.touched_label == "pages changed"


# --- API flow ------------------------------------------------------------------

client = TestClient(app)


def upload(name: str, data: bytes):
    return client.post("/api/v1/uploads", files={"file": (name, data, "application/octet-stream")})


def test_api_text_flow_find_replace_download():
    created = upload("notes.txt", b"Jane Doe met Jane Doe.")
    assert created.status_code == 201
    info = created.json()
    assert info["format"] == "text"

    found = client.post(f"/api/v1/uploads/{info['id']}/find", json={"find": "Jane Doe"}).json()
    assert found["count"] == 2

    before_result = client.get(f"/api/v1/uploads/{info['id']}/download?version=result")
    assert before_result.status_code == 404

    replaced = client.post(
        f"/api/v1/uploads/{info['id']}/replace", json={"find": "Jane Doe", "replace": "Alex"}
    )
    assert replaced.status_code == 200
    body = replaced.json()
    assert body["replacements"] == 2
    assert body["remaining"] == 0
    assert body["render_url"] is None

    download = client.get(f"/api/v1/uploads/{info['id']}/download?version=result")
    assert download.content == b"Alex met Alex."
    assert 'filename="notes-edited.txt"' in download.headers["content-disposition"]

    original = client.get(f"/api/v1/uploads/{info['id']}/download?version=original")
    assert original.content == b"Jane Doe met Jane Doe."


def test_api_office_flow_returns_excerpts():
    info = upload("letter.docx", docx()).json()
    body = client.post(
        f"/api/v1/uploads/{info['id']}/replace", json={"find": "Jane & Doe", "replace": "Alex"}
    ).json()
    assert body["replacements"] == 2
    assert body["touched_label"] == "parts changed"
    assert body["excerpts"][0]["after"].count("Alex") >= 1


def test_api_pdf_renders_pages():
    if not SAMPLE.exists():
        pytest.skip("samples/sample_before.pdf is not present")
    info = upload("sample.pdf", SAMPLE.read_bytes()).json()
    assert info["details"]["pages"] == 1

    body = client.post(
        f"/api/v1/uploads/{info['id']}/replace", json={"find": "Jane Doe", "replace": "Alex"}
    ).json()
    assert body["render_url"] == f"/api/v1/uploads/{info['id']}/render"

    for version in ("original", "result"):
        png = client.get(f"/api/v1/uploads/{info['id']}/render?version={version}&page=1")
        assert png.status_code == 200
        assert png.content.startswith(b"\x89PNG")

    assert client.get(f"/api/v1/uploads/{info['id']}/render?page=9").status_code == 404


def test_api_error_codes():
    info = upload("notes.txt", b"hello world").json()
    uploads = f"/api/v1/uploads/{info['id']}"

    assert client.post(f"{uploads}/find", json={"find": ""}).status_code == 400
    assert client.post(f"{uploads}/find", json={"find": "missing"}).json()["count"] == 0
    assert (
        client.post(f"{uploads}/replace", json={"find": "missing", "replace": "x"}).status_code == 404
    )
    assert (
        client.post(f"{uploads}/replace", json={"find": "hello", "replace": "hello"}).status_code
        == 400
    )
    assert client.post("/api/v1/uploads/unknown/find", json={"find": "x"}).status_code == 404
    assert upload("blob.bin", bytes(range(256)) * 4).status_code == 415
    assert upload("empty.txt", b"").status_code == 400


def test_api_formats_and_ui():
    assert client.get("/api/v1/formats").status_code == 404

    page = client.get("/find-replace", follow_redirects=False)
    assert page.headers["location"] == "/app/"


def test_legacy_pdf_endpoint_still_works():
    if not SAMPLE.exists():
        pytest.skip("samples/sample_before.pdf is not present")
    response = client.post(
        "/api/replace-name",
        files={"file": ("sample.pdf", SAMPLE.read_bytes(), "application/pdf")},
        data={"old_name": "Jane Doe", "new_name": "Alex"},
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
