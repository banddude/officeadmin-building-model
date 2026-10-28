"""Explicit construction-drafting proof; no private reference content or pixels."""
from dataclasses import replace
from types import SimpleNamespace
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from oabm.drawings.single_line.demo_drafting import make_model
from oabm.drawings.single_line import project_topology,plan_pages
from oabm.drawings.single_line.production import build_single_line_bodies,create_single_line_set
from oabm.drawings.company_sheets import resolve_company_sheet_set,render_company_sheet_svg

BOUNDS=(7.055555556,7.055555556,820.208333333,602.544444444)


def rendered(model=None):
    model=model or make_model();topology=project_topology(model)
    plan=plan_pages(topology,BOUNDS,first_sheet_id='E3.1')
    bodies,document=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=BOUNDS) for p in plan['pages']],layout=plan)
    return model,bodies,document


def test_one_sheet_has_actual_bus_and_inline_electrical_vocabulary():
    model,bodies,document=rendered()
    assert list(bodies)==['E3.1']
    assert len(document['sheets'][0]['equipment_symbols'])==8
    assert len({s['equipment_id'] for s in document['sheets'][0]['equipment_symbols']})==8
    kinds={symbol['kind'] for page in document['sheets'] for symbol in page['electrical_symbols']}
    assert {'bus','meter','breaker','fuse','disconnect','fused_disconnect','transformer_windings','ground_electrode','neutral_bus'}<=kinds
    paths=[path['connection_id'] for page in document['sheets'] for path in page['connection_paths']]
    assert sorted(paths)==sorted(edge['id'] for edge in document['topology']['connections'])
    assert {r['equipment_id'] for r in document['layout']['equipment_occurrences']}=={e.id for e in model.electrical_equipment}
    text=' '.join(ET.fromstring(bodies['E3.1']).itertext())
    for phrase in ('POWER ONE-LINE','ONE-LINE NOTES','FEEDER SCHEDULE','(E) MS1','(N) HP1','22000 A','N/G ISOLATED'):
        assert phrase in text
    assert all(node.tag.split('}')[-1] in {'g','line','polyline','circle','text'} for node in ET.fromstring(bodies['E3.1']).iter())


def test_missing_protection_and_grounding_facts_do_not_create_symbols():
    model=make_model()
    model=replace(model,circuits=tuple(replace(c,attributes={}) for c in model.circuits if c.id not in {'F06','F07'}),
                  conductors=tuple(w for w in model.conductors if w.circuit_id not in {'F06','F07'}),
                  electrical_equipment=tuple(replace(e,attributes={}) for e in model.electrical_equipment if e.id not in {'N1','G1'}),
                  ports=tuple(p for p in model.ports if p.owner_id not in {'N1','G1'} and p.id not in {'F06-source','F07-source'}))
    _,bodies,doc=rendered(model)
    kinds={s['kind'] for page in doc['sheets'] for s in page['electrical_symbols']}
    assert not {'breaker','fuse','disconnect','fused_disconnect','ground_electrode','neutral_bus'}&kinds
    assert 'PROTECTION NOT SUPPLIED' in ' '.join(' '.join(ET.fromstring(v).itertext()) for v in bodies.values())


def test_contradictory_supplied_poles_fail_before_layout():
    model=make_model()
    circuits=[]
    for c in model.circuits:
        if c.id=='F02':
            facts=dict(c.attributes['single_line_drafting']);facts['protection']={**facts['protection'],'poles':2}
            c=replace(c,attributes={'single_line_drafting':facts})
        circuits.append(c)
    with pytest.raises(ValueError,match='conflicts with canonical circuit.poles'):
        project_topology(replace(model,circuits=tuple(circuits)))


def test_representative_shared_svg_pdf_preserves_all_source_identity(tmp_path):
    pytest.importorskip('cairosvg')
    from pypdf import PdfReader
    model=make_model();(tmp_path/'model.json').write_text(model.to_json())
    (tmp_path/'profile.json').write_text(json.dumps({'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC','project':'SYNTHETIC ONE LINE'},'template':{}}))
    (tmp_path/'job.json').write_text(json.dumps({'version':'company-sheet-job-v1','set_id':'drafting-proof','date':'2026-10-27','revision':'A','formats':['svg','pdf'],'sheets':[{'id':'E3.1','title':'POWER ONE LINE'}]}))
    delivery=create_single_line_set(tmp_path/'model.json',tmp_path/'profile.json',tmp_path/'job.json',output_directory=tmp_path/'packet')
    shared=delivery/'company-sheets/delivery'
    reader=PdfReader(shared/'sheet-set.pdf');assert len(reader.pages)==1
    text=' '.join(reader.pages[0].extract_text().split())
    assert all(c.id in text for c in model.circuits)
    assert '22000 A' in text and 'EXAMPLE ELECTRIC' in text
    assert (delivery/'model.json').read_bytes()==(tmp_path/'model.json').read_bytes()

@pytest.mark.parametrize('case',['residential','commercial-27','representative'])
def test_feeders_clear_all_text_and_status_has_enclosure_padding(case):
    """Actual source-to-terminal paths must clear measured text ink bands."""
    import runpy
    from oabm.drawings.single_line.layout import _crosses_box
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    model=make_model() if case=='representative' else fixtures['make_model'](case=='commercial-27')
    _,_,doc=rendered(model)
    checked=0
    for page in doc['sheets']:
        for path in page['connection_paths']:
            for label in page['text_bounds']:
                l,t,r,b=label['bounds_mm']
                assert not any(_crosses_box(a,z,(l-.15,t-.15,r+.15,b+.15)) for a,z in zip(path['points_mm'],path['points_mm'][1:])),(case,page['sheet_id'],path['connection_id'],label['text'])
                checked+=1
        for symbol in page['equipment_symbols']:
            if next(e for e in doc['topology']['equipment'] if e['id']==symbol['equipment_id'])['equipment_type'] not in {'panelboard','switchboard','disconnect','transfer_switch'}:continue
            x,y,r,b=symbol['bounds_mm']
            footer=[label for label in page['text_bounds'] if label['equipment_id']==symbol['equipment_id'] and
                    label['text'].startswith(('STATE UNSPECIFIED','CONTINUED ','N/G ')) and
                    x<=label['bounds_mm'][0] and label['bounds_mm'][2]<=r and y<=label['bounds_mm'][1] and label['bounds_mm'][3]<=b]
            for label in footer:assert label['bounds_mm'][3]<=b-.5-2,(case,label)
    assert checked
    for page in doc['sheets']:
        for glyph in page['electrical_symbols']:
            if glyph['kind'] not in {'breaker','fuse','disconnect','fused_disconnect'}:continue
            l,t,r,b=glyph['bounds_mm'];center=((l+r)/2,(t+b)/2)
            assert not any(x<=center[0]<=right and y<=center[1]<=bottom for symbol in page['equipment_symbols'] for x,y,right,bottom in [symbol['bounds_mm']]),glyph


def test_supplied_opaque_circuit_label_reaches_shared_grid_svg_and_pdf(tmp_path):
    pytest.importorskip('cairosvg')
    from pypdf import PdfReader
    model=make_model()
    model=replace(model,circuits=tuple(replace(c,circuit_number='OPAQUE-LABEL-947') if c.id=='F02' else c for c in model.circuits))
    _,bodies,doc=rendered(model)
    assert next(e for e in doc['topology']['connections'] if e['circuit_id']=='F02')['circuit_number']=='OPAQUE-LABEL-947'
    visible=' '.join(' '.join(ET.fromstring(v).itertext()) for v in bodies.values())
    assert 'OPAQUE-LABEL-947' in visible and 'SUPPLIED LABEL:' in visible
    assert not doc['layout']['delegated_feeder_connection_ids']
    (tmp_path/'model.json').write_text(model.to_json())
    (tmp_path/'profile.json').write_text(json.dumps({'version':'company-sheet-profile-v1','fields':{'company':'EXAMPLE ELECTRIC'},'template':{}}))
    (tmp_path/'job.json').write_text(json.dumps({'version':'company-sheet-job-v1','set_id':'opaque-label-proof','date':'2026-10-27','revision':'A','formats':['svg','pdf'],'sheets':[{'id':identity,'title':'POWER ONE LINE'} for identity in bodies]}))
    delivery=create_single_line_set(tmp_path/'model.json',tmp_path/'profile.json',tmp_path/'job.json',output_directory=tmp_path/'packet')
    text=' '.join(page.extract_text() for page in PdfReader(delivery/'company-sheets/delivery/sheet-set.pdf').pages)
    assert 'OPAQUE-LABEL-947' in text and 'SUPPLIED LABEL:' in text


def test_feeder_grid_wraps_ordinary_words_at_spaces_and_keeps_every_row():
    from oabm.drawings.single_line.feeder_grid import paginate_feeder_grid
    import runpy
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    topology=project_topology(fixtures['make_model'](True))
    grids=paginate_feeder_grid(topology,[e['id'] for e in topology.connections],(5,5,315,590),sheet_id='SL-020',width_mm=900,height_mm=650)
    assert sorted(key for grid in grids for key in grid['connection_ids'])==sorted(e['id'] for e in topology.connections)
    tokens=[word for grid in grids for value in ET.fromstring(grid['body_svg']).itertext() for word in value.split()]
    assert tokens.count('PROTECTION')==len(topology.connections)+len(grids)
    assert tokens.count('SUPPLIED')==len(topology.connections)+len(grids)
    assert tokens.count('supplied')==len(topology.connections)


def test_residential_grammar_uses_direct_axis_and_reserved_side_labels():
    import runpy
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    _,_,doc=rendered(fixtures['make_model']())
    assert len(doc['sheets'])==1
    page=doc['sheets'][0]
    assert len(page['equipment_symbols'])==4 and len(page['connection_paths'])==3
    assert len({symbol['axis_x'] for symbol in page['equipment_symbols']})==1
    for path in page['connection_paths']:
        assert len({round(p[0],8) for p in path['points_mm']})==1,path
        assert all(a[1]<=b[1] for a,b in zip(path['points_mm'],path['points_mm'][1:])),path
    for symbol in page['equipment_symbols']:
        l,t,r,b=symbol['label_lane']
        assert r+8<symbol['axis_x']
        for label in page['text_bounds']:
            if label['equipment_id']!=symbol['equipment_id'] or label['text'] in {'S','M','G'}:continue
            x,y,right,bottom=label['bounds_mm']
            assert l<=x and t<=y and right<=r and bottom<=b,(symbol,label)

@pytest.mark.parametrize('case',['representative','commercial-27'])
def test_branch_grammar_reserves_annotations_and_never_traces_enclosures(case):
    import runpy
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    model=make_model() if case=='representative' else fixtures['make_model'](True)
    _,_,doc=rendered(model)
    edges={e['id']:e for e in doc['topology']['connections']}
    if case=='representative':
        assert len(doc['sheets'])==1
        assert len(doc['sheets'][0]['equipment_symbols'])==len(model.electrical_equipment)==8
    for page in doc['sheets']:
        for symbol in page['equipment_symbols']:
            if symbol['label_lane']:
                assert 0<symbol['axis_x']-symbol['label_lane'][2]<=32,symbol
            enclosure=symbol['enclosure_bounds_mm']
            if not enclosure:continue
            l,t,r,b=enclosure
            for path in page['connection_paths']:
                for a,z in zip(path['points_mm'],path['points_mm'][1:]):
                    if abs(a[0]-z[0])<1e-7 and any(abs(a[0]-edge)<1e-7 for edge in (l,r)):
                        assert min(a[1],z[1])>=b or max(a[1],z[1])<=t,(symbol,path)
                    if abs(a[1]-z[1])<1e-7 and any(abs(a[1]-edge)<1e-7 for edge in (t,b)):
                        assert min(a[0],z[0])>=r or max(a[0],z[0])<=l,(symbol,path)
        if case=='representative':
            for path in page['connection_paths']:
                if edges[path['connection_id']]['drafting']['connection_kind']=='power':
                    assert len({round(p[0],8) for p in path['points_mm']})==1,path
    assert sorted(p['connection_id'] for page in doc['sheets'] for p in page['connection_paths'])==sorted(edges)


def test_commercial_first_page_has_one_ats_and_direct_utility_main_axis():
    import runpy
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    _,_,doc=rendered(fixtures['make_model'](True))
    page=doc['sheets'][0]
    ids=[symbol['equipment_id'] for symbol in page['equipment_symbols']]
    assert ids.count('ATS')==1 and len(ids)==len(set(ids))
    path=next(path for path in page['connection_paths'] if path['connection_id'].startswith('F001::'))
    assert len({round(point[0],7) for point in path['points_mm']})==1
    assert all(a[1]<=b[1] for a,b in zip(path['points_mm'],path['points_mm'][1:]))
    for row in doc['sheets']:
        ids=[s['equipment_id'] for s in row['equipment_symbols']]
        assert len(ids)==len(set(ids))
    assert len(page['source_buses'])==2
    assert {bus['equipment_id'] for bus in page['source_buses']}=={'ATS'}
    assert len({tuple(bus['path_mm'][0]) for bus in page['source_buses']})==1


def test_two_transfer_p16_bus_fallback_uses_whole_diagram_bounds(monkeypatch):
    import runpy
    from oabm.drawings.single_line import layout as layout_module
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    model=fixtures['make_variant_model']('multi-transfer',transfer_count=2)
    actual=layout_module._channel_path;calls=[]
    def checked(start,end,bounds,boxes,segments,**kwargs):
        calls.append(tuple(bounds))
        return actual(start,end,bounds,boxes,segments,**kwargs)
    monkeypatch.setattr(layout_module,'_channel_path',checked)
    _,_,doc=rendered(model)
    expected=tuple(round(v,7) for v in doc['layout']['effective_regions']['diagram'])
    assert expected in calls
    buses=[bus for page in doc['sheets'] for bus in page['source_buses'] if bus['equipment_id']=='P16']
    assert buses
    for page in doc['sheets']:
        ids=[s['equipment_id'] for s in page['equipment_symbols']]
        assert len(ids)==len(set(ids))
    assert sorted(p['connection_id'] for page in doc['sheets'] for p in page['connection_paths'])==sorted(e['id'] for e in doc['topology']['connections'])


@pytest.mark.parametrize('count',[2,4])
def test_multi_transfer_routes_land_on_visible_normal_and_alternate_terminals(count):
    import runpy
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    _,_,doc=rendered(fixtures['make_variant_model']('multi-transfer',transfer_count=count))
    edges={e['id']:e for e in doc['topology']['connections']}
    assert doc['layout']['bus_reflows']<=2+len(doc['topology']['equipment'])
    for page in doc['sheets']:
        symbols={s['equipment_id']:s for s in page['equipment_symbols']}
        assert len(symbols)==len(page['equipment_symbols'])
        for path in page['connection_paths']:
            edge=edges[path['connection_id']]
            if not edge['load_id'].startswith('ATS'):continue
            actual=symbols[edge['load_id']]
            expected=(actual['axis_x']-8,actual['bounds_mm'][1]+(21 if edge['load_role']=='normal' else 27))
            assert path['points_mm'][-1]==pytest.approx(expected,abs=1e-6)
    assert sorted(p['connection_id'] for page in doc['sheets'] for p in page['connection_paths'])==sorted(edges)


def test_supplied_branch_protection_is_on_its_private_feeder_not_shared_bus():
    import runpy
    fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
    model=fixtures['make_model'](True)
    model=replace(model,circuits=tuple(replace(c,attributes={'single_line_drafting':{'protection':{'kind':'breaker','rating_a':60,'poles':3}}}) if c.id=='H01' else c for c in model.circuits))
    _,_,doc=rendered(model)
    page=next(page for page in doc['sheets'] if any(g['source_id']=='H01' and g['kind']=='breaker' for g in page['electrical_symbols']))
    glyph=next(g for g in page['electrical_symbols'] if g['source_id']=='H01' and g['kind']=='breaker')
    l,t,r,b=glyph['bounds_mm'];center=((l+r)/2,(t+b)/2)
    panel=next(s for s in page['equipment_symbols'] if s['equipment_id']=='P01')
    row=next(bus for bus in page['source_buses'] if any(key.startswith('H01::') for key in bus['connection_ids']))
    assert center[0]==pytest.approx(panel['axis_x'])
    assert row['row_mm'][1]<center[1]<panel['bounds_mm'][1]


def mixed_network_model(*,extra_neutral=5,extra_ground=0,transfer=False):
    """Explicit copies of F06/F07; no inferred bond, voltage, pole or conductor."""
    reference=make_model()
    if transfer:
        import runpy
        fixtures=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
        model=fixtures['make_variant_model']('multi-transfer',transfer_count=2)
        source='MAIN'
        model=replace(model,electrical_equipment=tuple(replace(e,attributes={**e.attributes,'single_line_drafting':{'neutral_ground_bond':'isolated'}}) if e.id==source else e for e in model.electrical_equipment))
    else:model=reference;source='MS1'
    equipment=list(model.electrical_equipment);ports=list(model.ports);circuits=list(model.circuits);wires=list(model.conductors)
    for kind,count,base_id,target_id in [('neutral',extra_neutral,'F06','N1'),('grounding',extra_ground,'F07','G1')]:
        for i in range(count):
            stem=('XN' if kind=='neutral' else 'XG')+str(i+1)
            base=next(c for c in reference.circuits if c.id==base_id)
            equipment.append(replace(next(e for e in reference.electrical_equipment if e.id==target_id),id=stem,name='Explicit '+kind+' terminal '+str(i+1)))
            ports.extend((replace(next(p for p in reference.ports if p.id==base.source_port_id),id=stem+'-source',owner_id=source),replace(next(p for p in reference.ports if p.id==base.load_port_ids[0]),id=stem+'-load',owner_id=stem)))
            circuits.append(replace(base,id=stem+'-circuit',source_port_id=stem+'-source',load_port_ids=(stem+'-load',)))
            wires.append(replace(next(w for w in reference.conductors if w.circuit_id==base_id),id=stem+'-wire',circuit_id=stem+'-circuit'))
    return replace(model,model_id='synthetic-separated-terminal-networks',electrical_equipment=tuple(equipment),ports=tuple(ports),circuits=tuple(circuits),conductors=tuple(wires))


@pytest.mark.parametrize('extra_ground,transfer',[(0,False),(5,False),(5,True)])
def test_expanded_mixed_fans_preserve_actual_terminal_networks(extra_ground,transfer):
    import runpy
    from oabm.drawings.single_line.layout import _segments_cross
    # This is deliberately the existing assertion, without mixed-kind exceptions.
    check=runpy.run_path(str(Path(__file__).with_name('test_single_line.py')))['assert_bus_joins_only']
    model=mixed_network_model(extra_ground=extra_ground,transfer=transfer)
    topology=project_topology(model);bounds=(5,5,1000,900)
    plan=plan_pages(topology,bounds,first_sheet_id='SL-020')
    bodies,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in plan['pages']],layout=plan)
    edges={e['id']:e for e in topology.connections};equipment={e['id']:e for e in topology.equipment}
    source='MAIN' if transfer else 'MS1'
    networks=set()
    for page in doc['sheets']:
        root=ET.fromstring(bodies[page['sheet_id']])
        for bus in page['source_buses']:
            if bus['equipment_id']!=source:continue
            owner,kind,role=bus['terminal_key'];networks.add(kind)
            assert {edges[key]['drafting']['connection_kind'] for key in bus['connection_ids']}=={kind}
            assert {edges[key]['source_role'] for key in bus['connection_ids']}=={role}
            symbol=next(s for s in page['equipment_symbols'] if s['equipment_id']==owner)
            assert bus['path_mm'][0][1]-symbol['bounds_mm'][1]==pytest.approx({'power':23,'neutral':28,'grounding':32}[kind])
            if kind!='power':
                point=bus['path_mm'][0]
                assert any(n.tag.endswith('line') and float(n.get('y1'))==pytest.approx(point[1]) and float(n.get('y2'))==pytest.approx(point[1]) and min(float(n.get('x1')),float(n.get('x2')))<=point[0]<=max(float(n.get('x1')),float(n.get('x2'))) and n.get('stroke-width')=='0.45' for n in root.iter()),(kind,point)
        for i,path in enumerate(page['connection_paths']):
            for other in page['connection_paths'][i+1:]:check(path,other,edges,page['equipment_symbols'],equipment,page['source_buses'])
            kind=edges[path['connection_id']]['drafting']['connection_kind']
            for bar in page['electrical_symbols']:
                if bar['kind']!='terminal_bar' or bar['network_kind']==kind:continue
                assert not any(_segments_cross(a,b,c,d) for a,b in zip(path['points_mm'],path['points_mm'][1:]) for c,d in bar['segments_mm'])
            edge=edges[path['connection_id']]
            if edge['load_id'].startswith('ATS'):
                symbol=next(s for s in page['equipment_symbols'] if s['equipment_id']==edge['load_id'])
                assert path['points_mm'][-1]==pytest.approx((symbol['axis_x']-8,symbol['bounds_mm'][1]+(21 if edge['load_role']=='normal' else 27)),abs=1e-6)
    assert networks=={'power','neutral','grounding'}
    assert sorted(p['connection_id'] for page in doc['sheets'] for p in page['connection_paths'])==sorted(edges)


def test_distinct_auxiliary_source_roles_refuse_unsupported_shared_bar():
    model=mixed_network_model()
    model=replace(model,ports=tuple(replace(p,role='separate-neutral-terminal') if p.id=='XN1-source' else p for p in model.ports))
    with pytest.raises(ValueError,match='distinct neutral source roles'):
        plan_pages(project_topology(model),(5,5,1000,900))


def test_terminal_bar_runtime_guard_refuses_an_injected_phase_neutral_join():
    from copy import deepcopy
    from oabm.drawings.single_line.render import render_body
    model=mixed_network_model();topology=project_topology(model);bounds=(5,5,1000,900)
    plan=plan_pages(topology,bounds,first_sheet_id='SL-020')
    page=next(p for p in plan['pages'] if any(g.get('source_terminal_key',[None,None])[1]=='neutral' for g in p['groups']))
    page=deepcopy(page)
    neutral=next(g for g in page['groups'] if g.get('source_terminal_key',[None,None])[1]=='neutral')
    phase=next(g for g in page['groups'] if g.get('source_terminal_key',[None,None])[1]=='power')
    key=neutral['connections'][0]
    wrong=phase['source_bus_path_mm'][0]
    neutral['feeder_paths_mm'][key][0]=wrong
    with pytest.raises(ValueError,match='intersects power terminal'):
        render_body(topology,plan,page,SimpleNamespace(id=page['id'],drawing_bounds_mm=bounds))


def test_exact_large_mixed_terminal_buses_have_separate_ink_and_clear_enclosures():
    """Actual 1200x900 / rail140 profile bounds from the rejected PDF."""
    from math import hypot
    from oabm.drawings.single_line.layout import _crosses_box,_segments_cross
    model=mixed_network_model(extra_ground=5,transfer=True)
    bounds=(7.055555556,7.055555556,1052.944444444,892.944444444)
    plan=plan_pages(project_topology(model),bounds,first_sheet_id='SL-020')
    _,doc=build_single_line_bodies(model,[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in plan['pages']],layout=plan)
    def point_distance(p,a,b):
        dx,dy=b[0]-a[0],b[1]-a[1]
        length=dx*dx+dy*dy
        t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length)) if length else 0
        return hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)
    def distance(a,b,c,d):
        return 0 if _segments_cross(a,b,c,d) else min(point_distance(a,c,d),point_distance(b,c,d),point_distance(c,a,b),point_distance(d,a,b))
    for page in doc['sheets']:
        buses=page['source_buses']
        def segments(bus):return [*zip(bus['path_mm'],bus['path_mm'][1:]),(bus['row_mm'][:2],bus['row_mm'][2:])]
        for i,bus in enumerate(buses):
            for symbol in page['equipment_symbols']:
                if symbol['equipment_id']==bus['equipment_id'] or not symbol['enclosure_bounds_mm']:continue
                l,t,r,b=symbol['enclosure_bounds_mm']
                # Actual retained stroke widths, not only conductor centrelines.
                pad=(bus['stroke_width_mm']+symbol['enclosure_stroke_width_mm'])/2
                assert all(not _crosses_box(a,z,(l-pad,t-pad,r+pad,b+pad)) for a,z in segments(bus)),(page['sheet_id'],bus['equipment_id'],symbol['equipment_id'])
            for other in buses[i+1:]:
                if bus['terminal_key']==other['terminal_key']:continue
                assert all(distance(a,b,c,d)>=.90-1e-6 for a,b in segments(bus) for c,d in segments(other)),(page['sheet_id'],bus['terminal_key'],other['terminal_key'])
