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
    register=[r['connection_id'] for p in doc['sheets'] for r in p.get('feeder_grid_rows',[])]
    assert sorted(register)==sorted(e['id'] for e in topology['connections'])
    assert all(ET.fromstring(body).tag.endswith('g') for body in bodies.values())
    if commercial:
        assert sum(e['equipment_type']=='panelboard' for e in topology['equipment'])==27
        assert len(bodies)==4
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
    assert {k:v for k,v in doc['layout'].items() if k not in {'source_model_sha256','topology_fingerprint','plan_fingerprint'}}=={k:v for k,v in other_doc['layout'].items() if k not in {'source_model_sha256','topology_fingerprint','plan_fingerprint'}}
    assert build(model=model)==(bodies,doc)


def test_unknown_facts_visible_and_long_labels_preserved():
    model=make_model()
    model=replace(model,circuits=tuple(replace(c,voltage_v=None,poles=None,phase=None) for c in model.circuits),conductors=())
    bodies,doc=build(model=model)
    text=' '.join(bodies.values())
    assert 'V UNKNOWN' in text and 'Conductor specification UNKNOWN' in text
    visible=' '.join(' '.join(ET.fromstring(body).itertext()) for body in bodies.values())
    assert 'deliberately long supplied' in ' '.join(visible.split())
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
        build_single_line_bodies(make_model(),[SimpleNamespace(id='SL101',drawing_bounds_mm=BOUNDS),SimpleNamespace(id='WRONG',drawing_bounds_mm=BOUNDS)])


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
    if commercial:
        normalized=' '.join(text.split())
        doc=json.loads((first/'topology-layout.json').read_bytes())
        assert normalized.count('STATE UNSPECIFIED CONTINUED E601')==sum(s['equipment_id']=='ATS' and s['continued'] for page in doc['sheets'] for s in page['equipment_symbols'])
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


def test_missing_glyph_and_oversized_labels_fail_instead_of_dropping():
    model=make_model()
    bad=replace(model,electrical_equipment=tuple(replace(e,name='Oversized '*100) if e.id=='SUB' else e for e in model.electrical_equipment))
    with pytest.raises(ValueError,match='equipment name exceeds'):
        build(model=bad)
    from oabm.drawings.company_sheets import _font
    coverage=_font('DejaVu Sans',400)[0]._oabm_codepoints
    glyph=next(c for c in ('⌀','🧑','𓀀') if ord(c) not in coverage)
    bad=replace(model,electrical_equipment=tuple(replace(e,name='Panel '+glyph) if e.id=='SUB' else e for e in model.electrical_equipment))
    with pytest.raises(ValueError,match=r'glyph U\+'):
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


def test_covered_unicode_equipment_label_is_preserved():
    model=make_model();label='José Peña — 45° Ø'
    model=replace(model,electrical_equipment=tuple(replace(e,name=label) if e.id=='SUB' else e for e in model.electrical_equipment))
    bodies,document=build(model=model)
    visible=''.join(''.join(ET.fromstring(body).itertext()) for body in bodies.values())
    assert label in visible
    assert next(e for e in document['topology']['equipment'] if e['id']=='SUB')['name']==label


@pytest.mark.parametrize('bounds',[BOUNDS,(5,5,220,210)])
def test_repeated_transfer_keeps_switch_state_and_home_reference(bounds):
    _,doc=build(True,bounds=bounds)
    home=doc['layout']['equipment_home_sheets']['ATS']
    seen=False
    repeats=[]
    for page in doc['sheets']:
        for symbol in page['equipment_symbols']:
            if symbol['equipment_id']!='ATS':continue
            x,y,r,s=symbol['bounds_mm']
            labels=[label for label in page['text_bounds'] if label['equipment_id']=='ATS' and
                    x<=label['bounds_mm'][0] and y<=label['bounds_mm'][1] and
                    label['bounds_mm'][2]<=r and label['bounds_mm'][3]<=s]
            visible=' '.join(label['text'] for label in labels)
            assert 'STATE UNSPECIFIED' in visible
            if seen or page['sheet_id']!=home:
                assert 'CONTINUED '+home in visible
                repeats.append(page['sheet_id'])
                # The two footer lines retain readable size and do not overlap.
                footer=[label for label in labels if label['text'] in {'STATE UNSPECIFIED','CONTINUED '+home}]
                assert len(footer)==2
                assert footer[0]['bounds_mm'][3]<footer[1]['bounds_mm'][1]
                assert all(label['font_size_mm']>=2.3 for label in footer)
            seen=True
    assert repeats
    assert all(sheet in {p['id'] for p in doc['layout']['pages']} for sheet in repeats)


@pytest.mark.parametrize('base,expected',[('SL101',['SL101','SL102','SL103','SL104']),('POWER009',['POWER009','POWER010','POWER011','POWER012']),('ONE-LINE',['ONE-LINE','ONE-LINE-2','ONE-LINE-3','ONE-LINE-4'])])
def test_caller_sheet_base_controls_all_multi_page_references(base,expected):
    model=make_model(True)
    topology=project_topology(model)
    layout=plan_pages(topology,BOUNDS,first_sheet_id=base,reserved_sheet_ids=('C001','SITE001'))
    assert [p['id'] for p in layout['pages']]==expected
    sheets=[SimpleNamespace(id=identity,drawing_bounds_mm=BOUNDS) for identity in expected]
    bodies,doc=build_single_line_bodies(model,sheets)
    assert list(bodies)==expected
    assert {r['id'] for r in doc['layout']['sheet_index']}==set(expected)
    assert set(doc['layout']['equipment_home_sheets'].values())<=set(expected)
    for row in doc['layout']['connections']:
        assert {row['source_sheet'],row['load_sheet'],row['register_sheet']}<=set(expected)
    for page in doc['sheets']:
        assert page['sheet_id'] in expected
        assert all(r['sheet_id'] in expected and r['counterpart_sheet'] in expected for r in page['terminal_occurrences'])
    visible=' '.join(' '.join(ET.fromstring(body).itertext()) for body in bodies.values())
    assert 'CONTINUED '+base in visible
    assert 'E601' not in visible and 'E602' not in visible


@pytest.mark.parametrize('reserved',[('SL101',),('sl102',)])
def test_caller_sheet_numbering_collision_is_explicit(reserved):
    with pytest.raises(ValueError,match='collides with a reserved sheet'):
        plan_pages(project_topology(make_model(True)),BOUNDS,first_sheet_id='SL101',reserved_sheet_ids=reserved)


def test_reserved_sheet_ids_and_overlong_continuations_are_rejected():
    topology=project_topology(make_model(True))
    with pytest.raises(ValueError,match='unique ignoring case'):
        plan_pages(topology,BOUNDS,first_sheet_id='SL101',reserved_sheet_ids=('C001','c001'))
    with pytest.raises(ValueError,match='1-64 ASCII'):
        plan_pages(topology,BOUNDS,first_sheet_id='A'*64)


def test_real_non_demo_sheet_ids_survive_shared_svg_pdf_and_package_manifest(tmp_path):
    pytest.importorskip('cairosvg')
    import json
    from pypdf import PdfReader
    from oabm.drawings.single_line import create_single_line_set
    source,profile,job,_=company_inputs(tmp_path,make_model(True))
    request=json.loads(job.read_bytes())
    request['sheets']=[{'id':'SL101','title':'SINGLE LINE'},{'id':'SL102','title':'SINGLE LINE CONT.'},{'id':'SL103','title':'SINGLE LINE CONT.'},{'id':'SL104','title':'FEEDER FACTS'}]
    job.write_text(json.dumps(request))
    delivery=create_single_line_set(source,profile,job,output_directory=tmp_path/'non-demo')
    assert json.loads((delivery/'MANIFEST.json').read_bytes())['sheet_order']==['SL101','SL102','SL103','SL104']
    shared=delivery/'company-sheets/delivery'
    assert json.loads((shared/'MANIFEST.json').read_bytes())['sheet_order']==['SL101','SL102','SL103','SL104']
    assert {p.name for p in (shared/'sheets').glob('*.svg')}=={'001-SL101.svg','002-SL102.svg','003-SL103.svg','004-SL104.svg'}
    text=' '.join(page.extract_text() for page in PdfReader(shared/'sheet-set.pdf').pages)
    assert 'SL101' in text and 'SL102' in text and 'E601' not in text
    assert 'STATE UNSPECIFIED CONTINUED SL101' in ' '.join(text.split())


@pytest.mark.parametrize('panel_count',[1,8,16,27,40,60])
def test_caller_ids_preserve_supported_panel_cardinalities(panel_count):
    model=make_model(True)
    ports={p.id:p for p in model.ports}
    if panel_count<27:
        keep={'P16'}|{f'P{i:02d}' for i in range(1,panel_count)}
        ids={e.id for e in model.electrical_equipment if e.equipment_type!='panelboard'}|keep
        circuits=tuple(c for c in model.circuits if ports[c.source_port_id].owner_id in ids and all(ports[p].owner_id in ids for p in c.load_port_ids))
        used={c.source_port_id for c in circuits}|{p for c in circuits for p in c.load_port_ids}
        circuit_ids={c.id for c in circuits}
        model=replace(model,electrical_equipment=tuple(e for e in model.electrical_equipment if e.id in ids),circuits=circuits,
                      ports=tuple(p for p in model.ports if p.id in used),conductors=tuple(w for w in model.conductors if w.circuit_id in circuit_ids))
    elif panel_count>27:
        base=next(e for e in model.electrical_equipment if e.id=='P27')
        circuit=next(c for c in model.circuits if c.id=='L27')
        wire=next(w for w in model.conductors if w.circuit_id=='L27')
        equipment=list(model.electrical_equipment);circuits=list(model.circuits);added_ports=list(model.ports);wires=list(model.conductors)
        for i in range(28,panel_count+1):
            identity=f'P{i:02d}';cid=f'L{i:02d}'
            equipment.append(replace(base,id=identity,name=f'Synthetic added panel {i}'))
            circuits.append(replace(circuit,id=cid,source_port_id=cid+'-out',load_port_ids=(cid+'-in',)))
            added_ports.extend((replace(ports[circuit.source_port_id],id=cid+'-out',owner_id='P16'),
                                replace(ports[circuit.load_port_ids[0]],id=cid+'-in',owner_id=identity)))
            wires.append(replace(wire,id=cid+'-wire',circuit_id=cid))
        model=replace(model,electrical_equipment=tuple(equipment),circuits=tuple(circuits),ports=tuple(added_ports),conductors=tuple(wires))
    topology=project_topology(model)
    layout=plan_pages(topology,BOUNDS,first_sheet_id='SL201')
    bodies,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in layout['pages']])
    assert sum(e['equipment_type']=='panelboard' for e in topology.equipment)==panel_count
    assert list(bodies)==[f'SL{201+i}' for i in range(len(bodies))]
    assert {r['equipment_id'] for r in layout['equipment_occurrences']}=={e.id for e in model.electrical_equipment}
    assert len([p for sheet in doc['sheets'] for p in sheet['connection_paths']])==len(model.circuits)
    assert len([r for page in doc['sheets'] for r in page.get('feeder_grid_rows',[])])==len(model.circuits)


@pytest.mark.parametrize('shape,count',[('multi-transfer',2),('multi-transfer',3),('multi-transfer',4),('two-transformers',2),('shallow',2),('deep',2)])
def test_general_valid_branching_graphs_have_complete_bounded_layout(shape,count):
    from oabm.drawings.single_line.layout import _crosses_box,_segments_cross
    model=module.make_variant_model(shape,transfer_count=count)
    topology=project_topology(model)
    layout=plan_pages(topology,BOUNDS,first_sheet_id='SL-020')
    sheets=[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in layout['pages']]
    bodies,doc=build_single_line_bodies(model,sheets)
    assert len(bodies)<=5
    assert list(bodies)==[f'SL-{20+i:03d}' for i in range(len(bodies))]
    edges={e['id']:e for e in topology.connections}
    shown=[path['connection_id'] for sheet in doc['sheets'] for path in sheet['connection_paths']]
    assert sorted(shown)==sorted(edges)
    assert {r['equipment_id'] for r in layout['equipment_occurrences']}=={e.id for e in model.electrical_equipment}
    for page in doc['sheets']:
        paths=page['connection_paths']
        for path in paths:
            edge=edges[path['connection_id']]
            for symbol in page['equipment_symbols']:
                if symbol['equipment_id'] in {edge['source_id'],edge['load_id']}:continue
                x,y,r,s=symbol['bounds_mm']
                assert not any(_crosses_box(a,b,(x+.1,y+.1,r-.1,s-.1)) for a,b in zip(path['points_mm'],path['points_mm'][1:]))
        for i,path in enumerate(paths):
            for other in paths[i+1:]:
                assert_bus_joins_only(path,other,edges,page['equipment_symbols'],{e['id']:e for e in topology.equipment},page.get('source_buses',()))
        assert min(r['font_size_mm'] for r in page['text_bounds'])>=2.3
    if shape=='deep':assert any(group.get('kind')=='chain' and len(group['connections'])>1 for p in layout['pages'] for group in p['groups'])


@pytest.mark.parametrize('right',[1052.944444444,1105.808333333])
def test_multiple_transfer_large_paper_is_not_limited_to_one_fixed_gutter(right):
    from oabm.drawings.single_line.layout import _crosses_box
    model=module.make_variant_model('multi-transfer',transfer_count=2)
    bounds=(7.055555556,7.055555556,right,892.944444444)
    topology=project_topology(model)
    layout=plan_pages(topology,bounds,first_sheet_id='SL-020')
    bodies,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in layout['pages']])
    assert len(bodies)<=(4 if right==1052.944444444 else 3)
    assert len([p for sheet in doc['sheets'] for p in sheet['connection_paths']])==len(topology.connections)
    assert {e['equipment_id'] for page in doc['sheets'] for e in page['equipment_symbols']}=={e['id'] for e in topology.equipment}
    for page in doc['sheets']:
        # Shared rows include ink beyond individual connection paths: inspect
        # the actual rendered full row and trunk, not only feeder endpoints.
        for bus in page['source_buses']:
            segments=[*zip(bus['path_mm'],bus['path_mm'][1:]),(bus['row_mm'][:2],bus['row_mm'][2:])]
            for symbol in page['equipment_symbols']:
                if symbol['equipment_id']==bus['equipment_id']:continue
                box=symbol.get('enclosure_bounds_mm')
                if box is None:continue
                l,t,r,b=box
                assert not any(_crosses_box(a,z,(l+.1,t+.1,r-.1,b-.1)) for a,z in segments),(bus['equipment_id'],symbol['equipment_id'])
        edges={edge['id']:edge for edge in topology.connections}
        for i,path in enumerate(page['connection_paths']):
            for other in page['connection_paths'][i+1:]:
                assert_bus_joins_only(path,other,edges,page['equipment_symbols'],{e['id']:e for e in topology.equipment},page.get('source_buses',()))


def test_channel_exhaustion_reflows_into_complete_explicit_source_groups(monkeypatch):
    from oabm.drawings.single_line import layout as module_layout
    def unavailable(*args):raise ValueError('synthetic bounded channel exhaustion')
    monkeypatch.setattr(module_layout,'backbone_geometry',unavailable)
    model=module.make_variant_model('multi-transfer',transfer_count=2)
    topology=project_topology(model)
    planned=module_layout.plan_pages(topology,BOUNDS,first_sheet_id='SL-020')
    bodies,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in planned['pages']])
    assert all(not page['nodes'] for page in planned['pages'])
    assert sorted(p['connection_id'] for sheet in doc['sheets'] for p in sheet['connection_paths'])==sorted(e['id'] for e in topology.connections)


@pytest.mark.parametrize('template',[{}, {'width_mm':1200,'height_mm':900,'rail_width_mm':140}])
def test_real_multiple_transfer_pdf_with_caller_seed_and_complete_topology(tmp_path,template):
    pytest.importorskip('cairosvg')
    import json
    from pypdf import PdfReader
    from oabm.drawings.single_line import create_single_line_set
    model=module.make_variant_model('multi-transfer',transfer_count=2)
    source,profile,job,layout=company_inputs(tmp_path,model,template)
    request=json.loads(job.read_bytes())
    request['sheets']=[{'id':f'SL-{20+i:03d}','title':'SINGLE LINE'} for i in range(len(layout['pages']))]
    job.write_text(json.dumps(request))
    delivery=create_single_line_set(source,profile,job,output_directory=tmp_path/'two-transfers')
    reader=PdfReader(delivery/'company-sheets/delivery/sheet-set.pdf')
    assert len(reader.pages)==len(layout['pages'])
    assert len(reader.pages)<=5
    if len(reader.pages)>4:
        assert layout['bus_reflows']==1
        assert layout['bus_reflow_sources']==['MAIN']
        topology=project_topology(model)
        normal={e['id'] for e in topology.connections if e['load_role']=='normal'}
        alternate={e['id'] for e in topology.connections if e['load_role']=='alternate'}
        normal_sheets={r['source_sheet'] for r in layout['connections'] if r['connection_id'] in normal}
        alternate_sheets={r['source_sheet'] for r in layout['connections'] if r['connection_id'] in alternate}
        assert len(normal_sheets)==1 and normal_sheets.isdisjoint(alternate_sheets)
    text=' '.join(' '.join(page.extract_text().split()) for page in reader.pages)
    assert 'ATS2' in text and 'STATE UNSPECIFIED' in text and 'SL-020' in text and 'E601' not in text
    assert all(c.id in text for c in model.circuits)
    manifest=json.loads((delivery/'MANIFEST.json').read_bytes())
    assert manifest['sheet_order']==[f'SL-{20+i:03d}' for i in range(len(reader.pages))]


@pytest.mark.parametrize('height',[168,169,176,191.9,192])
def test_marginal_height_splits_oversized_fans_with_complete_coverage(height):
    model=make_model(True)
    bounds=(5,5,220,5+height)
    topology=project_topology(model)
    planned=plan_pages(topology,bounds,first_sheet_id='SL-020')
    assert len(planned['pages'])<=len(topology.connections)+len(topology.equipment)
    bodies,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in planned['pages']])
    assert {r['equipment_id'] for r in planned['equipment_occurrences']}=={e.id for e in model.electrical_equipment}
    assert sorted(path['connection_id'] for sheet in doc['sheets'] for path in sheet['connection_paths'])==sorted(e['id'] for e in topology.connections)
    assert sorted(row['connection_id'] for p in planned['pages'] for row in p.get('entries',[]) if 'connection_id' in row)==sorted(e['id'] for e in topology.connections)
    for page in planned['pages']:
        assert all(g['height']<=height-44 for g in page['groups'])
    if height<192:
        assert all(g.get('kind')=='chain' for p in planned['pages'] for g in p['groups'])


def test_exact_small_paper_reproduction_completes_in_bounded_subprocess():
    import json,os,subprocess,sys
    root=Path(__file__).parents[1]
    script="""
import json,signal,sys
from pathlib import Path
from oabm.model import BuildingModel
from oabm.drawings.single_line import project_topology,plan_pages
model=BuildingModel.from_json(Path(sys.argv[1]).read_bytes())
topology=project_topology(model)
def deadline(*args):raise TimeoutError('pagination did not terminate within three seconds')
if hasattr(signal,'SIGALRM'):
    signal.signal(signal.SIGALRM,deadline)
    signal.alarm(3)
layout=plan_pages(topology,(5,5,220,173),first_sheet_id='SL-020')
if hasattr(signal,'alarm'):signal.alarm(0)
print(json.dumps({'pages':len(layout['pages']),'connections':len(layout['connections'])}))
"""
    result=subprocess.run([sys.executable,'-c',script,str(root/'examples/single_line/commercial-27.model.json')],
                          capture_output=True,text=True,timeout=8,env={**os.environ,'PYTHONPATH':str(root/'src')})
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert 1<=value['pages']<=64 and value['connections']==31


@pytest.mark.parametrize('bounds',[(5,5,220,172.9),(5,5,214.9,173)])
def test_paper_below_minimum_capacity_is_rejected_actionably(bounds):
    with pytest.raises(ValueError,match='at least 210 x 168 mm drawing bounds'):
        plan_pages(project_topology(make_model(True)),bounds,first_sheet_id='SL-020')


def assert_bus_joins_only(path,other,edges,symbols,equipment,buses=()):
    """Every intersection must lie on the same electrically common terminal.

    An enclosure alone is insufficient: transformer primary/secondary,
    transfer normal/alternate/load and auxiliary/phase terminals stay separate.
    Collinear overlap is checked at both ends, not just its midpoint.
    """
    from oabm.drawings.single_line.layout import _segments_cross
    first,second=edges[path['connection_id']],edges[other['connection_id']]
    common={first['source_id'],first['load_id']}&{second['source_id'],second['load_id']}
    def terminal(edge,identity):
        endpoint='source' if edge['source_id']==identity else 'load'
        kind=edge['drafting']['connection_kind']
        device=equipment[identity]['equipment_type']
        if device=='transformer':return kind,endpoint
        if device=='transfer_switch':return kind,endpoint,edge[endpoint+'_role']
        return kind
    allowed=[s['bounds_mm'] for s in symbols if s['equipment_id'] in common and terminal(first,s['equipment_id'])==terminal(second,s['equipment_id'])]
    for a,b in zip(path['points_mm'],path['points_mm'][1:]):
        for c,d in zip(other['points_mm'],other['points_mm'][1:]):
            if not _segments_cross(a,b,c,d):continue
            lo=(max(min(a[0],b[0]),min(c[0],d[0])),max(min(a[1],b[1]),min(c[1],d[1])))
            hi=(min(max(a[0],b[0]),max(c[0],d[0])),min(max(a[1],b[1]),max(c[1],d[1])))
            inside=any(all(l-1e-7<=p[0]<=r+1e-7 and t-1e-7<=p[1]<=bottom+1e-7 for p in (lo,hi)) for l,t,r,bottom in allowed)
            on_bus=False
            if first['source_id']==second['source_id'] and first['drafting']['connection_kind']==second['drafting']['connection_kind'] and first['voltage_v']==second['voltage_v']:
                owner=first['source_id']
                def on(a,b,p):return abs((b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]))<1e-6 and min(a[0],b[0])-1e-7<=p[0]<=max(a[0],b[0])+1e-7 and min(a[1],b[1])-1e-7<=p[1]<=max(a[1],b[1])+1e-7
                for bus in buses:
                    if bus['equipment_id']!=owner:continue
                    segments=[*zip(bus['path_mm'],bus['path_mm'][1:]),(bus['row_mm'][:2],bus['row_mm'][2:])]
                    if any(on(a,b,lo) and on(a,b,hi) for a,b in segments):on_bus=True
            assert inside or on_bus,(path['connection_id'],other['connection_id'],lo,hi,allowed)


@pytest.mark.parametrize('device,first_role,second_role',[('transformer','load','source'),('transfer_switch','normal','alternate'),('panelboard','power','neutral')])
def test_bus_intersection_checker_rejects_noncommon_terminals(device,first_role,second_role):
    def edge(identity,role):
        source=role=='source'
        return {'id':identity,'source_id':'OWNER' if source else identity+'-source','load_id':identity+'-load' if source else 'OWNER',
                'source_role':'load','load_role':role,'drafting':{'connection_kind':role if role in {'power','neutral'} else 'power'}}
    edges={'a':edge('a',first_role),'b':edge('b',second_role)}
    with pytest.raises(AssertionError):
        assert_bus_joins_only({'connection_id':'a','points_mm':[(0,5),(10,5)]},{'connection_id':'b','points_mm':[(5,0),(5,10)]},edges,
                             [{'equipment_id':'OWNER','bounds_mm':(0,0,10,10)}],{'OWNER':{'equipment_type':device}})
