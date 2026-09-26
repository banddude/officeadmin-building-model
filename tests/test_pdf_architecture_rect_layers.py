"""Wall-layer rectangles keep their CAD layer and count as wall evidence.

Synthetic generated source PDFs only: every case draws its own optional-content
layers and rectangle bands, then runs the real ``extract_pdf`` and importer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    TextStringObject,
)

from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_architecture.layered_rooms import _wall_layer_lines
from oabm.importers.pdf_architecture.types import (
    ImportOptions,
    PdfDocumentObservation,
    PdfLineObservation,
    PdfPageObservation,
    PdfRectObservation,
    ScaleOverride,
)

_QUARTER_INCH_SCALE_M_PER_POINT = 0.016933333333
_TWO_INCH_WALL_THICKNESS_M = 0.0508


def _write_rect_source(
    path: Path,
    *,
    band_property: str = "WALL",
    twin_layers: bool = False,
    room_bands: bool = False,
) -> None:
    """Draw synthetic rectangles inside named optional-content groups.

    ``band_property`` selects the page /Properties key (and so the
    optional-content group) the room bands ride on; ``twin_layers`` draws one
    identical rectangle twice on two different layers.
    """

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    wall_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-WALL"),
    })
    note_group = DictionaryObject({
        NameObject("/Type"): NameObject("/OCG"),
        NameObject("/Name"): TextStringObject("A-TAG-NOTE"),
    })
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    wall_ref = writer._add_object(wall_group)
    note_ref = writer._add_object(note_group)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/Properties"): DictionaryObject({
            NameObject("/WALL"): wall_ref,
            NameObject("/NOTE"): note_ref,
        }),
    })
    writer._root_object[NameObject("/OCProperties")] = DictionaryObject({
        NameObject("/OCGs"): ArrayObject([wall_ref, note_ref]),
        NameObject("/D"): DictionaryObject({NameObject("/BaseState"): NameObject("/ON")}),
    })
    commands = [
        "BT /F1 12 Tf 1 0 0 1 20 740 Tm (X50 FLOOR PLAN) Tj ET",
    ]
    if room_bands:
        commands.extend((
            "BT /F1 10 Tf 1 0 0 1 20 720 Tm (SCALE: 1/4\" = 1'-0\") Tj ET",
            "BT /F1 10 Tf 1 0 0 1 190 330 Tm (ROOM: STUDIO) Tj ET",
            f"/OC /{band_property} BDC",
            # Four thin wall bands framing one room interior.
            "150 250 180 3 re S",
            "150 407 180 3 re S",
            "150 253 3 154 re S",
            "327 253 3 154 re S",
            "EMC",
        ))
    elif twin_layers:
        commands.extend((
            "/OC /WALL BDC",
            "150 250 180 3 re S",
            "EMC",
            "/OC /NOTE BDC",
            "150 250 180 3 re S",
            "EMC",
        ))
    else:
        commands.extend((
            "/OC /WALL BDC",
            "150 250 180 3 re S",
            "EMC",
            "410 600 40 40 re S",
        ))
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _import_options() -> ImportOptions:
    return ImportOptions(
        scale_overrides=(
            ScaleOverride(1, _QUARTER_INCH_SCALE_M_PER_POINT),
        ),
        default_wall_height_m=3.0,
    )


def test_rectangle_on_named_layer_keeps_layer(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-rect-layer.pdf"
    _write_rect_source(source)
    assert not source.with_suffix(".expected.json").exists()

    extracted = extract_pdf(source, source_id="fixture:rect-layer")
    rects = extracted.pages[0].rects
    assert [rect.source_layer for rect in rects] == ["A-WALL", None]
    # Layered and unlayered rectangles are distinct observations.
    assert len({rect.element_id for rect in rects}) == 2

    # Extraction of the same source is stable.
    assert extract_pdf(source, source_id="fixture:rect-layer") == extracted


def test_identical_rectangles_on_different_layers_both_kept(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-rect-twin-layers.pdf"
    _write_rect_source(source, twin_layers=True)

    extracted = extract_pdf(source, source_id="fixture:rect-twin-layers")
    rects = extracted.pages[0].rects
    assert len(rects) == 2
    assert {rect.source_layer for rect in rects} == {"A-WALL", "A-TAG-NOTE"}
    assert len({rect.bbox_pt for rect in rects}) == 1
    assert len({rect.element_id for rect in rects}) == 2
    assert extract_pdf(source, source_id="fixture:rect-twin-layers") == extracted


def test_thin_wall_layer_rectangles_close_a_room(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-rect-wall-bands.pdf"
    _write_rect_source(source, room_bands=True)

    extracted = extract_pdf(source, source_id="fixture:rect-wall-bands")
    assert len(extracted.pages[0].rects) == 4
    assert all(rect.source_layer == "A-WALL" for rect in extracted.pages[0].rects)

    model = import_observations(extracted, options=_import_options())
    assert len(model.walls) == 4
    assert len(model.spaces) == 1
    for wall in model.walls:
        attributes = wall.attributes["pdf_architecture"]
        assert attributes["recognition"] == "geometric_parallel_wall_faces"
        assert attributes["source_layers"] == ["A-WALL"]
        assert attributes["primitive_families"] == ["rect"]
        assert wall.thickness_m == pytest.approx(_TWO_INCH_WALL_THICKNESS_M)


def test_non_wall_layer_rectangles_do_not_close_a_room(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-rect-nonwall-bands.pdf"
    # /NOTE maps to the A-TAG-NOTE group, which is not a wall layer.
    _write_rect_source(source, room_bands=True, band_property="NOTE")

    model = import_observations(
        extract_pdf(source, source_id="fixture:rect-nonwall-bands"),
        options=_import_options(),
    )
    assert model.walls == ()
    assert model.spaces == ()
    assert model.attributes["pdf_architecture"]["pages"][0]["status"] == (
        "no_supported_geometry_recognized"
    )


def test_rect_wall_import_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-rect-determinism.pdf"
    _write_rect_source(source, room_bands=True)

    first = import_observations(
        extract_pdf(source, source_id="fixture:rect-determinism"),
        options=_import_options(),
    )
    second = import_observations(
        extract_pdf(source, source_id="fixture:rect-determinism"),
        options=_import_options(),
    )
    assert first.to_json() == second.to_json()


def test_wall_layer_lines_include_wall_layer_rect_edges() -> None:
    page = PdfPageObservation(
        page_number=1,
        width_pt=612,
        height_pt=792,
        lines=(
            PdfLineObservation(
                element_id="p1:line:probe",
                start_pt=(20.0, 30.0),
                end_pt=(120.0, 30.0),
                source_layers=("A-WALL",),
            ),
        ),
        rects=(
            PdfRectObservation(
                element_id="p1:rect:wall",
                bbox_pt=(40.0, 60.0, 160.0, 63.0),
                source_layer="A-WALL",
            ),
            PdfRectObservation(
                element_id="p1:rect:note",
                bbox_pt=(200.0, 60.0, 320.0, 63.0),
                source_layer="A-TAG-NOTE",
            ),
            PdfRectObservation(
                element_id="p1:rect:unlayered",
                bbox_pt=(400.0, 60.0, 520.0, 63.0),
            ),
        ),
    )

    wall_lines = _wall_layer_lines(page)
    # The wall line plus the four edges of the wall-layer rectangle only.
    assert len(wall_lines) == 5
    assert "p1:line:probe" in {line.element_id for line in wall_lines}
    rect_edges = [line for line in wall_lines if line.primitive_family == "rect"]
    assert len(rect_edges) == 4
    for edge in rect_edges:
        assert edge.source_layers == ("A-WALL",)
        for point in (edge.start_pt, edge.end_pt):
            assert 40.0 <= point[0] <= 160.0 and 60.0 <= point[1] <= 63.0
