"""Native analytic sweeps, EXPRESS validity and source-bound curve readback."""
from dataclasses import replace
import math
import pytest
import ifcopenshell
import ifcopenshell.validate
from oabm.ifc import to_ifc, from_ifc, round_trip, canonical_id_to_ifc_guid, IfcAdapterError
from oabm.ifc.comparison import round_trip_differences
from oabm.model import compute_physical_source_sha256, compute_physical_request_sha256
from test_physical_drawing_sheets import drawing_inputs
from test_physical_route_schedule import measured_model


def model_fixture(two=False,odd=False):
    model=measured_model(two=two,odd=odd)
    source=replace(model,routes=(),name='Synthetic native curved IFC')
    route=model.routes[0]
    context=replace(route.context,source_model_sha256=compute_physical_source_sha256(source))
    context=replace(context,request_sha256=compute_physical_request_sha256(context,route.requirements))
    return replace(source,routes=(replace(route,context=context),))


@pytest.mark.parametrize('two,odd',[(False,False),(True,False),(False,True)])
def test_actual_native_curves_solids_express_and_roundtrip(two,odd):
    model=model_fixture(two,odd);before=model.to_json()
    native=to_ifc(model,physical_routes=True)
    route=model.routes[0]
    solids=[*native.by_type('IfcExtrudedAreaSolid'),*native.by_type('IfcRevolvedAreaSolid')]
    assert len(solids)==len(route.segments)
    assert all(solid.SweptArea.Radius==.01 for solid in solids)
    circles=native.by_type('IfcCircle')
    assert len(circles)==(2 if two else 1)
    assert all(circle.Radius==route.requirements.centerline_radius_m for circle in circles)
    assert all(curve.Trim2[0].wrappedValue==math.pi/2 for curve in native.by_type('IfcTrimmedCurve'))
    logger=ifcopenshell.validate.json_logger()
    ifcopenshell.validate.validate(native,logger,express_rules=True)
    assert not logger.statements
    # Reparse real STEP bytes, not only the in-memory entity graph.
    restored=from_ifc(ifcopenshell.file.from_string(native.to_string()))
    assert restored.to_dict()==model.to_dict()
    assert round_trip_differences(model,restored)==()
    assert round_trip(model,physical_routes=True).to_dict()==model.to_dict()
    assert model.to_json()==before
    assert to_ifc(model,physical_routes=True).to_string()==native.to_string()


@pytest.mark.parametrize('tamper',['radius','arc_radius','trim','centre','normal','line','placement','extra_body','type','deleted'])
def test_native_curve_tampering_is_refused_not_resurrected(tamper):
    model=model_fixture();native=to_ifc(model,physical_routes=True)
    route=model.routes[0]
    product=native.by_guid(canonical_id_to_ifc_guid(f'{route.id}#segment:1'))
    if tamper=='radius':native.by_type('IfcExtrudedAreaSolid')[0].SweptArea.Radius=.02
    elif tamper=='arc_radius':native.by_type('IfcCircle')[0].Radius=.5
    elif tamper=='trim':native.by_type('IfcTrimmedCurve')[0].Trim2=(native.create_entity('IfcParameterValue',math.pi),)
    elif tamper=='centre':native.by_type('IfcCircle')[0].Position.Location.Coordinates=(20.,20.,20.)
    elif tamper=='normal':native.by_type('IfcCircle')[0].Position.Axis.DirectionRatios=(1.,0.,0.)
    elif tamper=='line':native.by_type('IfcPolyline')[0].Points[0].Coordinates=(5.,5.,5.)
    elif tamper=='placement':product.ObjectPlacement.RelativePlacement.Location.Coordinates=(1.,0.,0.)
    elif tamper=='extra_body':
        body=next(rep for rep in product.Representation.Representations if rep.RepresentationIdentifier=='Body')
        body.Items=(*body.Items,body.Items[0])
    elif tamper=='type':product.PredefinedType='CABLETRAYSEGMENT'
    else:native.remove(product)
    with pytest.raises(IfcAdapterError,match='physical route readback refused'):
        from_ifc(native)


def test_physical_comparison_preserves_exact_nested_geometry():
    model=model_fixture()
    assert round_trip_differences(model,model)==()
    # Different, independently valid odd-nanometre supplied radius is real change.
    assert any('routes' in path for path in round_trip_differences(model,model_fixture(odd=True)))


def test_ifc_physical_route_json_admission_is_bounded_and_exact():
    import json
    from oabm.ifc.physical_routes import decode_route_payload
    raw=model_fixture().to_dict()['routes'][0]
    assert decode_route_payload(json.dumps(raw))==raw
    with pytest.raises(IfcAdapterError,match='duplicate'):
        decode_route_payload('{"id":"a","id":"b"}')
    with pytest.raises(IfcAdapterError,match='depth'):
        decode_route_payload('['*200+'0'+']'*200)
    text=json.dumps(raw).replace('0.02','0.0200000000000000000001',1)
    with pytest.raises(IfcAdapterError,match='nanometre'):
        decode_route_payload(text)


@pytest.mark.parametrize('edit',['name','pose'])
def test_native_source_edits_without_endpoint_change_refuse_stale_binding(edit):
    from oabm.ifc.adapter import _set_pose
    from oabm.model import Point3,Pose
    model=model_fixture();native=to_ifc(model,physical_routes=True)
    device=native.by_guid(canonical_id_to_ifc_guid(model.electrical_devices[0].id))
    if edit=='name':device.Name='Changed source device'
    else:
        _set_pose(native,device,Pose(position=Point3(x=.1,y=.1,z=0)))
        # Keep the endpoint port at its original absolute position: this is
        # specifically a source-context edit, not a joint/port mismatch.
        port=native.by_guid(canonical_id_to_ifc_guid(model.ports[0].id))
        _set_pose(native,port,model.ports[0].pose)
    with pytest.raises(IfcAdapterError,match='physical IFC source binding refused'):
        from_ifc(native)


def test_untagged_duplicate_native_conduit_cannot_disappear_on_readback():
    model=model_fixture();native=to_ifc(model,physical_routes=True)
    product=native.by_type('IfcCableCarrierSegment')[0]
    native.create_entity(product.is_a(),GlobalId=ifcopenshell.guid.new(),
        Representation=product.Representation,ObjectPlacement=product.ObjectPlacement)
    with pytest.raises(IfcAdapterError,match='untagged or unowned'):
        from_ifc(native)


@pytest.mark.parametrize('two,odd',[(False,False),(True,False),(False,True)])
def test_actual_native_closed_solid_volume_and_arc_bounds(two,odd):
    import ifcopenshell.geom
    import numpy as np
    from oabm.model import physical_segment_facts
    model=model_fixture(two,odd);native=to_ifc(model,physical_routes=True);route=model.routes[0]
    settings=ifcopenshell.geom.settings();settings.set('iterator-output',ifcopenshell.ifcopenshell_wrapper.NATIVE)
    mesh_settings=ifcopenshell.geom.settings();mesh_settings.set(mesh_settings.USE_WORLD_COORDS,True)
    for index,(segment,fact) in enumerate(zip(route.segments,physical_segment_facts(route))):
        product=native.by_guid(canonical_id_to_ifc_guid(f'{route.id}#segment:{index}'))
        shape=ifcopenshell.geom.create_shape(settings,product)
        length=fact['length_ticks']/1e9 if segment.kind=='line' else fact['radius_ticks']/1e9*math.pi/2
        assert shape.volume==pytest.approx(math.pi*.01**2*length,rel=1e-8,abs=1e-12)
        if segment.kind=='quarter_arc':
            mesh=ifcopenshell.geom.create_shape(mesh_settings,product)
            vertices=np.asarray(mesh.geometry.verts).reshape(-1,3)
            endpoints=np.asarray([[getattr(route.joints[i].position,key) for key in 'xyz'] for i in (index,index+1)])
            assert np.all(vertices.min(axis=0)>=endpoints.min(axis=0)-.01000001)
            assert np.all(vertices.max(axis=0)<=endpoints.max(axis=0)+.01000001)


def test_native_angular_unit_tampering_refuses_wrong_trim_interpretation():
    model=model_fixture();native=to_ifc(model,physical_routes=True)
    units=native.by_type('IfcUnitAssignment')[0]
    units.Units=tuple(unit for unit in units.Units if getattr(unit,'UnitType',None)!='PLANEANGLEUNIT')
    with pytest.raises(IfcAdapterError,match='radian angular unit'):
        from_ifc(native)
