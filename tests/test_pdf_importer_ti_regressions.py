"""Public-safe generated sheets for the two synthetic #259 failure modes."""
from pathlib import Path

import pytest
from pypdf import PdfWriter, PdfReader
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_architecture import import_architectural_pdf
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import region_wall_evidence, printed_sheet_scale
from oabm.importers.pdf_electrical import ElectricalPdfImporter, extract_pdf as extract_electrical_pdf
from oabm.model import validate_model


def _write_sheet(path: Path, commands: list[str]) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=1000, height=800)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(("\n".join(commands) + "\n").encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _text(x: float, y: float, value: str, size: float = 10) -> str:
    return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({value}) Tj ET"


def _architecture_commands(scale: str) -> list[str]:
    return [
        _text(830, 60, "A-101 FLOOR PLAN"),
        _text(830, 95, "DRAWN BY: TEST"),
        _text(830, 130, "PROJECT NO: SYNTHETIC"),
        _text(40, 50, scale),
        _text(40, 80, "LEVEL: GROUND"),
        _text(40, 100, "CEILING HEIGHT: 9'-0\""),
        _text(240, 400, "ROOM: WORKROOM"),
        "20 20 960 760 re S", "23 23 954 754 re S",
        "810 23 167 754 re S", "813 26 161 748 re S",
        # Untagged paired faces, drawn as lines, and a partition with a door gap.
        "100 200 m 700 200 l S", "700 200 m 700 650 l S",
        "700 650 m 100 650 l S", "100 650 m 100 200 l S",
        "104 204 m 696 204 l S", "696 204 m 696 646 l S",
        "696 646 m 104 646 l S", "104 646 m 104 204 l S",
        "400 204 m 400 350 l S", "404 204 m 404 350 l S",
        "400 380 m 400 646 l S", "404 380 m 404 646 l S",
    ]


@pytest.mark.parametrize("scale", [
    'SCALE: 1/8" = 1\'-0"',
    r"SCALE: 1/8\224 = 1\222-0\224",
])
@pytest.mark.parametrize("boxed_title", [True, False])
def test_floor_plan_evidence_excludes_title_block_and_keeps_partitions(tmp_path, scale, boxed_title):
    path = tmp_path / "synthetic-plan.pdf"
    commands = _architecture_commands(scale)
    if not boxed_title:
        commands = [command for command in commands
                    if not command.startswith(("810 ", "813 "))]
    _write_sheet(path, commands)
    page = extract_pdf(path).pages[0]
    resolved = printed_sheet_scale(page)
    assert resolved is not None
    assert resolved[0] == pytest.approx(0.0254 * 96 / 72)
    _, evidence = region_wall_evidence(page, (0, 0, 1000, 800), resolved[0])
    assert evidence
    assert all(90 < p[0] < 710 and 190 < p[1] < 660
               for item in evidence for p in (item.start_pt, item.end_pt))
    model = import_architectural_pdf(path, source_id="synthetic:ti-wall-regression")
    assert len(model.walls) == 6
    assert len(model.attributes["pdf_architecture"]["drawing_regions"]) == 1
    assert not any(space.name in {"DRAWN BY: TEST", "PROJECT NO: SYNTHETIC"}
                   for space in model.spaces)
    assert model.to_json() == import_architectural_pdf(
        path, source_id="synthetic:ti-wall-regression",
    ).to_json()


def test_t_junction_without_consumed_shell_and_unsupported_parallel_faces(tmp_path):
    path = tmp_path / "synthetic-unlabelled-plan.pdf"
    commands = [c for c in _architecture_commands('SCALE: 1/8" = 1\'-0"')
                if "ROOM: WORKROOM" not in c]
    # These two long faces have no crossing junction at either end.
    commands += ["500 350 m 500 500 l S", "504 350 m 504 500 l S"]
    # An outward stub has only a mid-face junction, with no interior proof.
    commands += ["700 500 m 800 500 l S", "700 504 m 800 504 l S"]
    _write_sheet(path, commands)
    model = import_architectural_pdf(path)
    assert len(model.walls) == 6
    assert len(model.spaces) == 1
    assert model.spaces[0].name is None
    assert len({wall.id for wall in model.walls}) == 6


def _fixture(x, y):
    return f"{x-7} {y-5} m {x+6} {y-5} l {x+6} {y+1} l {x+1} {y+1} l {x+1} {y+6} l {x-7} {y+6} l h S"


def _switch(x, y):
    return f"{x-5} {y-5} m {x+5} {y-5} l {x} {y+5} l h S"


def _lighting_set(tmp_path, *, reference=True, duplicate_legend=False, other_legend=False,
                  circuit=False, ambiguous_other=False, reverse_notes=False,
                  page_order=None, local_legend=False):
    legend = [_text(900, 40, "E002"), _text(600, 730, "LIGHTING FIXTURE LEGEND")]
    for tag, y in zip(("RE", "EX", "L1", "L2"), (690, 660, 630, 600)):
        legend.extend([_fixture(610, y), _text(632, y, tag, 8)])
    for code, y, description in (("S", 690, "SINGLE POLE SWITCH"),
                                ("S3", 660, "3-WAY SWITCH"),
                                ("S4", 630, "4-WAY SWITCH")):
        legend.extend([_switch(740, y), _text(756, y, code, 8),
                       _text(780, y, description, 7)])
    field = [_text(900, 40, "A107"), _text(20, 740, "REFLECTED CEILING PLAN")]
    if reference:
        field.append(_text(20, 700, "SEE E002 FOR LIGHTING LEGEND"))
    if other_legend:
        field.append(_text(20, 680, "SEE E003 FOR LIGHTING LEGEND"))
    if reverse_notes:
        field[2:4] = reversed(field[2:4])
    for x, tag in ((100, "RE"), (250, "EX"), (400, "L1"), (550, "L2"), (700, "RE")):
        field.extend([_fixture(x, 450), _text(x+18, 450, tag, 8)])
    for x, code in ((100, "S"), (250, "S3"), (400, "S4")):
        field.extend([_switch(x, 300), _text(x+16, 300, code, 8)])
    # A naked known tag and a known tag beside mismatched geometry stay out.
    field.extend([_text(100, 100, "RE", 8), _switch(250, 100), _text(266, 100, "RE", 8)])
    if circuit:
        field.extend([
            _text(300, 700, "PANEL LP 120/208V 3PH"), _text(35, 515, "LP-12", 8),
            "40 495 m 30 500 l 40 505 l S", "30 500 m 100 450 l S",
        ])
    if local_legend:
        field.extend(legend[1:])
    writer = PdfWriter()
    sheets = [legend, field]
    if duplicate_legend:
        sheets.append(legend)
    if other_legend:
        sheets.append([command.replace("E002", "E003") for command in legend])
    if ambiguous_other:
        assert other_legend
        sheets.append(sheets[-1])
    if page_order is not None:
        sheets = [sheets[index] for index in page_order]
    for index, commands in enumerate(sheets):
        page_path = tmp_path / f"synthetic-page-{index}.pdf"
        _write_sheet(page_path, commands)
        writer.add_page(PdfReader(page_path).pages[0])
    path = tmp_path / "synthetic-lighting-set.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_lighting_tags_resolve_against_explicitly_referenced_legend(tmp_path):
    path = _lighting_set(tmp_path)
    model = ElectricalPdfImporter().import_pdf(path, source_id="synthetic:ti-lighting")
    fixtures = [d for d in model.electrical_devices if d.device_type == "luminaire"]
    switches = [d for d in model.electrical_devices if d.device_type == "switch"]
    assert sorted(d.name for d in fixtures) == ["EX", "L1", "L2", "RE", "RE"]
    assert sorted(d.name for d in switches) == ["S", "S3", "S4"]
    assert next(d for d in switches if d.name == "S4").attributes["pdf_electrical"]["switch_type"] == "four_way"
    assert not model.circuits
    assert all(p.page == 2 for d in model.electrical_devices for p in d.provenance
               if p.method == "pdf-lighting-fixture-tag")
    assert all(any(p.page == 1 and "legend" in p.method
                   for p in d.provenance) for d in model.electrical_devices)
    assert all(any(p.method == "pdf-lighting-legend-reference" for p in d.provenance)
               for d in model.electrical_devices)
    validate_model(model)
    assert model.to_json() == ElectricalPdfImporter().import_pdf(
        path, source_id="synthetic:ti-lighting",
    ).to_json()


def test_referenced_lighting_devices_can_be_circuit_loads(tmp_path):
    model = ElectricalPdfImporter().import_pdf(_lighting_set(tmp_path, circuit=True))
    assert len(model.electrical_equipment) == 1
    assert [c.circuit_number for c in model.circuits] == ["12"]
    assert len(model.circuits[0].load_port_ids) == 1
    owner = next(p.owner_id for p in model.ports if p.id == model.circuits[0].load_port_ids[0])
    assert next(d.name for d in model.electrical_devices if d.id == owner) == "RE"
    validate_model(model)


@pytest.mark.parametrize("kwargs", [
    {"reference": False}, {"duplicate_legend": True}, {"other_legend": True},
])
def test_lighting_does_not_borrow_unreferenced_or_ambiguous_legend(tmp_path, kwargs):
    model = ElectricalPdfImporter().import_pdf(_lighting_set(tmp_path, **kwargs))
    assert not model.electrical_devices


@pytest.mark.parametrize("reverse_notes", [False, True])
@pytest.mark.parametrize("page_order", [(0, 1, 2, 3), (3, 1, 0, 2), (1, 2, 3, 0)])
@pytest.mark.parametrize("local_legend", [False, True])
def test_mixed_valid_and_ambiguous_references_keep_uncertainty(
    tmp_path, reverse_notes, page_order, local_legend,
):
    path = _lighting_set(
        tmp_path, other_legend=True, ambiguous_other=True,
        reverse_notes=reverse_notes, page_order=page_order, local_legend=local_legend,
    )
    document = extract_electrical_pdf(path, source_id="synthetic:mixed-legend-references")
    model = ElectricalPdfImporter().import_document(document)
    assert len(model.electrical_devices) == (8 if local_legend else 0)
    lane = model.attributes["pdf_electrical"]
    misses = [item for item in lane["unresolved_observations"]
              if item.get("reason_code") == "lighting_legend_reference_ambiguous"]
    assert len(misses) == 1
    miss = misses[0]
    notes = [item for item in document.texts if item.text.startswith("SEE E00")]
    assert miss["source_element_ids"] == sorted(item.element_id for item in notes)
    assert miss["page"] == notes[0].page
    assert miss["local_legend_authoritative"] == local_legend
    legend_pages = {item.text: [] for item in document.texts if item.text in {"E002", "E003"}}
    for item in document.texts:
        if item.text in legend_pages:
            legend_pages[item.text].append(item.page)
    assert miss["candidate_legend_pages"] == sorted(legend_pages["E002"] + legend_pages["E003"])
    outcomes = {item["text"]: item for item in miss["reference_outcomes"]}
    for note in notes:
        outcome = outcomes[note.text]
        assert outcome["source_element_id"] == note.element_id
        alias = "E002" if "E002" in note.text else "E003"
        assert outcome["candidate_legend_pages"] == sorted(legend_pages[alias])
        assert outcome["status"] == ("resolved" if alias == "E002" else "ambiguous")
    summary = lane["lighting_recognition"]
    assert summary["unresolved_by_reason"]["lighting_legend_reference_ambiguous"] == 1
    assert "None" not in summary["unresolved_by_reason"]
    assert not any(item["applied"] for item in summary["legend_references"])
    assert not any(p.method == "pdf-lighting-legend-reference"
                   for d in model.electrical_devices for p in d.provenance)
    validate_model(model)
    assert model.to_json() == ElectricalPdfImporter().import_document(document).to_json()
