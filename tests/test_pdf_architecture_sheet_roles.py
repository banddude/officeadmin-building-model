"""Independent mixed-role PDFs: title compartments outrank note cross-references."""
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import _explicit_drawing_title, classify_page, import_observations


def _source(path, title, *, sheet="A1.01", label="SHEET NAME", number_label="SHEET NUMBER",
            label_x=810, duplicate_label=False, unlabeled=False, rule=True, body_column=False, bottom_rule=False, competing_cell=False):
    writer = PdfWriter();page = writer.add_blank_page(width=1000, height=700)
    font = DictionaryObject({NameObject("/Type"):NameObject("/Font"),
        NameObject("/Subtype"):NameObject("/Type1"), NameObject("/BaseFont"):NameObject("/Helvetica"), NameObject("/Encoding"):NameObject("/WinAnsiEncoding")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):writer._add_object(font)})})
    def text(x,y,value,size=10):
        value=value.replace("\\", "\\\\").replace("(","\\(").replace(")","\\)")
        return f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({value}) Tj ET"
    commands=[text(60,600,"SEE ARCHITECTURAL FIRST FLOOR PLAN FOR ROOM LEVEL; SEE ELECTRICAL POWER PLAN."),
        text(60,580,"SEE FRAMING PLAN, GRADING PLAN AND DRAINAGE PLAN FOR COORDINATION."),
        text(60,50,'SCALE: 1/4" = 1\'-0"'),text(60,70,'LEVEL CEILING HEIGHT: 9\'-0"'),
        text(180,250,"ROOM: BEDROOM"),"100 180 300 200 re S","109 189 282 182 re S"]
    if body_column:commands.append(text(715,155,"DUCT NOTES",10))
    if unlabeled:
        if competing_cell:commands.extend(("800 110 m 990 110 l S",text(810,120,"SECOND FLOOR PLAN",12)))
        if bottom_rule:commands.append("800 70 m 990 70 l S")
        if rule:commands.append("800 130 m 990 130 l S")
        commands.extend((text(810,155,"NOT FOR CONSTRUCTION",6),text(810,100,title,12),text(810,20,sheet,32)))
    else:
        commands.extend((text(label_x,180,label),text(810,155,title),
            text(810,130,"FOR REFERENCE ONLY - SEE FLOOR PLAN",4),
            text(810,95,number_label),text(810,35,sheet,32)))
        if duplicate_label:commands.append(text(810,200,"DRAWING TITLE:"))
    stream=DecodedStreamObject();stream.set_data("\n".join(commands).encode("ascii"));page[NameObject("/Contents")]=writer._add_object(stream)
    with path.open("wb") as handle:writer.write(handle)
    assert not path.with_suffix(".expected.json").exists()
    return path


@pytest.mark.parametrize("label",["SHEET NAME","SHEET NAME:","SHEET TITLE","DRAWING TITLE:"])
@pytest.mark.parametrize("number_label",["SHEET NUMBER","SHEET NUMBER:"])
def test_optional_colons_keep_local_title_compartment_and_font_guard(tmp_path: Path,label,number_label):
    page=extract_pdf(_source(tmp_path/'title.pdf',"FIRST FLOOR PLAN",label=label,number_label=number_label)).pages[0]
    assert _explicit_drawing_title(page)=="FIRST FLOOR PLAN"
    assert classify_page(page).kind=="architectural_plan"


@pytest.mark.parametrize("title",["COVER SHEET","COVER PAGE","SHEET INDEX","GENERAL NOTES",
    "FIRST FLOOR FRAMING PLAN","PRECISE GRADING PLAN","DRAINAGE PLAN","DEMO SITE PLAN",
    "EXTERIOR ELEVATIONS","SECTIONS","DOOR SCHEDULE",
    "ENLARGED PLANS & INTERIOR ELEVATIONS_ROOM", "WALL PARTITION TYPES EXTERIOR INTERIOR"])
def test_explicit_nonfloor_roles_do_not_inherit_floor_plan_note_words(tmp_path: Path,title):
    document=extract_pdf(_source(tmp_path/'nonfloor.pdf',title))
    assert classify_page(document.pages[0]).kind=="other"
    model=import_observations(document)
    assert not model.walls and not model.spaces
    row=model.attributes['pdf_architecture']['pages'][0]
    assert row['floor_plan_exclusion_reason']=='explicit_non_floor_drawing_title'
    assert row['explicit_drawing_title']==title
    assert row['drawing_role_evidence']['source_element_ids']


def test_unlabeled_framing_title_requires_ruled_sheet_mark_compartment(tmp_path: Path):
    document=extract_pdf(_source(tmp_path/'framing.pdf',"FIRST FLOOR FRAMING PLAN",sheet="S2.01",unlabeled=True))
    assert _explicit_drawing_title(document.pages[0])=="FIRST FLOOR FRAMING PLAN"
    assert classify_page(document.pages[0]).kind=="other"
    row=import_observations(document).attributes['pdf_architecture']['pages'][0]
    assert row['drawing_role_evidence']['method']=='ruled_title_compartment_above_sheet_mark'
    assert row['drawing_role_evidence']['rule_source_element_ids']
    no_rule=extract_pdf(_source(tmp_path/'unbounded.pdf',"FIRST FLOOR FRAMING PLAN",sheet="S2.01",unlabeled=True,rule=False))
    assert _explicit_drawing_title(no_rule.pages[0]) is None


@pytest.mark.parametrize("changes",[{"label_x":100},{"duplicate_label":True}])
def test_nonlocal_or_competing_title_labels_leave_role_unresolved(tmp_path: Path,changes):
    page=extract_pdf(_source(tmp_path/'ambiguous.pdf',"GENERAL NOTES",**changes)).pages[0]
    assert _explicit_drawing_title(page) is None
    assert classify_page(page).kind=="architectural_plan"  # Legacy content fallback, not an invented explicit title.


@pytest.mark.parametrize("sheet",["A1.01","S2.01","E1.01"])
def test_floor_role_is_content_based_and_cross_references_do_not_exclude_it(tmp_path: Path,sheet):
    page=extract_pdf(_source(tmp_path/'floor.pdf',"FIRST FLOOR PLAN",sheet=sheet)).pages[0]
    assert classify_page(page).kind=="architectural_plan"


def test_a_series_electrical_title_keeps_its_printed_identity(tmp_path: Path):
    document=extract_pdf(_source(tmp_path/'power.pdf',"FIRST FLOOR ELECTRICAL PLAN",sheet="A1.02"))
    assert classify_page(document.pages[0]).kind=="electrical"
    assert any(item.text=="A1.02" for item in document.pages[0].texts)


def test_all_mixed_role_source_pages_are_retained_but_only_floor_is_materialized(tmp_path: Path):
    titles=["COVER SHEET","GENERAL NOTES","FIRST FLOOR FRAMING PLAN","PRECISE GRADING PLAN",
            "DRAINAGE PLAN","FIRST FLOOR PLAN","FIRST FLOOR ELECTRICAL PLAN"]
    writer=PdfWriter()
    for index,title in enumerate(titles):writer.add_page(PdfReader(_source(tmp_path/f'{index}.pdf',title)).pages[0])
    source=tmp_path/'mixed.pdf'
    with source.open('wb') as handle:writer.write(handle)
    document=extract_pdf(source,source_id='synthetic:mixed-roles');model=import_observations(document)
    rows=model.attributes['pdf_architecture']['pages']
    assert {row['page'] for row in rows}==set(range(1,8))
    assert {row['page'] for row in rows if row['classification']=='architectural_plan'}=={6}
    assert len(model.walls)==4 and len(model.spaces)==1
    assert model.to_json()==import_observations(document).to_json()


def test_body_notes_outside_local_title_column_do_not_contaminate_title(tmp_path):
    page=extract_pdf(_source(tmp_path/'column.pdf',"GENERAL NOTES",body_column=True)).pages[0]
    assert _explicit_drawing_title(page)=="GENERAL NOTES"
    assert classify_page(page).kind=="other"


def test_empty_gap_above_sheet_mark_does_not_hide_ruled_title_cell(tmp_path):
    page=extract_pdf(_source(tmp_path/'cell.pdf',"FIRST FLOOR FRAMING PLAN",unlabeled=True,bottom_rule=True)).pages[0]
    assert _explicit_drawing_title(page)=="FIRST FLOOR FRAMING PLAN"
    assert classify_page(page).kind=="other"


@pytest.mark.parametrize("title,role",[("DRAINAGE PLAN","other"),("GENERAL NOTES","other"),
    ("FIRST FLOOR PLAN AND DETAILS","architectural_plan")])
def test_combined_plan_details_preserves_floor_component_while_notes_do_not(tmp_path,title,role):
    document=extract_pdf(_source(tmp_path/'combined.pdf',title))
    assert classify_page(document.pages[0]).kind==role
    model=import_observations(document)
    assert bool(model.walls)==(role=="architectural_plan")


def test_competing_occupied_ruled_cells_do_not_supply_an_explicit_title(tmp_path):
    page=extract_pdf(_source(tmp_path/'competing-cells.pdf',"FIRST FLOOR FRAMING PLAN",
        unlabeled=True,competing_cell=True)).pages[0]
    assert _explicit_drawing_title(page) is None


@pytest.mark.parametrize("title,role",[
    ("FIRST FLOOR PLAN AND ROOF PLAN", "architectural_plan"),
    ("FIRST FLOOR PLAN AND SITE PLAN", "architectural_plan"),
    ("SITE PLAN AND FIRST FLOOR PLAN", "architectural_plan"),
    ("FIRST FLOOR FRAMING PLAN", "other"),
    ("FIRST FLOOR ELECTRICAL PLAN", "electrical"),
])
def test_independent_floor_component_survives_other_plan_roles(tmp_path,title,role):
    document=extract_pdf(_source(tmp_path/'mixed-plan-title.pdf',title))
    assert classify_page(document.pages[0]).kind==role
    model=import_observations(document)
    assert bool(model.walls)==(role=="architectural_plan")
