"""Replace a name in a PDF's text and scrub the metadata that could still show it.

The engine works on content streams, not on rendered pixels:

* Each text object (BT...ET) is walked with a small text-state tracker, so the
  position of every show operator (Tj, TJ, ', ") is known exactly.
* Where the old name appears in a single text run, its glyphs are replaced by a
  TJ displacement of the same width. Text before and after the name keeps its
  position. The new name is drawn in Helvetica (Helvetica-Bold when the original
  run was bold), in a q/Q block placed just before the text object.
* Background images, vector graphics and every other operator are left alone.
* Info dictionary, XMP metadata, page thumbnails and piece info are removed, and
  the file is written as a full rewrite (not an incremental update).
* The output is verified before it is returned. The text layer (pdfminer) must
  not contain the old name, and no stream or string object in the file may
  contain its bytes. If either check fails, nothing is returned.

Limits: the old name must sit inside one text-show operator (a name split across
runs is not matched). Composite (Type0) fonts, fonts with custom /Differences
encodings and encrypted files are rejected. Tagged-PDF structure, form fields and
annotations are not rewritten, so a name stored there makes verification fail.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pikepdf
from pdfminer.encodingdb import EncodingDB
from pdfminer.fontmetrics import FONT_METRICS
from pdfminer.high_level import extract_text

REGULAR_FONT = "/PDFEditorHelv"
BOLD_FONT = "/PDFEditorHelvBold"
MAX_NAME_LENGTH = 200
MAX_TEXT_LENGTH = 1000
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


class ReplacementError(Exception):
    """The name cannot be replaced safely. The message is safe to show to users."""


class InvalidInput(ReplacementError):
    """The names given by the caller are not usable."""


class NameNotFound(ReplacementError):
    """The old name does not occur in a single text run of the document."""


class VerificationError(ReplacementError):
    """The output still contains the old name, or the new name did not render."""


@dataclass(frozen=True)
class ReplacementReport:
    replacements: int
    pages_changed: int
    new_name_occurrences: int

    def as_dict(self) -> dict:
        return {
            "replacements": self.replacements,
            "pages_changed": self.pages_changed,
            "new_name_occurrences": self.new_name_occurrences,
            "verified": True,
        }


def replace_name(pdf_bytes: bytes, old: str, new: str) -> tuple[bytes, ReplacementReport]:
    """Return a copy of the PDF with every occurrence of `old` replaced by `new`."""
    old, new = _clean_names(old, new)
    old_bytes, new_bytes = old.encode("cp1252"), new.encode("cp1252")

    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except pikepdf.PdfError as exc:
        raise ReplacementError("the file could not be read as a PDF") from exc

    with pdf:
        replacements = 0
        pages_changed = 0
        for page in pdf.pages:
            operations, count = _rewrite_page(page, old_bytes, new_bytes)
            if not count:
                continue
            _add_fonts(pdf, page)
            page.obj["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(operations))
            replacements += count
            pages_changed += 1

        if not replacements:
            raise NameNotFound(
                "the name was not found in a single text run of the document"
            )

        _scrub_metadata(pdf)
        buffer = io.BytesIO()
        pdf.save(buffer, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.disable)

    output = buffer.getvalue()
    new_count = _verify(output, old, new, old_bytes, new_bytes, replacements)
    return output, ReplacementReport(replacements, pages_changed, new_count)


def _clean_names(old: str, new: str) -> tuple[str, str]:
    old = (old or "").strip()
    new = (new or "").strip()
    if not old:
        raise InvalidInput("the name to replace is empty")
    if not new:
        raise InvalidInput("the replacement name is empty")
    if len(old) > MAX_NAME_LENGTH or len(new) > MAX_NAME_LENGTH:
        raise InvalidInput(f"names may be at most {MAX_NAME_LENGTH} characters")
    if old == new:
        raise InvalidInput("the old and new names are the same")
    for value in (old, new):
        try:
            value.encode("cp1252")
        except UnicodeEncodeError as exc:
            raise InvalidInput(
                "names may only use Windows-1252 characters (Western European letters, digits and common punctuation)"
            ) from exc
    return old, new


# --- Content stream rewriting -------------------------------------------------

def _rewrite_page(page, old: bytes, new: bytes, *, mode: str = "all", target: int | None = None, collector: list | None = None):
    """Return (operations, replacement_count) for one page.

    mode "all" replaces every match (whole-document replace). mode "one" replaces only the match
    whose index on the page is `target`, counting matches in content order. mode "none" changes
    nothing and fills `collector` with each match's index and page-space box.
    """
    state = _TextState(page)
    state.mode, state.target, state.collector = mode, target, collector
    saved_states = []
    out: list = []
    pending: list = []
    text_start = None
    count = 0

    for operands, operator in pikepdf.parse_content_stream(page):
        name = str(operator)

        if name == "q":
            saved_states.append(state.graphics_state())
        elif name == "Q":
            if saved_states:
                state.restore_graphics_state(saved_states.pop())
        elif name == "BT":
            state.begin_text()
            text_start = len(out)
        elif name == "ET":
            if pending:
                out[text_start:text_start] = pending
                pending = []
        elif name == "cm":
            state.ctm = _mul(tuple(float(x) for x in operands[:6]), state.ctm)
        elif name in ("rg", "g", "k"):
            state.fill = (name, [float(x) for x in operands])
        elif name == "Tf":
            state.font_key = str(operands[0])
            state.font_size = float(operands[1])
        elif name == "Tc":
            state.char_spacing = float(operands[0])
        elif name == "Tw":
            state.word_spacing = float(operands[0])
        elif name == "Tz":
            state.horizontal_scale = float(operands[0])
        elif name == "TL":
            state.leading = float(operands[0])
        elif name == "Ts":
            state.rise = float(operands[0])
        elif name == "Tm":
            state.set_matrix(tuple(float(x) for x in operands[:6]))
        elif name == "Td":
            state.move(float(operands[0]), float(operands[1]))
        elif name == "TD":
            state.leading = -float(operands[1])
            state.move(float(operands[0]), float(operands[1]))
        elif name == "T*":
            state.next_line()
        elif name in ("'", '"'):
            if name == '"':
                state.word_spacing = float(operands[0])
                state.char_spacing = float(operands[1])
            state.next_line()
            result = _show(state, [bytes(operands[-1])], old, new, pending)
            if result is None:
                out.append((operands, operator))
            else:
                items, hits = result
                count += hits
                if name == '"':
                    out.append(([operands[0]], pikepdf.Operator("Tw")))
                    out.append(([operands[1]], pikepdf.Operator("Tc")))
                out.append(([], pikepdf.Operator("T*")))
                out.append(([_tj_array(items)], pikepdf.Operator("TJ")))
            continue
        elif name in ("Tj", "TJ"):
            items = (
                [_text_item(x) for x in operands[0]] if name == "TJ" else [bytes(operands[0])]
            )
            result = _show(state, items, old, new, pending)
            if result is None:
                out.append((operands, operator))
            else:
                items_out, hits = result
                count += hits
                out.append(([_tj_array(items_out)], pikepdf.Operator("TJ")))
            continue

        out.append((operands, operator))

    return out, count


def _text_item(item):
    return bytes(item) if isinstance(item, pikepdf.String) else float(item)


def _tj_array(items: list) -> pikepdf.Array:
    return pikepdf.Array(
        [pikepdf.String(item) if isinstance(item, bytes) else float(item) for item in items]
    )


def _show(state, items, old: bytes, new: bytes, pending: list):
    """Process one show operator.

    Returns None when `old` does not occur in it. Otherwise returns the rewritten
    TJ items and the number of replacements. In both cases the text matrix is
    advanced exactly as the original operator would have advanced it.
    """
    font = state.font()
    if str(font.get("/Subtype", "")) == "/Type0" and any(
        isinstance(item, bytes) and old in item for item in items
    ):
        raise ReplacementError("composite (Type0) fonts are not supported")

    origin_matrix = state.matrix
    displacement = 0.0
    out_items: list = []
    hits = 0

    for item in items:
        if isinstance(item, float):
            displacement += -item / 1000.0 * state.font_size * state.horizontal_scale / 100.0
            out_items.append(item)
            continue

        cursor = 0
        for start, end in _occurrences(item, old):
            before = item[cursor:start]
            if before:
                out_items.append(before)
                displacement += state.displacement(font, before)

            units = state.units(font, item[start:end])
            width = units * state.horizontal_scale / 100.0
            text_matrix = _translate_matrix(origin_matrix, displacement)
            index = state.match_index
            state.match_index += 1
            if state.collector is not None:
                state.collector.append({"index": index, "box": _user_box(text_matrix, width, state)})
            if state.selects(index):
                if new:
                    pending.extend(_draw_block(state, font, new, text_matrix))
                # A TJ number moves the text by -n/1000 * size * scale, so this keeps
                # the removed glyphs' width as blank space.
                out_items.append(-units * 1000.0 / state.font_size)
                hits += 1
            else:
                out_items.append(item[start:end])
            displacement += width
            cursor = end

        tail = item[cursor:]
        if tail:
            out_items.append(tail)
            displacement += state.displacement(font, tail)

    state.matrix = _mul(_translate(displacement, 0.0), state.matrix)
    if not hits:
        return None
    return out_items, hits


def _occurrences(data: bytes, needle: bytes):
    found = []
    index = data.find(needle)
    while index != -1:
        found.append((index, index + len(needle)))
        index = data.find(needle, index + len(needle))
    return found


def _draw_block(state, font, text: bytes, matrix) -> list:
    """Ops that draw `text` at `matrix`, wrapped so no text state leaks out."""
    bold = "bold" in _base_font(font).lower()
    font_name = BOLD_FONT if bold else REGULAR_FONT
    op = pikepdf.Operator
    fill = [] if state.fill is None else [(state.fill[1], op(state.fill[0]))]
    return [
        ([], op("q")),
        *fill,
        ([0], op("Tc")),
        ([0], op("Tw")),
        ([100], op("Tz")),
        ([state.rise], op("Ts")),
        ([], op("BT")),
        ([pikepdf.Name(font_name), state.font_size], op("Tf")),
        ([float(x) for x in matrix], op("Tm")),
        ([pikepdf.String(text)], op("Tj")),
        ([], op("ET")),
        ([], op("Q")),
    ]


class _TextState:
    """The subset of the text state needed to place glyphs exactly."""

    def __init__(self, page):
        self.fonts = _page_fonts(page)
        self.font_key = None
        self.font_size = 0.0
        self.char_spacing = 0.0
        self.word_spacing = 0.0
        self.horizontal_scale = 100.0
        self.leading = 0.0
        self.rise = 0.0
        self.matrix = IDENTITY
        self.line_matrix = IDENTITY
        self.ctm = IDENTITY
        self.fill = None
        self.mode = "all"
        self.target = None
        self.collector = None
        self.match_index = 0

    def selects(self, index: int) -> bool:
        return self.mode == "all" or (self.mode == "one" and self.target == index)

    def begin_text(self):
        self.matrix = IDENTITY
        self.line_matrix = IDENTITY

    def set_matrix(self, matrix):
        self.matrix = self.line_matrix = matrix

    def move(self, tx: float, ty: float):
        self.line_matrix = _mul(_translate(tx, ty), self.line_matrix)
        self.matrix = self.line_matrix

    def next_line(self):
        self.move(0.0, -self.leading)

    def font(self):
        if self.font_key is None:
            raise ReplacementError("text is shown before a font is selected")
        try:
            return self.fonts[self.font_key]
        except KeyError as exc:
            raise ReplacementError(f"font {self.font_key} is not in the page resources") from exc

    def graphics_state(self):
        return (
            self.font_key,
            self.font_size,
            self.char_spacing,
            self.word_spacing,
            self.horizontal_scale,
            self.leading,
            self.rise,
            self.ctm,
            self.fill,
        )

    def restore_graphics_state(self, saved):
        (
            self.font_key,
            self.font_size,
            self.char_spacing,
            self.word_spacing,
            self.horizontal_scale,
            self.leading,
            self.rise,
            self.ctm,
            self.fill,
        ) = saved

    def units(self, font, data: bytes) -> float:
        """Advance of `data` in text space, before horizontal scaling."""
        if not self.font_size:
            raise ReplacementError("the font size is zero")
        total = 0.0
        for code in data:
            total += _glyph_width(font, code) / 1000.0 * self.font_size + self.char_spacing
            if code == 32:
                total += self.word_spacing
        return total

    def displacement(self, font, data: bytes) -> float:
        """Horizontal movement of the text matrix caused by showing `data`."""
        return self.units(font, data) * self.horizontal_scale / 100.0


def _translate(tx: float, ty: float = 0.0):
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def _translate_matrix(matrix, tx: float):
    return _mul(_translate(tx, 0.0), matrix)


def _mul(a, b):
    """Concatenate two PDF matrices: apply `a`, then `b`."""
    a1, b1, c1, d1, e1, f1 = a
    a2, b2, c2, d2, e2, f2 = b
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _page_fonts(page):
    resources = page.Resources
    fonts = resources.get("/Font") if resources is not None else None
    return fonts if fonts is not None else pikepdf.Dictionary()


def _base_font(font) -> str:
    return _SUBSET_PREFIX.sub("", str(font.get("/BaseFont", "")).lstrip("/"))


def _glyph_width(font, code: int) -> float:
    """Width of one glyph in thousandths of text space."""
    if str(font.get("/Subtype", "")) == "/Type0":
        raise ReplacementError("composite (Type0) fonts are not supported")

    widths = font.get("/Widths")
    if widths is not None:
        index = code - int(font.get("/FirstChar", 0))
        if 0 <= index < len(widths):
            return float(widths[index])
        return float(font.get("/MissingWidth", 0))

    base = _base_font(font)
    metrics = FONT_METRICS.get(base)
    if metrics is None:
        raise ReplacementError(f"no width data for font {base or 'without a name'}")
    glyph = EncodingDB.get_encoding(_encoding_name(font)).get(code)
    return float(metrics[1].get(glyph, 0.0)) if glyph else 0.0


def _encoding_name(font) -> str:
    encoding = font.get("/Encoding")
    if isinstance(encoding, pikepdf.Dictionary):
        if "/Differences" in encoding:
            raise ReplacementError("fonts with custom /Differences encodings are not supported")
        encoding = encoding.get("/BaseEncoding")
    name = str(encoding).lstrip("/") if encoding is not None else ""
    if name in ("WinAnsiEncoding", "MacRomanEncoding", "StandardEncoding"):
        return name
    return "StandardEncoding"


def _add_fonts(pdf, page):
    """Make the Helvetica resources available to the page."""
    resources = page.Resources
    if "/Font" not in resources:
        resources["/Font"] = pikepdf.Dictionary()
    fonts = resources["/Font"]
    helvetica = {
        "/Type": pikepdf.Name.Font,
        "/Subtype": pikepdf.Name.Type1,
        "/Encoding": pikepdf.Name.WinAnsiEncoding,
    }
    fonts[REGULAR_FONT] = pdf.make_indirect(
        pikepdf.Dictionary({**helvetica, "/BaseFont": pikepdf.Name.Helvetica})
    )
    fonts[BOLD_FONT] = pdf.make_indirect(
        pikepdf.Dictionary({**helvetica, "/BaseFont": pikepdf.Name("/Helvetica-Bold")})
    )


# --- Metadata and verification ------------------------------------------------

def _scrub_metadata(pdf):
    for key in list(pdf.docinfo.keys()):
        del pdf.docinfo[key]
    for key in ("/Metadata", "/PieceInfo"):
        if key in pdf.Root:
            del pdf.Root[key]
    for page in pdf.pages:
        for key in ("/Thumb", "/PieceInfo", "/LastModified", "/Metadata"):
            if key in page.obj:
                del page.obj[key]


def _verify(
    output: bytes, old: str, new: str, old_bytes: bytes, new_bytes: bytes, replacements: int
) -> int:
    try:
        reopened = pikepdf.open(io.BytesIO(output))
    except pikepdf.PdfError as exc:
        raise VerificationError("the rewritten file could not be reopened") from exc

    with reopened:
        location = _find_bytes_in_objects(reopened, old_bytes)
        drawn = _count_drawn(reopened, new_bytes)
    if location is not None:
        raise VerificationError(f"the name still appears in object {location}")
    # The new name is checked in the content stream, which is exact for any page rotation.
    if drawn < replacements:
        raise VerificationError("the new name was not drawn on the page")

    # The text layer is only checked for leftovers of the old name. Its character order is
    # unreliable on rotated pages, so the new name is not searched for there.
    text = compact_text(extract_text(io.BytesIO(output)))
    old_key, new_key = compact_text(old), compact_text(new)
    remaining = text.count(old_key) - compact_text(new).count(old_key) * text.count(new_key)
    if remaining > 0:
        raise VerificationError("the name still appears in the text layer")
    return drawn


def _count_drawn(pdf, needle: bytes) -> int:
    """Count the text-show operators whose whole string is `needle`, across all pages."""
    count = 0
    for page in pdf.pages:
        for operands, operator in pikepdf.parse_content_stream(page):
            name = str(operator)
            if name == "Tj" and isinstance(operands[0], pikepdf.String) and bytes(operands[0]) == needle:
                count += 1
            elif name == "TJ":
                count += sum(
                    1 for item in operands[0] if isinstance(item, pikepdf.String) and bytes(item) == needle
                )
    return count


def compact_text(text: str) -> str:
    """Text with all whitespace removed, for matching that ignores line breaks and spacing."""
    return re.sub(r"\s+", "", text)


def _find_bytes_in_objects(pdf, needle: bytes):
    """Return the object id of the first stream or string holding `needle`, else None."""
    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Stream):
            try:
                data = obj.read_bytes()
            except pikepdf.PdfError:
                data = obj.read_raw_bytes()
            if needle in data:
                return obj.objgen
        elif isinstance(obj, pikepdf.Dictionary):
            values = obj.values()
            for value in values:
                if isinstance(value, pikepdf.String) and needle in bytes(value):
                    return obj.objgen
        elif isinstance(obj, pikepdf.Array):
            for value in obj:
                if isinstance(value, pikepdf.String) and needle in bytes(value):
                    return obj.objgen
    return None


# --- Single occurrences (Permanent edits on one spot) -----------------------------

@dataclass(frozen=True)
class OccurrenceReport:
    page: int
    index: int
    other_copies: int  # matches of the same text still in the file

    def as_dict(self) -> dict:
        return {
            "replacements": 1,
            "page": self.page,
            "index": self.index,
            "other_copies": self.other_copies,
            "verified": True,
        }


def _open(pdf_bytes: bytes):
    try:
        return pikepdf.open(io.BytesIO(pdf_bytes))
    except pikepdf.PdfError as exc:
        raise ReplacementError("the file could not be read as a PDF") from exc


def _pair_bytes(find: str, replace: str) -> tuple[bytes, bytes]:
    if not find:
        raise InvalidInput("the text to change is empty")
    if find == replace:
        raise InvalidInput("the new text is the same as the old text")
    if len(find) > MAX_TEXT_LENGTH or len(replace) > MAX_TEXT_LENGTH:
        raise InvalidInput(f"texts may be at most {MAX_TEXT_LENGTH} characters")
    try:
        return find.encode("cp1252"), replace.encode("cp1252")
    except UnicodeEncodeError as exc:
        raise InvalidInput(
            "this text uses characters the PDF's standard fonts can't write"
        ) from exc


def _user_box(text_matrix, width: float, state) -> list[float]:
    """Page-space box of a match: from 25% of the font size below the baseline to 85% above."""
    m = _mul(text_matrix, state.ctm)
    size = state.font_size
    corners = [(0.0, -0.25 * size), (width, -0.25 * size), (0.0, 0.85 * size), (width, 0.85 * size)]
    xs = [x * m[0] + y * m[2] + m[4] for x, y in corners]
    ys = [x * m[1] + y * m[3] + m[5] for x, y in corners]
    return [round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)]


def _same_box(a, b, tolerance: float = 0.5) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def list_occurrences(pdf_bytes: bytes, find: str) -> list[dict]:
    """Every match of `find`, page by page in content order. Nothing is changed."""
    old, _ = _pair_bytes(find, "")
    pdf = _open(pdf_bytes)
    found: list[dict] = []
    with pdf:
        for number, page in enumerate(pdf.pages, start=1):
            collector: list = []
            _rewrite_page(page, old, b"", mode="none", collector=collector)
            found.extend(
                {"page": number, "index": entry["index"], "box": entry["box"]} for entry in collector
            )
    return found


def replace_occurrence(
    pdf_bytes: bytes, find: str, replace: str, page_number: int, index: int
) -> tuple[bytes, OccurrenceReport]:
    """Replace one match (page and its index from list_occurrences). The other matches stay as they are."""
    old, new = _pair_bytes(find, replace)
    before = list_occurrences(pdf_bytes, find)
    target = next(
        (entry for entry in before if entry["page"] == page_number and entry["index"] == index), None
    )
    if target is None:
        raise NameNotFound("there is no match of that text at that position")

    pdf = _open(pdf_bytes)
    with pdf:
        page = pdf.pages[page_number - 1]
        operations, count = _rewrite_page(page, old, new, mode="one", target=index)
        if count != 1:
            raise ReplacementError("this match could not be replaced on its own")
        _add_fonts(pdf, page)
        page.obj["/Contents"] = pdf.make_stream(pikepdf.unparse_content_stream(operations))
        buffer = io.BytesIO()
        pdf.save(buffer, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    output = buffer.getvalue()

    # Checks: one match fewer, the removed one gone, and the new text drawn.
    after = list_occurrences(output, find)
    if len(after) != len(before) - 1:
        raise VerificationError("the number of matches did not drop by one")
    if any(entry["page"] == page_number and _same_box(entry["box"], target["box"]) for entry in after):
        raise VerificationError("the match is still there")
    if new:
        with _open(output) as check:
            if _count_drawn(check, new) < 1:
                raise VerificationError("the new text was not drawn on the page")
    return output, OccurrenceReport(page_number, index, len(after))
