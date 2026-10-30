"""Synthetic physical assembly model -> quantity, GLB, IFC and cut-view SVG.

Run: python examples/assembly_consumers.py FRESH_OUTPUT_DIRECTORY
No Blender/Bonsai process or raster renderer is launched.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from oabm.building_assemblies.combined_fixtures import synthetic_wall_inserts_ceiling_assembly
from oabm.drawings import PlanSpec,SectionSpec,generate_plan,generate_section,view_to_svg
from oabm.exports.gltf import build_glb_bytes
from oabm.ifc import to_ifc,from_ifc
from oabm.model import Point3,Vector3
from oabm.quantities import extract_quantities


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    model=synthetic_wall_inserts_ceiling_assembly()
    (output/'model.json').write_text(model.to_json())
    (output/'preview.glb').write_bytes(build_glb_bytes(model))
    native=to_ifc(model,assembly_geometry='tessellated')
    assert from_ifc(native).to_dict()==model.to_dict()
    native.write(str(output/'physical.ifc'))
    views=[generate_plan(model,PlanSpec(id='assembly-plan',level_id=model.levels[0].id),assembly_geometry='cut'),
        generate_section(model,SectionSpec(id='assembly-section',origin=Point3(x=1,y=0,z=0),direction=Vector3(x=1,y=0,z=0)),assembly_geometry='cut')]
    for view in views:(output/(view.id+'.svg')).write_text(view_to_svg(view))
    report={'assemblies':len(model.building_assemblies),'components':len(model.assembly_components),
        'ifc_roundtrip':'exact canonical dictionary equality','quantity_items':len(extract_quantities(model).items),
        'views':{v.id:{'metadata':dict(v.metadata),'cut_source_ids':sorted({p.source_ids[0] for p in v.primitives if p.layer=='architecture:assembly-cuts'})} for v in views},
        'limitations':['IFC assembly geometry is tessellated, not parametrically editable.',
            'Assembly drawing geometry is cut-plane-only; no beyond-cut projection or hidden-line elevation.',
            'WireSweep surfaces use straight round segments.',
            'Synthetic illustrative assemblies are not manufacturer fit or engineering approval.']}
    (output/'report.json').write_text(json.dumps(report,sort_keys=True,indent=2)+'\n')
    manifest={p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(output.iterdir())}
    (output/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
    print(json.dumps(report,sort_keys=True))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output');run(parser.parse_args().output)
