"""Source-bound site/service projections, never independent drawing positions."""
from __future__ import annotations
import hashlib
import math
import stat
from pathlib import Path

from oabm.model import ContractError, validate_model
from oabm.model.site import SiteFeature,ServiceNode,service_node_position
from oabm.reports.physical_routes import _snapshot


def _source_bindings(model,source_files):
    expected={}
    for feature in (*model.site_features,*model.service_nodes):
        for evidence in feature.evidence:
            if evidence.source_id in expected and expected[evidence.source_id]!=evidence.sha256:
                raise ContractError('conflicting site source digests for one source identity')
            expected[evidence.source_id]=evidence.sha256
    result={}
    if not isinstance(source_files,dict):raise ContractError('explicit site source-file mapping required')
    for identity,digest in sorted(expected.items()):
        if identity not in source_files:raise ContractError(f'missing site source bytes for {identity}')
        info=Path(source_files[identity]).stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size>256*1024*1024:
            raise ContractError('site source must be a regular file no larger than256MiB')
        actual=hashlib.sha256()
        with Path(source_files[identity]).open('rb') as handle:
            for block in iter(lambda:handle.read(1024*1024),b''):actual.update(block)
        if actual.hexdigest()!=digest:raise ContractError(f'stale site source bytes for {identity}')
        result[identity]=digest
    return result


def _point(model,owner):
    if isinstance(owner,SiteFeature):
        return owner.geometry.points[0] if owner.geometry.kind=='point' else None
    if isinstance(owner,ServiceNode):return service_node_position(model,owner)
    from oabm.model.assemblies import Point2D
    return Point2D(x=owner.pose.position.x,y=owner.pose.position.y)


def _boundary(owner,*,enclosure=False):
    if isinstance(owner,SiteFeature) and owner.geometry.kind=='polygon':
        if enclosure and not (owner.kind=='gas_location' and owner.extent_basis=='confirmed_envelope'):return None
        return owner.geometry.points
    # Placed equipment envelopes require a dedicated projected physical-footprint
    # adapter. A generic square or center anchor must not stand in for its extent.
    return None


def _nearest(point,polygon):
    choices=[]
    for a,b in zip(polygon,(*polygon[1:],polygon[0])):
        dx,dy=b.x-a.x,b.y-a.y
        t=max(0.,min(1.,((point.x-a.x)*dx+(point.y-a.y)*dy)/(dx*dx+dy*dy)))
        x,y=a.x+t*dx,a.y+t*dy
        choices.append((math.hypot(point.x-x,point.y-y),(x,y)))
    return min(choices)


def _measurements(model):
    owners={item.id:item for item in (*model.site_features,*model.service_nodes,*model.electrical_equipment)}
    result=[]
    for requirement in sorted(model.site_requirements,key=lambda row:row.id):
        source,target=(owners[key] for key in (requirement.source_id,requirement.target_id))
        row={'requirement_id':requirement.id,'source_id':source.id,'target_id':target.id,
             'reference_basis':requirement.reference_basis,'minimum_m':requirement.minimum_m,
             'distance_m':None,'endpoints':None,'status':'unresolved','engineering_approval':False}
        if requirement.kind=='working_surface_for':
            row.update(reason='Working-surface width/depth require explicit equipment-front orientation and known enclosure interface; a plan polygon is not verified access clearance.',
                       supplied_width_m=requirement.width_m,supplied_depth_m=requirement.depth_m,
                       orientation_radians=requirement.orientation_radians)
        elif requirement.reference_basis=='point_to_point':
            a,b=_point(model,source),_point(model,target)
            if a is not None and b is not None:
                row.update(distance_m=math.hypot(a.x-b.x,a.y-b.y),endpoints=[[a.x,a.y],[b.x,b.y]],status='coordinate_measurement_only')
            else:row['reason']='An owner has unresolved location.'
        elif requirement.reference_basis=='anchor_to_boundary':
            a,polygon=_point(model,source),_boundary(target)
            if a is not None and polygon is not None:
                distance,end=_nearest(a,polygon)
                row.update(distance_m=distance,endpoints=[[a.x,a.y],list(end)],status='coordinate_measurement_only')
            else:row['reason']='Anchor or actual boundary extent is unknown; no generic equipment square is substituted.'
        else:
            # Concave/polygon overlap and placed-equipment projection must be
            # explicitly implemented before an envelope clearance can be shown.
            row['reason']='Envelope-to-envelope measurement is unresolved; no bounding-box certification.'
        result.append(row)
    return result


def project_model_site(model,*,source_files,drawing):
    """Return a derived site request and complete ownership/omission receipt.

    Drawing settings control scale/layout only. North derives from the canonical
    frame. Source files are read/hash-checked before geometry is projected.
    """
    model,raw=_snapshot(model)
    bindings=_source_bindings(model,source_files)
    allowed={'scale','placement_mm','padding_mm','legend_width_mm'}
    if not isinstance(drawing,dict) or set(drawing)!=allowed:
        raise ContractError('model site accepts only explicit scale/layout presentation settings')
    north=model.coordinate_system.true_north_radians
    if north is None:raise ContractError('model site requires supplied canonical true north; none is inferred')
    features=[];coverage=[];omissions=[]
    def add(identity,kind,points,label,evidence,closed=False,route=False):
        row=dict(id=identity,kind=kind,frame_id=model.coordinate_system.frame_id,
                 points=[[point.x,point.y] for point in points],closed=closed,label=label,evidence=evidence)
        if route:row['route_status']='supplied'
        features.append(row);coverage.append(identity)
    def evidence(identity,records=()):
        references='; '.join(f'{item.source_id} page {item.page} {item.sheet_id} SHA256 {item.sha256}' for item in records)
        return dict(source_id=identity,reference=references or 'Canonical owner; normalized model binding retained in projection receipt',
                    qualification='Model-derived plan-only XY; unknown elevation and enclosure clearance are not inferred.',confidence=1.)
    kinds={'property_boundary':'property_boundary','building_outline':'building','street_reference':'street_reference',
           'gas_location':'gas_marker','utility_intercept':'utility_intercept','working_surface':'working_surface'}
    for feature in sorted(model.site_features,key=lambda row:row.id):
        kind=kinds[feature.kind]
        if feature.kind=='gas_location' and feature.geometry.kind=='polygon':kind='gas_region'
        add(feature.id,kind,feature.geometry.points,feature.name or feature.id,
            {**evidence(feature.id,feature.evidence),'confidence':feature.confidence},feature.geometry.kind=='polygon')
    for node in sorted(model.service_nodes,key=lambda row:row.id):
        position=service_node_position(model,node)
        if position is None:
            omissions.append(dict(id=node.id,reason=node.unresolved_reason));continue
        add(node.id,'service_marker',(position,),node.name or node.id,{**evidence(node.id,node.evidence),'confidence':node.confidence})
    for lateral in sorted(model.plan_service_laterals,key=lambda row:row.id):
        add(lateral.id,'route',lateral.geometry.points,(lateral.name or lateral.id)+'; '+lateral.raceway_description+'; PLAN ONLY, DEPTH UNKNOWN',
            {**evidence(lateral.id),'confidence':lateral.confidence},route=True)
    request={'version':'supplied-site-plan-v1','id':'site-model-'+hashlib.sha256(model.model_id.encode()).hexdigest()[:24],'units':'m',
             'frame':{'id':model.coordinate_system.frame_id,'axes':'local-xy,+x-right,+y-up'},
             'features':features,'drawing':{**drawing,'north_degrees':math.degrees(north)%360}}
    receipt={'kind':'canonical_site_projection','version':'1.0.0','model_sha256':hashlib.sha256(raw.encode()).hexdigest(),
             'source_sha256':bindings,'geometry_owner_ids':coverage,'omissions':omissions,
             'scope':'complete canonical site/service inventory; not a whole-building geometry export',
             'non_geometric_owner_ids':sorted(item.id for item in (*model.site_requirements,*model.service_connections,*model.service_equipment_facts)),
             'placed_equipment_joins':{node.id:node.equipment_id for node in model.service_nodes if node.equipment_id is not None},
             'measurement_requirements':_measurements(model),'plan_only':True,'engineering_approval':False,
             'limitations':['Equipment markers are anchors, not physical enclosures or clearance proof.',
                            'Private laterals have plan-only length and unknown depth; no 3D takeoff is generated.']}
    return request,receipt


def project_service_topology(model):
    """Schematic service arrangement, retaining unresolved identities without ports."""
    from .single_line.topology import Topology
    validate_model(model)
    facts={item.equipment_id:item for item in model.service_equipment_facts}
    equipment={item.id:item for item in model.electrical_equipment}
    incoming={node.id:[] for node in model.service_nodes};outgoing={node.id:[] for node in model.service_nodes}
    connections=[];diagnostics=[]
    circuits={item.id:item for item in model.circuits};ports={item.id:item for item in model.ports}
    service_nodes={node.id:node for node in model.service_nodes}
    for ordinal,edge in enumerate(sorted(model.service_connections,key=lambda row:row.id),1):
        incoming[edge.target_node_id].append(edge.id);outgoing[edge.source_node_id].append(edge.id)
        connections.append(dict(id=edge.id,circuit_id=edge.circuit_id,source_port_id=None,load_port_id=None,
            source_id=edge.source_node_id,load_id=edge.target_node_id,source_role='supplied_service_relationship',
            load_role='supplied_service_relationship',drafting={'status':'unknown','connection_kind':'power','protection':None,'notes':[]},
            circuit_number=None,voltage_v=None,poles=None,phase=None,conductor_specification='Feeder specification unresolved; no spatial terminal or route inferred',
            wire_labels=[],source_ids=[edge.id,edge.source_node_id,edge.target_node_id,*([edge.circuit_id] if edge.circuit_id else [])],route_ids=[],conductor_ids=[],
            drawing_labels=[f'S{ordinal:03d} / supplied service relationship','Feeder specification unresolved']))
        if edge.circuit_id is not None:
            from .single_line.drafting_facts import circuit_facts,feeder_labels
            circuit=circuits[edge.circuit_id]
            source=ports[circuit.source_port_id]
            target=next(ports[key] for key in circuit.load_port_ids if ports[key].owner_id==service_nodes[edge.target_node_id].equipment_id)
            wires=sorted((wire for wire in model.conductors if wire.circuit_id==circuit.id),key=lambda wire:wire.id)
            connection=connections[-1]
            connection.update(source_port_id=source.id,load_port_id=target.id,source_role=source.role,load_role=target.role,
                drafting=circuit_facts(circuit),circuit_number=circuit.circuit_number,voltage_v=circuit.voltage_v,
                poles=circuit.poles,phase=circuit.phase,route_ids=list(circuit.route_ids),conductor_ids=[wire.id for wire in wires],
                wire_labels=[f'{wire.count}x {wire.size or "size UNKNOWN"} {wire.material or "material UNKNOWN"} {wire.role}' for wire in wires],
                conductor_specification=' | '.join(f'{wire.count}x {wire.role}; {wire.size or "size UNKNOWN"}; {wire.material or "material UNKNOWN"}' for wire in wires) or 'Conductor specification UNKNOWN')
            connection['source_ids'].extend([source.id,target.id,*[wire.id for wire in wires],*circuit.route_ids])
            connection['drawing_labels']=feeder_labels(connection,f'S{ordinal:03d}')
    rows=[]
    for node in sorted(model.service_nodes,key=lambda row:row.id):
        owner=equipment.get(node.equipment_id)
        value=facts.get(node.id) or facts.get(node.equipment_id)
        notes=[node.unresolved_reason] if node.unresolved_reason else []
        if value:
            if value.integrated_service_disconnect is True:notes.append('One integrated service disconnect in this meter-main; no separate disconnect.')
            if value.main_lug_only is True:notes.append('MAIN LUG ONLY')
            if value.continuous_meter_rating_a is not None:notes.append(f'{value.continuous_meter_rating_a:g} A continuous metering')
            if value.circuit_positions is not None:notes.append(f'{value.circuit_positions} circuit positions')
        if node.location_basis!='placed_equipment':diagnostics.append(f'{node.id}: physical terminal geometry unresolved; {node.location_basis}')
        rows.append(dict(id=node.id,name=node.name or (owner.name if owner else None) or node.id,
            equipment_type=node.equipment_type,rated_voltage_v=owner.rated_voltage_v if owner else None,
            supplied_notes=notes,drafting=dict(status='unknown',bus_current_a=value.equipment_rating_a if value else None,
                neutral_ground_bond='unknown',notes=[]),incoming=incoming[node.id],outgoing=outgoing[node.id]))
    source=[]
    document=model.to_dict()
    selected={key for edge in connections for key in edge['source_ids']}
    selected.update(node.equipment_id for node in model.service_nodes if node.equipment_id is not None)
    for collection in ('service_nodes','service_connections','service_equipment_facts','electrical_equipment','ports','circuits','conductors','routes'):
        source.extend({'collection':collection,'entity':row} for row in document[collection]
                      if collection.startswith('service_') or row['id'] in selected)
    return Topology(hashlib.sha256(model.to_json(indent=None).encode()).hexdigest(),tuple(rows),tuple(connections),tuple(sorted(diagnostics)),tuple(source))
