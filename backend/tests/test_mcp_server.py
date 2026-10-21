"""Tests for the MCP server: the tools are registered, write new files, and report errors clearly.

Run from the backend folder:  python -m pytest tests
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import mcp_server
import replace_name
from pdf_fixtures import make_form_pdf, make_pdf, text_of

EXPECTED_TOOLS = {
    "pdf_info",
    "pdf_find",
    "pdf_occurrences",
    "pdf_form_fields",
    "pdf_render_page",
    "pdf_replace",
    "pdf_replace_occurrence",
    "pdf_cover_occurrence",
    "pdf_add_text",
    "pdf_highlight",
    "pdf_whiteout",
    "pdf_rotate_page",
    "pdf_move_page",
    "pdf_delete_page",
    "pdf_append_pages",
    "pdf_fill_form",
    "pdf_clean",
}


@pytest.fixture
def pdf_path(tmp_path: Path) -> Path:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(make_pdf(["Hello world", "Second page"]))
    return path


def test_every_editor_option_is_a_tool():
    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert names == EXPECTED_TOOLS


def test_an_edit_writes_a_new_file_and_leaves_the_input_alone(pdf_path: Path):
    original = pdf_path.read_bytes()
    result = mcp_server.pdf_rotate_page(str(pdf_path), 1)
    output = Path(result["output_path"])
    assert output == pdf_path.with_name("invoice-edited.pdf")
    assert output.read_bytes().startswith(b"%PDF-")
    assert pdf_path.read_bytes() == original


def test_repeated_edits_never_overwrite_earlier_results(pdf_path: Path):
    first = Path(mcp_server.pdf_rotate_page(str(pdf_path), 1)["output_path"])
    second = Path(mcp_server.pdf_rotate_page(str(pdf_path), 1)["output_path"])
    assert second.name == "invoice-edited-2.pdf"
    assert first.exists() and second.exists()


def test_an_explicit_output_path_is_never_overwritten(pdf_path: Path, tmp_path: Path):
    taken = tmp_path / "taken.pdf"
    taken.write_bytes(b"keep me")
    with pytest.raises(ToolError, match="already exists"):
        mcp_server.pdf_rotate_page(str(pdf_path), 1, output_path=str(taken))
    assert taken.read_bytes() == b"keep me"


def test_output_must_be_a_new_pdf_name(pdf_path: Path, tmp_path: Path):
    with pytest.raises(ToolError, match="must not be the input file"):
        mcp_server.pdf_rotate_page(str(pdf_path), 1, output_path=str(pdf_path))
    with pytest.raises(ToolError, match="must end in .pdf"):
        mcp_server.pdf_rotate_page(str(pdf_path), 1, output_path=str(tmp_path / "out.txt"))


def test_edits_chain_through_output_paths(pdf_path: Path):
    moved = mcp_server.pdf_move_page(str(pdf_path), 1, 2)
    # After the move, page 1 is "Second page" and page 2 is "Hello world". Rotate page 2.
    rotated = mcp_server.pdf_rotate_page(moved["output_path"], 2)
    assert rotated["pages"] == 2
    first_page = text_of(Path(rotated["output_path"]).read_bytes(), 1)
    assert replace_name.compact_text(first_page) == "Secondpage"


def test_expected_errors_reach_the_client_with_their_message(tmp_path: Path):
    single = tmp_path / "single.pdf"
    single.write_bytes(make_pdf(["Only"]))
    with pytest.raises(ToolError, match="at least one page"):
        mcp_server.pdf_delete_page(str(single), 1)

    text_file = tmp_path / "notes.pdf"
    text_file.write_text("not a pdf")
    with pytest.raises(ToolError, match="is not a PDF"):
        mcp_server.pdf_info(str(text_file))

    with pytest.raises(ToolError, match="no file at"):
        mcp_server.pdf_info(str(tmp_path / "missing.pdf"))


def test_reading_tools_report_without_changing_the_file(pdf_path: Path, tmp_path: Path):
    before = list(tmp_path.iterdir())
    assert mcp_server.pdf_info(str(pdf_path))["pages"] == 2
    assert mcp_server.pdf_find(str(pdf_path), "world")["count"] == 1
    occurrences = mcp_server.pdf_occurrences(str(pdf_path), "world")
    assert occurrences["count"] == 1
    assert occurrences["occurrences"][0]["page"] == 1
    assert list(tmp_path.iterdir()) == before


def test_render_page_returns_a_png(pdf_path: Path):
    image = mcp_server.pdf_render_page(str(pdf_path), 1)
    assert image.data.startswith(b"\x89PNG")
    with pytest.raises(ToolError, match="scale must be between"):
        mcp_server.pdf_render_page(str(pdf_path), 1, scale=10)


def test_permanent_edit_of_one_match(pdf_path: Path):
    result = mcp_server.pdf_replace_occurrence(str(pdf_path), "world", "there", 1, 0)
    output = Path(result["output_path"])
    assert result["other_copies"] == 0
    text = text_of(output.read_bytes())
    assert "there" in text and "world" not in text


def test_whole_document_replace(pdf_path: Path):
    result = mcp_server.pdf_replace(str(pdf_path), "world", "earth")
    assert result["replacements"] == 1
    assert result["remaining"] == 0
    assert "earth" in text_of(Path(result["output_path"]).read_bytes())


def test_cover_and_annotation_tools_write_files(pdf_path: Path):
    covered = mcp_server.pdf_cover_occurrence(str(pdf_path), "world", "planet", 1, 0)
    assert "planet" in text_of(Path(covered["output_path"]).read_bytes())

    noted = mcp_server.pdf_add_text(str(pdf_path), 1, 40, 100, "Reviewed")
    assert "Reviewed" in text_of(Path(noted["output_path"]).read_bytes())

    marked = mcp_server.pdf_highlight(str(pdf_path), 1, 40, 40, 200, 80)
    hidden = mcp_server.pdf_whiteout(str(pdf_path), 1, 40, 40, 200, 80)
    assert Path(marked["output_path"]).exists() and Path(hidden["output_path"]).exists()


def test_page_tools_and_append(pdf_path: Path, tmp_path: Path):
    other = tmp_path / "other.pdf"
    other.write_bytes(make_pdf(["Third", "Fourth"]))
    appended = mcp_server.pdf_append_pages(str(pdf_path), str(other))
    assert appended["pages"] == 4

    deleted = mcp_server.pdf_delete_page(appended["output_path"], 1)
    assert deleted["pages"] == 3


def test_form_tools_list_and_fill(tmp_path: Path):
    form = tmp_path / "form.pdf"
    form.write_bytes(make_form_pdf())
    listed = mcp_server.pdf_form_fields(str(form))
    assert listed["count"] == 2

    filled = mcp_server.pdf_fill_form(str(form), {"name": "Ada", "agree": True})
    values = {field["name"]: field["value"] for field in mcp_server.pdf_form_fields(filled["output_path"])["fields"]}
    assert values == {"name": "Ada", "agree": True}


def test_clean_tool_writes_a_pdf(pdf_path: Path):
    result = mcp_server.pdf_clean(str(pdf_path))
    assert result["output_path"].endswith("invoice-edited.pdf")
    assert Path(result["output_path"]).read_bytes().startswith(b"%PDF-")
