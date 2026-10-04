"""Synthetic in-memory native PDF text, with no private plan content."""

from io import BytesIO
import math
import pdfplumber
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from oabm.importers.pdf_architecture.extract import _group_words


def pdf(commands, *, page_rotation=0, width=600, height=600):
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    if page_rotation:
        page.rotate(page_rotation)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    output.seek(0)
    return output


def command(text, angle=0, x=300, y=300, size=12):
    c = round(math.cos(math.radians(angle)), 9)
    s = round(math.sin(math.radians(angle)), 9)
    return f"BT /F1 {size} Tf {c} {s} {-s} {c} {x} {y} Tm ({text}) Tj ET"


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_native_orthogonal_text_keeps_reading_order_and_displayed_bounds(angle):
    with pdfplumber.open(pdf([command("CONTINUED ON A-901", angle)])) as doc:
        page = doc.pages[0]
        observations = _group_words(page, 1)
        matches = [item for item in observations if item.text == "CONTINUED ON A-901"]
        assert len(matches) == 1
        actual = matches[0]
        chars = [c for c in page.chars if str(c["text"]).strip()]
        expected = (
            min(c["x0"] for c in chars),
            600 - max(c["bottom"] for c in chars),
            max(c["x1"] for c in chars),
            600 - min(c["top"] for c in chars),
        )
        assert actual.bbox_pt == pytest.approx(expected)
        assert actual.font_size_pt == pytest.approx(12)


@pytest.mark.parametrize("angle", [90, 270])
def test_vertical_fractional_dimension_is_one_observation(angle):
    value = "12'-3 1/2\""
    with pdfplumber.open(pdf([command(value, angle)])) as doc:
        assert value in [item.text for item in _group_words(doc.pages[0], 1)]


def test_rotated_columns_and_horizontal_caption_do_not_merge():
    commands = [
        command("LEFT COLUMN", 90, 100, 100),
        command("RIGHT COLUMN", 90, 150, 100),
        command("HORIZONTAL NOTE", 0, 220, 200),
        command("OTHER NOTE", 270, 450, 400),
    ]
    with pdfplumber.open(pdf(commands)) as doc:
        values = [item.text for item in _group_words(doc.pages[0], 1)]
        assert sorted(values) == sorted(
            ["LEFT COLUMN", "RIGHT COLUMN", "HORIZONTAL NOTE", "OTHER NOTE"]
        )


def test_unrelated_horizontal_annotations_keep_the_same_identity():
    baseline = command("UNCHANGED NOTE", 0, 50, 50)
    with pdfplumber.open(pdf([baseline])) as doc:
        first = _group_words(doc.pages[0], 1)[0]
    with pdfplumber.open(
        pdf([baseline, command("VERTICAL CAPTION", 90, 300, 300)])
    ) as doc:
        second = next(
            item
            for item in _group_words(doc.pages[0], 1)
            if item.text == "UNCHANGED NOTE"
        )
    assert first == second


@pytest.mark.parametrize("angle", [90, 270])
def test_small_font_spaces_survive_even_below_the_word_gap_threshold(angle):
    with pdfplumber.open(pdf([command("ONE TWO THREE", angle, size=4)])) as doc:
        items = _group_words(doc.pages[0], 1)
        assert [item.text for item in items] == ["ONE TWO THREE"]
        assert items[0].font_size_pt == pytest.approx(4)


def test_rendered_font_size_follows_the_rotated_text_matrix():
    with pdfplumber.open(
        pdf(["BT /F1 60 Tf 0 0.12 -0.12 0 300 300 Tm (SCALED NOTE) Tj ET"])
    ) as doc:
        items = _group_words(doc.pages[0], 1)
        assert items[0].text == "SCALED NOTE"
        assert items[0].font_size_pt == pytest.approx(7.2)


def test_mixed_normal_and_upside_down_runs_do_not_lose_characters():
    with pdfplumber.open(
        pdf([command("EAST", 0, 100, 200), command("WEST", 180, 140, 200)])
    ) as doc:
        assert sorted(item.text for item in _group_words(doc.pages[0], 1)) == [
            "EAST",
            "WEST",
        ]


def test_distant_labels_on_the_same_rotated_baseline_stay_separate():
    with pdfplumber.open(
        pdf([command("FIRST", 90, 100, 100), command("SECOND", 90, 100, 300)])
    ) as doc:
        assert sorted(item.text for item in _group_words(doc.pages[0], 1)) == [
            "FIRST",
            "SECOND",
        ]


def test_missing_matrix_does_not_guess_vertical_reading_direction():
    class Page:
        height = 600
        chars = [{"matrix": None}]

        @staticmethod
        def extract_words(**kwargs):
            return [
                {
                    "text": letter,
                    "x0": 100,
                    "x1": 110,
                    "top": 100 + i * 15,
                    "bottom": 110 + i * 15,
                    "size": 10,
                    "upright": False,
                }
                for i, letter in enumerate("RAW")
            ]

    assert [item.text for item in _group_words(Page(), 1)] == ["R", "A", "W"]


def test_reflections_and_diagonal_text_are_not_claimed_as_quarter_turns():
    from oabm.importers.pdf_architecture.extract import _char_quarter_turn

    assert _char_quarter_turn({"matrix": (-1, 0, 0, 1, 0, 0)}) is None
    assert _char_quarter_turn({"matrix": (0.866, 0.5, -0.5, 0.866, 0, 0)}) is None
    assert _char_quarter_turn({"matrix": (0, 0, 0, 0, 0, 0)}) is None
    assert _char_quarter_turn({"matrix": (float("nan"), 0, 0, 1, 0, 0)}) is None


def test_repeated_extraction_is_byte_identical():
    data = pdf(
        [command("TURNED CAPTION", 90), command("NORMAL CAPTION", 0, 100, 100)]
    ).getvalue()
    with pdfplumber.open(BytesIO(data)) as doc:
        first = _group_words(doc.pages[0], 1)
    with pdfplumber.open(BytesIO(data)) as doc:
        second = _group_words(doc.pages[0], 1)
    assert first == second


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_page_rotation_preserves_native_text_and_display_coordinates(rotation):
    with pdfplumber.open(
        pdf(
            [command("PAGE ROTATION", 0, 150, 150)],
            page_rotation=rotation,
            width=600,
            height=400,
        )
    ) as doc:
        page = doc.pages[0]
        item = next(
            item for item in _group_words(page, 1) if item.text == "PAGE ROTATION"
        )
        chars = [c for c in page.chars if str(c["text"]).strip()]
        assert item.bbox_pt == pytest.approx(
            (
                min(c["x0"] for c in chars),
                page.height - max(c["bottom"] for c in chars),
                max(c["x1"] for c in chars),
                page.height - min(c["top"] for c in chars),
            )
        )


def test_negative_glyph_advance_is_not_normalized_as_an_ordinary_rotation():
    from oabm.importers.pdf_architecture.extract import _char_quarter_turn

    with pdfplumber.open(
        pdf(["BT /F1 12 Tf -100 Tz 0 1 -1 0 300 300 Tm (MIRRORED) Tj ET"])
    ) as doc:
        chars = doc.pages[0].chars
        assert all(char["adv"] < 0 for char in chars)
        assert all(_char_quarter_turn(char) is None for char in chars)
