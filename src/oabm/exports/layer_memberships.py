"""Source-bound named memberships shared by IFC, DXF and review sidecars."""
from __future__ import annotations

import json
from oabm.model import BuildingModel, LayerSelectionConfig, resolve_preset
from oabm.model.layers import _universe, expand_layer_members
from .layers import LayerGlbDelivery, build_layer_glb


def verify_layer_delivery(model, delivery):
    """Replay the existing bounded source/config/request boundary exactly."""
    if type(delivery) is not LayerGlbDelivery:
        raise ValueError('named layers require a source-bound LayerGlbDelivery')
    from oabm.routing.physical_capture import bounded_source_json
    config=LayerSelectionConfig.from_json(bounded_source_json(delivery.config_json))
    manifest=json.loads(bounded_source_json(delivery.manifest_json))
    request=manifest.get('request')
    if not isinstance(request,dict) or set(request)!={'preset','unassigned','isolate','level_ids','physical_routes','include_obstacles'}:
        raise ValueError('invalid saved layer request')
    replay=build_layer_glb(model,config,**request)
    if replay!=delivery:
        raise ValueError('layer delivery is stale or mismatched with model/configuration/request/GLB')
    snapshot=BuildingModel.from_json(replay.source_model_json)
    selection=resolve_preset(config,snapshot,preset=request['preset'],unassigned=request['unassigned'],
        isolate=request['isolate'],level_ids=request['level_ids'],include_obstacles=request['include_obstacles'])
    return snapshot,config,selection


def membership_payload(model, delivery):
    snapshot,config,_=verify_layer_delivery(model,delivery)
    universe=set(_universe(snapshot,include_obstacles=delivery.to_dict()['request']['include_obstacles']))
    return {'kind':'oabm.named-layer-memberships','version':'1.0.0','selection':delivery.to_dict(),
        'layers':[{'id':layer.id,'name':layer.name,
            **{axis:getattr(layer,axis) for axis in ('system','trade','phase') if getattr(layer,axis) is not None},
            'explicit_member_ids':list(layer.member_ids),
            'drawable_member_ids':sorted(expand_layer_members(snapshot,layer.member_ids)&universe),
            'out_of_scope_member_ids':sorted(expand_layer_members(snapshot,layer.member_ids)-universe)}
            for layer in sorted(config.layers,key=lambda layer:layer.id)]}


def verify_membership_payload(model,payload):
    if not isinstance(payload,dict) or set(payload)!={'kind','version','selection','layers'}:
        raise ValueError('invalid named membership metadata')
    selection=payload.get('selection')
    if not isinstance(selection,dict):raise ValueError('missing saved layer selection')
    config=LayerSelectionConfig.from_dict(selection.get('configuration'))
    request=selection.get('request')
    if not isinstance(request,dict) or set(request)!={'preset','unassigned','isolate','level_ids','physical_routes','include_obstacles'}:
        raise ValueError('invalid saved layer request')
    delivery=build_layer_glb(model,config,**request)
    expected=membership_payload(model,delivery)
    if expected!=payload:raise ValueError('named membership metadata is stale or inconsistent with canonical source')
    return delivery


def _json(payload):
    return json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


def assign_ifc_named_layers(ifc,model,delivery,entity_ifc):
    """Overlapping native IfcGroups; no copied products or representations."""
    from oabm.ifc.adapter import canonical_id_to_ifc_guid,_mark_adapter
    payload=membership_payload(model,delivery)
    canonical=set(_universe(model))
    route_ids={route.id for route in model.routes}
    groups=[]
    for layer in payload['layers']:
        if len(layer['name'])>255:raise ValueError('IFC named layer name exceeds 255 characters')
        key=f'{model.model_id}#layer:{layer["id"]}'
        group=ifc.create_entity('IfcGroup',GlobalId=canonical_id_to_ifc_guid(key),Name=layer['name'])
        _mark_adapter(ifc,group,Role='named-layer')
        products={entity_ifc[i].id():entity_ifc[i] for i in layer['drawable_member_ids'] if i in entity_ifc}
        if len(products)!=len(layer['drawable_member_ids']):raise ValueError('IFC named layer has missing canonical products')
        for identity in set(layer['drawable_member_ids'])&route_ids:
            products.update({product.id():product for key,product in entity_ifc.items()
                if key not in canonical and key.startswith(identity+'#segment:')})
        members=sorted(products.values(),key=lambda product:product.GlobalId)
        if members:
            ifc.create_entity('IfcRelAssignsToGroup',GlobalId=canonical_id_to_ifc_guid(key+'#members'),
                RelatingGroup=group,RelatedObjects=members)
        groups.append({'layer_id':layer['id'],'global_id':group.GlobalId,
                       'member_global_ids':[product.GlobalId for product in members]})
    wrapper={'memberships':payload,'native_groups':groups}
    project=ifc.by_type('IfcProject')[0]
    prop=ifc.create_entity('IfcPropertySingleValue',Name='Json',NominalValue=ifc.create_entity('IfcText',_json(wrapper)))
    pset=ifc.create_entity('IfcPropertySet',GlobalId=canonical_id_to_ifc_guid(model.model_id+'#named-layers'),
        Name='OABM_LayerSelections',HasProperties=[prop])
    ifc.create_entity('IfcRelDefinesByProperties',GlobalId=canonical_id_to_ifc_guid(model.model_id+'#named-layers-rel'),
        RelatedObjects=[project],RelatingPropertyDefinition=pset)


def read_ifc_layer_memberships(ifc):
    """Verify retained config/source and actual native group memberships."""
    from oabm.ifc import from_ifc
    from oabm.routing.physical_capture import bounded_source_json
    sets=[p for p in ifc.by_type('IfcPropertySet') if p.Name=='OABM_LayerSelections']
    if len(sets)!=1:raise ValueError('expected exactly one IFC named layer metadata set')
    props=sets[0].HasProperties
    if len(props)!=1 or props[0].Name!='Json':raise ValueError('invalid IFC named layer metadata')
    from .review_bundle import _strict_json_loads
    wrapper=_strict_json_loads(bounded_source_json(props[0].NominalValue.wrappedValue),'IFC named layers')
    if set(wrapper)!={'memberships','native_groups'}:raise ValueError('invalid IFC layer wrapper')
    owners=[rel for rel in ifc.by_type('IfcRelDefinesByProperties') if rel.RelatingPropertyDefinition==sets[0]]
    if len(owners)!=1 or list(owners[0].RelatedObjects)!=ifc.by_type('IfcProject'):
        raise ValueError('IFC named layer metadata must belong to canonical project')
    model=from_ifc(ifc)
    verify_membership_payload(model,wrapper['memberships'])
    expected={row['id']:row for row in wrapper['memberships']['layers']}
    rows=wrapper['native_groups']
    if len(rows)!=len(expected) or {row['layer_id'] for row in rows}!=set(expected):
        raise ValueError('IFC named layer group coverage mismatch')
    from oabm.ifc.adapter import canonical_id_to_ifc_guid
    routes={route.id for route in model.routes}
    for row in rows:
        if set(row)!={'layer_id','global_id','member_global_ids'}:raise ValueError('invalid IFC native layer record')
        layer=expected[row['layer_id']]
        expected_guid=canonical_id_to_ifc_guid(f'{model.model_id}#layer:{layer["id"]}')
        if row['global_id']!=expected_guid:raise ValueError('IFC named layer identity mismatch')
        group=ifc.by_guid(row['global_id'])
        if not group.is_a('IfcGroup') or group.Name!=layer['name']:raise ValueError('IFC named layer label mismatch')
        actual=[product.GlobalId for rel in group.IsGroupedBy for product in rel.RelatedObjects]
        expected_members={canonical_id_to_ifc_guid(i) for i in layer['drawable_member_ids']}
        for route in model.routes:
            if route.id in set(layer['drawable_member_ids'])&routes:
                expected_members.update(canonical_id_to_ifc_guid(f'{route.id}#segment:{i}') for i in range(len(route.centerline.points)-1))
        if len(actual)!=len(set(actual)) or set(actual)!=expected_members or sorted(actual)!=row['member_global_ids']:
            raise ValueError('IFC named layer native membership mismatch')
    return wrapper


def assign_dxf_named_layers(doc,entries,payload):
    """Native GROUPs share plotted handles; primitive CAD layers stay intact."""
    import hashlib
    sources=[{'handle':row['handle'],'source_id':row['source_ids'][0]} for row in entries if row.get('source_ids')]
    groups=[]
    for layer in payload['layers']:
        name='OABM_LAYER_'+hashlib.sha256(layer['id'].encode()).hexdigest()
        members=sorted(row['handle'] for row in sources if row['source_id'] in set(layer['drawable_member_ids']))
        if members:
            group=doc.groups.new(name,description='OABM named layer; full identity in XRECORD')
            group.set_data([doc.entitydb[handle] for handle in members])
        groups.append({'layer_id':layer['id'],'name':name,'handles':members})
    wrapper={'memberships':payload,'native_groups':groups,'handle_sources':sources}
    text=_json(wrapper)
    doc.rootdict.add_xrecord('OABM_LAYER_SELECTIONS').reset([(1,text[i:i+200]) for i in range(0,len(text),200)])
    return wrapper


def read_dxf_layer_memberships(data,model):
    """Verify retained membership metadata, native groups and handle references.

    CAD coordinates remain editable derived geometry. This reader does not
    prove canonical coordinate/geometry freshness or detect coordinate edits.
    """
    import ezdxf,io
    from oabm.routing.physical_capture import bounded_source_json
    if type(data) is not bytes or len(data)>32*1024*1024:raise ValueError('invalid or oversized named-layer DXF')
    doc=ezdxf.read(io.StringIO(data.decode('utf-8')))
    record=doc.rootdict.get('OABM_LAYER_SELECTIONS')
    if record is None:raise ValueError('missing DXF named layer metadata')
    from .review_bundle import _strict_json_loads
    wrapper=_strict_json_loads(bounded_source_json(''.join(tag.value for tag in record.tags if tag.code==1)),'DXF named layers')
    if set(wrapper)!={'memberships','native_groups','handle_sources'}:raise ValueError('invalid DXF layer wrapper')
    verify_membership_payload(model,wrapper['memberships'])
    layers={layer['id']:layer for layer in wrapper['memberships']['layers']}
    if len(wrapper['native_groups'])!=len(layers) or {g['layer_id'] for g in wrapper['native_groups']}!=set(layers):
        raise ValueError('DXF layer group coverage mismatch')
    sources=wrapper['handle_sources']
    handles=[row['handle'] for row in sources]
    if len(handles)!=len(set(handles)):raise ValueError('duplicate DXF source handles')
    hidden=set(wrapper['memberships']['selection']['resolution']['hidden_ids'])
    from oabm.model.layers import _model_entity_ids
    canonical=_model_entity_ids(model)
    for row in sources:
        if set(row)!={'handle','source_id'}:raise ValueError('invalid DXF source record')
        entity=doc.entitydb.get(row['handle'])
        if entity is None or row['source_id'] not in canonical or row['source_id'] in hidden:
            raise ValueError('invalid DXF canonical source handle')
    import hashlib
    for row in wrapper['native_groups']:
        if set(row)!={'layer_id','name','handles'}:raise ValueError('invalid DXF native layer record')
        layer=layers[row['layer_id']]
        if row['name']!='OABM_LAYER_'+hashlib.sha256(layer['id'].encode()).hexdigest():raise ValueError('DXF layer group identity mismatch')
        group=doc.groups.get(row['name'])
        expected=sorted(s['handle'] for s in sources if s['source_id'] in set(layer['drawable_member_ids']))
        if row['handles']!=expected or (not expected and group is not None) or (expected and (group is None or sorted(entity.dxf.handle for entity in group)!=expected)):
            raise ValueError('DXF layer native membership mismatch')
    return wrapper
