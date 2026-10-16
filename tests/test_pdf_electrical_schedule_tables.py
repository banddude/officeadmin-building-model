"""Independent synthetic ruled schedules; no customer data or recognition labels."""
from copy import deepcopy
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                          FloatObject, NameObject)

from oabm.importers.pdf_electrical.importer import (
    _detect_lighting_fixture_schedules, _lighting_heading_kind,
    _lighting_schedule_attributes, ElectricalPdfImporter, extract_pdf,
)


def _write_schedule(path: Path, *, heading="LIGHTING FIXTURE SCHEDULE",
                    rows=None, offset=(0, 0), rotate=0, merged_body=False):
    # Explicit expected content is authored here, independently of detection.
    if rows is None:
        rows = [(f"Q-{i + 1}", "WALL LIGHT") for i in range(30)]
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    dx, dy = offset
    page.mediabox = ArrayObject([FloatObject(dx), FloatObject(dy),
                                FloatObject(600 + dx), FloatObject(800 + dy)])
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
        DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    bottom = 700 - 20 * len(rows)
    commands = [f"q 1 0 0 1 {dx} {dy} cm", "0.5 w"]
    for y in [760, 730, 700, *[700 - 20 * (i + 1) for i in range(len(rows))]]:
        commands.append(f"40 {y} m 450 {y} l S")
    for x in [40, 450]:
        commands.append(f"{x} {bottom} m {x} 760 l S")
    if merged_body:
        commands.extend([f"130 {bottom} m 130 680 l S", "130 700 m 130 730 l S"])
    else:
        commands.append(f"130 {bottom} m 130 730 l S")
    commands.append(f"BT /F1 9 Tf 50 740 Td ({heading}) Tj ET")
    # One native visitor callback per row, yet geometrically distinct cells.
    commands.append("BT /F1 9 Tf 50 710 Td (TAG) Tj 90 0 Td (DESCRIPTION) Tj ET")
    for i, (tag, description) in enumerate(rows):
        commands.append(f"BT /F1 9 Tf 50 {690 - 20*i} Td ({tag}) Tj "
                        f"90 0 Td ({description}) Tj ET")
    commands.append("Q")
    content = DecodedStreamObject()
    content.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    if rotate:
        page.rotate(rotate)
    writer.write(path)


def _parse(document, tables=None):
    if tables is None:
        tables = document.page_provenance.get(1, {}).get("lighting_schedule_tables", ())
    return _detect_lighting_fixture_schedules(document.texts, table_evidence=tables)


def test_merged_native_rows_use_observed_grid_beyond_old_span(tmp_path):
    path = tmp_path / "schedule.pdf"
    _write_schedule(path)
    document = extract_pdf(path)
    assert len(document.texts) == 32  # heading, merged header, 30 merged rows
    assert "TAG DESCRIPTION" in document.texts[1].text
    rows, claimed, unresolved, metadata = _parse(document)
    assert not unresolved
    assert len(rows) == 30
    assert rows[(1, "Q-30")].fields == {"tag": "Q-30", "description": "WALL LIGHT"}
    final = rows[(1, "Q-30")]
    assert len(final.source_element_ids) == 1
    assert set(final.source_element_ids) <= {item.element_id for item in document.texts}
    assert final.cell_evidence[0]["bbox_pt"] == [40.0, 100.0, 130.0, 120.0]
    assert final.cell_evidence[1]["bbox_pt"] == [130.0, 100.0, 450.0, 120.0]
    assert final.cell_evidence[0]["source_element_ids"] == list(final.source_element_ids)
    assert metadata[0]["bbox_pt"] == [40.0, 100.0, 450.0, 760.0]
    assert metadata[0]["row_count"] == 30
    assert _lighting_schedule_attributes(final)["derived_cells"][1]["field"] == "description"
    assert _parse(extract_pdf(path)) == (rows, claimed, unresolved, metadata)


def test_offset_media_box_preserves_cells_and_native_ids(tmp_path):
    base, shifted = tmp_path / "base.pdf", tmp_path / "shifted.pdf"
    _write_schedule(base)
    _write_schedule(shifted, offset=(-320, 450))
    a, b = extract_pdf(base), extract_pdf(shifted)
    assert a.texts == b.texts
    assert _parse(a) == _parse(b)


@pytest.mark.parametrize("heading", ["PLUMBING FIXTURE SCHEDULE",
    "MECHANICAL FIXTURE SCHEDULE", "HVAC LIGHTING FIXTURE SCHEDULE",
    "PLUMBING FIXTURE SCHEDULE - MECHANICAL ROOM",
    "MECHANICAL ROOM FIXTURE SCHEDULE"])
def test_unrelated_trade_schedule_is_not_lighting(tmp_path, heading):
    path = tmp_path / "other-trade.pdf"
    _write_schedule(path, heading=heading)
    document = extract_pdf(path)
    assert not any(_lighting_heading_kind(item) for item in document.texts)
    assert "lighting_schedule_tables" not in document.page_provenance[1]
    assert _parse(document) == ({}, set(), [], [])


@pytest.mark.parametrize("descriptions,expected", [
    (("WALL LIGHT", "WALL LIGHT"), 1),
    (("WALL LIGHT", "PENDANT"), 0),
])
def test_duplicate_and_conflicting_rows(tmp_path, descriptions, expected):
    path = tmp_path / "duplicates.pdf"
    _write_schedule(path, rows=[("Q-1", description) for description in descriptions])
    rows, _, unresolved, _ = _parse(extract_pdf(path))
    assert len(rows) == expected
    if expected:
        assert len(rows[(1, "Q-1")].source_element_ids) == 2
        assert len(rows[(1, "Q-1")].cell_evidence) == 4
    assert [item["reason_code"] for item in unresolved] == (
        [] if expected else ["lighting_schedule_tag_conflict"])


@pytest.mark.parametrize("damage,reason", [
    ("merged", "lighting_schedule_merged_body_cell"),
    ("unbound", "lighting_schedule_cell_source_unresolved"),
    ("conflicting_header", "lighting_schedule_header_conflict"),
    ("duplicate_header", "lighting_schedule_header_not_confirmed"),
    ("ambiguous_table", "lighting_schedule_table_boundary_ambiguous"),
])
def test_malformed_evidence_stays_unresolved(tmp_path, damage, reason):
    path = tmp_path / "damaged.pdf"
    _write_schedule(path, rows=[("Q-1", "WALL LIGHT")])
    document = extract_pdf(path)
    tables = deepcopy(document.page_provenance[1]["lighting_schedule_tables"])
    table = tables[0]
    if damage == "merged":
        table["rows"][2][1] = None
    elif damage == "unbound":
        table["rows"][2][1]["source_element_ids"] = ["nonexistent"]
    elif damage == "conflicting_header":
        table["rows"][1].append(deepcopy(table["rows"][1][1]))
    elif damage == "duplicate_header":
        table["rows"].append(deepcopy(table["rows"][1]))
    elif damage == "ambiguous_table":
        tables.append(deepcopy(table))
    rows, _, unresolved, _ = _parse(document, tables)
    assert not rows
    assert reason in {item["reason_code"] for item in unresolved}


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_table_never_guesses_reflowed_columns(tmp_path, rotation):
    path = tmp_path / "rotated.pdf"
    _write_schedule(path, rows=[("Q-1", "WALL LIGHT")], rotate=rotation)
    document = extract_pdf(path)
    rows, _, unresolved, _ = _parse(document)
    # A rotated cell reader may leave text unreadable. It must not transpose
    # the columns into a different fixture identity or silently fall back.
    if rows:
        assert rows[(1, "Q-1")].fields == {"tag": "Q-1", "description": "WALL LIGHT"}
    else:
        assert unresolved
    width, height = ((800, 600) if rotation in {90, 270} else (600, 800))
    for table in document.page_provenance[1].get("lighting_schedule_tables", ()):
        x0, y0, x1, y1 = table["bbox_pt"]
        assert 0 <= x0 < x1 <= width
        assert 0 <= y0 < y1 <= height


def test_schedule_cells_reach_import_metadata_without_inventing_fixtures(tmp_path):
    path = tmp_path / "schedule-only.pdf"
    _write_schedule(path, rows=[("Q-1", "WALL LIGHT")])
    model = ElectricalPdfImporter().import_pdf(path)
    assert not model.electrical_devices
    from oabm.model import validate_model
    validate_model(model)
    # The table is evidence about fixture types, not placed fixture geometry.
    assert "lighting-fixture-schedule-ruled-cells" in model.to_json()


def test_observed_merged_body_grid_cannot_invent_a_column_split(tmp_path):
    path = tmp_path / "merged-body.pdf"
    _write_schedule(path, rows=[("Q-1", "WALL LIGHT"), ("Q-2", "PENDANT")],
                    merged_body=True)
    document = extract_pdf(path)
    table = document.page_provenance[1]["lighting_schedule_tables"][0]
    assert table["rows"][2][1] is None
    rows, _, unresolved, _ = _parse(document)
    assert set(rows) == {(1, "Q-2")}
    assert [item["reason_code"] for item in unresolved] == ["lighting_schedule_merged_body_cell"]


@pytest.mark.parametrize("heading", [
    "LIGHTING FIXTURE SCHEDULE - MECHANICAL ROOM",
    "MECHANICAL ROOM LIGHTING FIXTURE SCHEDULE",
    "LIGHTING FIXTURE SCHEDULE - HVAC EQUIPMENT ROOM",
])
def test_location_qualifier_does_not_change_lighting_trade(tmp_path, heading):
    path = tmp_path / "qualified-lighting.pdf"
    _write_schedule(path, heading=heading, rows=[("Q-1", "WALL LIGHT")])
    rows, _, unresolved, _ = _parse(extract_pdf(path))
    assert not unresolved
    assert rows[(1, "Q-1")].fields["description"] == "WALL LIGHT"


@pytest.mark.parametrize("enclosed_body", [False, True])
def test_decoy_heading_box_and_enclosed_body_are_distinguished(tmp_path, enclosed_body):
    path = tmp_path / "decoy-box.pdf"
    _write_schedule(path, rows=[("Q-1", "WALL LIGHT")])
    # Replace the authored geometry with a two-cell heading box and separate
    # unruled schedule columns below it. The box contains no schedule header.
    from pypdf import PdfReader
    reader = PdfReader(path)
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    outline = "40 660 410 100 re S" if enclosed_body else "40 730 410 30 re S"
    commands = [outline, "40 730 m 450 730 l S", "400 730 m 400 760 l S",
                "BT /F1 9 Tf 50 740 Td (LIGHTING FIXTURE SCHEDULE) Tj ET",
                "BT /F1 9 Tf 50 710 Td (TAG) Tj ET",
                "BT /F1 9 Tf 140 710 Td (DESCRIPTION) Tj ET",
                "BT /F1 9 Tf 50 690 Td (Q-1) Tj ET",
                "BT /F1 9 Tf 140 690 Td (WALL LIGHT) Tj ET"]
    content = DecodedStreamObject()
    content.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)
    document = extract_pdf(path)
    tables = document.page_provenance[1]["lighting_schedule_tables"]
    assert len(tables) == 1  # A real observed decoy, not fabricated metadata.
    baseline, _, _, _ = _parse(document, [])
    assert baseline[(1, "Q-1")].fields["description"] == "WALL LIGHT"
    rows, claimed, unresolved, _ = _parse(document)
    assert any(item["reason_code"] == "lighting_schedule_header_not_confirmed"
               for item in unresolved)
    if enclosed_body:
        assert len(tables[0]["rows"]) == 2
        assert tables[0]["rows"][1][1] is None
    assert rows == baseline
    assert not rows[(1, "Q-1")].cell_evidence
    assert all(source_id in claimed for source_id in rows[(1, "Q-1")].source_element_ids)


def test_zero_header_frame_does_not_split_merged_native_callbacks(tmp_path):
    path = tmp_path / "merged-frame.pdf"
    _write_schedule(path, rows=[("Q-1", "WALL LIGHT")])
    from pypdf import PdfReader
    reader = PdfReader(path)
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    commands = ["40 660 410 100 re S", "40 730 m 450 730 l S",
                "400 730 m 400 760 l S",
                "BT /F1 9 Tf 50 740 Td (LIGHTING FIXTURE SCHEDULE) Tj ET",
                "BT /F1 9 Tf 50 710 Td (TAG) Tj 90 0 Td (DESCRIPTION) Tj ET",
                "BT /F1 9 Tf 50 690 Td (Q-1) Tj 90 0 Td (WALL LIGHT) Tj ET"]
    content = DecodedStreamObject()
    content.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)
    document = extract_pdf(path)
    assert len(document.page_provenance[1]["lighting_schedule_tables"]) == 1
    assert len(document.texts) == 3
    assert not _parse(document, [])[0]
    rows, _, unresolved, _ = _parse(document)
    assert not rows
    assert any(item.get("header_candidate_count") == 0 for item in unresolved)
