"""Format handlers for the universal find-and-replace service.

A file's format is detected from its content, not its name, so a mislabelled
file is still handled correctly. Each handler can:

* ``info``    - describe the file (page count, encoding, parts)
* ``find``    - count a string and return excerpts, without changing anything
* ``replace`` - write a new file with the string replaced, plus excerpts and
                a count of occurrences that remain in the output

Handlers:

* ``pdf``    - PDF documents. Edited by replace_name.py, which works on the
               content stream and verifies the output before returning it.
* ``ooxml``  - zip packages with XML parts. Only text between XML tags changes,
               never tag names or attribute values.
* ``text``   - text content: UTF-8 (with or without BOM) or Windows-1252.
"""

from __future__ import annotations

import codecs
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from xml.sax.saxutils import escape, unescape

import pypdfium2 as pdfium
from pdfminer.high_level import extract_text

import replace_name as pdf_engine

EXCERPT_LIMIT = 8
EXCERPT_RADIUS = 60
MAX_STRING_LENGTH = 1000
OOXML_MARKERS = ("word/document.xml", "xl/workbook.xml", "ppt/presentation.xml")
_TEXT_NODE = re.compile(r">([^<]+)<")


class FormatError(Exception):
    """A request could not be completed. The message is safe to show to users."""


class UnsupportedFormat(FormatError):
    """The file is not a supported type."""


class InvalidInput(FormatError):
    """The find or replace text is not usable."""


class NotFound(FormatError):
    """The text to find does not occur in the file."""


class OutputInvalid(FormatError):
    """The rewritten file failed its own validity checks, so it was not returned."""


@dataclass(frozen=True)
class Excerpt:
    location: str
    before: str
    after: str | None

    def as_dict(self) -> dict:
        return {"location": self.location, "before": self.before, "after": self.after}


@dataclass(frozen=True)
class FindResult:
    count: int
    excerpts: list[Excerpt]


@dataclass(frozen=True)
class ReplaceResult:
    data: bytes
    replacements: int
    remaining: int
    touched: int
    touched_label: str
    excerpts: list[Excerpt]


def _validate(find: str, replace: str) -> None:
    if not find:
        raise InvalidInput("the text to find is empty")
    if find == replace:
        raise InvalidInput("the new text is the same as the text to find")
    if len(find) > MAX_STRING_LENGTH or len(replace) > MAX_STRING_LENGTH:
        raise InvalidInput(f"texts may be at most {MAX_STRING_LENGTH} characters")


def _windows(
    before: str,
    after: str | None,
    find: str,
    replace: str,
    location: Callable[[int], str],
    limit: int,
) -> list[Excerpt]:
    """Excerpts around each match. `after` is the same text once replaced, if known."""
    excerpts: list[Excerpt] = []
    shift = 0
    covered_until = -1
    covered_location = None
    index = before.find(find)
    while index != -1 and len(excerpts) < limit:
        # A match close to the previous one in the same location joins that excerpt.
        where = location(index)
        if where != covered_location or index > covered_until:
            window_before = before[max(0, index - EXCERPT_RADIUS) : index + len(find) + EXCERPT_RADIUS]
            window_after = None
            if after is not None:
                start = index + shift
                window_after = after[max(0, start - EXCERPT_RADIUS) : start + len(replace) + EXCERPT_RADIUS]
            excerpts.append(Excerpt(where, window_before, window_after))
            covered_until = index + len(find) + EXCERPT_RADIUS
            covered_location = where
        shift += len(replace) - len(find)
        index = before.find(find, index + len(find))
    return excerpts


# --- PDF ----------------------------------------------------------------------

class PdfHandler:
    name = "pdf"
    label = "PDF document"

    def info(self, data: bytes) -> dict:
        document = pdfium.PdfDocument(data)
        try:
            return {"pages": len(document)}
        finally:
            document.close()

    def find(self, data: bytes, find: str) -> FindResult:
        _validate(find, "")
        text = _pdf_text(data)
        return FindResult(
            _pdf_count(text, find),
            _windows(text, None, find, "", lambda _: "text layer", EXCERPT_LIMIT),
        )

    def replace(self, data: bytes, find: str, replace: str) -> ReplaceResult:
        _validate(find, replace)
        before_text = _pdf_text(data)
        try:
            output, report = pdf_engine.replace_name(data, find, replace)
        except pdf_engine.InvalidInput as exc:
            raise InvalidInput(str(exc)) from exc
        except pdf_engine.NameNotFound as exc:
            raise NotFound(str(exc)) from exc
        except pdf_engine.VerificationError as exc:
            raise OutputInvalid(f"verification failed, no file returned: {exc}") from exc
        except pdf_engine.ReplacementError as exc:
            raise FormatError(str(exc)) from exc

        after_text = _pdf_text(output)
        return ReplaceResult(
            data=output,
            replacements=report.replacements,
            remaining=_pdf_count(after_text, find),
            touched=report.pages_changed,
            touched_label="pages changed",
            excerpts=_windows(
                before_text,
                after_text,
                find,
                replace,
                lambda _: "text layer",
                EXCERPT_LIMIT,
            ),
        )

    def render_png(self, data: bytes, page: int, scale: float = 1.5) -> bytes:
        document = pdfium.PdfDocument(data)
        try:
            if not 1 <= page <= len(document):
                raise NotFound(f"the document has {len(document)} pages")
            image = document[page - 1].render(scale=scale).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        finally:
            document.close()


def _pdf_count(text: str, needle: str) -> int:
    """Occurrences in PDF text, ignoring whitespace (same rule as the replace check)."""
    return pdf_engine.compact_text(text).count(pdf_engine.compact_text(needle))


def _pdf_text(data: bytes) -> str:
    try:
        return extract_text(io.BytesIO(data))
    except Exception as exc:  # pdfminer raises several parser error types
        raise FormatError("the PDF text could not be read") from exc


# --- Text ---------------------------------------------------------------

def _try_decode(data: bytes) -> tuple[str, str] | None:
    """Return (text, codec) for text data, or None for binary data."""
    if b"\x00" in data:
        return None
    control = sum(1 for byte in data if byte < 32 and byte not in (9, 10, 12, 13))
    if data and control / len(data) > 0.01:
        return None
    if data.startswith(codecs.BOM_UTF8):
        if _decodes(data, "utf-8"):
            return data.decode("utf-8-sig"), "utf-8-sig"
        return None
    for codec in ("utf-8", "cp1252"):
        if _decodes(data, codec):
            return data.decode(codec), codec
    return None


def _decodes(data: bytes, codec: str) -> bool:
    try:
        data.decode(codec)
    except UnicodeDecodeError:
        return False
    return True


class TextHandler:
    name = "text"
    label = "text"

    def info(self, data: bytes) -> dict:
        decoded = _try_decode(data)
        return {"encoding": decoded[1] if decoded else None, "lines": decoded[0].count("\n") + 1 if decoded else 0}

    def find(self, data: bytes, find: str) -> FindResult:
        _validate(find, "")
        text, _ = self._decode(data)
        return FindResult(
            text.count(find),
            _windows(text, None, find, "", _line_of(text), EXCERPT_LIMIT),
        )

    def replace(self, data: bytes, find: str, replace: str) -> ReplaceResult:
        _validate(find, replace)
        text, codec = self._decode(data)
        count = text.count(find)
        if not count:
            raise NotFound("the text was not found in the file")

        new_text = text.replace(find, replace)
        try:
            output = new_text.encode(codec)
        except UnicodeEncodeError as exc:
            raise InvalidInput(
                "the new text contains characters this file's encoding cannot store"
            ) from exc

        decoded_output, _ = self._decode(output)
        if decoded_output != new_text:
            raise OutputInvalid("the rewritten file did not decode to the expected text")

        return ReplaceResult(
            data=output,
            replacements=count,
            remaining=new_text.count(find),
            touched=1,
            touched_label="file",
            excerpts=_windows(text, new_text, find, replace, _line_of(text), EXCERPT_LIMIT),
        )

    def _decode(self, data: bytes) -> tuple[str, str]:
        decoded = _try_decode(data)
        if decoded is None:
            raise UnsupportedFormat("the file is not readable text")
        return decoded


def _line_of(text: str) -> Callable[[int], str]:
    return lambda index: f"line {text.count(chr(10), 0, index) + 1}"


# --- Zip packages with XML parts ---------------------------------------------

def _open_package(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFormat("the file is not a valid zip package") from exc


def _xml_parts(package: zipfile.ZipFile):
    """Yield (name, text) for every XML part that can hold document text."""
    for info in package.infolist():
        name = info.filename
        if not name.lower().endswith(".xml") or name == "[Content_Types].xml" or "_rels/" in name:
            continue
        try:
            text = package.read(name).decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield name, text


def _replace_text_nodes(xml: str, find: str, replace: str) -> tuple[str, int]:
    """Replace `find` only inside text nodes (between '>' and '<')."""
    count = 0

    def substitute(match: re.Match) -> str:
        nonlocal count
        node = match.group(1)
        hits = node.count(find)
        if not hits:
            return match.group(0)
        count += hits
        return ">" + node.replace(find, replace) + "<"

    return _TEXT_NODE.sub(substitute, xml), count


class OoxmlHandler:
    name = "ooxml"
    label = "zip package"

    def info(self, data: bytes) -> dict:
        package = _open_package(data)
        parts = [name for name, _ in _xml_parts(package)]
        return {"xml_parts": len(parts)}

    def find(self, data: bytes, find: str) -> FindResult:
        _validate(find, "")
        package = _open_package(data)
        needle = escape(find)
        count = 0
        excerpts: list[Excerpt] = []
        for name, xml in _xml_parts(package):
            for node in _TEXT_NODE.findall(xml):
                hits = node.count(needle)
                if not hits:
                    continue
                count += hits
                room = EXCERPT_LIMIT - len(excerpts)
                if room > 0:
                    excerpts.extend(
                        _windows(unescape(node), None, find, "", lambda _, n=name: n, room)
                    )
        return FindResult(count, excerpts)

    def replace(self, data: bytes, find: str, replace: str) -> ReplaceResult:
        _validate(find, replace)
        package = _open_package(data)
        needle, substitute = escape(find), escape(replace)

        replacements = 0
        new_parts: dict[str, str] = {}
        excerpts: list[Excerpt] = []
        for name, xml in _xml_parts(package):
            new_xml, count = _replace_text_nodes(xml, needle, substitute)
            if not count:
                continue
            replacements += count
            new_parts[name] = new_xml
            # Replacement never adds tags, so before and after nodes line up one to one.
            for node_before, node_after in zip(_TEXT_NODE.findall(xml), _TEXT_NODE.findall(new_xml)):
                if needle in node_before and len(excerpts) < EXCERPT_LIMIT:
                    excerpts.extend(
                        _windows(
                            unescape(node_before),
                            unescape(node_after),
                            find,
                            replace,
                            lambda _, n=name: n,
                            EXCERPT_LIMIT - len(excerpts),
                        )
                    )

        if not replacements:
            raise NotFound("the text was not found in the file")

        output = _rebuild_package(data, new_parts)
        for name, xml in new_parts.items():
            try:
                ET.fromstring(xml.encode("utf-8"))
            except ET.ParseError as exc:
                raise OutputInvalid(f"{name} is no longer well-formed XML") from exc

        remaining = self.find(output, find).count
        return ReplaceResult(
            data=output,
            replacements=replacements,
            remaining=remaining,
            touched=len(new_parts),
            touched_label="parts changed",
            excerpts=excerpts,
        )


def _rebuild_package(data: bytes, new_parts: dict[str, str]) -> bytes:
    source = _open_package(data)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename in new_parts:
                payload = new_parts[info.filename].encode("utf-8")
            else:
                payload = source.read(info.filename)
            target.writestr(info.filename, payload)
    return buffer.getvalue()




# --- Detection and registry ---------------------------------------------------

HANDLERS = {"pdf": PdfHandler(), "ooxml": OoxmlHandler(), "text": TextHandler()}


def detect(data: bytes) -> str:
    """Return the handler name for the file content, or raise UnsupportedFormat."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04") and _is_ooxml(data):
        return "ooxml"
    if _try_decode(data) is not None:
        return "text"
    raise UnsupportedFormat(
        "unsupported file type: upload a PDF"
    )


def _is_ooxml(data: bytes) -> bool:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile:
        return False
    return any(marker in names for marker in OOXML_MARKERS)
