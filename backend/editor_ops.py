"""Editor options that the browser editor does with pdf-lib, done here with pikepdf.

The MCP server uses these so an AI client can run the same page, annotation and form
tools on a file without the browser. Every function takes PDF bytes and returns new PDF
bytes; the input is never changed. Pages are numbered from 1. Boxes are [x0, y0, x1, y1]
in PDF points with the origin at the bottom left, the same boxes the occurrence tools return.
"""

from __future__ import annotations

import io
import math

import pikepdf
from pdfminer.encodingdb import EncodingDB
from pdfminer.fontmetrics import FONT_METRICS
from pdfminer.high_level import extract_text
from pikepdf.form import CheckboxField, DefaultAppearanceStreamGenerator, Form, TextField

import replace_name

# Values match the browser editor (PdfEditor.jsx).
TEXT_NOTE_SIZE = 12
TEXT_NOTE_RGB = (0.12, 0.3, 0.6)
HIGHLIGHT_RGB = (1.0, 0.93, 0.3)
HIGHLIGHT_OPACITY = 0.4
# A match box runs from 25% of the font size below the baseline to 85% above it (replace_name).
BELOW_BASELINE = 0.25
ABOVE_BASELINE = 0.85
# Cover only shrinks a longer replacement to fit the original width, down to this share.
MIN_COVER_SCALE = 0.6
MIN_BOX_SIZE = 2.0


class EditError(ValueError):
    """The edit cannot be made. The message is safe to show to users."""


def _open(data: bytes) -> pikepdf.Pdf:
    if not data.startswith(b"%PDF-"):
        raise EditError("the file is not a PDF")
    try:
        return pikepdf.open(io.BytesIO(data))
    except pikepdf.PdfError as exc:
        raise EditError("the file could not be read as a PDF") from exc


def _save(pdf: pikepdf.Pdf) -> bytes:
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def _page(pdf: pikepdf.Pdf, number: int) -> pikepdf.Page:
    count = len(pdf.pages)
    if not 1 <= number <= count:
        raise EditError(f"page {number} does not exist; the document has {count} page{'s' if count != 1 else ''}")
    return pdf.pages[number - 1]


def _rotation(page: pikepdf.Page) -> int:
    """The page's /Rotate, read from the page or from the page tree it inherits from."""
    node = page.obj
    while node is not None:
        if "/Rotate" in node:
            return int(node["/Rotate"]) % 360
        node = node.get("/Parent")
    return 0


def _box(box: list[float]) -> tuple[float, float, float, float]:
    if len(box) != 4 or not all(math.isfinite(v) for v in box):
        raise EditError("a box needs four numbers: x0, y0, x1, y1")
    x0, x1 = sorted((float(box[0]), float(box[2])))
    y0, y1 = sorted((float(box[1]), float(box[3])))
    if x1 - x0 < MIN_BOX_SIZE or y1 - y0 < MIN_BOX_SIZE:
        raise EditError("the box is too small to draw")
    return x0, y0, x1, y1


def _text_operand(text: str) -> bytes:
    """The text as a PDF string in WinAnsi, the encoding of the standard Helvetica font."""
    if not text:
        raise EditError("the text is empty")
    if len(text) > replace_name.MAX_TEXT_LENGTH:
        raise EditError(f"texts may be at most {replace_name.MAX_TEXT_LENGTH} characters")
    if any(ord(ch) < 32 for ch in text):
        raise EditError("the text must be on one line")
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise EditError("this text uses characters the PDF's standard fonts can't write") from exc
    escaped = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    return b"(" + escaped + b")"


def _helvetica(pdf: pikepdf.Pdf) -> pikepdf.Dictionary:
    return pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name.Font,
            "/Subtype": pikepdf.Name.Type1,
            "/BaseFont": pikepdf.Name.Helvetica,
            "/Encoding": pikepdf.Name.WinAnsiEncoding,
        }
    )


def _text_width(data: bytes, size: float) -> float:
    """Width of WinAnsi text set in Helvetica at `size` points."""
    widths = FONT_METRICS["Helvetica"][1]
    glyphs = EncodingDB.get_encoding("WinAnsiEncoding")
    return sum(float(widths.get(glyphs.get(code), 0.0)) for code in data) * size / 1000


def _add_content(pdf: pikepdf.Pdf, page: pikepdf.Page, content: bytes) -> None:
    """Draw on top of the page. The content is wrapped in q/Q so the page's own state is untouched."""
    page.contents_add(pdf.make_stream(b"q\n" + content + b"\nQ\n"), prepend=False)


def _page_text(data: bytes, number: int) -> str:
    return extract_text(io.BytesIO(data), page_numbers=[number - 1])


def _verify_text(data: bytes, number: int, text: str) -> None:
    """The edit is only returned if the new text can be read back from the page."""
    if replace_name.compact_text(text) not in replace_name.compact_text(_page_text(data, number)):
        raise EditError("verification failed, no file returned: the new text was not found on the page")


def rotate_page(data: bytes, number: int, turns: int = 1) -> bytes:
    """Turn a page clockwise by 90 degrees per turn. Negative turns turn it counter-clockwise."""
    with _open(data) as pdf:
        page = _page(pdf, number)
        page.obj["/Rotate"] = (_rotation(page) + 90 * turns) % 360
        return _save(pdf)


def move_page(data: bytes, number: int, to_number: int) -> bytes:
    """Move a page so it ends up at position `to_number`. The other pages keep their order."""
    with _open(data) as pdf:
        page = _page(pdf, number)
        _page(pdf, to_number)
        del pdf.pages[number - 1]
        pdf.pages.insert(to_number - 1, page)
        return _save(pdf)


def delete_page(data: bytes, number: int) -> bytes:
    with _open(data) as pdf:
        _page(pdf, number)
        if len(pdf.pages) < 2:
            raise EditError("a PDF must keep at least one page")
        del pdf.pages[number - 1]
        return _save(pdf)


def append_pages(data: bytes, other: bytes) -> bytes:
    """Add every page of `other` to the end of the document."""
    with _open(data) as pdf, _open(other) as source:
        for page in source.pages:
            pdf.pages.append(page)
        return _save(pdf)


def add_text(data: bytes, number: int, x: float, y: float, text: str) -> bytes:
    """Write a short note in blue, 12 pt Helvetica, with its baseline at (x, y)."""
    operand = _text_operand(text)
    with _open(data) as pdf:
        page = _page(pdf, number)
        font = page.add_resource(_helvetica(pdf), pikepdf.Name.Font, prefix="F")
        r, g, b = TEXT_NOTE_RGB
        head = f"{r} {g} {b} rg BT {font} {TEXT_NOTE_SIZE} Tf 1 0 0 1 {x:.3f} {y:.3f} Tm "
        _add_content(pdf, page, head.encode("ascii") + operand + b" Tj ET")
        output = _save(pdf)
    _verify_text(output, number, text)
    return output


def highlight(data: bytes, number: int, box: list[float]) -> bytes:
    """A translucent yellow rectangle over the box."""
    x0, y0, x1, y1 = _box(box)
    with _open(data) as pdf:
        page = _page(pdf, number)
        state = page.add_resource(
            pikepdf.Dictionary({"/Type": pikepdf.Name.ExtGState, "/ca": HIGHLIGHT_OPACITY, "/CA": HIGHLIGHT_OPACITY}),
            pikepdf.Name.ExtGState,
            prefix="GS",
        )
        r, g, b = HIGHLIGHT_RGB
        ops = f"{state} gs {r} {g} {b} rg {x0:.3f} {y0:.3f} {x1 - x0:.3f} {y1 - y0:.3f} re f"
        _add_content(pdf, page, ops.encode("ascii"))
        return _save(pdf)


def whiteout(data: bytes, number: int, box: list[float]) -> bytes:
    """A solid white rectangle over the box. It hides content but does not remove it."""
    x0, y0, x1, y1 = _box(box)
    with _open(data) as pdf:
        page = _page(pdf, number)
        ops = f"1 1 1 rg {x0:.3f} {y0:.3f} {x1 - x0:.3f} {y1 - y0:.3f} re f"
        _add_content(pdf, page, ops.encode("ascii"))
        return _save(pdf)


def cover_occurrence(data: bytes, find: str, replace: str, number: int, index: int) -> bytes:
    """Hide one match and write the new text over it. The original characters stay in the file.

    Same as the editor's Cover only mode. Page and index come from the occurrence tool.
    """
    if find == replace:
        raise EditError("the new text is the same as the old text")
    try:
        matches = replace_name.list_occurrences(data, find)
    except replace_name.ReplacementError as exc:
        raise EditError(str(exc)) from exc
    target = next((m for m in matches if m["page"] == number and m["index"] == index), None)
    if target is None:
        raise EditError("there is no match of that text at that position")

    x0, y0, x1, y1 = target["box"]
    width = x1 - x0
    size = (y1 - y0) / (BELOW_BASELINE + ABOVE_BASELINE)
    operand = _text_operand(replace) if replace else b""
    font_size = size
    if replace:
        natural = _text_width(replace.encode("cp1252"), size)
        if natural > width:
            font_size = size * max(MIN_COVER_SCALE, width / natural)

    with _open(data) as pdf:
        page = _page(pdf, number)
        if _rotation(page):
            raise EditError("cover only doesn't work on rotated pages yet; use pdf_replace_occurrence")
        ops = f"1 1 1 rg {x0 - 1:.3f} {y0 - 1:.3f} {width + 2:.3f} {y1 - y0 + 2:.3f} re f".encode("ascii")
        if replace:
            font = page.add_resource(_helvetica(pdf), pikepdf.Name.Font, prefix="F")
            baseline = y0 + BELOW_BASELINE * size
            text_ops = f"\n0 g BT {font} {font_size:.3f} Tf 1 0 0 1 {x0:.3f} {baseline:.3f} Tm ".encode("ascii")
            ops += text_ops + operand + b" Tj ET"
        _add_content(pdf, page, ops)
        output = _save(pdf)
    if replace:
        _verify_text(output, number, replace)
    return output


def list_form_fields(data: bytes) -> list[dict]:
    """Text fields and checkboxes, the two kinds the editor's Form panel shows."""
    with _open(data) as pdf:
        if "/AcroForm" not in pdf.Root:
            return []
        found: list[dict] = []
        for name, field in Form(pdf).items():
            if isinstance(field, TextField):
                found.append({"name": name, "kind": "text", "value": field.value or ""})
            elif isinstance(field, CheckboxField):
                found.append({"name": name, "kind": "checkbox", "value": bool(field.checked)})
        return found


def fill_form_fields(data: bytes, values: dict[str, str | bool]) -> bytes:
    """Set text fields to strings and checkboxes to true or false, and rebuild their appearance."""
    if not values:
        raise EditError("no field values were given")
    with _open(data) as pdf:
        if "/AcroForm" not in pdf.Root:
            raise EditError("this PDF has no form fields")
        fields = dict(Form(pdf, generate_appearances=DefaultAppearanceStreamGenerator).items())
        unknown = sorted(name for name in values if name not in fields)
        if unknown:
            raise EditError("no such form field: " + ", ".join(unknown))
        for name, value in values.items():
            field = fields[name]
            if isinstance(field, TextField):
                if not isinstance(value, str):
                    raise EditError(f"{name} is a text field and needs a string")
                field.value = value
            elif isinstance(field, CheckboxField):
                if not isinstance(value, bool):
                    raise EditError(f"{name} is a checkbox and needs true or false")
                field.checked = value
            else:
                raise EditError(f"{name} is not a text field or a checkbox")
        return _save(pdf)
