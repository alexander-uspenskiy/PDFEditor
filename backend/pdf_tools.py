"""PDF tools used by the editor: inspection, cleaning and text search with positions.

pikepdf works on the document structure (metadata, JavaScript, embedded files,
compression). pdfminer.six works on the text layout (where each match sits on the
page). Both read the same bytes the browser editor holds, so results line up with
what the editor displays.
"""

from __future__ import annotations

import io
import math

import pikepdf
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar, LTTextLine

from formats import FormatError, InvalidInput
from replace_name import compact_text

MAX_HITS = 500


class ToolError(FormatError):
    """A PDF tool could not complete. The message is safe to show to users."""


# --- Structure helpers ---------------------------------------------------------

def _is_js_action(action) -> bool:
    return isinstance(action, pikepdf.Dictionary) and str(action.get("/S", "")) == "/JavaScript"


def _name_tree_count(node) -> int:
    total = len(node["/Names"]) // 2 if "/Names" in node else 0
    for kid in node.get("/Kids", []):
        total += _name_tree_count(kid)
    return total


def _field_count(node) -> int:
    kids = node.get("/Kids")
    if kids:
        return sum(_field_count(kid) for kid in kids)
    return 1 if ("/T" in node or "/FT" in node) else 0


def _javascript_count(pdf) -> int:
    """Count JavaScript actions: document-level, open action, page and annotation actions."""
    count = 0
    root = pdf.Root
    names = root.get("/Names")
    if names is not None and "/JavaScript" in names:
        count += 1
    if _is_js_action(root.get("/OpenAction")):
        count += 1
    for page in pdf.pages:
        page_aa = page.obj.get("/AA")
        if page_aa is not None:
            count += sum(1 for value in page_aa.values() if _is_js_action(value))
        for annot in page.obj.get("/Annots", []):
            if _is_js_action(annot.get("/A")):
                count += 1
            annot_aa = annot.get("/AA")
            if annot_aa is not None:
                count += sum(1 for value in annot_aa.values() if _is_js_action(value))
    return count


def _embedded_file_count(pdf) -> int:
    count = 0
    names = pdf.Root.get("/Names")
    if names is not None and "/EmbeddedFiles" in names:
        count += _name_tree_count(names["/EmbeddedFiles"])
    for page in pdf.pages:
        for annot in page.obj.get("/Annots", []):
            if str(annot.get("/Subtype", "")) == "/FileAttachment":
                count += 1
    return count


def _remove_javascript(pdf) -> int:
    removed = 0
    root = pdf.Root
    names = root.get("/Names")
    if names is not None and "/JavaScript" in names:
        del names["/JavaScript"]
        removed += 1
    if _is_js_action(root.get("/OpenAction")):
        del root["/OpenAction"]
        removed += 1
    for page in pdf.pages:
        obj = page.obj
        page_aa = obj.get("/AA")
        if page_aa is not None:
            for key in list(page_aa.keys()):
                if _is_js_action(page_aa[key]):
                    del page_aa[key]
                    removed += 1
            if not len(page_aa):
                del obj["/AA"]
        for annot in obj.get("/Annots", []):
            if _is_js_action(annot.get("/A")):
                del annot["/A"]
                removed += 1
            annot_aa = annot.get("/AA")
            if annot_aa is not None:
                for key in list(annot_aa.keys()):
                    if _is_js_action(annot_aa[key]):
                        del annot_aa[key]
                        removed += 1
                if not len(annot_aa):
                    del annot["/AA"]
    return removed


def _remove_embedded_files(pdf) -> int:
    removed = 0
    names = pdf.Root.get("/Names")
    if names is not None and "/EmbeddedFiles" in names:
        removed += _name_tree_count(names["/EmbeddedFiles"])
        del names["/EmbeddedFiles"]
    for page in pdf.pages:
        kept = []
        for annot in page.obj.get("/Annots", []):
            if str(annot.get("/Subtype", "")) == "/FileAttachment":
                removed += 1
            else:
                kept.append(annot)
        if len(kept) != len(page.obj.get("/Annots", [])):
            page.obj["/Annots"] = pikepdf.Array(kept)
    return removed


def _value_text(value) -> str:
    """Text of a document-info value: UTF-16BE with a BOM, or PDFDocEncoding."""
    if isinstance(value, pikepdf.String):
        raw = bytes(value)
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be", errors="replace")
        return str(value)
    return str(value)


# --- Inspection ----------------------------------------------------------------

def inspect_pdf(data: bytes) -> dict:
    """Describe the document: pages, metadata, JavaScript, attachments and forms."""
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PdfError as exc:
        raise ToolError("the file could not be read as a PDF") from exc
    with pdf:
        first = pdf.pages[0].mediabox if len(pdf.pages) else [0, 0, 0, 0]
        acroform = pdf.Root.get("/AcroForm")
        fields = sum(_field_count(field) for field in acroform.get("/Fields", [])) if acroform is not None else 0
        return {
            "pages": len(pdf.pages),
            "pdf_version": pdf.pdf_version,
            "encrypted": bool(pdf.is_encrypted),
            "linearized": bool(pdf.is_linearized),
            "first_page_size": [round(float(first[2]) - float(first[0]), 2), round(float(first[3]) - float(first[1]), 2)],
            "metadata": {key[1:]: _value_text(value) for key, value in pdf.docinfo.items()},
            "has_xmp": "/Metadata" in pdf.Root,
            "javascript_actions": _javascript_count(pdf),
            "embedded_files": _embedded_file_count(pdf),
            "form_fields": fields,
            "annotations": sum(len(page.obj.get("/Annots", [])) for page in pdf.pages),
        }


# --- Cleaning ------------------------------------------------------------------

def clean_pdf(
    data: bytes,
    *,
    strip_metadata: bool = True,
    strip_javascript: bool = True,
    strip_embedded_files: bool = True,
    compress: bool = True,
) -> tuple[bytes, dict]:
    """Remove the selected extras and optionally compress. Returns (pdf bytes, report)."""
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PdfError as exc:
        raise ToolError("the file could not be read as a PDF") from exc

    removed: list[str] = []
    with pdf:
        pages = len(pdf.pages)
        if strip_metadata:
            if len(pdf.docinfo):
                removed.append("document info fields")
                for key in list(pdf.docinfo.keys()):
                    del pdf.docinfo[key]
            if "/Metadata" in pdf.Root:
                del pdf.Root["/Metadata"]
                removed.append("XMP metadata")
            thumbnails = 0
            for page in pdf.pages:
                for key in ("/Thumb", "/PieceInfo", "/LastModified", "/Metadata"):
                    if key in page.obj:
                        if key == "/Thumb":
                            thumbnails += 1
                        del page.obj[key]
            if "/PieceInfo" in pdf.Root:
                del pdf.Root["/PieceInfo"]
            if thumbnails:
                removed.append(f"{thumbnails} page thumbnail(s)")

        if strip_javascript:
            count = _remove_javascript(pdf)
            if count:
                removed.append(f"{count} JavaScript action(s)")

        if strip_embedded_files:
            count = _remove_embedded_files(pdf)
            if count:
                removed.append(f"{count} embedded file(s)")

        options: dict = {"compress_streams": True}
        if compress:
            options.update(
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
        else:
            options["object_stream_mode"] = pikepdf.ObjectStreamMode.disable
        buffer = io.BytesIO()
        pdf.save(buffer, **options)

    output = buffer.getvalue()
    _verify_clean(output, pages, strip_metadata, strip_javascript, strip_embedded_files)
    report = {
        "pages": pages,
        "size_before": len(data),
        "size_after": len(output),
        "removed": removed,
    }
    return output, report


def _verify_clean(output: bytes, pages: int, metadata: bool, javascript: bool, embedded: bool) -> None:
    try:
        check = pikepdf.open(io.BytesIO(output))
    except pikepdf.PdfError as exc:
        raise ToolError("the cleaned file could not be reopened, so it was not returned") from exc
    with check:
        if len(check.pages) != pages:
            raise ToolError("the page count changed during cleaning, so the file was not returned")
        if metadata and (len(check.docinfo) or "/Metadata" in check.Root):
            raise ToolError("metadata remained after cleaning, so the file was not returned")
        if javascript and _javascript_count(check):
            raise ToolError("JavaScript remained after cleaning, so the file was not returned")
        if embedded and _embedded_file_count(check):
            raise ToolError("embedded files remained after cleaning, so the file was not returned")


# --- Text search with positions -------------------------------------------------

def _text_lines(element):
    if isinstance(element, LTTextLine):
        yield element
        return
    if hasattr(element, "__iter__"):
        for child in element:
            yield from _text_lines(child)


def _quarter(char: LTChar) -> int:
    """Text direction in quarter turns: 0 upright, 1 reads upwards, 2 upside down, 3 downwards."""
    a, b = char.matrix[0], char.matrix[1]
    return round(math.degrees(math.atan2(b, a)) / 90) % 4


def _reading_order(chars: list[LTChar], quarter: int) -> list[LTChar]:
    """Order rotated characters into lines and along each line, in the text's own direction."""
    angle = math.radians(quarter * 90)
    along_x, along_y = math.cos(angle), math.sin(angle)
    across_x, across_y = -along_y, along_x

    rows = sorted(
        (
            (item.matrix[4] * across_x + item.matrix[5] * across_y, item.matrix[4] * along_x + item.matrix[5] * along_y, item)
            for item in chars
        ),
        key=lambda row: -row[0],
    )
    lines: list[list] = []
    for across, along, item in rows:
        if lines and abs(lines[-1][0] - across) <= 0.5 * item.size:
            lines[-1][1].append((along, item))
        else:
            lines.append([across, [(along, item)]])

    ordered: list[LTChar] = []
    for _, members in lines:
        members.sort(key=lambda member: member[0])
        ordered.extend(item for _, item in members)
    return ordered


def _ordered_chars(page) -> list[LTChar]:
    """Characters in reading order. Upright text keeps pdfminer's order; rotated text is re-ordered,
    because pdfminer applies the page rotation and returns rotated text one character per line."""
    upright: list[LTChar] = []
    rotated: dict[int, list[LTChar]] = {}
    for line in _text_lines(page):
        for item in line:
            if not isinstance(item, LTChar):
                continue
            quarter = _quarter(item)
            if quarter == 0:
                upright.append(item)
            else:
                rotated.setdefault(quarter, []).append(item)
    ordered = list(upright)
    for quarter in sorted(rotated):
        ordered.extend(_reading_order(rotated[quarter], quarter))
    return ordered


def search_pdf(data: bytes, needle: str, limit: int = MAX_HITS) -> dict:
    """Find `needle` on every page. Each hit has its page (1-based) and box in PDF points.

    Matching is case-sensitive and happens inside one text line, the same rule the
    replace service uses. Boxes are in user space, so the editor converts them with
    the page viewport.
    """
    if not needle:
        raise InvalidInput("the text to search for is empty")
    try:
        pages = list(extract_pages(io.BytesIO(data)))
    except Exception as exc:  # pdfminer raises several parser error types
        raise ToolError("the PDF text could not be read") from exc

    # Spaces and line breaks are ignored, so names on rotated pages (one character per
    # text line in pdfminer's output) and names broken across lines are found.
    key = compact_text(needle)
    if not key:
        raise InvalidInput("the text to search for is empty")
    hits = []
    total = 0
    for page_number, page in enumerate(pages, start=1):
        chars: list[tuple[str, tuple]] = []
        for item in _ordered_chars(page):
            for char in item.get_text():
                if not char.isspace():
                    chars.append((char, item.bbox))

        text = "".join(char for char, _ in chars)
        start = text.find(key)
        while start != -1:
            total += 1
            boxes = [box for _, box in chars[start : start + len(key)]]
            if len(hits) < limit:
                hits.append(
                    {
                        "page": page_number,
                        "box": [
                            round(min(b[0] for b in boxes), 2),
                            round(min(b[1] for b in boxes), 2),
                            round(max(b[2] for b in boxes), 2),
                            round(max(b[3] for b in boxes), 2),
                        ],
                        "context": text[max(0, start - 30) : start + len(key) + 30],
                    }
                )
            start = text.find(key, start + len(key))

    return {"count": total, "hits": hits, "truncated": total > len(hits)}
