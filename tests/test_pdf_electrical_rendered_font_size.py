"""Rendered font size in the electrical PDF extractor (#72).

CAD exports often set a large ``Tf`` size and shrink it with the text matrix,
for example ``Tf 60`` with a ``Tm`` scale of 0.12, which draws 7.2 pt glyphs.
The extractor must record the drawn size, not the raw ``Tf`` operand, or real
legend labels fail the importer's short-label filter and no legend is found.

Every PDF here is generated in the test from synthetic content.
"""

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_electrical import importer as pdf_electrical_importer
from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.model import BuildingModel, validate_model


def _write_pdf(
    path: Path,
    commands: list[str],
    *,
    width: float = 612.0,
    height: float = 396.0,
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            ),
        }
    )
    content = DecodedStreamObject()
    content.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def _single_text_size(tmp_path: Path, commands: list[str], text: str) -> float:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(pdf, commands)
    extracted = extract_pdf(pdf, source_id="test:rendered-font-size")
    matches = [item for item in extracted.texts if item.text == text]
    assert len(matches) == 1
    assert matches[0].font_size_pt is not None
    return matches[0].font_size_pt


def test_scaled_text_matrix_reports_the_drawn_size(tmp_path: Path) -> None:
    size = _single_text_size(
        tmp_path,
        ["BT /F1 60 Tf 0.12 0 0 0.12 100 200 Tm (GFCI) Tj ET"],
        "GFCI",
    )
    assert size == pytest.approx(7.2, abs=1e-9)


def test_identity_text_matrix_reports_the_tf_size(tmp_path: Path) -> None:
    size = _single_text_size(
        tmp_path,
        ["BT /F1 7 Tf 1 0 0 1 100 200 Tm (GFCI) Tj ET"],
        "GFCI",
    )
    assert size == pytest.approx(7.0, abs=1e-9)


def test_graphics_matrix_scale_halves_the_size(tmp_path: Path) -> None:
    pdf = tmp_path / "probe.pdf"
    _write_pdf(
        pdf,
        [
            "q 0.5 0 0 0.5 0 0 cm",
            "BT /F1 8 Tf 1 0 0 1 200 400 Tm (GFCI) Tj ET",
            "Q",
        ],
    )
    extracted = extract_pdf(pdf, source_id="test:rendered-font-size")
    (label,) = [item for item in extracted.texts if item.text == "GFCI"]
    assert label.font_size_pt == pytest.approx(4.0, abs=1e-9)
    # The position already went through the CTM; the size now does too.
    assert (label.x_pt, label.y_pt) == pytest.approx((100.0, 200.0), abs=1e-9)


def test_graphics_and_text_matrix_scales_compose(tmp_path: Path) -> None:
    size = _single_text_size(
        tmp_path,
        [
            "q 0.5 0 0 0.5 0 0 cm",
            "BT /F1 60 Tf 0.12 0 0 0.12 200 400 Tm (GFCI) Tj ET",
            "Q",
        ],
        "GFCI",
    )
    assert size == pytest.approx(3.6, abs=1e-9)


@pytest.mark.parametrize(
    "text_matrix",
    ["0 1 -1 0 100 200", "0 -1 1 0 100 200"],
    ids=["rotated-90", "rotated-270"],
)
def test_rotated_unit_text_matrix_keeps_the_size(
    tmp_path: Path,
    text_matrix: str,
) -> None:
    size = _single_text_size(
        tmp_path,
        [f"BT /F1 7 Tf {text_matrix} Tm (GFCI) Tj ET"],
        "GFCI",
    )
    assert size == pytest.approx(7.0, abs=1e-9)


# The geometry-only power sheet from
# fixtures/pdf_electrical/geometry-only-power-sheet-with-legend.pdf, with every
# label drawn the CAD way: a large Tf shrunk by a 0.12 text matrix. The drawn
# sizes (11 pt heading, 9 pt labels) match the fixture.
_FIELD_GLYPHS = [
    "85 300 10 10 re f",
    "83 305 m 97 305 l S",
    "175 300 10 10 re f",
    "173 305 m 187 305 l S",
    "90 212 m 97 205 l 90 198 l 83 205 l h S",
    "88 203 4 4 re f",
    "180 212 m 187 205 l 180 198 l 173 205 l h S",
    "178 203 4 4 re f",
    "282 275 m 282 278.866 278.866 282 275 282 c 271.134 282 268 278.866 268 275 c"
    " 268 271.134 271.134 268 275 268 c 278.866 268 282 271.134 282 275 c h S",
    "268 275 m 282 275 l S",
    "275 268 m 275 282 l S",
    "282 165 m 282 168.866 278.866 172 275 172 c 271.134 172 268 168.866 268 165 c"
    " 268 161.134 271.134 158 275 158 c 278.866 158 282 161.134 282 165 c h S",
    "268 165 m 282 165 l S",
    "275 158 m 275 172 l S",
    "90 97 m 97 83 l 83 83 l h S",
    "180 97 m 187 83 l 173 83 l h S",
]
_LEGEND_GLYPHS = [
    "390 315 10 10 re f",
    "388 320 m 402 320 l S",
    "395 282 m 402 275 l 395 268 l 388 275 l h S",
    "393 273 4 4 re f",
    "402 230 m 402 233.866 398.866 237 395 237 c 391.134 237 388 233.866 388 230 c"
    " 388 226.134 391.134 223 395 223 c 398.866 223 402 226.134 402 230 c h S",
    "388 230 m 402 230 l S",
    "395 223 m 395 237 l S",
]


def _cad_text(x: float, y: float, text: str, *, rendered_pt: float) -> str:
    scale = 0.12
    return (
        f"BT /F1 {rendered_pt / scale:.4f} Tf {scale} 0 0 {scale} {x} {y} Tm"
        f" ({text}) Tj ET"
    )


def _write_scaled_text_legend_sheet(path: Path) -> None:
    _write_pdf(
        path,
        [
            *_FIELD_GLYPHS,
            *_LEGEND_GLYPHS,
            _cad_text(370, 365, "ELECTRICAL SYMBOL LEGEND", rendered_pt=11.0),
            _cad_text(425, 317, "GFCI", rendered_pt=9.0),
            _cad_text(425, 272, "JBOX", rendered_pt=9.0),
            _cad_text(425, 227, "LIGHT", rendered_pt=9.0),
        ],
    )


def _legend_shape_matched_devices(model: BuildingModel) -> list:
    return [
        device
        for device in model.electrical_devices
        if device.attributes.get("pdf_electrical", {})
        .get("shape_recognition", {})
        .get("method")
        == "sheet-legend-geometry-match"
    ]


def test_legend_drawn_with_scaled_text_matches_field_glyphs(tmp_path: Path) -> None:
    pdf = tmp_path / "scaled-text-legend.pdf"
    _write_scaled_text_legend_sheet(pdf)

    extracted = extract_pdf(pdf, source_id="test:scaled-text-legend")
    assert extracted == extract_pdf(pdf, source_id="test:scaled-text-legend")
    sizes = {item.text: item.font_size_pt for item in extracted.texts}
    assert sizes == pytest.approx(
        {
            "ELECTRICAL SYMBOL LEGEND": 11.0,
            "GFCI": 9.0,
            "JBOX": 9.0,
            "LIGHT": 9.0,
        },
        abs=1e-3,
    )

    model = ElectricalPdfImporter().import_document(extracted)

    matched = _legend_shape_matched_devices(model)
    assert len(matched) == 6
    device_types = sorted(device.device_type for device in matched)
    assert device_types == [
        "junction_box",
        "junction_box",
        "luminaire",
        "luminaire",
        "receptacle",
        "receptacle",
    ]
    assert {
        device.attributes["pdf_electrical"]["shape_recognition"]["legend_label"]
        for device in matched
    } == {"GFCI", "JBOX", "LIGHT"}
    validate_model(model)


def test_legend_drawn_with_scaled_text_is_missed_with_raw_tf_sizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Control: record the raw Tf operand, as the extractor did before the fix.
    # The 75 pt labels then fail the short-label filter and no legend is found.
    monkeypatch.setattr(
        pdf_electrical_importer,
        "_rendered_font_size",
        lambda font_size, cm, tm: None if font_size is None else float(font_size),
    )
    pdf = tmp_path / "scaled-text-legend.pdf"
    _write_scaled_text_legend_sheet(pdf)

    extracted = extract_pdf(pdf, source_id="test:scaled-text-legend")
    assert {item.text: item.font_size_pt for item in extracted.texts}[
        "GFCI"
    ] == pytest.approx(75.0, abs=1e-3)

    model = ElectricalPdfImporter().import_document(extracted)

    assert _legend_shape_matched_devices(model) == []
