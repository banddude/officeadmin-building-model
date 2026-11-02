"""Source-bound site/service projections, never independent drawing positions."""
from __future__ import annotations
import hashlib
import math

from oabm.model import ContractError, validate_model
from oabm.model.site import SiteFeature,ServiceNode,service_node_position,_intersects,_on_segment
from oabm.reports.physical_routes import _snapshot
from oabm.reports.source_files import hash_source_file


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
        if hash_source_file(source_files[identity])!=digest:raise ContractError(f'stale site source bytes for {identity}')
        result[identity]=digest
    return result


def _point(model,owner):
    if isinstance(owner,SiteFeature):
        return owner.geometry.points[0] if owner.geometry.kind=='point' else None
    if isinstance(owner,ServiceNode):return service_node_position(model,owner)
    from oabm.model.assemblies import Point2D
    return Point2D(x=owner.pose.position.x,y=owner.pose.position.y)


def _equipment_footprint(owner):
    if owner.size is None:return None
    from oabm.model.structural import _rotate_vector
    from oabm.model.assemblies import Point2D
    points=set()
    for sx in (-1,1):
        for sy in (-1,1):
            for sz in (-1,1):
                delta=_rotate_vector((sx*owner.size.x/2,sy*owner.size.y/2,sz*owner.size.z/2),owner.pose.rotation)
                points.add((owner.pose.position.x+delta[0],owner.pose.position.y+delta[1]))
    def turn(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    ordered=sorted(points);lower=[];upper=[]
    for point in ordered:
        while len(lower)>=2 and turn(lower[-2],lower[-1],point)<=0:lower.pop()
        lower.append(point)
    for point in reversed(ordered):
        while len(upper)>=2 and turn(upper[-2],upper[-1],point)<=0:upper.pop()
        upper.append(point)
    polygon=tuple(Point2D(x=x,y=y) for x,y in (*lower[:-1],*upper[:-1]))
    from oabm.model.site import SitePlanGeometry
    return SitePlanGeometry(kind='polygon',points=polygon).points


def _boundary(model,owner,*,enclosure=False):
    if isinstance(owner,SiteFeature) and owner.geometry.kind=='polygon':
        if enclosure and not (owner.kind=='gas_location' and owner.extent_basis=='confirmed_envelope'):return None
        return owner.geometry.points
    if isinstance(owner,SiteFeature):return None
    if isinstance(owner,ServiceNode):
        if owner.equipment_id is None:return None
        owner=next(item for item in model.electrical_equipment if item.id==owner.equipment_id)
    return _equipment_footprint(owner)


def _inside(point,polygon):
    inside=False
    for a,b in zip(polygon,(*polygon[1:],polygon[0])):
        if _on_segment(a,b,point):return True
        if (a.y>point.y)!=(b.y>point.y):
            crossing=a.x+(point.y-a.y)*(b.x-a.x)/(b.y-a.y)
            if point.x<crossing:inside=not inside
    return inside


def _nearest(point,polygon):
    if _inside(point,polygon):return (0.,(point.x,point.y))
    choices=[]
    for a,b in zip(polygon,(*polygon[1:],polygon[0])):
        dx,dy=b.x-a.x,b.y-a.y
        t=max(0.,min(1.,((point.x-a.x)*dx+(point.y-a.y)*dy)/(dx*dx+dy*dy)))
        x,y=a.x+t*dx,a.y+t*dy
        choices.append((math.hypot(point.x-x,point.y-y),(x,y)))
    return min(choices)


def _envelope_distance(first,second):
    from oabm.model.assemblies import Point2D
    for point in first:
        if _inside(point,second):return 0.,[[point.x,point.y],[point.x,point.y]]
    for point in second:
        if _inside(point,first):return 0.,[[point.x,point.y],[point.x,point.y]]
    for a,b in zip(first,(*first[1:],first[0])):
        for c,d in zip(second,(*second[1:],second[0])):
            if not _intersects(a,b,c,d):continue
            point=next((p for p in (a,b,c,d) if _on_segment(a,b,p) and _on_segment(c,d,p)),None)
            if point is None:
                dx,dy=b.x-a.x,b.y-a.y;ex,ey=d.x-c.x,d.y-c.y
                t=((c.x-a.x)*ey-(c.y-a.y)*ex)/(dx*ey-dy*ex)
                point=Point2D(x=a.x+t*dx,y=a.y+t*dy)
            return 0.,[[point.x,point.y],[point.x,point.y]]
    choices=[]
    for point in first:
        distance,end=_nearest(point,second);choices.append((distance,[[point.x,point.y],list(end)]))
    for point in second:
        distance,end=_nearest(point,first);choices.append((distance,[list(end),[point.x,point.y]]))
    return min(choices)


def _working_surface(model,requirement,surface,owner):
    footprint=_boundary(model,owner,enclosure=True)
    identity=owner.equipment_id or owner.id if isinstance(owner,ServiceNode) else owner.id
    nodes={node.id:node for node in model.service_nodes}
    facts=next((item for item in model.service_equipment_facts
                if (nodes[item.equipment_id].equipment_id or item.equipment_id if item.equipment_id in nodes else item.equipment_id)==identity),None)
    if footprint is None or facts is None or facts.front_normal_radians is None:
        return {'reason':'Known enclosure footprint and explicit equipment-front normal are required; no anchor or inferred facing substitutes.'}
    front=facts.front_normal_radians
    angle=front-math.pi/2 if requirement.orientation_radians is None else requirement.orientation_radians
    wx,wy=math.cos(angle),math.sin(angle);dx,dy=-wy,wx
    if math.cos(front)*dx+math.sin(front)*dy<1-1e-8:
        return {'reason':'Working-surface orientation does not follow the supplied equipment front.'}
    origin=footprint[0]
    def coordinates(point):
        x,y=point.x-origin.x,point.y-origin.y
        return x*wx+y*wy,x*dx+y*dy
    polygon=surface.geometry.points
    values=[coordinates(point) for point in polygon]
    if len(values)!=4 or any(not ((abs(b[0]-a[0])<=1e-8) != (abs(b[1]-a[1])<=1e-8))
                             for a,b in zip(values,(*values[1:],values[0]))):
        return {'reason':'Working-surface dimensions require an actual rectangle aligned with the equipment-front axes; no bounding-box substitution.'}
    u0,u1=min(p[0] for p in values),max(p[0] for p in values)
    v0,v1=min(p[1] for p in values),max(p[1] for p in values)
    equipment=[coordinates(point) for point in footprint];face=max(p[1] for p in equipment)
    has_face=any(abs(a[1]-face)<=1e-8 and abs(b[1]-face)<=1e-8 and abs(a[0]-b[0])>1e-8
                 for a,b in zip(equipment,(*equipment[1:],equipment[0])))
    if not has_face or abs(v0-face)>1e-8 or u0>min(p[0] for p in equipment)+1e-8 or u1<max(p[0] for p in equipment)-1e-8:
        return {'reason':'Surface does not meet and cover a known front enclosure face; physical access remains unresolved.'}
    def vertex(u,v):
        return min(polygon,key=lambda point:(coordinates(point)[0]-u)**2+(coordinates(point)[1]-v)**2)
    corner,width_end,depth_end=vertex(u0,v0),vertex(u1,v0),vertex(u0,v1)
    return {'status':'coordinate_measurement_only','width_m':u1-u0,'depth_m':v1-v0,
            'width_endpoints':[[corner.x,corner.y],[width_end.x,width_end.y]],
            'depth_endpoints':[[corner.x,corner.y],[depth_end.x,depth_end.y]],
            'orientation_radians':angle,'meets_supplied_horizontal_minima':u1-u0>=requirement.width_m and v1-v0>=requirement.depth_m,
            'reason':'Horizontal proposed geometry only; level construction, access, vertical clearance and regulatory approval are not verified.'}


def _measurements(model):
    owners={item.id:item for item in (*model.site_features,*model.service_nodes,*model.electrical_equipment)}
    result=[]
    for requirement in sorted(model.site_requirements,key=lambda row:row.id):
        source,target=(owners[key] for key in (requirement.source_id,requirement.target_id))
        row={'requirement_id':requirement.id,'source_id':source.id,'target_id':target.id,
             'reference_basis':requirement.reference_basis,'minimum_m':requirement.minimum_m,
             'distance_m':None,'endpoints':None,'status':'unresolved','engineering_approval':False}
        if requirement.kind=='working_surface_for':
            row.update(_working_surface(model,requirement,source,target),
                       supplied_width_m=requirement.width_m,supplied_depth_m=requirement.depth_m)
            polygon=source.geometry.points
            row['surface_edges']=[{'edge_index':index,'length_m':math.hypot(b.x-a.x,b.y-a.y),
                                   'endpoints':[[a.x,a.y],[b.x,b.y]]}
                                  for index,(a,b) in enumerate(zip(polygon,(*polygon[1:],polygon[0])))]
        elif requirement.reference_basis=='point_to_point':
            a,b=_point(model,source),_point(model,target)
            if a is not None and b is not None:
                row.update(distance_m=math.hypot(a.x-b.x,a.y-b.y),endpoints=[[a.x,a.y],[b.x,b.y]],status='coordinate_measurement_only')
            else:row['reason']='An owner has unresolved location.'
        elif requirement.reference_basis=='anchor_to_boundary':
            a,polygon=_point(model,source),_boundary(model,target)
            if a is not None and polygon is not None:
                distance,end=_nearest(a,polygon)
                row.update(distance_m=distance,endpoints=[[a.x,a.y],list(end)],status='coordinate_measurement_only')
            else:row['reason']='Anchor or actual boundary extent is unknown; no generic equipment square is substituted.'
        else:
            a,b=_boundary(model,source,enclosure=True),_boundary(model,target,enclosure=True)
            if a is not None and b is not None:
                distance,ends=_envelope_distance(a,b)
                row.update(distance_m=distance,endpoints=ends,status='coordinate_measurement_only')
            else:row['reason']='Actual enclosure extents are unknown; no bounding-box certification.'
        if row['distance_m'] is not None:
            if not math.isfinite(row['distance_m']):raise ContractError('derived site distance exceeds finite range')
            row['meets_supplied_horizontal_minimum']=row['distance_m']>=requirement.minimum_m
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
    for key in ('scale','padding_mm','legend_width_mm'):
        value=drawing[key]
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
            raise ContractError('model site drawing sizes/scale must be positive finite numbers')
    position=drawing['placement_mm']
    if not isinstance(position,list) or len(position)!=2 or any(type(value) not in (int,float) or not math.isfinite(value) for value in position):
        raise ContractError('model site placement must be two explicit finite paper coordinates')
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
    owner_feature_ids={feature.id:feature.id for feature in model.site_features}
    for node in sorted(model.service_nodes,key=lambda row:row.id):
        position=service_node_position(model,node)
        if position is None:
            omissions.append(dict(id=node.id,reason=node.unresolved_reason));continue
        footprint=_boundary(model,node,enclosure=True)
        add(node.id,'equipment_region' if footprint is not None else 'service_marker',
            footprint or (position,),node.name or node.id,{**evidence(node.id,node.evidence),'confidence':node.confidence},footprint is not None)
        owner_feature_ids[node.id]=node.id
        if node.equipment_id is not None:owner_feature_ids[node.equipment_id]=node.id
    referenced={key for item in model.site_requirements for key in (item.source_id,item.target_id)}
    referenced.update(feature.owner_id for feature in model.site_features if feature.owner_id)
    for owner in sorted(model.electrical_equipment,key=lambda item:item.id):
        if owner.id not in referenced or owner.id in owner_feature_ids:continue
        footprint=_equipment_footprint(owner);position=_point(model,owner)
        add(owner.id,'equipment_region' if footprint is not None else 'service_marker',footprint or (position,),
            owner.name or owner.id,{**evidence(owner.id),'confidence':owner.confidence},footprint is not None)
        owner_feature_ids[owner.id]=owner.id
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
             'owner_feature_ids':owner_feature_ids,
             'measurement_requirements':_measurements(model),'plan_only':True,'engineering_approval':False,
             'limitations':['Equipment markers are anchors, not physical enclosures or clearance proof.',
                            'Private laterals have plan-only length and unknown depth; no 3D takeoff is generated.']}
    return request,receipt


def render_model_site_body(model,sheet,*,source_files,drawing,dimension_offset_mm=8.):
    """Render only canonical projections and derived requirement dimensions."""
    from .site_plan import _render_site_body_fixed
    if type(dimension_offset_mm) not in (int,float) or not math.isfinite(dimension_offset_mm):
        raise ContractError('dimension_offset_mm must be finite paper-space presentation')
    request,receipt=project_model_site(model,source_files=source_files,drawing=drawing)
    if sheet.fields.get('scale')!=f'1:{drawing["scale"]:g}':
        raise ContractError('site titleblock must state the actual canonical projection scale1:N')
    bindings=[];features={row['id']:row for row in request['features']}
    for row in receipt['measurement_requirements']:
        source=receipt['owner_feature_ids'].get(row['source_id']);target=receipt['owner_feature_ids'].get(row['target_id'])
        descriptions=[]
        if row['distance_m'] is not None:
            descriptions.append(('separation',row['distance_m'],row['endpoints'],row['minimum_m']))
        for axis in ('width','depth'):
            if row.get(axis+'_m') is not None:
                descriptions.append((axis,row[axis+'_m'],row[axis+'_endpoints'],row['supplied_'+axis+'_m']))
        if row['status']=='unresolved':
            descriptions.extend((f'surface-edge-{edge["edge_index"]}',edge['length_m'],edge['endpoints'],None)
                                for edge in row.get('surface_edges',()))
        measurement_notes=[]
        for axis,value,endpoints,minimum in descriptions:
            if value<=0 or source is None or (minimum is not None and target is None):continue
            identity='dimension:'+row['requirement_id']+':'+axis
            text=f'{value:.3f} m SURFACE EDGE' if minimum is None else f'{value:.3f} m PLAN; min {minimum:g} m'
            # Keep dense measurements in the shared measured/wrapped legend.
            # Paper-offset rails around tiny adjacent edges cannot establish
            # readable placement merely by sharing a fixed offset. The legend
            # is finite and refuses overflow rather than overlapping/truncating.
            measurement_notes.append(f'{identity}: {text}; owners {row["source_id"]} -> {row["target_id"]}; basis {row["reference_basis"]}.')
            bindings.append({'dimension_id':identity,'requirement_id':row['requirement_id'],'source_id':row['source_id'],
                             'target_id':row['target_id'],'basis':row['reference_basis'],'value_m':value,'minimum_m':minimum,
                             'presentation':'measured_legend','endpoints':endpoints})
        summary=(f'{row["requirement_id"]}: '+
                 (f'{row["distance_m"]:.3f} m {row["reference_basis"]}; supplied min {row["minimum_m"]:g} m; geometric evidence only.'
                  if row['distance_m'] is not None else row.get('reason','Measurement unresolved.')))
        attachment=source or target or next(iter(features),None)
        if attachment is None and measurement_notes:raise ContractError('measurement legend requires a projected canonical owner')
        if attachment is not None:features[attachment]['evidence']['qualification']+=' '+summary+' '+' '.join(measurement_notes)
    body,drawings,composition,audit,source_map=_render_site_body_fixed(request,sheet,dimensions=())
    receipt['dimension_bindings']=bindings
    receipt['unresolved_measurements']=[row['requirement_id'] for row in receipt['measurement_requirements'] if row['status']=='unresolved']
    return body,{'projection':receipt,'drawing':drawings.to_dict(),'audit':audit,'source_map':source_map,'request':request}


def project_service_topology(model):
    """Schematic service arrangement, retaining unresolved identities without ports."""
    from .single_line.topology import ServiceTopology,_project_canonical_electrical_topology
    validate_model(model)
    if not model.service_nodes:raise ContractError('explicit service-only projection requires canonical service nodes')
    facts={item.equipment_id:item for item in model.service_equipment_facts}
    equipment={item.id:item for item in model.electrical_equipment}
    incoming={node.id:[] for node in model.service_nodes};outgoing={node.id:[] for node in model.service_nodes}
    connections=[];diagnostics=[]
    canonical=None
    if any((model.electrical_equipment,model.electrical_devices,model.ports,model.circuits,model.conductors,model.routes)):
        # Exactly the same established admission as the default SLD. Explicit
        # service selection is not permission to weaken source-circuit checks.
        canonical=_project_canonical_electrical_topology(model)
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
            from .single_line.drafting_facts import feeder_labels
            candidates=[row for row in canonical.connections if row['circuit_id']==edge.circuit_id
                        and row['source_id']==service_nodes[edge.source_node_id].equipment_id
                        and row['load_id']==service_nodes[edge.target_node_id].equipment_id]
            if len(candidates)!=1:raise ContractError('service relationship must reference exactly one admitted canonical connection')
            original=candidates[0]
            connection={**original,'id':edge.id,'source_id':edge.source_node_id,'load_id':edge.target_node_id,
                        'source_ids':[edge.id,edge.source_node_id,edge.target_node_id,*original['source_ids']]}
            connections[-1]=connection
            connection['drawing_labels']=feeder_labels(connection,f'S{ordinal:03d}')
    rows=[]
    canonical_equipment={row['id']:row for row in canonical.equipment} if canonical else {}
    for node in sorted(model.service_nodes,key=lambda row:row.id):
        owner=equipment.get(node.equipment_id)
        established=canonical_equipment.get(node.equipment_id)
        value=facts.get(node.id) or facts.get(node.equipment_id)
        notes=[*(established['supplied_notes'] if established else []),*([node.unresolved_reason] if node.unresolved_reason else [])]
        if value:
            if value.integrated_service_disconnect is True:notes.append('One integrated service disconnect in this meter-main; no separate disconnect.')
            if value.main_lug_only is True:notes.append('MAIN LUG ONLY')
            if value.continuous_meter_rating_a is not None:notes.append(f'{value.continuous_meter_rating_a:g} A continuous metering')
            if value.circuit_positions is not None:notes.append(f'{value.circuit_positions} circuit positions')
        if node.location_basis!='placed_equipment':diagnostics.append(f'{node.id}: physical terminal geometry unresolved; {node.location_basis}')
        drafting=dict(established['drafting']) if established else dict(status='unknown',bus_current_a=None,neutral_ground_bond='unknown',notes=[])
        if value and value.equipment_rating_a is not None:drafting['bus_current_a']=value.equipment_rating_a
        rows.append(dict(id=node.id,name=node.name or (owner.name if owner else None) or node.id,
            equipment_type=node.equipment_type,rated_voltage_v=owner.rated_voltage_v if owner else None,
            supplied_notes=notes,drafting=drafting,incoming=incoming[node.id],outgoing=outgoing[node.id]))
    source=[]
    document=model.to_dict()
    selected={key for edge in connections for key in edge['source_ids']}
    selected.update(node.equipment_id for node in model.service_nodes if node.equipment_id is not None)
    selected_fact_owners=set(service_nodes)|{node.equipment_id for node in model.service_nodes if node.equipment_id is not None}
    for collection in ('service_nodes','service_connections','service_equipment_facts','electrical_equipment','ports','circuits','conductors','routes'):
        source.extend({'collection':collection,'entity':row} for row in document[collection]
                      if collection in {'service_nodes','service_connections'} or
                      (collection=='service_equipment_facts' and row['equipment_id'] in selected_fact_owners) or row['id'] in selected)
    inventory={(collection,row['id']) for collection,items in document.items() if isinstance(items,list)
               for row in items if isinstance(row,dict) and isinstance(row.get('id'),str)}
    consumed={(row['collection'],row['entity']['id']) for row in source}
    projected={('service_nodes',row.id) for row in model.service_nodes}|{('service_connections',row.id) for row in model.service_connections}
    omitted=sorted(inventory-consumed)
    coverage={'selection_mode':'service_only_explicit','is_whole_electrical_graph':False,
              'source_model_sha256':hashlib.sha256(model.to_json(indent=None).encode()).hexdigest(),
              'model_entity_count':len(inventory),
              'projected':[{'collection':collection,'id':identity} for collection,identity in sorted(projected)],
              'supporting':[{'collection':collection,'id':identity} for collection,identity in sorted(consumed-projected)],
              'omissions':[{'collection':collection,'id':identity,'reason':'Outside explicitly selected service-only projection; not claimed as rendered.'} for collection,identity in omitted]}
    diagnostics.append(f'EXPLICIT SERVICE-ONLY SCOPE: {len(omitted)} canonical model owners omitted; complete identities retained in topology coverage report.')
    for collection,identity in omitted:
        if collection in {'electrical_equipment','electrical_devices','circuits'}:
            diagnostics.append(f'SERVICE-ONLY OMISSION: {collection} {identity}')
    return ServiceTopology(coverage['source_model_sha256'],tuple(rows),tuple(connections),tuple(sorted(diagnostics)),tuple(source),coverage)
