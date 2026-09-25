"""#72: square annotation codes, status-marker annotations and adjacent counts.

Every case starts from the public synthetic notes-column fixture, edits its
content stream and replaces its square annotations, writes the edited PDF, and
runs it through the real extractor and importer. No private plan data and no
pre-extracted answer keys are used.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf
from oabm.model import validate_model

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "fixtures"
    / "pdf_electrical"
    / "geometry-only-power-sheet-notes-column-legend.pdf"
)
# The fixture's own square annotations: a card reader and a CATV outlet code
# drawn on their field glyphs.
FIXTURE_ANNOTATIONS = (("CR", 84.0, 170.0), ("TV", 300.0, 170.0))
# Legend rows of the ruled SYMBOL | FUNCTION table (symbol cell x 594..636).
CATV_LABEL = b"1 0 0 1 640 162 Tm (cable TV outlet) Tj"
QUAD_EXISTING_MARKER = b"BT /F2 6 Tf 1 0 0 1 308 504 Tm (E) Tj ET"
DATA_JBOX_EXISTING_MARKER = b"BT /F2 6 Tf 1 0 0 1 308 284 Tm (E) Tj ET"


def _text(x: float, y: float, value: str, size: float = 5.5) -> bytes:
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({value}) Tj ET\n".encode("ascii")


def _ring(x: float, y: float, radius: float) -> bytes:
    k = 0.5523 * radius
    return (
        f".7 w n {x + radius} {y} m "
        f"{x + radius} {y + k} {x + k} {y + radius} {x} {y + radius} c "
        f"{x - k} {y + radius} {x - radius} {y + k} {x - radius} {y} c "
        f"{x - radius} {y - k} {x - k} {y - radius} {x} {y - radius} c "
        f"{x + k} {y - radius} {x + radius} {y - k} {x + radius} {y} c S\n"
    ).encode("ascii")


def _variant(
    tmp_path: Path,
    name: str,
    *edits: tuple[bytes, bytes],
    append: bytes = b"",
    annotations: tuple[tuple[str, float, float], ...] = FIXTURE_ANNOTATIONS,
) -> Path:
    writer = PdfWriter(clone_from=PdfReader(FIXTURE))
    page = writer.pages[0]
    data = page.get_contents().get_data()
    for old, new in edits:
        assert data.count(old) == 1, old
        data = data.replace(old, new)
    stream = DecodedStreamObject()
    stream.set_data(data + append)
    page[NameObject("/Contents")] = writer._add_object(stream)
    annots = ArrayObject()
    for contents, x, y in annotations:
        annots.append(
            writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Square"),
                        NameObject("/Rect"): ArrayObject(
                            [
                                NumberObject(x - 4),
                                NumberObject(y - 4),
                                NumberObject(x + 4),
                                NumberObject(y + 4),
                            ]
                        ),
                        NameObject("/Contents"): TextStringObject(contents),
                    }
                )
            )
        )
    page[NameObject("/Annots")] = annots
    path = tmp_path / f"{name}.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


def _model(path: Path):
    model = ElectricalPdfImporter().import_document(
        extract_pdf(path, source_id=f"fixture:{path.stem}")
    )
    validate_model(model)
    return model


def _lane(device) -> dict:
    return device.attributes["pdf_electrical"]


def _at(model, device_type: str, x: float, y: float):
    matches = [
        device
        for device in model.electrical_devices
        if device.device_type == device_type
        and abs(_lane(device)["source_position_pt"]["x"] - x) <= 3.0
        and abs(_lane(device)["source_position_pt"]["y"] - y) <= 3.0
    ]
    assert len(matches) == 1, [
        (d.device_type, _lane(d)["source_position_pt"]) for d in model.electrical_devices
    ]
    return matches[0]


def _unresolved_for(model, element_id: str) -> list[dict]:
    return [
        row
        for row in model.attributes["pdf_electrical"]["unresolved_observations"]
        if row.get("source_element_id") == element_id
    ]


def test_status_letter_annotation_marks_the_glyph_beside_it(tmp_path: Path) -> None:
    # The quad's printed E is replaced by a square annotation carrying E, and a
    # second E annotation marks nothing.
    path = _variant(
        tmp_path,
        "status-annotation",
        (QUAD_EXISTING_MARKER, b""),
        annotations=(*FIXTURE_ANNOTATIONS, ("E", 308.0, 504.0), ("E", 520.0, 450.0)),
    )
    model = _model(path)
    assert len(model.electrical_devices) == 16

    quad = _at(model, "receptacle_quad", 298.0, 500.0)
    lane = _lane(quad)
    assert lane["status"] == "E"
    assert lane["scope_status"] == "existing_to_remain"
    assert lane["scope_marker_source_element_id"] == "p1:annotation:0003:text"
    assert any(
        record.method == "pdf-field-status-tag"
        and record.source_element_id == "p1:annotation:0003:text"
        for record in quad.provenance
    )
    # A status letter is never an annotation code or a device of its own.
    assert not any(
        row.get("annotation_code") == "E"
        for row in model.attributes["pdf_electrical"]["unresolved_observations"]
    )
    assert _unresolved_for(model, "p1:annotation:0003") == []
    unbound = _unresolved_for(model, "p1:annotation:0004")
    assert len(unbound) == 1
    assert unbound[0]["status"] == "unbound_status_marker"
    assert unbound[0]["field_status_marker"] == "E"
    assert unbound[0]["reason"] == (
        "status marker annotation is not adjacent to a recognized device"
    )


def test_symbol_cell_code_names_its_legend_row(tmp_path: Path) -> None:
    # The CATV row reads "cable T.V. outlet" and draws CX in its symbol cell.
    # One CX annotation sits on a CATV glyph, one on no glyph at all.
    path = _variant(
        tmp_path,
        "symbol-cell-code",
        (CATV_LABEL, CATV_LABEL.replace(b"cable TV outlet", b"cable T.V. outlet")),
        append=_text(597.0, 148.0, "CX", size=4.0),
        annotations=(("CR", 84.0, 170.0), ("CX", 300.0, 170.0), ("CX", 500.0, 230.0)),
    )
    model = _model(path)
    lane = model.attributes["pdf_electrical"]
    region = lane["legend_recognition"]["regions"][0]
    assert region["classified_row_count"] == 8

    catv = [d for d in model.electrical_devices if d.device_type == "catv_outlet"]
    assert len(catv) == 3
    on_glyph = _at(model, "catv_outlet", 298.0, 170.0)
    recognition = _lane(on_glyph)["annotation_recognition"]
    assert recognition["match_kind"] == "legend-symbol-code"
    assert recognition["legend_symbol_code_text"] == "CX"
    assert recognition["legend_row_label"] == "cable T.V. outlet"
    assert _lane(on_glyph)["shape_recognition"]["canonical_type"] == "catv_outlet"

    standalone = _at(model, "catv_outlet", 500.0, 230.0)
    assert "shape_recognition" not in _lane(standalone)
    assert _lane(standalone)["annotation_code"] == "CX"
    assert _lane(standalone)["stable_identity_key"] == (
        "annotation:annotation:square:p1:annotation:0003"
    )
    assert not any(
        row.get("kind") == "legend_label"
        for row in lane["unresolved_observations"]
    )


def test_symbol_cell_code_shared_by_rows_of_different_types_fails_closed(
    tmp_path: Path,
) -> None:
    path = _variant(
        tmp_path,
        "shared-symbol-code",
        append=_text(597.0, 148.0, "CX", size=4.0) + _text(597.0, 184.0, "CX", size=4.0),
        annotations=(*FIXTURE_ANNOTATIONS, ("CX", 500.0, 230.0)),
    )
    model = _model(path)
    rows = _unresolved_for(model, "p1:annotation:0003")
    assert len(rows) == 1
    assert rows[0]["status"] == "unresolved_classification"
    assert rows[0]["reason"] == (
        "annotation code is drawn in the symbol cell of legend rows with different types"
    )
    assert {
        candidate["canonical_type"]
        for candidate in rows[0]["annotation_code_recognition"]["classification_candidates"]
    } == {"access_control_device", "catv_outlet"}
    assert not any(
        _lane(device).get("annotation_code") == "CX"
        for device in model.electrical_devices
    )


def test_each_annotation_code_is_its_own_instance(tmp_path: Path) -> None:
    # Two J codes 40 pt apart with no glyph under them are two J-boxes, not
    # one. Two J codes near one J-box glyph: only the nearer one names it.
    codes = (
        ("J", 100.0, 292.0),
        ("J", 140.0, 230.0),
        ("J", 180.0, 230.0),
        ("J", 84.0, 282.0),
    )
    # Annotation order sets element ids: the code drawn on the glyph is the
    # sixth annotation forward and the third in reverse.
    for name, ordered, glyph_code_id in (
        ("codes-forward", codes, "p1:annotation:0006"),
        ("codes-reverse", tuple(reversed(codes)), "p1:annotation:0003"),
    ):
        model = _model(
            _variant(
                tmp_path,
                name,
                append=_text(597.0, 256.0, "J", size=4.0),
                annotations=(*FIXTURE_ANNOTATIONS, *ordered),
            )
        )
        boxes = [
            d for d in model.electrical_devices if d.device_type == "junction_box_power"
        ]
        # Two drawn glyphs plus three codes that sit on no recognized glyph.
        assert len(boxes) == 5
        glyph = _at(model, "junction_box_power", 82.0, 280.0)
        recognition = _lane(glyph)["annotation_recognition"]
        assert recognition["match_kind"] == "legend-symbol-code"
        assert recognition["annotation_code"] == "J"
        assert {
            record.source_element_id
            for record in glyph.provenance
            if record.method == "annotation-code"
        } == {glyph_code_id}
        for x, y in ((100.0, 292.0), (140.0, 230.0), (180.0, 230.0)):
            box = _at(model, "junction_box_power", x, y)
            assert "shape_recognition" not in _lane(box)
            assert _lane(box)["annotation_code"] == "J"
        # The other glyph is 51 pt from the nearest code and keeps no code.
        assert "annotation_code" not in _lane(
            _at(model, "junction_box_power", 190.0, 280.0)
        )


def test_annotation_text_that_names_no_legend_row_says_so(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "room-label-annotations",
        annotations=(*FIXTURE_ANNOTATIONS, ("LOBBY 2", 480.0, 520.0), ("X.Y.Z.", 480.0, 120.0)),
    )
    model = _model(path)
    assert len(model.electrical_devices) == 16
    for element_id, normalized in (
        ("p1:annotation:0003", "LOBBY 2"),
        ("p1:annotation:0004", "X Y Z"),
    ):
        rows = _unresolved_for(model, element_id)
        assert len(rows) == 1
        assert rows[0]["reason"] == "annotation code matches no classified legend row"
        assert rows[0]["annotation_code_recognition"]["normalized_code"] == normalized


def test_adjacent_count_reads_only_what_the_legend_row_defines(tmp_path: Path) -> None:
    model = _model(FIXTURE)
    counted = _at(model, "junction_box_power", 82.0, 280.0)
    lane = _lane(counted)
    assert lane["adjacent_count"] == 3
    assert lane["adjacent_count_unit"] == "circuits"
    assert lane["adjacent_count_status"] == "read"
    assert lane["adjacent_count_source_element_id"] in lane["source_element_ids"]
    assert any(
        record.method == "pdf-field-adjacent-count"
        and record.attributes["adjacent_count"] == 3
        for record in counted.provenance
    )
    uncounted = _lane(_at(model, "junction_box_power", 190.0, 280.0))
    assert uncounted["adjacent_count_status"] == "no_adjacent_number"
    assert "adjacent_count" not in uncounted
    for device in model.electrical_devices:
        if device.device_type == "junction_box_data":
            assert _lane(device)["adjacent_count_unit"] == "lines"
    # The duplex row defines no count, so the number beside a duplex is not one.
    duplex = _lane(_at(model, "receptacle_duplex", 82.0, 500.0))
    assert not any(key.startswith("adjacent_count") for key in duplex)
    # Counting never multiplies devices.
    assert len(model.electrical_devices) == 16


def test_count_fused_with_status_marker_sets_both(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "count-status-token",
        (DATA_JBOX_EXISTING_MARKER, DATA_JBOX_EXISTING_MARKER.replace(b"(E)", b"(2E)")),
    )
    model = _model(path)
    lane = _lane(_at(model, "junction_box_data", 298.0, 280.0))
    assert lane["status"] == "E"
    assert lane["scope_status"] == "existing_to_remain"
    assert lane["adjacent_count"] == 2
    assert lane["adjacent_count_unit"] == "lines"
    assert lane["adjacent_count_source_element_id"] == lane[
        "scope_marker_source_element_id"
    ]


def test_bubbled_or_contested_numbers_are_not_counts(tmp_path: Path) -> None:
    # A keynote-style bubble number beside the new J-box, and two different
    # numbers equidistant from the new data J-box.
    path = _variant(
        tmp_path,
        "count-negatives",
        append=(
            _ring(174.0, 290.0, 5.0)
            + _text(172.5, 288.0, "5")
            + _text(396.0, 280.0, "2")
            + _text(416.0, 280.0, "6")
        ),
    )
    model = _model(path)
    bubbled = _lane(_at(model, "junction_box_power", 190.0, 280.0))
    assert bubbled["adjacent_count_status"] == "no_adjacent_number"
    contested = _lane(_at(model, "junction_box_data", 406.0, 280.0))
    assert contested["adjacent_count_status"] == "ambiguous"
    assert "adjacent_count" not in contested
    assert sorted(item["count"] for item in contested["adjacent_count_candidates"]) == [2, 6]


def test_annotation_and_count_results_are_deterministic(tmp_path: Path) -> None:
    path = _variant(
        tmp_path,
        "determinism",
        (DATA_JBOX_EXISTING_MARKER, DATA_JBOX_EXISTING_MARKER.replace(b"(E)", b"(2E)")),
        (QUAD_EXISTING_MARKER, b""),
        append=_text(597.0, 256.0, "J", size=4.0),
        annotations=(
            *FIXTURE_ANNOTATIONS,
            ("E", 308.0, 504.0),
            ("J", 140.0, 230.0),
            ("J", 180.0, 230.0),
            ("LOBBY 2", 480.0, 520.0),
        ),
    )
    first = extract_pdf(path, source_id="fixture:annotation-determinism")
    second = extract_pdf(path, source_id="fixture:annotation-determinism")
    assert first == second
    assert (
        ElectricalPdfImporter().import_document(first).to_dict()
        == ElectricalPdfImporter().import_document(second).to_dict()
    )
