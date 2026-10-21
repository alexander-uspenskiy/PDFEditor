"""Small PDFs and readers shared by the engine and MCP tests."""

from __future__ import annotations

import io

import pikepdf
import pypdfium2 as pdfium
from pdfminer.high_level import extract_text


def make_pdf(pages: list[str], size=(400, 300), rotate: int | None = None) -> bytes:
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
        if rotate is not None:
            page.obj["/Rotate"] = rotate
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def make_form_pdf() -> bytes:
    """One page with a text field called `name` and a checkbox called `agree`."""
    pdf = pikepdf.new()
    helv = pdf.make_indirect(
        pikepdf.Dictionary({"/Type": pikepdf.Name.Font, "/Subtype": pikepdf.Name.Type1, "/BaseFont": pikepdf.Name.Helvetica})
    )
    resources = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/Helv": helv})})
    page = pdf.add_blank_page(page_size=(300, 200))
    page.obj["/Resources"] = resources
    appearance = pdf.make_stream(
        b"0 0 14 14 re S",
        pikepdf.Dictionary({"/Type": pikepdf.Name.XObject, "/Subtype": pikepdf.Name.Form, "/BBox": [0, 0, 14, 14]}),
    )
    text_field = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name.Annot,
                "/Subtype": pikepdf.Name.Widget,
                "/FT": pikepdf.Name.Tx,
                "/T": pikepdf.String("name"),
                "/Rect": [20, 150, 200, 170],
                "/DA": pikepdf.String("/Helv 12 Tf 0 g"),
                "/P": page.obj,
            }
        )
    )
    checkbox = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name.Annot,
                "/Subtype": pikepdf.Name.Widget,
                "/FT": pikepdf.Name.Btn,
                "/T": pikepdf.String("agree"),
                "/Rect": [20, 100, 34, 114],
                "/V": pikepdf.Name.Off,
                "/AS": pikepdf.Name.Off,
                "/DA": pikepdf.String("/Helv 12 Tf 0 g"),
                "/AP": pikepdf.Dictionary({"/N": pikepdf.Dictionary({"/Yes": appearance, "/Off": appearance})}),
                "/P": page.obj,
            }
        )
    )
    page.obj["/Annots"] = pikepdf.Array([text_field, checkbox])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary(
        {"/Fields": pikepdf.Array([text_field, checkbox]), "/DR": resources, "/DA": pikepdf.String("/Helv 12 Tf 0 g")}
    )
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def text_of(data: bytes, page: int | None = None) -> str:
    """The text layer of the whole document, or of one page (1-based)."""
    numbers = None if page is None else [page - 1]
    return extract_text(io.BytesIO(data), page_numbers=numbers)


def render(data: bytes, page: int = 1, scale: float = 1.0, forms: bool = False):
    """The page as a PIL image, for checking what is drawn. forms=True also draws form fields, as a viewer does."""
    document = pdfium.PdfDocument(data)
    try:
        if forms:
            document.init_forms()
        return document[page - 1].render(scale=scale, may_draw_forms=forms).to_pil().convert("RGB")
    finally:
        document.close()


def count_dark(image, box: tuple[int, int, int, int]) -> int:
    """Pixels in the rectangle (left, top, right, bottom) that are dark, for seeing ink on the page."""
    left, top, right, bottom = box
    return sum(
        1
        for x in range(left, right)
        for y in range(top, bottom)
        if sum(image.getpixel((x, y))) < 300
    )
