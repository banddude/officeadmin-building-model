"""Stroke/fill gray and line width on electrical vector path observations (#197).

Groundwork for a deterministic shape-class table: each vector path records the
colour luminance and stroking width it was painted with, following the same
graphics-state rules as the architecture extractor's line styles. A pattern or
unknown colour omits its key instead of guessing, and no model output changes:
the importer keeps consuming geometry and paint operators only.

Every PDF here is generated in the test from synthetic content.
"""

import math
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from oabm.importers.pdf_electrical import importer as pdf_electrical_importer
from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.model import validate_model

_STYLE_KEYS = ("stroke_gray", "fill_gray", "line_width_pt")


def _pattern_stream() -> DecodedStreamObject:
    """A minimal valid coloured tiling pattern."""

    pattern = DecodedStreamObject()
    pattern.set_data(b"0.5 g 0 0 8 8 re f")
    pattern[NameObject("/Type")] = NameObject("/Pattern")
    pattern[NameObject("/PatternType")] = NumberObject(1)
    pattern[NameObject("/PaintType")] = NumberObject(1)
    pattern[NameObject("/TilingType")] = NumberObject(1)
    pattern[NameObject("/BBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(8), NumberObject(8)]
    )
    pattern[NameObject("/XStep")] = NumberObject(8)
    pattern[NameObject("/YStep")] = NumberObject(8)
    pattern[NameObject("/Resources")] = DictionaryObject({})
    return pattern


def _write_pdf(
    path: Path,
    commands: list[str],
    *,
    with_pattern: bool = False,
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612.0, height=792.0)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources: dict = {
        NameObject("/Font"): DictionaryObject(
            {NameObject("/F1"): writer._add_object(font)}
        ),
    }
    if with_pattern:
        resources[NameObject("/Pattern")] = DictionaryObject(
            {NameObject("/P1"): writer._add_object(_pattern_stream())}
        )
    page[NameObject("/Resources")] = DictionaryObject(resources)
    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def _line(x1: float, y1: float, x2: float, y2: float) -> str:
    return f"{x1} {y1} m {x2} {y2} l S"


def _bezier_circle(center: tuple[float, float], radius: float) -> list[str]:
    kappa = 0.5522847498307936
    off = radius * kappa
    cx, cy = center
    return [
        f"{cx:.3f} {cy + radius:.3f} m",
        f"{cx + off:.3f} {cy + radius:.3f} {cx + radius:.3f} {cy + off:.3f} {cx + radius:.3f} {cy:.3f} c",
        f"{cx + radius:.3f} {cy - off:.3f} {cx + off:.3f} {cy - radius:.3f} {cx:.3f} {cy - radius:.3f} c",
        f"{cx - off:.3f} {cy - radius:.3f} {cx - radius:.3f} {cy - off:.3f} {cx - radius:.3f} {cy:.3f} c",
        f"{cx - radius:.3f} {cy + off:.3f} {cx - off:.3f} {cy + radius:.3f} {cx:.3f} {cy + radius:.3f} c",
        "h S",
    ]


def _single_path_metadata(tmp_path: Path, commands: list[str]) -> dict:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(pdf, commands)
    extracted = extract_pdf(pdf, source_id="test:vector-style")
    assert len(extracted.vectors) == 1
    return dict(extracted.vectors[0].metadata)


def test_stroked_circle_records_black_and_line_width(tmp_path: Path) -> None:
    # Default fill/stroke colour is DeviceGray black; the explicit width and
    # the curve flattening must not disturb either value.
    metadata = _single_path_metadata(
        tmp_path, ["1.5 w", *_bezier_circle((150.0, 150.0), 25.0)]
    )
    assert metadata["stroke_gray"] == 0.0
    assert metadata["line_width_pt"] == 1.5
    assert "fill_gray" not in metadata


def test_filled_gray_triangle_records_fill_gray(tmp_path: Path) -> None:
    metadata = _single_path_metadata(
        tmp_path,
        ["0.3 g", "20 20 m 60 20 l 40 50 l h f"],
    )
    assert metadata["fill_gray"] == 0.3
    assert "stroke_gray" not in metadata
    assert "line_width_pt" not in metadata


@pytest.mark.parametrize(
    ("operands", "expected_gray"),
    [
        pytest.param("1 0 0 RG", 0.299, id="rgb-red"),
        pytest.param("0.2 0.4 0.8 RG", 0.3858, id="rgb-mixed"),
        pytest.param("0 1 1 0 K", 0.299, id="cmyk-red"),
        pytest.param("1 0 0 0 K", 0.701, id="cmyk-cyan"),
    ],
)
def test_stroke_colour_luminance_matches_the_shared_rule(
    tmp_path: Path, operands: str, expected_gray: float
) -> None:
    metadata = _single_path_metadata(tmp_path, [operands, _line(200, 250, 250, 250)])
    assert metadata["stroke_gray"] == expected_gray


def test_gray_inside_qq_does_not_leak_past_q(tmp_path: Path) -> None:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(
        pdf,
        [
            # Both style values are changed inside the saved state.
            "q 0.6 G 4 w",
            _line(100, 100, 150, 100),
            "Q",
            _line(100, 120, 150, 120),
        ],
    )
    extracted = extract_pdf(pdf, source_id="test:vector-style")
    inside, outside = extracted.vectors
    assert inside.metadata["stroke_gray"] == 0.6
    assert inside.metadata["line_width_pt"] == 4.0
    # The PDF default state is restored: black and 1 pt.
    assert outside.metadata["stroke_gray"] == 0.0
    assert outside.metadata["line_width_pt"] == 1.0


def test_pattern_colour_omits_the_key(tmp_path: Path) -> None:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(
        pdf,
        [
            "/Pattern cs /P1 scn 20 200 40 40 re f",
            "/Pattern CS /P1 SCN 20 200 m 60 200 l S",
        ],
        with_pattern=True,
    )
    extracted = extract_pdf(pdf, source_id="test:vector-style")
    fill, stroke = extracted.vectors
    assert fill.metadata["paint_operator"] == "f"
    assert "fill_gray" not in fill.metadata
    assert "fill_alpha" not in fill.metadata
    assert "line_width_pt" not in fill.metadata
    assert stroke.metadata["paint_operator"] == "S"
    assert "stroke_gray" not in stroke.metadata


def test_line_width_follows_the_ctm(tmp_path: Path) -> None:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(
        pdf,
        [
            "q 2 0 0 2 0 0 cm",
            "1.5 w",
            _line(100, 100, 120, 100),
            "Q",
            "q 0 1 -1 0 300 0 cm",
            "1.5 w",
            _line(100, 100, 120, 100),
            "Q",
        ],
    )
    extracted = extract_pdf(pdf, source_id="test:vector-style")
    scaled, rotated = extracted.vectors
    # sqrt of the determinant: 2x scaling doubles the stroke, a quarter turn
    # keeps it.
    assert scaled.metadata["line_width_pt"] == 3.0
    assert rotated.metadata["line_width_pt"] == 1.5


def test_default_state_records_black_and_one_point(tmp_path: Path) -> None:
    metadata = _single_path_metadata(tmp_path, ["80 80 30 20 re S"])
    assert metadata["stroke_gray"] == 0.0
    assert metadata["line_width_pt"] == 1.0


def test_style_metadata_stays_out_of_the_model_output(tmp_path: Path) -> None:
    pdf = tmp_path / "style-sheet.pdf"
    _write_pdf(
        pdf,
        [
            "BT /F1 10 Tf 1 0 0 1 72 700 Tm (PANEL PA 120/240V 1PH) Tj ET",
            "0.5 G 2 w",
            "72 690 m 200 690 l S",
            "0.25 g",
            "72 600 20 20 re f",
        ],
    )
    extracted = extract_pdf(pdf, source_id="test:vector-style")
    assert extracted.vectors[0].metadata["stroke_gray"] == 0.5
    assert extracted.vectors[1].metadata["fill_gray"] == 0.25

    model = ElectricalPdfImporter().import_document(extracted)
    serialized = model.to_json()
    for key in _STYLE_KEYS:
        assert f'"{key}"' not in serialized
    validate_model(model)


def test_provenance_mirror_drops_only_the_style_keys() -> None:
    mirrored = pdf_electrical_importer._provenance_vector_metadata(
        {
            "paint_operator": "S",
            "stroke_gray": 0.5,
            "fill_gray": 0.0,
            "line_width_pt": 2.0,
            "stroke_alpha": 0.5,
        }
    )
    assert mirrored == {"metadata": {"paint_operator": "S", "stroke_alpha": 0.5}}
    assert pdf_electrical_importer._provenance_vector_metadata({}) == {}


def test_extraction_is_deterministic(tmp_path: Path) -> None:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(
        pdf,
        [
            "0.3 g",
            *_bezier_circle((150.0, 400.0), 20.0)[:-1],
            "h f",
            "1 0 0 RG 2 w",
            _line(100, 100, 150, 100),
        ],
    )
    first = extract_pdf(pdf, source_id="test:vector-style")
    second = extract_pdf(pdf, source_id="test:vector-style")
    assert first == second
    assert math.isclose(first.vectors[0].metadata["fill_gray"], 0.3)
    assert first.vectors[1].metadata["stroke_gray"] == 0.299
