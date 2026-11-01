"""Enclosed synthetic room: explicit preset edits -> same-model consumers.

Run with a fresh output directory. --render requires the optional CairoSVG
adapter. Rendered views are canonical mesh cuts, not proposed construction.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from oabm.building_assemblies import regenerate_building_assemblies, AssemblyRegenerationError
from oabm.building_assemblies.room_fixtures import (
    synthetic_enclosed_room_assembly, synthetic_room_edit_requests)
from oabm.drawings import VisibilityPolicy, PlanSpec, SectionSpec, generate_plan, generate_section, view_to_svg
from oabm.exports.gltf import build_glb_bytes
from oabm.ifc import to_ifc, from_ifc
from oabm.model import ElectricalEquipment, Point3, Port, Pose, Vector3
from oabm.quantities import extract_quantities
from oabm.routing import RoutingOptions, derive_suspended_ceiling_route_domain, route_between_ports, RoutingError


def with_room_ports(model):
    hanger = next(c for c in model.assembly_components if c.component_kind == "hanger_wire"
        and .4 < c.geometry.centerline.points[0].x < 4.3)
    y = hanger.geometry.centerline.points[0].y + .3
    equipment = tuple(ElectricalEquipment(id=f"equipment:room-{i}",equipment_type="junction",
        level_id="level:proof",pose=Pose(position=Point3(x=x,y=y,z=2.6)))
        for i,x in enumerate((.3,4.5)))
    ports = tuple(Port(id=f"port:room-{i}",owner_id=e.id,domain="electrical",role="source" if i == 0 else "load",
        pose=e.pose,direction=Vector3(x=1. if i == 0 else -1.,y=0.,z=0.),nominal_diameter_m=.02)
        for i,e in enumerate(equipment))
    return replace(model,electrical_equipment=equipment,ports=ports)


def checked_room_route(model, domain=None):
    options = RoutingOptions(clearance_m=.005,port_stub_m=.05)
    domain = domain or derive_suspended_ceiling_route_domain(model,"assembly:ceiling-proof",
        nominal_diameter_m=.02,options=options)
    result = route_between_ports(model,"port:room-0","port:room-1","emt",
        nominal_diameter_m=.02,options=options,allowed_domain=domain)
    route, fittings = result
    return replace(model, routes=(route,), route_fittings=fittings), domain


def room_review_svg(model):
    """Demo-only isometric projection of the shared physical meshes.

    Category colors/translucency aid review; they are not material claims.
    The existing source deck is an explicit top/underside wireframe.
    """
    from oabm.assembly_mesh import assembly_component_mesh
    def project(point):
        x,y,z=point
        return (150+145*(.8*x+.6*y), 770+145*(.3*x-.4*y-z))
    faces=[]
    palette={"header":"#d98926", "door_frame":"#347a9b", "door_leaf":"#9dbac8",
        "window_frame":"#347a9b", "window_glazing":"#94d5dc", "tile":"#9dabb8",
        "drywall_panel":"#bbc4c8", "hanger_wire":"#18293b"}
    for part in model.assembly_components:
        vertices,indices=assembly_component_mesh(part)
        for offset in range(0,len(indices),3):
            triangle=[vertices[index] for index in indices[offset:offset+3]]
            depth=sum(x-y+z for x,y,z in triangle)/3
            points=" ".join(f"{u:.3f},{v:.3f}" for u,v in map(project,triangle))
            opacity=.10 if part.component_kind in {"drywall_panel","tile","window_glazing"} else .85
            color=palette.get(part.component_kind,"#966f49" if part.component_kind in
                {"stud","cripple","bottom_plate","top_plate","sill"} else "#607182")
            faces.append((depth,f'<polygon points="{points}" fill="{color}" fill-opacity="{opacity}"/>'))
    deck=model.slabs[0]
    rings=[[(point.x,point.y,point.z-delta) for point in deck.footprint.points]
           for delta in (0,deck.thickness_m)]
    lines=[]
    for ring in rings:
        for first,second in zip(ring,ring[1:]+ring[:1]):
            a,b=project(first),project(second)
            lines.append(f'<path d="M {a[0]:.3f} {a[1]:.3f} L {b[0]:.3f} {b[1]:.3f}"/>')
    for first,second in zip(*rings):
        a,b=project(first),project(second)
        lines.append(f'<path d="M {a[0]:.3f} {a[1]:.3f} L {b[0]:.3f} {b[1]:.3f}"/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1100" viewBox="0 0 1200 1100">'
        '<rect width="1200" height="1100" fill="white"/>'
        '<g font-family="sans-serif" fill="#18293b"><text x="35" y="40" font-size="24">Synthetic enclosed room — canonical mesh review</text>'
        '<text x="35" y="68" font-size="15">Translucent drywall/tiles; category colors only. Blue wireframe = existing deck top and underside.</text></g>'
        + ''.join(face for _,face in sorted(faces,key=lambda row:row[0]))
        + '<g fill="none" stroke="#3978a5" stroke-width="1">'+''.join(lines)+'</g></svg>')


def run(output, render=False):
    output = Path(output)
    output.mkdir(parents=True,exist_ok=False)
    before = with_room_ports(synthetic_enclosed_room_assembly())
    after = regenerate_building_assemblies(before,synthetic_room_edit_requests())
    reports = {}
    domains = {}
    for label,model in (("before",before),("after",after)):
        folder = output / label
        folder.mkdir()
        (folder/"model.json").write_text(model.to_json())
        (folder/"preview.glb").write_bytes(build_glb_bytes(model))
        native = to_ifc(model,assembly_geometry="tessellated")
        assert from_ifc(native).to_dict() == model.to_dict()
        native.write(str(folder/"physical.ifc"))
        quantities = extract_quantities(model)
        (folder/"quantities.json").write_text(quantities.to_json())
        visibility = VisibilityPolicy(electrical=False,routes=False,spaces=False,labels=False,dimensions=False)
        views = (
            generate_plan(model,PlanSpec(id="room-plan",level_id="level:proof",cut_height_m=1.2,visibility=visibility),assembly_geometry="cut"),
            generate_section(model,SectionSpec(id="front-wall-cut",origin=Point3(x=0.,y=0.,z=0.),direction=Vector3(x=0.,y=1.,z=0.),visibility=visibility),assembly_geometry="cut"),
            generate_section(model,SectionSpec(id="room-ceiling-cut",origin=Point3(x=0.,y=next(part.geometry.centerline.points[0].y for part in model.assembly_components if part.component_kind == "hanger_wire"),z=0.),direction=Vector3(x=0.,y=1.,z=0.),visibility=visibility),assembly_geometry="cut"),
        )
        for view in views:
            svg = view_to_svg(view)
            (folder/f"{view.id}.svg").write_text(svg)
            if render:
                import cairosvg
                cairosvg.svg2png(bytestring=svg.encode(),write_to=str(folder/f"{view.id}.png"),output_width=1400,background_color="white")
        review_svg = room_review_svg(model)
        (folder/"room-review.svg").write_text(review_svg)
        if render:
            cairosvg.svg2png(bytestring=review_svg.encode(),write_to=str(folder/"room-review.png"),background_color="white")
        route,domain = checked_room_route(model)
        domains[label] = domain
        (folder/"routed-model.json").write_text(route.to_json())
        try:
            regenerate_building_assemblies(route,synthetic_room_edit_requests())
        except AssemblyRegenerationError as error:
            refusal = str(error)
        else:
            raise AssertionError("routed model regeneration must refuse")
        reports[label] = {"components":len(model.assembly_components),"assemblies":len(model.building_assemblies),
            "ifc_roundtrip":"exact", "quantity_geometry_volume_m3":sum(item.quantity for item in quantities.items if item.category == "assembly_component_geometry_volume"), "route_points":[[p.x,p.y,p.z] for p in route.routes[0].centerline.points],
            "route_geometry_check":dict(route.routes[0].attributes["geometry_check"]),
            "routed_edit_refusal":refusal,
            "domain_box":str(domain.volumes[0])}
    try:
        checked_room_route(after,domains["before"])
    except RoutingError as error:
        reports["stale_domain_refusal"] = str(error)
    else:
        raise AssertionError("old domain must refuse after edit")
    old = {p.id:p for p in before.assembly_components}
    new = {p.id:p for p in after.assembly_components}
    reports["changed_component_ids"] = sorted(key for key in old.keys() & new.keys() if old[key].geometry != new[key].geometry)
    reports["preserved_component_ids"] = len(old.keys() & new.keys())
    reports["source_facts_unchanged"] = all(getattr(before,key)==getattr(after,key)
        for key in ("walls","openings","ceilings","slabs","spaces","levels","electrical_equipment","ports"))
    reports["limitations"] = ["Synthetic butt-ended wall envelopes close the room; corner connection design is unknown.",
        "Ceiling perimeter support, tee connections and deck fasteners remain unmodeled.",
        "No structural/code approval, manufacturer-exact parts, wall penetrations or other trades.",
        "Routing is the existing bounded assembly-aware polyline verifier; no new swept-arc claim."]
    (output/"report.json").write_text(json.dumps(reports,sort_keys=True,indent=2)+"\n")
    manifest = {str(path.relative_to(output)):{"bytes":path.stat().st_size,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(output.rglob("*")) if path.is_file()}
    (output/"manifest.json").write_text(json.dumps(manifest,sort_keys=True,indent=2)+"\n")
    return reports

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output");parser.add_argument("--render",action="store_true")
    print(json.dumps(run(**vars(parser.parse_args())),sort_keys=True))
