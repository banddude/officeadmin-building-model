"""Synthetic source constraints for adjoining, non-overlapping drawing regions."""
import math
from dataclasses import replace

import pytest

from oabm.importers.pdf_architecture.adjacent_grids import (
    _ink_length, adjacent_grid_proposal, grid_axes,
)
from oabm.importers.pdf_architecture.types import PdfLineObservation, PdfPageObservation, PdfTextObservation

BOX = (200.0, 200.0, 600.0, 600.0)
MPP = .02


def _ring(key, x, y):
    pts = [(x+12*math.cos(i*math.tau/24), y+12*math.sin(i*math.tau/24)) for i in range(24)]
    return tuple(PdfLineObservation(f"{key}:{i}",a,b,primitive_family="polyline")
                 for i,(a,b) in enumerate(zip(pts,pts[1:]+pts[:1])))


def _page(number, left):
    own, other = ("A-704 X","A704 Y") if left else ("A-704 Y","A704 X")
    x = 200 if left else 600
    side_x = 650 if left else 150
    markers = [("8",x,150),("K",side_x,250),("L",side_x,550)]
    lines = tuple(line for label,cx,cy in markers for line in _ring(f"p{number}:{label}",cx,cy))
    lines += (PdfLineObservation(f"p{number}:axis8",(x,170),(x,620)),
              PdfLineObservation(f"p{number}:axisK",(170,250),(630,250)),
              PdfLineObservation(f"p{number}:axisL",(170,550),(630,550)))
    texts = tuple(PdfTextObservation(f"p{number}:text:{label}",label,(cx-4,cy-5,cx+4,cy+5),font_size_pt=8)
                  for label,cx,cy in markers)
    note_x = 150 if left else 650
    texts += (PdfTextObservation(f"p{number}:sheet",own,(1000,30,1100,50),font_size_pt=20),
              PdfTextObservation(f"p{number}:note",f"CONTINUED ON SHEET {other}",(note_x-5,280,note_x+5,420),font_size_pt=12))
    return PdfPageObservation(number,1200,900,texts=texts,lines=lines)


def _proposal(source=None,target=None,source_mpp=MPP,source_level="level-four"):
    return adjacent_grid_proposal(source or _page(2,False),BOX,source_mpp,source_level,
                                  target or _page(1,True),BOX,MPP,"level-four")


def test_reciprocal_edges_and_orthogonal_axes_establish_adjacent_translation():
    result = _proposal()
    assert result["accepted"] and result["applicable"]
    assert result["translation_pt"] == pytest.approx([-400,0])
    assert result["scale_ratio"] == 1
    assert result["max_residual_m"] == 0
    assert result["shared_axes"] == ["8","K","L"]
    assert result["boundary_axis"] == "8"
    assert (result["source_side"],result["target_side"]) == ("right","left")
    assert all(c["source_element_ids"] for c in result["source_controls"])


@pytest.mark.parametrize("level",[None,"another-level"])
def test_unknown_or_different_level_is_not_aligned(level):
    assert not _proposal(source_level=level)["accepted"]


def test_one_way_continuation_does_not_establish_a_frame():
    target = _page(1,True)
    target = replace(target,texts=tuple(t for t in target.texts if t.element_id != "p1:note"))
    result = _proposal(target=target)
    assert not result["accepted"] and result["applicable"]
    assert result["reason_codes"] == ["reciprocal_continuation_unresolved"]


def test_same_side_continuations_are_not_an_adjacent_pair():
    source = _page(2,False)
    source = replace(source,texts=tuple(replace(t,bbox_pt=(145,280,155,420)) if t.element_id=="p2:note" else t for t in source.texts))
    assert _proposal(source=source)["reason_codes"] == ["continuation_sides_conflict"]


def test_reflected_parallel_grid_labels_conflict_instead_of_flipping_the_drawing():
    source = _page(2,False)
    source = replace(source,texts=tuple(replace(t,text={"K":"L","L":"K"}.get(t.text,t.text)) for t in source.texts))
    assert _proposal(source=source)["reason_codes"] == ["grid_axes_inconsistent_with_printed_scale"]


def test_printed_scale_conflict_is_not_fit_away():
    assert _proposal(source_mpp=.025)["reason_codes"] == ["grid_axes_inconsistent_with_printed_scale"]


def test_circles_without_axis_support_cannot_register():
    source = _page(2,False)
    source = replace(source,lines=tuple(l for l in source.lines if ":axis" not in l.element_id))
    assert not grid_axes(source,BOX,MPP)
    assert not _proposal(source=source)["accepted"]


def test_duplicate_grid_labels_stay_ambiguous():
    source = _page(2,False)
    source = replace(source,lines=(*source.lines,*_ring("duplicate",400,400)),
                     texts=(*source.texts,PdfTextObservation("duplicate-label","K",(396,395,404,405),font_size_pt=8)))
    assert not _proposal(source=source)["accepted"]


def test_two_labels_do_not_replace_the_three_independent_axis_requirements():
    source = _page(2,False)
    source = replace(source,texts=tuple(t for t in source.texts if t.text != "L"))
    assert not _proposal(source=source)["accepted"]


def test_reversed_source_order_keeps_identical_proposal_and_evidence():
    source,target=_page(2,False),_page(1,True)
    reverse=lambda p:replace(p,texts=tuple(reversed(p.texts)),lines=tuple(reversed(p.lines)))
    assert _proposal(source=reverse(source),target=reverse(target)) == _proposal(source=source,target=target)


def test_overlapping_strokes_do_not_inflate_axis_ink_coverage():
    assert _ink_length([(0,10,"a",0),(0,10,"b",0),(4,12,"c",0)]) == 12


def test_invalid_scale_and_unbounded_region_are_refused():
    assert not _proposal(source_mpp=0)["accepted"]
    result=adjacent_grid_proposal(_page(2,False),(0,0,0,10),MPP,"level-four",_page(1,True),BOX,MPP,"level-four")
    assert result["reason_codes"] == ["unbounded_drawing_region"]


def _write_adjacent_pdf(path, reciprocal=True):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject,DictionaryObject,NameObject
    writer=PdfWriter()
    for page_number,left in ((1,True),(2,False)):
        page=writer.add_blank_page(1200,900)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        commands=['.5 w 198 198 404 404 re S 202 202 396 396 re S']
        def text(x,y,value,size=10,vertical=False):
            matrix=f'0 1 -1 0 {x} {y}' if vertical else f'1 0 0 1 {x} {y}'
            commands.append(f'BT /F1 {size} Tf {matrix} Tm ({value}) Tj ET')
        def circle(cx,cy):
            r=12;k=.5522847498*r
            commands.append(f'{cx+r} {cy} m {cx+r} {cy+k} {cx+k} {cy+r} {cx} {cy+r} c {cx-k} {cy+r} {cx-r} {cy+k} {cx-r} {cy} c {cx-r} {cy-k} {cx-k} {cy-r} {cx} {cy-r} c {cx+k} {cy-r} {cx+r} {cy-k} {cx+r} {cy} c S')
        x=200 if left else 600;side=650 if left else 150
        for label,cx,cy in [('8',x,150),('K',side,250),('L',side,550)]:
            circle(cx,cy);text(cx-3,cy-3,label,8)
        commands.extend([f'{x} 170 m {x} 620 l S','170 250 m 630 250 l S','170 550 m 630 550 l S'])
        text(250,65,'CONSTRUCTION PLAN',12)
        text(250,85,'ARCHITECTURAL FLOOR PLAN',10)
        text(225,630,'SCALE: 1:100',10)
        text(250,580,'LEVEL: 4',10)
        text(250,560,"CEILING HEIGHT: 9'-0\"",10)
        text(310,400,'ROOM: '+('EAST STUDIO' if left else 'WEST STUDIO'),14)
        text(1000,40,'A-704 '+('X' if left else 'Y'),20)
        if reciprocal or not left:
            text(150 if left else 650,300,'CONTINUED ON SHEET A704 '+('Y' if left else 'X'),10,True)
        stream=DecodedStreamObject();stream.set_data('\n'.join(commands).encode());page[NameObject('/Contents')]=writer._add_object(stream)
    writer.write(path)


def test_generated_adjacent_pdf_imports_without_manual_registration_hints(tmp_path):
    from oabm.importers.pdf_architecture import import_architectural_pdf
    from oabm.model import validate_model
    path=tmp_path/'synthetic-adjacent-plans.pdf';_write_adjacent_pdf(path)
    model=import_architectural_pdf(path);validate_model(model)
    regions=model.attributes['pdf_architecture']['drawing_regions']
    second=next(r for r in regions if r['page']==2)
    assert second['status']=='resolved'
    assert second['adjacent_grid_registration']['status']=='registered'
    assert second['frame']['basis']=='registered_to_region'
    assert second['frame']['translation_m']==pytest.approx([-400*100*.0254/72,0],abs=1e-6)
    assert {s.name for s in model.spaces}=={'EAST STUDIO','WEST STUDIO'}
    assert import_architectural_pdf(path).to_json()==model.to_json()


def test_generated_one_way_reference_does_not_claim_an_adjacent_shared_frame(tmp_path):
    from oabm.importers.pdf_architecture import import_architectural_pdf
    path=tmp_path/'synthetic-one-way.pdf';_write_adjacent_pdf(path,reciprocal=False)
    model=import_architectural_pdf(path)
    second=next(r for r in model.attributes['pdf_architecture']['drawing_regions'] if r['page']==2)
    assert second['adjacent_grid_registration']['status']=='refused'
    assert second.get('frame',{}).get('basis')!='registered_to_region'


def test_conflicting_accepted_wall_frame_cannot_fall_back_to_local_coordinates(tmp_path, monkeypatch):
    from oabm.importers.pdf_architecture import importer
    path = tmp_path / 'synthetic-conflicting-methods.pdf'
    _write_adjacent_pdf(path)

    def conflicting_walls(page, scale, targets, evidence):
        target = targets[0]
        transform = importer._Transform2D(scale.meters_per_point, 0, 0, 0, 'synthetic wall proof', .9)
        registration = {'target_region_id': target.region_id, 'target_page': target.page_number,
                        'agreeing_region_ids': [target.region_id], 'method': 'synthetic wall proof'}
        return (transform, registration), {'status': 'registered', 'reason_codes': []}

    monkeypatch.setattr(importer, '_register_region_by_shared_walls', conflicting_walls)
    model = importer.import_architectural_pdf(path)
    second = next(r for r in model.attributes['pdf_architecture']['drawing_regions'] if r['page'] == 2)
    assert second['status'] == 'unresolved'
    assert 'registration_methods_disagree' in second['reason_codes']
    assert second['adjacent_grid_registration']['status'] == 'refused'
    assert 'WEST STUDIO' not in {s.name for s in model.spaces}


def test_multiple_footer_identities_do_not_choose_a_sheet():
    source = _page(2, False)
    source = replace(source, texts=(*source.texts, PdfTextObservation('ambiguous-footer', 'A-705 Z', (1000, 65, 1100, 85), font_size_pt=20)))
    assert _proposal(source=source)['reason_codes'] == ['sheet_identity_unresolved']


def test_reciprocal_notes_inside_the_drawing_do_not_establish_edge_orientation():
    source = _page(2, False)
    source = replace(source, texts=tuple(replace(t, bbox_pt=(395, 280, 405, 420)) if t.element_id == 'p2:note' else t for t in source.texts))
    assert _proposal(source=source)['reason_codes'] == ['reciprocal_continuation_unresolved']


def test_axis_crossing_both_directions_at_a_bubble_is_ambiguous():
    source = _page(2, False)
    source = replace(source, lines=(*source.lines, PdfLineObservation('ambiguous-horizontal', (300, 150), (700, 150))))
    assert '8' not in grid_axes(source, BOX, MPP)
    assert not _proposal(source=source)['accepted']


def test_different_printed_scales_preserve_source_geometry_instead_of_fitting_scale():
    source = _page(2, False)
    factor = 1.5
    point = lambda p: tuple(v * factor for v in p)
    source = replace(source, width_pt=source.width_pt * factor, height_pt=source.height_pt * factor,
                     lines=tuple(replace(line, start_pt=point(line.start_pt), end_pt=point(line.end_pt)) for line in source.lines),
                     texts=tuple(replace(text, bbox_pt=point(text.bbox_pt), font_size_pt=text.font_size_pt * factor) for text in source.texts))
    result = adjacent_grid_proposal(source, point(BOX), MPP / factor, 'level-four',
                                    _page(1, True), BOX, MPP, 'level-four')
    assert result['accepted']
    assert result['scale_ratio'] == pytest.approx(1 / factor)
    assert result['translation_pt'] == pytest.approx([-400, 0])


@pytest.mark.parametrize('reason', ['competing_targets', 'orientation_incompatible', 'scale_incompatible'])
def test_adjacency_does_not_override_a_strong_wall_refusal(tmp_path, monkeypatch, reason):
    from oabm.importers.pdf_architecture import importer
    path = tmp_path / 'synthetic-refused-walls.pdf'
    _write_adjacent_pdf(path)
    monkeypatch.setattr(importer, '_register_region_by_shared_walls',
                        lambda *args: (None, {'status': 'refused', 'reason_codes': [reason]}))
    model = importer.import_architectural_pdf(path)
    second = next(r for r in model.attributes['pdf_architecture']['drawing_regions'] if r['page'] == 2)
    assert second['adjacent_grid_registration']['reason_codes'] == ['shared_wall_registration_conflict']
    assert second.get('frame', {}).get('basis') != 'registered_to_region'
