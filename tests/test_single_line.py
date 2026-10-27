"""Public synthetic completeness, electrical truth and pagination regressions."""
from dataclasses import replace
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import pytest
from oabm.drawings.single_line import project_topology, plan_pages
from oabm.drawings.single_line.production import build_single_line_bodies

spec=importlib.util.spec_from_file_location('single_line_models',Path(__file__).parents[1]/'examples/single_line/make_models.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
make_model=module.make_model
BOUNDS=(7.055555556,7.055555556,820.208333333,602.544444444)


def build(commercial=False,bounds=BOUNDS,model=None):
    model=model or make_model(commercial)
    topology=project_topology(model)
    layout=plan_pages(topology,bounds)
    sheets=[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in layout['pages']]
    return build_single_line_bodies(model,sheets)


@pytest.mark.parametrize('commercial',[False,True])
def test_source_to_output_complete_and_paired(commercial):
    model=make_model(commercial)
    bodies,doc=build(commercial)
    topology,layout=doc['topology'],doc['layout']
    assert {x['id'] for x in topology['equipment']}=={x.id for x in model.electrical_equipment}
    assert len(topology['connections'])==sum(len(c.load_port_ids) for c in model.circuits)
    occurrences=[r for p in doc['sheets'] for r in p['terminal_occurrences']]
    for edge in topology['connections']:
        rows=[r for r in occurrences if r['connection_id']==edge['id']]
        assert sorted(r['endpoint'] for r in rows)==['load','source']
        assert rows[0]['counterpart_sheet']==rows[1]['sheet_id']
        assert rows[1]['counterpart_sheet']==rows[0]['sheet_id']
        assert all(r['sheet_id'] in bodies for r in rows)
    rendered_equipment=[r['equipment_id'] for r in layout['equipment_occurrences']]
    assert set(rendered_equipment)=={e.id for e in model.electrical_equipment}
    register=[r['connection_id'] for p in layout['pages'] for r in p.get('entries',[]) if 'connection_id' in r]
    assert sorted(register)==sorted(e['id'] for e in topology['connections'])
    assert all(ET.fromstring(body).tag.endswith('g') for body in bodies.values())
    if commercial:
        assert sum(e['equipment_type']=='panelboard' for e in topology['equipment'])==27
        assert len(bodies)==2
        assert any(row['sheet_id']!=layout['equipment_home_sheets'][row['equipment_id']] for row in layout['equipment_occurrences'])
    else:
        assert len(bodies)==1


def test_transformer_voltage_and_transfer_connectivity_are_explicit():
    bodies,doc=build(True)
    edges=doc['topology']['connections']
    assert {e['voltage_v'] for e in edges if e['load_id']=='XFMR'}=={480}
    assert {e['voltage_v'] for e in edges if e['source_id']=='XFMR'}=={120}
    assert {(e['source_id'],e['load_role']) for e in edges if e['load_id']=='ATS'}=={('GEN','alternate'),('MAIN','normal')}
    text=' '.join(bodies.values())
    assert '480 V' in text and '120 V' in text and 'STATE UNSPECIFIED' in text
    assert 'CONTINUED equipment' in text


def test_reproducibility_and_input_order():
    model=make_model(True)
    bodies,doc=build(model=model)
    reordered=replace(model,electrical_equipment=tuple(reversed(model.electrical_equipment)),
                      circuits=tuple(reversed(model.circuits)),ports=tuple(reversed(model.ports)))
    other,other_doc=build(model=reordered)
    assert bodies==other
    assert doc['layout']==other_doc['layout']
    assert build(model=model)==(bodies,doc)


def test_unknown_facts_visible_and_long_labels_preserved():
    model=make_model()
    model=replace(model,circuits=tuple(replace(c,voltage_v=None,poles=None,phase=None) for c in model.circuits),conductors=())
    bodies,doc=build(model=model)
    text=' '.join(bodies.values())
    assert 'V UNKNOWN' in text and 'Conductor specification UNKNOWN' in text
    visible=' '.join(' '.join(ET.fromstring(body).itertext()) for body in bodies.values())
    assert 'deliberately long supplied' in visible
    assert len(doc['topology']['diagnostics'])>=6


def test_invalid_voltage_and_ambiguous_transfer_fail():
    model=make_model(True)
    bad=replace(model,circuits=tuple(replace(c,voltage_v=120) if c.id=='F002' else c for c in model.circuits))
    with pytest.raises(ValueError,match='inconsistent supplied circuit voltages'):
        project_topology(bad)
    bad=replace(model,ports=tuple(replace(p,role='incoming') if p.id=='F002-in' else p for p in model.ports))
    with pytest.raises(ValueError,match='transfer requires'):
        project_topology(bad)


def test_invalid_cycle_and_missing_port_link_direction_fail():
    model=make_model()
    # Existing ports retain canonical validity; assigning a load owner as the
    # circuit's source creates a directed cycle in the semantic projection.
    ports=tuple(replace(p,owner_id='SUB') if p.id=='F001-out' else p for p in model.ports)
    bad=replace(model,ports=ports)
    with pytest.raises(ValueError,match='cyclic/ring'):
        project_topology(bad)
    ports=tuple(replace(p,connected_port_ids=('F003-in',)) if p.id=='F001-out' else
                replace(p,connected_port_ids=('F001-out',)) if p.id=='F003-in' else p for p in model.ports)
    with pytest.raises(ValueError,match='lacks a directed circuit'):
        project_topology(replace(model,ports=ports))


def test_layout_small_paper_and_sheet_mismatch_are_actionable():
    topology=project_topology(make_model())
    with pytest.raises(ValueError,match='drawing bounds'):
        plan_pages(topology,(10,10,100,100))
    with pytest.raises(ValueError,match='ordered deterministic page list'):
        build_single_line_bodies(make_model(),[SimpleNamespace(id='WRONG',drawing_bounds_mm=BOUNDS)])


def test_isolated_equipment_retained_and_unsupported_type_refused():
    model=make_model()
    isolated=replace(model.electrical_equipment[-1],id='ISOLATED')
    bodies,doc=build(model=replace(model,electrical_equipment=model.electrical_equipment+(isolated,)))
    assert 'ISOLATED / NO EXPLICIT CIRCUITS' in ' '.join(bodies.values())
    bad=replace(isolated,equipment_type='unknown-vendor-gear')
    with pytest.raises(ValueError,match='unsupported equipment_type'):
        project_topology(replace(model,electrical_equipment=model.electrical_equipment+(bad,)))


def test_every_diagnostic_and_equipment_on_small_valid_pages():
    model=make_model(True)
    bodies,doc=build(model=model,bounds=(5,5,220,210))
    assert len(bodies)>len(build(True)[0])
    assert len([r for p in doc['sheets'] for r in p['terminal_occurrences']])==2*len(model.circuits)
    note_lines=[line for p in doc['layout']['pages'] for row in p.get('entries',[]) if row.get('note_id')=='notes' for line in row['lines']]
    for diagnostic in doc['topology']['diagnostics']:
        assert diagnostic in ' '.join(note_lines)


def company_inputs(tmp_path,model,template=None):
    import json
    from oabm.drawings.company_sheets import resolve_company_sheet_set
    profile=tmp_path/'profile.json';job=tmp_path/'job.json';source=tmp_path/'model.json'
    profile.write_text(json.dumps({'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC','scale':'NOT TO SCALE'},'template':template or {}}))
    request={'version':'company-sheet-job-v1','set_id':'single-line-test','date':'2026-10-27','revision':'A','formats':['svg','pdf'],'sheets':[{'id':'E601','title':'SINGLE LINE'}]}
    job.write_text(json.dumps(request))
    template=resolve_company_sheet_set(profile,job).sheets[0]
    layout=plan_pages(project_topology(model),template.drawing_bounds_mm)
    request['sheets']=[{'id':p['id'],'title':'SINGLE LINE'} for p in layout['pages']]
    job.write_text(json.dumps(request))
    source.write_text(model.to_json())
    return source,profile,job,layout


@pytest.mark.parametrize('commercial',[False,True])
def test_real_shared_profile_svg_pdf_package_complete_and_repeatable(tmp_path,commercial):
    pytest.importorskip('cairosvg')
    import hashlib
    import json
    from pypdf import PdfReader
    from oabm.drawings.single_line import create_single_line_set
    model=make_model(commercial)
    source,profile,job,layout=company_inputs(tmp_path,model)
    original=(profile.read_bytes(),job.read_bytes())
    first=create_single_line_set(source,profile,job,output_directory=tmp_path/'first')
    second=create_single_line_set(source,profile,job,output_directory=tmp_path/'second')
    assert original==(profile.read_bytes(),job.read_bytes())
    # Every delivered byte, including the real shared PDF and manifests, repeats.
    for path in first.rglob('*'):
        if path.is_file():assert path.read_bytes()==(second/path.relative_to(first)).read_bytes()
    manifest=json.loads((first/'MANIFEST.json').read_bytes())
    for row in manifest['files']:
        assert hashlib.sha256((first/row['path']).read_bytes()).hexdigest()==row['sha256']
    assert manifest['sheet_order']==[p['id'] for p in layout['pages']]
    shared=first/'company-sheets/delivery'
    pdf=PdfReader(shared/'sheet-set.pdf')
    assert len(pdf.pages)==len(layout['pages'])
    text=' '.join(page.extract_text() for page in pdf.pages)
    assert 'EXAMPLE ELECTRIC' in text
    for edge in project_topology(model).connections:
        assert edge['circuit_id'] in text
    assert all(not list(page.images) for page in pdf.pages)
    svgs=sorted((shared/'sheets').glob('*.svg'))
    assert len(svgs)==len(layout['pages'])
    for svg,page in zip(svgs,layout['pages']):
        text=' '.join(ET.fromstring(svg.read_bytes()).itertext())
        assert 'EXAMPLE ELECTRIC' in text and page['id'] in text
    # All explicit edges appear once in the connected fan-out diagrams too.
    shown=[edge for page in layout['pages'] for group in page.get('groups',[]) for edge in group['connections']]
    shown.extend(edge for page in layout['pages'] for edge in page.get('connections',[]))
    assert sorted(shown)==sorted(edge['id'] for edge in project_topology(model).connections)


def test_failed_shared_render_never_leaves_completed_package(tmp_path,monkeypatch):
    from oabm.drawings import company_sheets
    from oabm.drawings.single_line import create_single_line_set
    source,profile,job,_=company_inputs(tmp_path,make_model())
    def fail(*args,**kwargs):raise ValueError('synthetic renderer failure')
    monkeypatch.setattr(company_sheets,'create_company_sheet_set',fail)
    with pytest.raises(ValueError,match='synthetic renderer failure'):
        create_single_line_set(source,profile,job,output_directory=tmp_path/'failed')
    assert not (tmp_path/'failed/delivery').exists()
    assert (tmp_path/'failed/.pending').is_dir()
    assert (tmp_path/'failed/failure.json').is_file()
    with pytest.raises(FileExistsError):
        create_single_line_set(source,profile,job,output_directory=tmp_path/'failed')


def test_printed_index_and_supplied_equipment_specs_are_complete():
    bodies,doc=build(True)
    assert {row['id'] for row in doc['layout']['sheet_index']}==set(bodies)
    for body in bodies.values():
        footer=' '.join(ET.fromstring(body).itertext())
        assert all(identity in footer for identity in bodies)
    visible=' '.join(' '.join(ET.fromstring(body).itertext()) for body in bodies.values())
    assert '200 A; 22 kAIC' in visible


def test_unicode_and_oversized_labels_fail_instead_of_dropping():
    model=make_model()
    bad=replace(model,electrical_equipment=tuple(replace(e,name='Oversized '*100) if e.id=='SUB' else e for e in model.electrical_equipment))
    with pytest.raises(ValueError,match='equipment name exceeds'):
        build(model=bad)
    bad=replace(model,electrical_equipment=tuple(replace(e,name='Panel \u2603') if e.id=='SUB' else e for e in model.electrical_equipment))
    with pytest.raises(ValueError,match='printable ASCII'):
        build(model=bad)


def test_alternate_feed_backbone_never_passes_through_main_equipment():
    from oabm.drawings.single_line.layout import backbone_geometry, _crosses_box
    topology=project_topology(make_model(True))
    layout=plan_pages(topology,BOUNDS)
    page=next(p for p in layout['pages'] if p['nodes'])
    edges={e['id']:e for e in topology.connections}
    geometry=backbone_geometry(page,edges,BOUNDS)
    alternate=next(g for g in geometry if edges[g['connection_id']]['load_role']=='alternate')
    assert len(alternate['points'])>2
    for g in geometry:
        e=edges[g['connection_id']]
        for n in page['nodes']:
            if n['equipment_id'] in {e['source_id'],e['load_id']}:continue
            box=(n['x']-n['width']/2,n['y'],n['x']+n['width']/2,n['y']+n['height'])
            assert not any(_crosses_box(a,b,box) for a,b in zip(g['points'],g['points'][1:]))


def test_wide_glyph_names_use_actual_shared_font_measurements():
    from oabm.drawings.single_line.layout import wrap
    from oabm.drawings.company_sheets import measure_sheet_text
    lines=wrap('WWWWWWWWWWWWWWWWWWWWWWWWWW',26)
    assert len(lines)>1
    assert ''.join(lines)=='W'*26
    assert all(measure_sheet_text(line,2.5,font_family='DejaVu Sans')<=45.5 for line in lines)


@pytest.mark.parametrize('commercial',[False,True])
def test_compact_vector_geometry_has_legible_labels_and_no_false_equipment_crossings(commercial):
    from oabm.drawings.single_line.layout import _crosses_box
    _,doc=build(commercial)
    edges={e['id']:e for e in doc['topology']['connections']}
    for page in doc['sheets']:
        symbols=page['equipment_symbols']
        assert all(row['font_size_mm']>=2.3 for row in page['text_bounds'])
        for label in page['text_bounds']:
            if label['equipment_id'] is None:continue
            a,b,c,d=label['bounds_mm']
            matching=[r['bounds_mm'] for r in symbols if r['equipment_id']==label['equipment_id']]
            assert any(x<=a and y<=b and c<=r and d<=s for x,y,r,s in matching),label
        for path in page['connection_paths']:
            edge=edges[path['connection_id']]
            for symbol in symbols:
                if symbol['equipment_id'] in {edge['source_id'],edge['load_id']}:continue
                x,y,r,s=symbol['bounds_mm']
                assert not any(_crosses_box(a,b,(x+.1,y+.1,r-.1,s-.1)) for a,b in zip(path['points_mm'],path['points_mm'][1:])),(path,symbol)


def test_standard_a3_residential_can_stay_on_one_sheet_without_font_shrinking():
    # Same shared right rail on a standard A3 landscape sheet.
    from oabm.drawings.company_sheets import DEFAULT_TEMPLATE
    margin=DEFAULT_TEMPLATE['margin_mm'];rail=DEFAULT_TEMPLATE['rail_width_mm']
    bodies,doc=build(bounds=(margin,margin,420-margin-rail,297-margin))
    assert len(bodies)==1
    assert len(doc['topology']['connections'])==3
    assert min(t['font_size_mm'] for p in doc['sheets'] for t in p['text_bounds'])>=2.3


def test_measured_wrapping_preserves_all_supplied_spaces():
    from oabm.drawings.single_line.layout import wrap_mm
    source='  PANEL  A   WITH  SUPPLIED   SPACES  '
    assert ''.join(wrap_mm(source,23))==source
    assert ''.join(wrap_mm('WWWWWWWW  WWWWWWWW',19))=='WWWWWWWW  WWWWWWWW'


def test_standard_a3_actual_shared_residential_pdf_is_one_page(tmp_path):
    pytest.importorskip('cairosvg')
    from pypdf import PdfReader
    from oabm.drawings.single_line import create_single_line_set
    source,profile,job,layout=company_inputs(tmp_path,make_model(),{'width_mm':420,'height_mm':297})
    delivery=create_single_line_set(source,profile,job,output_directory=tmp_path/'a3')
    reader=PdfReader(delivery/'company-sheets/delivery/sheet-set.pdf')
    assert len(layout['pages'])==len(reader.pages)==1
    assert abs(float(reader.pages[0].mediabox.width)-420*72/25.4)<.01
    assert abs(float(reader.pages[0].mediabox.height)-297*72/25.4)<.01
    text=' '.join(reader.pages[0].extract_text().split())
    assert all(identity in text for identity in ('UTILITY','METER','MAIN','SUB','F001','F002','F003'))
