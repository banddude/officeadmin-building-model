"""Panel arithmetic consumes supplied circuit facts regardless of route variant."""
from dataclasses import replace
import pytest
from oabm.model import Circuit, ContractError, ElectricalEquipment, Polyline3D, Route, compute_physical_source_sha256, compute_physical_request_sha256
from oabm.reports import generate_panel_schedule_report, PanelScheduleReport, SourceSnapshot
from test_physical_route_schedule import measured_model


def physical_panel_model():
    model = measured_model()
    route = model.routes[0]
    source = replace(model,routes=())
    panel = ElectricalEquipment(id='device:a',equipment_type='panelboard',pose=source.electrical_devices[0].pose)
    circuit = Circuit(id='circuit:physical-panel',source_port_id='port:a',load_port_ids=('port:b',),circuit_number='1',load_va=120.0)
    unknown = replace(circuit,id='circuit:unknown-panel',circuit_number='2',load_va=None)
    source = replace(source,electrical_equipment=(panel,),electrical_devices=(source.electrical_devices[1],),circuits=(circuit,unknown))
    context = replace(route.context,source_model_sha256=compute_physical_source_sha256(source))
    context = replace(context,request_sha256=compute_physical_request_sha256(context,route.requirements))
    return replace(source,routes=(replace(route,context=context),))


def test_physical_and_legacy_routes_have_the_same_panel_facts_but_distinct_source_bindings():
    model = physical_panel_model()
    assert any(segment.kind == "quarter_arc" for segment in model.routes[0].segments)
    frozen = model.to_json()
    physical = generate_panel_schedule_report(model)
    route = model.routes[0]
    ports = {p.id:p for p in model.ports}
    legacy = replace(model,routes=(Route(id=route.id,route_type=route.route_type,start_port_id=route.start_port_id,end_port_id=route.end_port_id,
        centerline=Polyline3D(points=(ports[route.start_port_id].pose.position,ports[route.end_port_id].pose.position))),))
    ordinary = generate_panel_schedule_report(legacy)
    assert physical.panels == ordinary.panels
    assert physical.source_model_fingerprint != ordinary.source_model_fingerprint
    assert physical.panels[0].known_connected_va == 120.0
    assert physical.panels[0].unknown_load_circuit_ids == ('circuit:unknown-panel',)
    assert physical.code_verification_performed is False
    assert PanelScheduleReport.from_json(physical.to_json()).to_json() == physical.to_json()
    physical.validate_against_model(model)
    with pytest.raises(ContractError,match='does not match'):
        physical.validate_against_model(legacy)
    assert model.to_json() == frozen


def test_physical_route_source_snapshot_retains_union_and_rejects_forged_binding():
    model = physical_panel_model()
    route = model.routes[0]
    snapshot = SourceSnapshot.capture('routes',route)
    assert snapshot.entity == model.to_dict()['routes'][0]
    assert SourceSnapshot.from_source_data('routes',snapshot.entity) == snapshot
    with pytest.raises(ContractError,match='content binding'):
        SourceSnapshot(collection='routes',entity=snapshot.entity,content_fingerprint='0'*64)
    broken = dict(snapshot.entity,geometry_kind='invented')
    with pytest.raises(ContractError):SourceSnapshot.from_source_data('routes',broken)


def test_mixed_physical_and_legacy_route_order_never_multiplies_panel_loads():
    model = physical_panel_model()
    route = model.routes[0]
    ports = {p.id:p for p in model.ports}
    legacy = Route(id='route:legacy',route_type='emt',start_port_id=route.start_port_id,end_port_id=route.end_port_id,
        centerline=Polyline3D(points=(ports[route.start_port_id].pose.position,ports[route.end_port_id].pose.position)))
    a = generate_panel_schedule_report(replace(model,routes=(route,legacy)))
    b = generate_panel_schedule_report(replace(model,routes=(legacy,route)))
    assert a.to_json() == b.to_json()
    assert a.panels[0].known_connected_va == 120.0
    assert len(a.panels[0].circuits) == 2
    assert a.panels == generate_panel_schedule_report(model).panels
    assert a.source_model_fingerprint != generate_panel_schedule_report(model).source_model_fingerprint
