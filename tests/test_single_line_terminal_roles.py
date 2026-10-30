"""Synthetic regressions for terminal identity in compact core layouts."""
from dataclasses import replace
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
import runpy

import pytest

from oabm.drawings.single_line import plan_pages, project_topology
from oabm.drawings.single_line.demo_drafting import make_model
from oabm.drawings.single_line.layout import validate_precomputed_layout
from oabm.drawings.single_line.production import build_single_line_bodies

BOUNDS = (7.055555556, 7.055555556, 820.208333333, 602.544444444)


def compact_branches(*, distinct_roles=False):
    model = make_model()
    circuits = tuple(replace(c, attributes={}) for c in model.circuits if c.id in {'F03', 'F04'})
    port_ids = {p for c in circuits for p in (c.source_port_id, *c.load_port_ids)}
    equipment = tuple(replace(e, equipment_type='panelboard', rated_voltage_v=480)
                      if e.id == 'T1' else e for e in model.electrical_equipment
                      if e.id in {'MS1', 'HP1', 'T1'})
    ports = tuple(replace(p, role='separate-terminal') if distinct_roles and p.id == 'F04-source' else p
                  for p in model.ports if p.id in port_ids)
    return replace(model, electrical_equipment=equipment, ports=ports, circuits=circuits,
                   conductors=tuple(w for w in model.conductors if w.circuit_id in {'F03', 'F04'}))


def test_compact_core_refuses_distinct_source_roles_instead_of_false_bar_join():
    topology = project_topology(compact_branches(distinct_roles=True))
    # At ac0c139 this produced a three-node core with both distinct roles
    # attached to the same continuous MS1 bar, despite the fan-mode guard.
    with pytest.raises(ValueError, match='MS1: distinct power source roles'):
        plan_pages(topology, BOUNDS, first_sheet_id='E3.1')
    # Old, sealed layouts must not bypass the new safety boundary either.
    safe_layout = plan_pages(project_topology(compact_branches()), BOUNDS, first_sheet_id='E3.1')
    with pytest.raises(ValueError, match='MS1: distinct power source roles'):
        validate_precomputed_layout(safe_layout, topology, BOUNDS)


def test_same_role_branches_keep_port_identity_and_stable_labels():
    model = compact_branches()
    topology = project_topology(model)
    plan = plan_pages(topology, BOUNDS, first_sheet_id='E3.1')
    assert len(plan['pages'][0]['nodes']) == 3  # exercise compact core, not fan fallback
    sheets = [SimpleNamespace(id=p['id'], drawing_bounds_mm=BOUNDS) for p in plan['pages']]
    bodies, doc = build_single_line_bodies(model, sheets, layout=plan)
    occurrences = [row for page in doc['sheets'] for row in page['terminal_occurrences']]
    by_edge = {edge['id']: edge for edge in topology.connections}
    assert len(occurrences) == 4
    for row in occurrences:
        edge = by_edge[row['connection_id']]
        endpoint = row['endpoint']
        assert (row['equipment_id'], row['port_id'], row['role']) == (
            edge[endpoint+'_id'], edge[endpoint+'_port_id'], edge[endpoint+'_role'])
        path = next(p for page in doc['sheets'] for p in page['connection_paths']
                    if p['connection_id'] == row['connection_id'])
        assert row['point_mm'] == (path['points_mm'][0] if endpoint == 'source' else path['points_mm'][-1])
    assert {row['port_id'] for row in occurrences if row['endpoint']=='source'} == {'F03-source', 'F04-source'}
    text = ' '.join(' '.join(ET.fromstring(body).itertext()) for body in bodies.values())
    assert 'F03-source' in text and 'F04-source' in text
    repeat, repeat_doc = build_single_line_bodies(model, sheets, layout=plan)
    assert repeat == bodies
    assert repeat_doc['sheets'] == doc['sheets']
    reordered=replace(model,electrical_equipment=tuple(reversed(model.electrical_equipment)),
                      ports=tuple(reversed(model.ports)),circuits=tuple(reversed(model.circuits)),
                      conductors=tuple(reversed(model.conductors)))
    fresh=plan_pages(project_topology(reordered),BOUNDS,first_sheet_id='E3.1')
    reordered_bodies,reordered_doc=build_single_line_bodies(reordered,sheets,layout=fresh)
    assert reordered_bodies==bodies
    assert [(row['connection_id'],row['tag']) for row in reordered_doc['layout']['connections']] == [
        (row['connection_id'],row['tag']) for row in doc['layout']['connections']]


def test_distinct_auxiliary_load_roles_are_not_collapsed_on_one_bar():
    model = make_model()
    circuit = next(c for c in model.circuits if c.id == 'F06')
    source = next(p for p in model.ports if p.id == circuit.load_port_ids[0])
    load = next(p for p in model.ports if p.id == circuit.source_port_id)
    equipment = [e for e in model.electrical_equipment if e.id in {'N1', 'MS1'}]
    equipment.append(replace(equipment[0], id='N2', name='Second synthetic neutral bus',
                             equipment_type='neutral_bus', rated_voltage_v=None))
    ports = (replace(source, id='N1-out', owner_id='N1'),
             replace(source, id='N2-out', owner_id='N2'),
             replace(load, id='MS1-in-a', role='neutral'),
             replace(load, id='MS1-in-b', role='separate-terminal'))
    circuits = (replace(circuit, id='N-A', source_port_id='N1-out', load_port_ids=('MS1-in-a',)),
                replace(circuit, id='N-B', source_port_id='N2-out', load_port_ids=('MS1-in-b',)))
    model = replace(model, electrical_equipment=tuple(equipment), ports=ports, circuits=circuits, conductors=())
    with pytest.raises(ValueError, match='MS1: distinct neutral load roles'):
        plan_pages(project_topology(model), BOUNDS)


def compact_transfer(panel_count):
    base = runpy.run_path(str(Path(__file__).parents[1] / 'examples/single_line/make_models.py'))['make_model'](True)
    ids = {'UTILITY', 'GEN', 'MAIN', 'ATS', 'XFMR', 'P16'} | {f'P{i:02d}' for i in range(1, panel_count+1)}
    ports = tuple(p for p in base.ports if p.owner_id in ids)
    port_ids = {p.id for p in ports}
    circuits = tuple(c for c in base.circuits if c.source_port_id in port_ids and set(c.load_port_ids) <= port_ids)
    used = {p for c in circuits for p in (c.source_port_id, *c.load_port_ids)}
    return replace(base, electrical_equipment=tuple(e for e in base.electrical_equipment if e.id in ids),
                   ports=tuple(p for p in ports if p.id in used), circuits=circuits,
                   conductors=tuple(w for w in base.conductors if w.circuit_id in {c.id for c in circuits}))


@pytest.mark.parametrize('count,bounds', [(1,(5,5,620,460)), (3,(5,5,500,350))])
def test_chain_transfer_and_fan_paths_do_not_falsely_join(count,bounds):
    model=compact_transfer(count);topology=project_topology(model)
    plan=plan_pages(topology,bounds)
    sheets=[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in plan['pages']]
    bodies,doc=build_single_line_bodies(model,sheets,layout=plan)
    check=runpy.run_path(str(Path(__file__).with_name('test_single_line.py')))['assert_bus_joins_only']
    edges={e['id']:e for e in topology.connections};equipment={e['id']:e for e in topology.equipment}
    for page in doc['sheets']:
        for i,path in enumerate(page['connection_paths']):
            for other in page['connection_paths'][i+1:]:
                check(path,other,edges,page['equipment_symbols'],equipment,page['source_buses'])
    assert sorted(p['connection_id'] for page in doc['sheets'] for p in page['connection_paths']) == sorted(edges)
    assert build_single_line_bodies(model,sheets,layout=plan)[0] == bodies
    # Normal, alternate and load labels retain the canonical identities and C-tags.
    for endpoint in (r for page in doc['sheets'] for r in page['terminal_occurrences']):
        edge=edges[endpoint['connection_id']];side=endpoint['endpoint']
        assert endpoint['port_id']==edge[side+'_port_id'] and endpoint['role']==edge[side+'_role']
    if count==1:
        # Check visible stroke clearance, not just centerline crossings, for
        # the exact ATS normal-input versus load-bus reproduction.
        from math import hypot
        from oabm.drawings.single_line.layout import _segments_cross, PAPER_BUS_CLEARANCE_MM
        def point_distance(p,a,b):
            dx,dy=b[0]-a[0],b[1]-a[1];length=dx*dx+dy*dy
            t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length)) if length else 0
            return hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)
        def distance(a,b,c,d):
            return 0 if _segments_cross(a,b,c,d) else min(point_distance(a,c,d),point_distance(b,c,d),point_distance(c,a,b),point_distance(d,a,b))
        page=next(p for p in doc['sheets'] if any(b['equipment_id']=='ATS' for b in p['source_buses']))
        bus=next(b for b in page['source_buses'] if b['equipment_id']=='ATS')
        feeder=next(p for p in page['connection_paths'] if p['connection_id']=='F002::F002-in')
        segments=[*zip(bus['path_mm'],bus['path_mm'][1:]),(bus['row_mm'][:2],bus['row_mm'][2:])]
        assert all(distance(a,b,c,d)>=PAPER_BUS_CLEARANCE_MM-1e-6 for a,b in segments
                   for c,d in zip(feeder['points_mm'],feeder['points_mm'][1:]))


def test_runtime_guard_rejects_bus_crossing_an_unrelated_chain_feeder():
    from oabm.drawings.single_line.render import render_body
    model=compact_transfer(1);topology=project_topology(model);bounds=(5,5,620,460)
    plan=plan_pages(topology,bounds)
    page=deepcopy(plan['pages'][0])
    fan=next(g for g in page['groups'] if g['equipment_id']=='ATS' and g['kind']=='fan')
    chain=next(g for g in page['groups'] if g['equipment_id']=='UTILITY' and g['kind']=='chain')
    # Force the original false join through the F002 chain gap.
    width=(chain['width']-34*(len(chain['connections'])))/(len(chain['connections'])+1)
    x=chain['x']+2*width+34+17
    start=fan['source_bus_path_mm'][0];bus_y=fan['bus_row_mm'][1]
    fan['source_bus_path_mm']=[start,(x,start[1]),(x,bus_y)]
    with pytest.raises(ValueError,match='source bus intersects unrelated feeder'):
        render_body(topology,plan,page,SimpleNamespace(id=page['id'],drawing_bounds_mm=bounds))


def test_runtime_guard_rejects_private_branch_recrossing_its_own_bus():
    from oabm.drawings.single_line.render import render_body
    model=compact_transfer(1);topology=project_topology(model);bounds=(5,5,620,460)
    plan=plan_pages(topology,bounds);page=deepcopy(plan['pages'][0])
    fan=next(g for g in page['groups'] if g['equipment_id']=='ATS' and g['kind']=='fan')
    key=fan['connections'][0];idx=fan['private_branch_index'][key]
    path=fan['feeder_paths_mm'][key];branch=path[idx];bus_y=fan['bus_row_mm'][1]
    x=branch[0]+5
    fan['feeder_paths_mm'][key]=[*path[:idx+1],(branch[0],bus_y+5),(x,bus_y+5),
                                  (x,bus_y-5),path[-1]]
    with pytest.raises(ValueError,match='source bus intersects unrelated feeder'):
        render_body(topology,plan,page,SimpleNamespace(id=page['id'],drawing_bounds_mm=bounds))


@pytest.mark.parametrize('shape,bounds', [
    ('compact-1',(5,5,620,460)), ('compact-3',(5,5,500,350)),
    ('large',(7.055555556,7.055555556,1105.808333333,892.944444444)),
])
def test_emitted_transfer_contacts_remain_clear_of_every_source_bus(shape,bounds):
    from math import hypot
    from oabm.drawings.single_line.layout import _segments_cross, PAPER_BUS_CLEARANCE_MM
    if shape=='large':
        factory=runpy.run_path(str(Path(__file__).parents[1]/'examples/single_line/make_models.py'))
        model=factory['make_variant_model']('multi-transfer',transfer_count=2)
    else:model=compact_transfer(int(shape[-1]))
    original=model.to_json();topology=project_topology(model);plan=plan_pages(topology,bounds)
    sheets=[SimpleNamespace(id=p['id'],drawing_bounds_mm=bounds) for p in plan['pages']]
    bodies,doc=build_single_line_bodies(model,sheets,layout=plan)
    assert model.to_json()==original
    if shape=='large':assert len(bodies)<=3
    def distance(a,b,c,d):
        def point(p,x,y):
            dx,dy=y[0]-x[0],y[1]-x[1];length=dx*dx+dy*dy
            t=max(0,min(1,((p[0]-x[0])*dx+(p[1]-x[1])*dy)/length)) if length else 0
            return hypot(p[0]-x[0]-t*dx,p[1]-x[1]-t*dy)
        return 0 if _segments_cross(a,b,c,d) else min(point(a,c,d),point(b,c,d),point(c,a,b),point(d,a,b))
    def segment(node):return ((float(node.get('x1')),float(node.get('y1'))),(float(node.get('x2')),float(node.get('y2'))))
    def rounded(s):return tuple(tuple(round(v,6) for v in p) for p in s)
    checked=0;repeat_seen=False
    for page in doc['sheets']:
        root=ET.fromstring(bodies[page['sheet_id']]);lines=[n for n in root.iter() if n.tag.endswith('line')]
        glyphs=[g for g in page['electrical_symbols'] if g['kind']=='transfer_input_contact']
        actual_lines={rounded(segment(n)) for n in lines if float(n.get('stroke-width','0'))==pytest.approx(.22)}
        expected=[]
        for symbol in page['equipment_symbols']:
            if next(e for e in topology.equipment if e['id']==symbol['equipment_id'])['equipment_type']!='transfer_switch':continue
            x=symbol['axis_x'];y=symbol['bounds_mm'][1]
            expected.extend((symbol['equipment_id'],role,((x-8,y+offset),(x-3,y+offset)))
                            for role,offset in (('normal',21),('alternate',27)))
        assert sorted((i,r,rounded(s)) for i,r,s in expected)==sorted((g['source_id'],g['role'],rounded(g['segments_mm'][0])) for g in glyphs)
        assert all(rounded(s) in actual_lines for _,_,s in expected)
        contacts=expected
        symbols=[s for s in page['equipment_symbols'] if s['equipment_id']=='ATS']
        repeat_seen |= len(symbols)>1
        bus_lines={rounded(segment(n)) for n in lines if float(n.get('stroke-width','0'))==pytest.approx(.65)}
        for bus in page['source_buses']:
            segments=[*zip(bus['path_mm'],bus['path_mm'][1:]),(bus['row_mm'][:2],bus['row_mm'][2:])]
            assert all(rounded(s) in bus_lines for s in segments)
            for a,b in segments:
                for _,_,(c,d) in contacts:
                    assert distance(a,b,c,d)>=PAPER_BUS_CLEARANCE_MM-1e-6
                    checked+=1
    assert checked
    if shape.startswith('compact'):assert repeat_seen


def test_runtime_guard_rejects_bus_crossing_repeated_transfer_contact_ink():
    from oabm.drawings.single_line.render import render_body
    model=compact_transfer(1);topology=project_topology(model);bounds=(5,5,620,460)
    plan=plan_pages(topology,bounds);page=deepcopy(plan['pages'][0])
    fan=next(g for g in page['groups'] if g['equipment_id']=='ATS' and g['kind']=='fan')
    chain=next(g for g in page['groups'] if g['equipment_id']=='UTILITY' and g['kind']=='chain')
    width=(chain['width']-34*len(chain['connections']))/(len(chain['connections'])+1)
    repeated_left=chain['x']+2*(width+34);axis=repeated_left+width/2
    x=axis-6.5;start=fan['source_bus_path_mm'][0];bus_y=fan['bus_row_mm'][1]
    fan['source_bus_path_mm']=[start,(x,start[1]),(x,bus_y)]
    with pytest.raises(ValueError,match='transfer input contact'):
        render_body(topology,plan,page,SimpleNamespace(id=page['id'],drawing_bounds_mm=bounds))
