"""MCP server for the PDF editor: the editor's options as tools for an AI client.

Run it from the backend folder. The client starts it and talks over stdio, so no port is opened:

    .venv/bin/python mcp_server.py

Each tool takes an absolute path to a file on this computer. A tool that changes a PDF writes
a new file and leaves the input as it was: by default "<name>-edited.pdf" beside the input,
or "-edited-2.pdf" and so on if that name is taken. Pass output_path to choose the name, and
pass a tool's output_path to the next call to chain edits.

Coordinates are PDF points with the origin at the bottom left of the page. Boxes are
[x0, y0, x1, y1]. pdf_find and pdf_occurrences return boxes in this form.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import editor_ops
import formats
import pdf_tools
import replace_name

# Same limit as the web API (main.MAX_UPLOAD_BYTES).
MAX_FILE_BYTES = 25 * 1024 * 1024
EXPECTED_ERRORS = (editor_ops.EditError, formats.FormatError, replace_name.ReplacementError)

mcp = MCPServer(
    "PDF Editor",
    instructions=(
        "Tools for the PDF editor. Use absolute paths. A change writes a new file and keeps the input. "
        "Call pdf_occurrences before the in-place edit tools: it gives the page, index and box of each match."
    ),
)


def _tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register a tool. Expected failures reach the client with their message; anything else is a crash."""

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return mcp.tool()(run)


def _source(path: str) -> tuple[Path, bytes]:
    """The input's resolved path and bytes, checked to be a PDF of an allowed size."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ToolError(f"no file at {path}")
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ToolError("the file is larger than 25 MB")
    data = source.read_bytes()
    if not data.startswith(b"%PDF-"):
        raise ToolError(f"{path} is not a PDF")
    return source, data


def _target(source: Path, output_path: str | None) -> Path:
    """Where the result goes. It is never the input, and never an existing file."""
    if output_path is None:
        target = source.with_name(f"{source.stem}-edited.pdf")
        number = 2
        while target.exists():
            target = source.with_name(f"{source.stem}-edited-{number}.pdf")
            number += 1
        return target
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".pdf":
        raise ToolError("output_path must end in .pdf")
    if target == source:
        raise ToolError("output_path must not be the input file")
    if target.exists():
        raise ToolError(f"{target} already exists; choose another output_path")
    return target


def _write(target: Path, data: bytes) -> str:
    with target.open("xb") as handle:  # "xb" fails rather than overwrite a file that appeared meanwhile
        handle.write(data)
    return str(target)


def _edit(path: str, output_path: str | None, change: Callable[[bytes], bytes]) -> dict:
    source, data = _source(path)
    target = _target(source, output_path)
    edited = change(data)
    _write(target, edited)
    return {"output_path": str(target), "pages": formats.HANDLERS["pdf"].info(edited)["pages"]}


@_tool
def pdf_info(path: str) -> dict:
    """Info panel: pages, size, PDF version, encryption, metadata, JavaScript, attachments, form fields and annotations. Changes nothing."""
    _, data = _source(path)
    return {**pdf_tools.inspect_pdf(data), "size": len(data)}


@_tool
def pdf_find(path: str, text: str) -> dict:
    """Find: count text (case-sensitive) and return each hit with its page, box and context. Changes nothing."""
    _, data = _source(path)
    return pdf_tools.search_pdf(data, text)


@_tool
def pdf_occurrences(path: str, text: str) -> dict:
    """Every match of text, page by page, with the page, index and box that the edit tools take. Changes nothing."""
    _, data = _source(path)
    found = replace_name.list_occurrences(data, text)
    return {"count": len(found), "occurrences": found}


@_tool
def pdf_form_fields(path: str) -> dict:
    """Form fields panel: text fields and checkboxes with their current values. Changes nothing."""
    _, data = _source(path)
    fields = editor_ops.list_form_fields(data)
    return {"count": len(fields), "fields": fields}


@_tool
def pdf_render_page(path: str, page: int, scale: float = 1.5) -> Image:
    """Render one page as a PNG so you can check it visually. Page numbers start at 1. Changes nothing."""
    if not 0.25 <= scale <= 4:
        raise ToolError("scale must be between 0.25 and 4")
    _, data = _source(path)
    return Image(data=formats.HANDLERS["pdf"].render_png(data, page, scale), format="png")


@_tool
def pdf_replace(path: str, find: str, replace: str, output_path: str | None = None) -> dict:
    """Replace: replace text everywhere in the document. The text must sit inside one text run. Writes a new file."""
    source, data = _source(path)
    target = _target(source, output_path)
    result = formats.HANDLERS["pdf"].replace(data, find, replace)
    _write(target, result.data)
    return {
        "output_path": str(target),
        "replacements": result.replacements,
        "remaining": result.remaining,
        "pages_changed": result.touched,
        "excerpts": [excerpt.as_dict() for excerpt in result.excerpts],
    }


@_tool
def pdf_replace_occurrence(
    path: str, find: str, replace: str, page: int, index: int, output_path: str | None = None
) -> dict:
    """Edit text (Permanent): replace one match, or remove it when replace is empty. The original text is removed from the file. Other matches stay."""
    source, data = _source(path)
    target = _target(source, output_path)
    edited, report = replace_name.replace_occurrence(data, find, replace, page, index)
    _write(target, edited)
    return {"output_path": str(target), **report.as_dict()}


@_tool
def pdf_cover_occurrence(
    path: str, find: str, replace: str, page: int, index: int, output_path: str | None = None
) -> dict:
    """Edit text (Cover only): hide one match under a white box and write the new text over it. The original characters stay in the file, so copy and search still find them. Empty replace only hides it. Not for rotated pages."""
    return _edit(path, output_path, lambda data: editor_ops.cover_occurrence(data, find, replace, page, index))


@_tool
def pdf_add_text(path: str, page: int, x: float, y: float, text: str, output_path: str | None = None) -> dict:
    """Text: add a one-line note in blue, 12 pt Helvetica, with its baseline at (x, y) in PDF points."""
    return _edit(path, output_path, lambda data: editor_ops.add_text(data, page, x, y, text))


@_tool
def pdf_highlight(
    path: str, page: int, x0: float, y0: float, x1: float, y1: float, output_path: str | None = None
) -> dict:
    """Highlight: a translucent yellow rectangle over the box."""
    return _edit(path, output_path, lambda data: editor_ops.highlight(data, page, [x0, y0, x1, y1]))


@_tool
def pdf_whiteout(
    path: str, page: int, x0: float, y0: float, x1: float, y1: float, output_path: str | None = None
) -> dict:
    """Whiteout: a solid white rectangle over the box. The content underneath stays in the file."""
    return _edit(path, output_path, lambda data: editor_ops.whiteout(data, page, [x0, y0, x1, y1]))


@_tool
def pdf_rotate_page(path: str, page: int, turns: int = 1, output_path: str | None = None) -> dict:
    """Rotate: turn a page 90 degrees clockwise per turn. turns=-1 turns it counter-clockwise."""
    return _edit(path, output_path, lambda data: editor_ops.rotate_page(data, page, turns))


@_tool
def pdf_move_page(path: str, page: int, to_page: int, output_path: str | None = None) -> dict:
    """Move: move a page so it ends up at position to_page. The other pages keep their order."""
    return _edit(path, output_path, lambda data: editor_ops.move_page(data, page, to_page))


@_tool
def pdf_delete_page(path: str, page: int, output_path: str | None = None) -> dict:
    """Delete: remove a page. A PDF keeps at least one page."""
    return _edit(path, output_path, lambda data: editor_ops.delete_page(data, page))


@_tool
def pdf_append_pages(path: str, other_path: str, output_path: str | None = None) -> dict:
    """Add pages: add every page of another PDF to the end of this one."""
    _, other = _source(other_path)
    return _edit(path, output_path, lambda data: editor_ops.append_pages(data, other))


@_tool
def pdf_fill_form(path: str, values: dict[str, str | bool], output_path: str | None = None) -> dict:
    """Form fields: set text fields (strings) and checkboxes (true or false) by field name. Get the names from pdf_form_fields."""
    return _edit(path, output_path, lambda data: editor_ops.fill_form_fields(data, values))


@_tool
def pdf_clean(
    path: str,
    strip_metadata: bool = True,
    strip_javascript: bool = True,
    strip_embedded_files: bool = True,
    compress: bool = True,
    output_path: str | None = None,
) -> dict:
    """Clean: remove document info and XMP metadata, JavaScript and embedded files, and optionally compress. Form fields are kept. The result is checked before it is returned."""
    source, data = _source(path)
    target = _target(source, output_path)
    output, report = pdf_tools.clean_pdf(
        data,
        strip_metadata=strip_metadata,
        strip_javascript=strip_javascript,
        strip_embedded_files=strip_embedded_files,
        compress=compress,
    )
    _write(target, output)
    return {"output_path": str(target), **report}


if __name__ == "__main__":
    mcp.run()
