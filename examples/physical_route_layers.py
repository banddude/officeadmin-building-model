"""Synthetic routed detour with saved visibility presets and GLB readback render.

The narrow offline renderer reads actual indexed GLB meshes, translations,
material alphas and KHR_node_visibility. It is display evidence, not a viewer
compatibility or clearance claim. All panels use the same orthographic frame.
"""
from pathlib import Path
import json
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw

from physical_route_schedule import create_example
from oabm.exports import build_layer_glb
from oabm.model import BuildingModel, LayerDefinition, LayerSelectionConfig, SavedPreset


def saved_config():
    return LayerSelectionConfig(layers=(
        LayerDefinition(id='layer:routes', name='Canonical conduit', member_ids=('route:demo',)),
        LayerDefinition(id='layer:obstacles', name='Supplied obstacles', member_ids=('obstacle:demo',)),
    ), presets=(
        SavedPreset(id='preset:ghost-obstacles', name='Routes visible, obstacles ghosted',
                    visible_layer_ids=('layer:routes',), ghosted_layer_ids=('layer:obstacles',)),
        SavedPreset(id='preset:hide-routes', name='Routes hidden, obstacles visible',
                    hidden_layer_ids=('layer:routes',), visible_layer_ids=('layer:obstacles',)),
        SavedPreset(id='preset:ghost-routes', name='Routes ghosted, obstacles visible',
                    ghosted_layer_ids=('layer:routes',), visible_layer_ids=('layer:obstacles',)),
    ))


def read_glb(payload):
    size=struct.unpack_from('<I',payload,12)[0]
    return json.loads(payload[20:20+size]),payload[28+size:]


def hidden_nodes(doc):
    hidden=set()
    def visit(index, inherited=False):
        node=doc['nodes'][index]
        invisible=inherited or node.get('extensions',{}).get('KHR_node_visibility',{}).get('visible') is False
        if invisible: hidden.add(index)
        for child in node.get('children',[]): visit(child,invisible)
    for root in doc['scenes'][doc.get('scene',0)]['nodes']: visit(root)
    return hidden


def mesh_triangles(doc,binary,node):
    # Bounded to this example's identity rotations and translated indexed meshes.
    if node.get('rotation') or node.get('matrix') or node.get('scale'):
        raise ValueError('Evidence renderer only supports translated identity poses')
    def read(index):
        acc=doc['accessors'][index];view=doc['bufferViews'][acc['bufferView']]
        dtype={5126:'<f4',5125:'<u4',5123:'<u2'}[acc['componentType']]
        width=3 if acc['type']=='VEC3' else 1
        return np.frombuffer(binary,dtype=dtype,count=acc['count']*width,
            offset=view.get('byteOffset',0)+acc.get('byteOffset',0)).reshape(-1,width)
    primitive=doc['meshes'][node['mesh']]['primitives'][0]
    if primitive.get('mode',4)!=4: raise ValueError('Expected triangle mesh')
    points=read(primitive['attributes']['POSITION'])+np.array(node.get('translation',[0,0,0]))
    indices=read(primitive['indices']).reshape(-1) if 'indices' in primitive else np.arange(len(points))
    return points[indices].reshape(-1,3,3),doc['materials'][primitive['material']]['pbrMetallicRoughness']['baseColorFactor']


def render_panel(payload,title):
    doc,binary=read_glb(payload);hidden=hidden_nodes(doc)
    image=Image.new('RGB',(660,330),'white');draw=ImageDraw.Draw(image)
    draw.text((20,16),title,fill='#152b3c')
    # Emitted GLB uses Y up. Looking down Y, Z is negative canonical Y.
    triangles=[]
    for index,node in enumerate(doc['nodes']):
        if index in hidden or 'mesh' not in node: continue
        mesh,colour=mesh_triangles(doc,binary,node)
        for tri in mesh: triangles.append((float(tri[:,1].mean()),tri,colour))
    canvas=image.convert('RGBA')
    for _,tri,colour in sorted(triangles,key=lambda item:item[0]):
        overlay=Image.new('RGBA',canvas.size,(0,0,0,0))
        points=[(65+130*float(p[0]),185+130*float(p[2])) for p in tri]
        fill=tuple(int(255*c) for c in colour)
        ImageDraw.Draw(overlay).polygon(points,fill=fill)
        canvas=Image.alpha_composite(canvas,overlay)
    return canvas.convert('RGB')


def create_layer_example(output):
    output=Path(output);output.mkdir(exist_ok=False)
    create_example(output/'route')
    model=BuildingModel.load(output/'route'/'model.json')
    config=saved_config();config.dump(output/'layers.json')
    panels=[];reports=[];buffers=[]
    requests=[(preset.id,preset.name,config,()) for preset in config.presets]
    requests.append(('preset:ghost-obstacles','Isolate route; obstacle retained and hidden',config,('layer:routes',)))
    # A second saved configuration deliberately leaves the obstacle unassigned.
    incomplete=LayerSelectionConfig(layers=(config.layers[0],),presets=(SavedPreset(
        id='preset:route-only',name='Route only, incomplete coverage',visible_layer_ids=('layer:routes',)),))
    incomplete.dump(output/'layers-incomplete.json')
    requests.append(('preset:route-only','Isolate route; unassigned obstacle disclosed',incomplete,('layer:routes',)))
    for index,(preset,title,selection,isolate) in enumerate(requests):
        delivery=build_layer_glb(model,selection,preset=preset,unassigned='visible',isolate=isolate,
                                 physical_routes=True,include_obstacles=True)
        stem=f'{index+1:02d}'
        (output/f'{stem}.glb').write_bytes(delivery.glb_bytes)
        (output/f'{stem}-manifest.json').write_text(delivery.manifest_json+'\n')
        doc,binary=read_glb(delivery.glb_bytes);buffers.append(binary)
        panels.append(render_panel(delivery.glb_bytes,title))
        reports.append(delivery.to_dict())
    assert all(body==buffers[0] for body in buffers)
    assert len({report['source']['sha256'] for report in reports})==1
    image=Image.new('RGB',(660,330*len(panels)),'white')
    for index,panel in enumerate(panels): image.paste(panel,(0,index*330))
    image.save(output/'presets.png')
    return reports


if __name__=='__main__':
    if len(sys.argv)!=2: raise SystemExit('Usage: python examples/physical_route_layers.py FRESH_OUTPUT_DIRECTORY')
    create_layer_example(sys.argv[1])
