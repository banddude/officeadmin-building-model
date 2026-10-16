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
    if mode in {"fixture-words", "nonlighting-fixture-words"}:
        field=[command.replace("SURFACE MOUNTED CEILING LIGHT", "SURFACE MOUNTED CEILING FIXTURE")
                      .replace("RECESSED DOWN LIGHT", "RECESSED CEILING FIXTURES") for command in field]
        schedule=[command.replace("RECESSED DOWNLIGHT", "RECESSED CEILING") for command in schedule]
        if mode == "nonlighting-fixture-words":
            field=[command.replace("LIGHTING LEGEND", "PLUMBING FIXTURE LEGEND") for command in field]
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
        assert lane['lighting_recognition']['tag_is_primary_type_evidence'] is False
        tag_records=[p for p in device.provenance if p.method=='pdf-lighting-fixture-tag']
        assert tag_records and all(p.attributes.get('tag_is_primary_type_evidence') is False for p in tag_records)
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


def test_shared_observed_prototype_is_scored_once_without_dropping_tag_identity(tmp_path, monkeypatch):
    from dataclasses import replace
    from oabm.importers.pdf_electrical import importer as api
    from oabm.importers.pdf_electrical.lighting_categories import detect_category_legends
    path=tmp_path/'shared-prototype.pdf';_source(path)
    document=api.extract_pdf(path)
    clusters=[c for c in api._glyph_clusters(document, document.vectors) if api._is_glyph_cluster(c)]
    entries, _, _, _, _=detect_category_legends(texts=document.texts,clusters=clusters,excluded_text_ids=set())
    surface=next(e for e in entries[1] if 'SURFACE' in e.label.text)
    recessed=next(e for e in entries[1] if 'RECESSED' in e.label.text)
    field=next(c for c in clusters if abs(c.center_pt[0]-149.5)<1 and abs(c.center_pt[1]-500.5)<1)
    aliases=[replace(surface,tag=f'A{i:02}') for i in range(10)]
    calls=[];original=api._cluster_match_score
    def score(candidate,prototype):
        calls.append(id(prototype));return original(candidate,prototype)
    monkeypatch.setattr(api,'_cluster_match_score',score)
    winner, similarity, guard=api._lighting_shape_support(field,[*aliases,recessed])
    assert winner.tag=='A00' and similarity==1.0 and guard is None
    assert len(calls)==2 and len(set(calls))==2


def test_leader_candidates_are_scoped_to_page_with_identical_geometry_keys(tmp_path):
    from pypdf import PdfReader
    from oabm.importers.pdf_electrical import importer as api
    from oabm.importers.pdf_electrical.lighting_categories import leader_candidates
    source=tmp_path/'leader-source.pdf';_source(source,mode='leader')
    reader=PdfReader(source);writer=PdfWriter()
    for _ in (1,2):
        writer.add_page(reader.pages[0])
    path=tmp_path/'identical-floor-pages.pdf';writer.write(path)
    assert not path.with_suffix('.expected.json').exists()
    document=api.extract_pdf(path)
    clusters=[c for c in api._glyph_clusters(document,document.vectors)
              if abs(c.center_pt[0]-149.5)<1 and abs(c.center_pt[1]-500.5)<1]
    assert {c.page for c in clusters}=={1,2}
    assert len({c.geometry_key for c in clusters})==1
    for page in (1,2):
        observation=next(t for t in document.texts if t.page==page and t.text=='Q-1')
        hits,evidence=leader_candidates(observation,clusters,document.vectors)
        assert [hit.page for hit in hits]==[page]
        assert evidence[clusters[0].geometry_key]['source_element_ids']
        assert all(source_id.startswith(f'p{page}:') for source_id in
                   evidence[clusters[0].geometry_key]['source_element_ids'])


@pytest.mark.parametrize('mode,expected', [('fixture-words', {'Q-1','Q-2'}),
                                         ('nonlighting-fixture-words', set())])
def test_fixture_description_words_require_confirmed_lighting_legend(tmp_path,mode,expected):
    path=tmp_path/'fixture-labels.pdf';_source(path,mode=mode)
    assert not path.with_suffix('.expected.json').exists()
    if not expected:
        from oabm.importers.pdf_electrical import importer as api
        from oabm.importers.pdf_electrical.lighting_categories import detect_category_legends
        document=api.extract_pdf(path)
        clusters=api._glyph_clusters(document,document.vectors)
        entries,keys,claimed,problems,metadata=detect_category_legends(
            texts=document.texts,clusters=clusters,excluded_text_ids=set())
        assert not entries and not keys and not claimed and not metadata
    else:
        model=ElectricalPdfImporter().import_pdf(path)
        assert {device.name for device in _fixtures(model)}==expected
