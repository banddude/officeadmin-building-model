"""Synthetic source-defined categories: expected placements are authored here."""
import math
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from oabm.importers.pdf_electrical.importer import ElectricalPdfImporter
from oabm.importers.pdf_electrical.lighting_categories import category_tokens
from oabm.model import validate_model


def _source(path: Path, *, mode="normal"):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = writer._add_object(font)
    surface = [(-7,-5),(6,-5),(6,1),(1,1),(1,6),(-7,6)]
    recessed = [(7*math.cos(i*math.pi/16),7*math.sin(i*math.pi/16)) for i in range(32)]
    if mode == "near-miss":
        recessed = [(-7,-5),(6,-5),(6,1.1),(1.1,1.1),(1.1,6),(-7,6)]
    def text(x,y,value):
        return f"BT /F1 8 Tf 1 0 0 1 {x} {y} Tm ({value}) Tj ET"
    def glyph(x,y,points):
        return " ".join([f"{x+points[0][0]:.5f} {y+points[0][1]:.5f} m",
            *[f"{x+px:.5f} {y+py:.5f} l" for px,py in points[1:]], "h S"])
    field = [text(580,770,"LIGHTING LEGEND"),
             glyph(600,730,surface), text(620,730,"SURFACE MOUNTED CEILING LIGHT"),
             glyph(600,690,recessed), text(620,690,"RECESSED DOWN LIGHT")]
    if mode == "ambiguous-category":
        field += [glyph(600,650,surface),text(620,650,"CEILING SURFACE MOUNT LIGHT")]
    if mode == "ambiguous-prototype":
        field += [glyph(575,730,recessed), glyph(575,690,surface)]
    if mode not in {"legend-only", "schedule-only"}:
        field += [glyph(150,500,surface), glyph(350,500,recessed),text(375,500,"Q-2")]
        if mode in {"leader", "ambiguous-leader"}:
            field += [text(100,400,"Q-1"), "100 400 m 143 500 l S"]
            if mode == "ambiguous-leader":
                field += ["100 400 m 343 500 l S"]
        else:
            field += [text(175,500,"Q-1")]
    if mode == "distractor-nearby":
        field += [glyph(185,500,recessed)]
    if mode == "multiple-matching-geometry":
        field += [glyph(190,500,surface)]
    if mode == "wide-legend-gap":
        field = [command.replace(" 620 730 Tm", " 700 730 Tm").replace(" 620 690 Tm", " 700 690 Tm")
                 for command in field]
    field += [text(850,30,"E1.1")]
    schedule = [text(50,740,"LIGHTING FIXTURE SCHEDULE"),text(50,720,"TAG"),
                text(150,720,"DESCRIPTION"), text(50,700,"Q-1"),
                text(150,700,"CEILING SURFACE MOUNT"), text(50,680,"Q-2"),
                text(150,680,"RECESSED DOWNLIGHT"), text(400,720,"QUANTITY"),
                text(400,700,"999"),text(400,680,"111"),
                glyph(30,700,surface),glyph(30,680,recessed),
                text(50,620,"SEE LIGHTING LEGEND ON E1.1")]
    for commands in [field, schedule]:
        page=writer.add_blank_page(width=900,height=800)
        page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"):font_ref})})
        content=DecodedStreamObject();content.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")]=writer._add_object(content)
    writer.write(path)


def _fixtures(model):
    return [device for device in model.electrical_devices if device.device_type == "luminaire"]


def test_source_category_equivalence_keeps_surface_and_recessed_distinct():
    assert category_tokens("CEILING SURFACE MOUNT") == category_tokens("SURFACE MOUNTED CEILING LIGHT")
    assert category_tokens("RECESSED DOWNLIGHT") == category_tokens("RECESSED DOWN LIGHT")
    assert category_tokens("RECESSED DOWNLIGHT") != category_tokens("CEILING SURFACE MOUNT")


def test_two_page_category_legend_confirms_only_observed_field_geometry(tmp_path):
    path=tmp_path/'categories.pdf';_source(path)
    model=ElectricalPdfImporter().import_pdf(path);validate_model(model)
    fixtures=_fixtures(model)
    assert len(fixtures)==2
    assert {device.name for device in fixtures}=={"Q-1","Q-2"}
    for device in fixtures:
        lane=device.attributes['pdf_electrical']
        assert lane['fixture_schedule']['source_page']==2
        assert lane['fixture_schedule']['quantity'] == ('999' if device.name=='Q-1' else '111')
        assert lane['lighting_recognition']['category_geometry']['expected_shape_score'] >= .75
        records=[p for p in device.provenance if p.method=='pdf-lighting-legend-category']
        assert len(records)==1 and records[0].page==1
        assert not any(p.method=='pdf-lighting-legend-tag' for p in device.provenance)
    assert model.to_json()==ElectricalPdfImporter().import_pdf(path).to_json()


@pytest.mark.parametrize('mode',['legend-only','schedule-only'])
def test_schedule_and_legend_symbols_are_never_field_placements(tmp_path,mode):
    path=tmp_path/'source.pdf';_source(path,mode=mode)
    assert not _fixtures(ElectricalPdfImporter().import_pdf(path))


def test_near_miss_competing_geometry_stays_unresolved(tmp_path):
    path=tmp_path/'near-miss.pdf';_source(path,mode='near-miss')
    model=ElectricalPdfImporter().import_pdf(path)
    assert not _fixtures(model)
    assert 'lighting_category_geometry_ambiguous' in model.to_json()


def test_two_source_categories_matching_one_description_stay_unresolved(tmp_path):
    path=tmp_path/'ambiguous-category.pdf';_source(path,mode='ambiguous-category')
    model=ElectricalPdfImporter().import_pdf(path)
    assert {device.name for device in _fixtures(model)}=={'Q-2'}
    assert 'lighting_schedule_category_ambiguous' in model.to_json()


def test_ambiguous_legend_prototypes_do_not_choose_the_nearest(tmp_path):
    path=tmp_path/'ambiguous-prototype.pdf';_source(path,mode='ambiguous-prototype')
    model=ElectricalPdfImporter().import_pdf(path)
    assert 'Q-1' not in {device.name for device in _fixtures(model)}
    assert 'lighting_category_prototype_unresolved' in model.to_json()


def test_observed_leader_associates_distant_tag_without_guessing_nearest(tmp_path):
    path=tmp_path/'leader.pdf';_source(path,mode='leader')
    model=ElectricalPdfImporter().import_pdf(path)
    fixtures=_fixtures(model)
    assert len(fixtures)==2
    surface=next(device for device in fixtures if device.name=='Q-1')
    records=[p for p in surface.provenance if p.method=='pdf-lighting-tag-leader']
    assert len(records)==1 and records[0].source_element_id.startswith('p1:vector:')
    assert records[0].derivation=='inferred'


def test_conflicting_leaders_stay_unresolved(tmp_path):
    path=tmp_path/'ambiguous-leader.pdf';_source(path,mode='ambiguous-leader')
    model=ElectricalPdfImporter().import_pdf(path)
    assert 'Q-1' not in {device.name for device in _fixtures(model)}
    assert 'lighting_fixture_tag_association_ambiguous' in model.to_json()


def test_repeated_legend_column_alignment_supports_wide_symbol_text_gap(tmp_path):
    path=tmp_path/'wide-legend.pdf';_source(path,mode='wide-legend-gap')
    model=ElectricalPdfImporter().import_pdf(path)
    assert {device.name for device in _fixtures(model)}=={'Q-1','Q-2'}


def test_category_evidence_excludes_nearer_wrong_category_geometry(tmp_path):
    path=tmp_path/'distractor.pdf';_source(path,mode='distractor-nearby')
    model=ElectricalPdfImporter().import_pdf(path)
    fixtures=_fixtures(model)
    assert len(fixtures)==2
    surface=next(device for device in fixtures if device.name=='Q-1')
    assert surface.pose.position.x==pytest.approx(149.5*0.0254/72)


def test_several_matching_field_glyphs_stay_unresolved_even_at_different_distances(tmp_path):
    path=tmp_path/'multiple-matches.pdf';_source(path,mode='multiple-matching-geometry')
    model=ElectricalPdfImporter().import_pdf(path)
    assert 'Q-1' not in {device.name for device in _fixtures(model)}
    assert 'lighting_fixture_tag_association_ambiguous' in model.to_json()
