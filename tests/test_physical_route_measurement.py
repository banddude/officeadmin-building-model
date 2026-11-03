"""Synthetic geometric evidence; not an electrical or clearance approval."""
from dataclasses import replace
import math
import pytest
from oabm.model import Circuit
from oabm.electrical.physical_route_measurement import measure_single_physical_route
from physical_route_fixture import make_physical_model
from test_physical_route_contract import make_arc_model

def with_circuit(model):
    route=model.routes[0]
    circuit=Circuit(id='circuit:synthetic',source_port_id=route.start_port_id,load_port_ids=(route.end_port_id,),route_ids=(route.id,))
    return replace(model,circuits=(circuit,))

def test_straight_is_bound_and_never_applied():
    model=with_circuit(make_physical_model()); before=model.to_json()
    result=measure_single_physical_route(model,'circuit:synthetic')
    assert result['one_way_centerline_feet_upper'] >= 2/.3048
    assert result['model_hash_scope']=='canonical_json_snapshot_utf8'
    assert result['source_context_status']==result['clearance_status']=='unverified' and result['applied'] is False
    assert result['geometry'][0]['length_ticks']==2_000_000_000
    assert model.to_json()==before

def test_arc_uses_arc_not_chord_and_rounds_up():
    model=with_circuit(make_arc_model()); route=model.routes[0]
    result=measure_single_physical_route(model,'circuit:synthetic')
    ticks=result['straight_ticks'];radius=result['quarter_arc_radius_ticks_sum']
    exact=(ticks + radius*math.pi/2)/304_800_000
    assert radius>0 and result['one_way_centerline_feet_upper']>=exact
    assert result['one_way_centerline_feet_upper']-exact<.000002
    assert len(result['geometry'])==len(route.segments) and all('provenance' in x for x in result['geometry'])

def test_wrong_circuit_or_route_is_rejected():
    m=with_circuit(make_physical_model())
    with pytest.raises(ValueError): measure_single_physical_route(m,'circuit:missing')
    with pytest.raises(TypeError): measure_single_physical_route({},'circuit:synthetic')

def test_nonportable_provenance_integer_is_refused():
    model=with_circuit(make_physical_model())
    model.routes[0].provenance[0].attributes['unsafe_integer']=2**53
    with pytest.raises(ValueError,match='interoperable JSON range'):
        measure_single_physical_route(model,'circuit:synthetic')

def test_requirement_and_joint_evidence_are_preserved():
    model=with_circuit(make_arc_model()); route=model.routes[0]
    output=measure_single_physical_route(model,'circuit:synthetic')
    assert output['requirements_id']==route.requirements.id
    assert output['centerline_radius_m']==route.requirements.centerline_radius_m
    assert output['declared_source_model_sha256']==route.context.source_model_sha256
    assert output['joints'][0]['position']['x']==route.joints[0].position.x
    assert output['geometry'][1]['turn']==route.segments[1].turn

def test_cyclic_or_oversized_metadata_is_refused_before_serialization():
    model=with_circuit(make_physical_model())
    attrs=model.routes[0].provenance[0].attributes
    attrs['cycle']=attrs
    with pytest.raises(ValueError,match='invalid or oversized'):
        measure_single_physical_route(model,'circuit:synthetic')
    del attrs['cycle']; attrs['large']='x'*3_000_000
    with pytest.raises(ValueError,match='invalid or oversized'):
        measure_single_physical_route(model,'circuit:synthetic')

def test_all_cardinal_rotations_preserve_conservative_arc_measurement():
    from test_physical_route_contract import rotations
    measured=[measure_single_physical_route(with_circuit(make_arc_model(transform)),'circuit:synthetic')['one_way_centerline_feet_upper'] for transform in rotations()]
    assert len(measured)==24 and len(set(measured))==1

def test_reversed_circuit_endpoints_are_not_accepted():
    model=with_circuit(make_physical_model()); c=model.circuits[0]
    reversed_c=replace(c,source_port_id=c.load_port_ids[0],load_port_ids=(c.source_port_id,))
    with pytest.raises(ValueError,match='explicitly connect'):
        measure_single_physical_route(replace(model,circuits=(reversed_c,)),'circuit:synthetic')

def test_repeated_large_integers_are_bounded_before_json_serialization():
    model=with_circuit(make_physical_model())
    model.routes[0].provenance[0].attributes['large_aliases']=[2**4095]*30_000
    with pytest.raises(ValueError,match='invalid or oversized'):
        measure_single_physical_route(model,'circuit:synthetic')

def test_capture_is_detached_from_later_mutation(monkeypatch):
    import oabm.electrical.physical_route_measurement as module
    model=with_circuit(make_physical_model()); attrs=model.routes[0].provenance[0].attributes
    original=module.capture
    def capture_then_mutate(*args,**kwargs):
        captured=original(*args,**kwargs)
        attrs['late_cycle']=attrs
        return captured
    monkeypatch.setattr(module,'capture',capture_then_mutate)
    result=module.measure_single_physical_route(model,'circuit:synthetic')
    assert result['source_context_status']=='unverified'
    assert 'late_cycle' not in result['route_provenance'][0]['attributes']

def test_rounding_and_integer_geometry_profile_is_explicit():
    model=with_circuit(make_arc_model());result=measure_single_physical_route(model,'circuit:synthetic')
    assert result['tick_unit']=='nanometre'
    assert result['rounding_profile']['version']=='pi-upper-microfoot-nextafter-v1'
    assert result['rounding_profile']['ticks_per_foot']==304_800_000
    assert all(len(j['position_ticks'])==3 and all(type(v) is int for v in j['position_ticks']) for j in result['joints'])

def test_cli_writes_geometric_measurement_and_fails_closed(tmp_path,capsys):
    import json
    from oabm.electrical.physical_route_measurement import main
    path=tmp_path/'model.json';path.write_text(with_circuit(make_physical_model()).to_json())
    assert main(['--model',str(path),'--circuit','circuit:synthetic'])==0
    data=json.loads(capsys.readouterr().out)
    assert data['clearance_status']=='unverified' and data['applied'] is False
    assert main(['--model',str(path),'--circuit','circuit:missing'])==2
    assert 'measurement failed' in capsys.readouterr().err

def test_cli_rejects_fifo_without_blocking(tmp_path,capsys):
    import os
    if not hasattr(os,'mkfifo'):
        pytest.skip('FIFO unavailable')
    from oabm.electrical.physical_route_measurement import main
    path=tmp_path/'fifo';os.mkfifo(path)
    assert main(['--model',str(path),'--circuit','circuit:synthetic'])==2
    assert 'measurement failed' in capsys.readouterr().err

def test_cli_rejects_malformed_duplicate_offgrid_and_oversized_without_echo(tmp_path,capsys):
    import json
    from oabm.electrical.physical_route_measurement import main
    marker='private-marker-never-echo'
    model=with_circuit(make_physical_model())
    valid=model.to_json(indent=None)
    assert '"model_id":' in valid
    duplicate=valid.replace('"model_id":',f'"model_id":"{marker}","model_id":',1)
    data=model.to_dict(); data['routes'][0]['joints'][0]['position']['x']=0.0000000001
    for name,content in [('bad','{'+marker),('duplicate',duplicate),('offgrid',json.dumps(data))]:
        path=tmp_path/name;path.write_text(content)
        assert main(['--model',str(path),'--circuit','circuit:synthetic'])==2
        out=capsys.readouterr();assert out.out=='' and marker not in out.err
    path=tmp_path/'large'
    with path.open('wb') as f:f.truncate(32*1024*1024+1)
    assert main(['--model',str(path),'--circuit','circuit:synthetic'])==2
    out=capsys.readouterr();assert out.out=='' and 'measurement failed' in out.err
