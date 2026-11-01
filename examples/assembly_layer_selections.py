"""Synthetic enclosed room + electrical: one saved resolver, actual consumers.

Run PYTHONPATH=src python examples/assembly_layer_selections.py /tmp/fresh-layers.
No OfficeAdmin application, automatic classification or engineering claim.
"""
from dataclasses import replace, fields
from pathlib import Path
import hashlib
import json
import sys

from oabm.building_assemblies.room_fixtures import synthetic_enclosed_room_assembly
from oabm.model import (ElectricalDevice, ElectricalBox, BoxSelectionOutcome, CatalogProduct, SupportAssembly,
    SupportComponent, SupportInterface, SupportSelectionOutcome, RuleReference, StructuralMember,
    Point3, Pose, Size3, Port, Vector3, Route, Polyline3D, Level,
    LayerDefinition, LayerSelectionConfig, SavedPreset, BuildingModel, resolve_preset, validate_model)
from oabm.model.layers import _universe
from oabm.exports import build_layer_glb
from oabm.exports.layer_presentation import apply_layer_selection
from oabm.drawings import PlanSpec, VisibilityPolicy, generate_plan, view_to_svg
from oabm.drawings.model import DrawingSet
from oabm.drawings.generator import _source_reference
from oabm.drawings.sheets import SheetSpec, ViewPlacement
from oabm.drawings.sheet_dxf import sheet_to_dxf
from oabm.ifc import to_ifc, from_ifc
from oabm.quantities import extract_quantities


def synthetic_model():
    model = synthetic_enclosed_room_assembly()
    level = model.levels[0].id
    def pose(x,y,z): return Pose(position=Point3(x=x,y=y,z=z))
    devices = tuple(ElectricalDevice(id=f'device:{key}', device_type=kind,level_id=level,
        pose=pose(x,1.2,1.2),size=Size3(x=.12,y=.12,z=.12))
        for key,kind,x in (('power','receptacle',1.),('lighting','luminaire',3.)))
    # Unknown level is deliberately disclosed, never inferred from Z.
    unknown = ElectricalDevice(id='device:unknown',device_type='sensor',pose=pose(2.,2.,1.2))
    ports = tuple(Port(id=f'port:{i}',owner_id=d.id,pose=d.pose,domain='electrical',role='source' if i==0 else 'load',
        direction=Vector3(x=1. if i==0 else -1.,y=0.,z=0.)) for i,d in enumerate(devices))
    route = Route(id='route:room',route_type='emt',start_port_id=ports[0].id,end_port_id=ports[1].id,
        centerline=Polyline3D(points=tuple(d.pose.position for d in devices)),nominal_diameter_m=.02)
    product=CatalogProduct(catalog_id='synthetic',catalog_revision='1',product_id='envelope')
    basis=RuleReference(authority='synthetic-caller',revision='1',rule_id='display-only')
    box=ElectricalBox(id='box:room',intent_id='intent:box',box_type='junction',level_id=level,
        host_id='wall:proof',mounting='surface',pose=pose(1.,.02,1.2),size=Size3(x=.2,y=.1,z=.2),
        occupant_ids=(devices[0].id,),product=product)
    component=SupportComponent(id='support:part',assembly_id='support:shared',part_key='strut',level_id=level,
        category='support',component_type='strut',product=product,pose=pose(2.,.02,1.8),size=Size3(x=1.,y=.04,z=.04))
    interface=SupportInterface(id='support:interface',assembly_id='support:shared',interface_key='wall',level_id=level,
        host_id='wall:proof',substrate='wood',pose=component.pose,component_ids=(component.id,))
    assembly=SupportAssembly(id='support:shared',intent_id='intent:shared',assembly_type='strut',level_id=level,
        supported_object_ids=tuple(d.id for d in devices),component_ids=(component.id,),interface_ids=(interface.id,),basis=basis)
    outcome=SupportSelectionOutcome(id='support:outcome',intent_id=assembly.intent_id,status='selected',
        supported_object_ids=assembly.supported_object_ids,selected_assembly_id=assembly.id,requested_level_id=level,requested_basis=basis)
    member=StructuralMember(id='member:room',kind='beam',level_id=level,pose=pose(2.,1.6,3.4),size=Size3(x=3.,y=.15,z=.15))
    model=replace(model,levels=model.levels+(Level(id='level:other',name='Other level',elevation_m=4.),),
        electrical_devices=devices+(unknown,),ports=ports,routes=(route,),electrical_boxes=(box,),
        box_selection_outcomes=(BoxSelectionOutcome(id="box:outcome",intent_id=box.intent_id,
            occupant_ids=box.occupant_ids,status="selected",selected_box_id=box.id,requested_product=product),),
        support_assemblies=(assembly,),support_components=(component,),support_interfaces=(interface,),
        support_selection_outcomes=(outcome,),structural_members=(member,))
    validate_model(model)
    return model


def saved_config(model):
    layers=(
        LayerDefinition(id='layer:architecture',name='Architecture',trade='architecture',phase='new',
            member_ids=tuple(e.id for e in (*model.walls,*model.slabs,*model.ceilings,*model.spaces))),
        LayerDefinition(id='layer:power',name='Power',trade='electrical',system='power',phase='new',
            member_ids=('device:power','box:room','route:room')),
        LayerDefinition(id='layer:lighting',name='Lighting',trade='electrical',system='lighting',phase='existing',
            member_ids=('device:lighting',)),
        LayerDefinition(id='layer:supports',name='Shared supports',trade='electrical',system='supports',phase='new',
            member_ids=('support:shared',)),
        LayerDefinition(id='layer:structure',name='Structure',trade='structure',phase='existing',member_ids=('member:room',)),
        LayerDefinition(id='layer:unknown',name='Explicit unknown level device',trade='electrical',member_ids=('device:unknown',)),
    )
    ids=tuple(layer.id for layer in layers)
    return LayerSelectionConfig(layers=layers,presets=(
        SavedPreset(id='preset:all',name='All visible reset',visible_layer_ids=ids),
        SavedPreset(id='preset:review',name='Electrical review',hidden_layer_ids=('layer:lighting',),
            ghosted_layer_ids=('layer:architecture','layer:supports','layer:structure'),
            visible_layer_ids=('layer:power','layer:unknown'))))


def render_presets(output, requests):
    """Offline readback renderer: actual indexed GLB geometry/alphas/visibility.

    Uses the existing physical route example decoder. Fixed isometric framing
    across states; no external viewer or OfficeAdmin compatibility is claimed.
    """
    from physical_route_layers import read_glb, hidden_nodes, mesh_triangles
    from PIL import Image, ImageDraw
    import numpy as np
    panels=[]
    for label,*_ in requests:
        doc,binary=read_glb((output/label/'model.glb').read_bytes())
        hidden=hidden_nodes(doc);triangles=[]
        for index,node in enumerate(doc['nodes']):
            if index in hidden or 'mesh' not in node: continue
            faces,colour=mesh_triangles(doc,binary,node)
            for tri in faces:
                # GLB X,Y,Z correspond to canonical X,Z,-Y.
                points=[(75+72*p[0]-25*p[2],295-65*p[1]-24*p[2]+9*p[0]) for p in tri]
                depth=float(np.mean(tri[:,0]+tri[:,2]-tri[:,1]))
                triangles.append((depth,points,tuple(int(c*255) for c in colour)))
        image=Image.new('RGB',(540,430),'white');draw=ImageDraw.Draw(image,'RGBA')
        draw.text((18,15),label,fill=(20,35,50,255))
        for _,points,colour in sorted(triangles,key=lambda row:row[0]):
            draw.polygon(points,fill=colour)
        panels.append(image)
    canvas=Image.new('RGB',(540*3,430*2),'#edf0f3')
    for i,panel in enumerate(panels):canvas.paste(panel,((i%3)*540,(i//3)*430))
    canvas.save(output/'presets.png')


def run(output):
    output=Path(output);output.mkdir(exist_ok=False)
    model=synthetic_model();config=saved_config(model)
    config.dump(output/'layers.json');config=LayerSelectionConfig.load(output/'layers.json')
    source=model.to_json();quantities=extract_quantities(model).to_json()
    reports=[]
    requests=(('reset','preset:all',(),()),('review','preset:review',(),()),
        ('isolate','preset:all',('layer:power',),()),
        ('level','preset:review',(),('level:proof',)),('other-level','preset:all',(),('level:other',)))
    for label,preset,isolate,levels in requests:
        folder=output/label;folder.mkdir()
        delivery=build_layer_glb(model,config,preset=preset,unassigned='error',isolate=isolate,level_ids=levels)
        (folder/'model.glb').write_bytes(delivery.glb_bytes)
        (folder/'selection.json').write_text(delivery.manifest_json)
        # Every consumer uses the retained exact source and same saved request.
        snapshot=BuildingModel.from_json(delivery.source_model_json)
        resolution=resolve_preset(config,snapshot,preset=preset,unassigned='error',isolate=isolate,level_ids=levels)
        sources=tuple(_source_reference(e) for f in fields(snapshot)
            for e in (getattr(snapshot,f.name) if isinstance(getattr(snapshot,f.name),tuple) else ())
            if hasattr(e,"id"))
        view_counts={}
        for view_name,cut in (('plan',1.2),('ceiling-cut',2.28)):
            base=generate_plan(snapshot,PlanSpec(id=view_name,level_id='level:proof',cut_height_m=cut,view_depth_above_m=2.5,
                visibility=VisibilityPolicy(labels=False,dimensions=False)),assembly_geometry='cut',
                symbol_provider=lambda entity, view_type: None)
            view=apply_layer_selection(base,snapshot,resolution)
            (folder/f'{view_name}.svg').write_text(view_to_svg(view))
            drawing=DrawingSet(snapshot.model_id,(view,),(),sources)
            dxf=sheet_to_dxf(drawing,SheetSpec('L-1',300,220,titleblock_height_mm=25,title='Synthetic layer review'),
                (ViewPlacement('main',view.id,15,15),))
            (folder/f'{view_name}.dxf').write_bytes(dxf.dxf)
            (folder/f'{view_name}-dxf-manifest.json').write_text(dxf.manifest_json)
            view_counts[view_name]=len(view.primitives)
        native=to_ifc(snapshot,assembly_geometry='tessellated',layer_resolution=resolution)
        assert from_ifc(native).to_dict()==snapshot.to_dict()
        native.write(str(folder/'model.ifc'))
        assert extract_quantities(snapshot).to_json()==quantities
        reports.append({'name':label,'resolution':resolution.to_dict(),
            'ifc_layers':len(native.by_type('IfcPresentationLayerWithStyle')),'drawing_primitives':view_counts})
    assert model.to_json()==source
    (output/'model.json').write_text(source);(output/'quantities.json').write_text(quantities)
    # Deliberately incomplete configuration still discloses coverage under isolate.
    incomplete=replace(config,layers=tuple(replace(l,member_ids=()) if l.id=='layer:unknown' else l for l in config.layers))
    unresolved=resolve_preset(incomplete,model,preset='preset:all',isolate=('layer:power',),unassigned='hidden')
    assert unresolved.unassigned_ids==('device:unknown',)
    (output/'unassigned-selection.json').write_text(json.dumps(unresolved.to_dict(),indent=2))
    (output/'REPORT.json').write_text(json.dumps({'universe_count':len(_universe(model)),'requests':reports,
        'boundary':'No OfficeAdmin controls or external viewer compatibility claim. IFC viewers must honor native layer flags.'},indent=2))
    render_presets(output,requests)
    files=sorted(p for p in output.rglob('*') if p.is_file())
    (output/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(output))+'\n' for p in files))
    return reports

if __name__=='__main__':
    run(sys.argv[1])
