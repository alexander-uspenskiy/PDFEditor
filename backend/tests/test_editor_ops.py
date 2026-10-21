"""Tests for editor_ops: the page, annotation and form options the browser editor has.

Run from the backend folder:  python -m pytest tests
"""

from __future__ import annotations

import io

import pikepdf
import pytest

import editor_ops
import replace_name
from editor_ops import EditError
from pdf_fixtures import count_dark, make_form_pdf, make_pdf, render, text_of

# make_pdf draws its text at (40, 200) in 14 pt Helvetica on a 400 x 300 page.
# In pixels at scale 1 the page is 300 pixels high, so pixel row = 300 - y.
TEXT_BOX = [35, 195, 130, 215]
TEXT_PIXELS = (35, 85, 130, 105)


def page_texts(data: bytes) -> list[str]:
    pdf = pikepdf.open(io.BytesIO(data))
    count = len(pdf.pages)
    pdf.close()
    return [replace_name.compact_text(text_of(data, n)) for n in range(1, count + 1)]


def rotation_of(data: bytes, number: int = 1) -> int:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return int(pdf.pages[number - 1].obj.get("/Rotate", 0)) % 360


def test_not_a_pdf_is_refused():
    with pytest.raises(EditError, match="not a PDF"):
        editor_ops.rotate_page(b"hello", 1)


def test_page_numbers_are_checked():
    with pytest.raises(EditError, match="page 9 does not exist"):
        editor_ops.rotate_page(make_pdf(["A"]), 9)


def test_rotate_turns_clockwise_and_wraps():
    data = make_pdf(["A"])
    once = editor_ops.rotate_page(data, 1)
    assert rotation_of(once) == 90
    twice = editor_ops.rotate_page(once, 1)
    assert rotation_of(twice) == 180
    assert rotation_of(editor_ops.rotate_page(twice, 1, turns=-1)) == 90
    assert rotation_of(editor_ops.rotate_page(data, 1, turns=4)) == 0
    assert rotation_of(data) == 0  # the input is never changed


def test_rotate_adds_to_rotation_inherited_from_the_page_tree():
    with pikepdf.open(io.BytesIO(make_pdf(["A"]))) as pdf:
        pdf.Root.Pages["/Rotate"] = 270
        buffer = io.BytesIO()
        pdf.save(buffer)
    rotated = editor_ops.rotate_page(buffer.getvalue(), 1)
    assert rotation_of(rotated) == 0


def test_move_page_keeps_the_other_pages_in_order():
    data = make_pdf(["A", "B", "C"])
    assert page_texts(editor_ops.move_page(data, 1, 3)) == ["B", "C", "A"]
    assert page_texts(editor_ops.move_page(data, 3, 1)) == ["C", "A", "B"]
    with pytest.raises(EditError, match="page 4 does not exist"):
        editor_ops.move_page(data, 1, 4)


def test_delete_page_removes_it_and_keeps_one_page():
    data = make_pdf(["A", "B"])
    assert page_texts(editor_ops.delete_page(data, 1)) == ["B"]
    with pytest.raises(EditError, match="at least one page"):
        editor_ops.delete_page(make_pdf(["A"]), 1)
    with pytest.raises(EditError, match="page 9 does not exist"):
        editor_ops.delete_page(data, 9)


def test_append_pages_adds_the_other_document_at_the_end():
    combined = editor_ops.append_pages(make_pdf(["A"]), make_pdf(["B", "C"]))
    assert page_texts(combined) == ["A", "B", "C"]


def test_add_text_writes_a_readable_note():
    out = editor_ops.add_text(make_pdf(["A"]), 1, 40, 100, "Approved by Ada")
    assert "ApprovedbyAda" in replace_name.compact_text(text_of(out))
    image = render(out, 1)
    blue = sum(1 for x in range(30, 200) for y in range(120, 200) if image.getpixel((x, y))[2] > image.getpixel((x, y))[0] + 60)
    assert blue > 0


def test_add_text_refuses_what_it_cannot_write():
    data = make_pdf(["A"])
    with pytest.raises(EditError, match="standard fonts"):
        editor_ops.add_text(data, 1, 40, 100, "日本")
    with pytest.raises(EditError, match="one line"):
        editor_ops.add_text(data, 1, 40, 100, "two\nlines")
    with pytest.raises(EditError, match="empty"):
        editor_ops.add_text(data, 1, 40, 100, "")


def test_highlight_tints_the_area_yellow():
    data = make_pdf(["A"], size=(400, 300))
    # Blank area of the page: the box is 100-200 across, 100-150 up. Sample its centre.
    out = editor_ops.highlight(data, 1, [100, 100, 200, 150])
    red, green, blue = render(out, 1).getpixel((150, 175))
    assert red >= 245 and green >= 235 and blue < 200


def test_highlight_refuses_bad_boxes():
    data = make_pdf(["A"])
    with pytest.raises(EditError, match="four numbers"):
        editor_ops.highlight(data, 1, [1, 2, 3])
    with pytest.raises(EditError, match="too small"):
        editor_ops.highlight(data, 1, [10, 10, 11, 11])


def test_whiteout_hides_ink_but_keeps_the_text():
    data = make_pdf(["Hello world"])
    assert count_dark(render(data, 1), TEXT_PIXELS) > 0
    out = editor_ops.whiteout(data, 1, TEXT_BOX)
    assert count_dark(render(out, 1), TEXT_PIXELS) == 0
    assert "Helloworld" in replace_name.compact_text(text_of(out))


def test_cover_only_writes_the_replacement_and_keeps_the_original():
    data = make_pdf(["Hello world"])
    out = editor_ops.cover_occurrence(data, "world", "planet", 1, 0)
    text = replace_name.compact_text(text_of(out))
    assert "planet" in text
    assert "world" in text  # cover only hides the original; it stays in the file


def test_cover_only_with_empty_replacement_just_hides_the_match():
    data = make_pdf(["Hello world"])
    out = editor_ops.cover_occurrence(data, "world", "", 1, 0)
    assert count_dark(render(out, 1), TEXT_PIXELS) < count_dark(render(data, 1), TEXT_PIXELS)


def test_cover_only_checks_its_inputs():
    data = make_pdf(["Hello world"])
    with pytest.raises(EditError, match="no match of that text"):
        editor_ops.cover_occurrence(data, "absent", "x", 1, 0)
    with pytest.raises(EditError, match="same as the old text"):
        editor_ops.cover_occurrence(data, "world", "world", 1, 0)
    with pytest.raises(EditError, match="rotated pages"):
        editor_ops.cover_occurrence(make_pdf(["Hello world"], rotate=90), "world", "planet", 1, 0)


def test_list_form_fields_reads_text_and_checkboxes():
    fields = {f["name"]: f for f in editor_ops.list_form_fields(make_form_pdf())}
    assert fields == {
        "name": {"name": "name", "kind": "text", "value": ""},
        "agree": {"name": "agree", "kind": "checkbox", "value": False},
    }
    assert editor_ops.list_form_fields(make_pdf(["A"])) == []


def test_fill_form_fields_sets_values_and_draws_them():
    filled = editor_ops.fill_form_fields(make_form_pdf(), {"name": "Ada Lovelace", "agree": True})
    fields = {f["name"]: f["value"] for f in editor_ops.list_form_fields(filled)}
    assert fields == {"name": "Ada Lovelace", "agree": True}
    # The field box is 20-200 across and 150-170 up, which is rows 30-50 in pixels on a 200-point page.
    assert count_dark(render(filled, 1, forms=True), (20, 30, 200, 50)) > 0


def test_fill_form_fields_checks_names_and_types():
    data = make_form_pdf()
    with pytest.raises(EditError, match="no such form field: missing"):
        editor_ops.fill_form_fields(data, {"missing": "x"})
    with pytest.raises(EditError, match="needs a string"):
        editor_ops.fill_form_fields(data, {"name": True})
    with pytest.raises(EditError, match="needs true or false"):
        editor_ops.fill_form_fields(data, {"agree": "yes"})
    with pytest.raises(EditError, match="no field values"):
        editor_ops.fill_form_fields(data, {})
    with pytest.raises(EditError, match="no form fields"):
        editor_ops.fill_form_fields(make_pdf(["A"]), {"name": "x"})
