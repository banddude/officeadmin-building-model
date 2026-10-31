"""Fixed trusted Blender entrypoint. Only strict JSON presentation data is consumed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tomllib
import xml.etree.ElementTree as ET

# Executed by Blender as a script, not imported by ordinary Python tests.

def main() -> None:
    mode, installed, destination = sys.argv[sys.argv.index('--') + 1:]
    if mode not in ('probe', 'export', 'export-ifc'):
        raise ValueError('unsupported worker mode')
    package, out = Path(installed), Path(destination)
    # The reviewed engine must win over unrelated copies in a runtime site-packages.
    sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(package)]
    import bpy
    import addon_utils
    import ifcopenshell
    manifest = tomllib.loads((package / 'bonsai/blender_manifest.toml').read_text())
    runtime = {'blender': bpy.app.version_string, 'blender_tuple': list(bpy.app.version),
               'bonsai': manifest['version'], 'ifcopenshell': ifcopenshell.version,
               'python': sys.version, 'probe_executed': True}
    if tuple(bpy.app.version) != (5, 2, 3) or manifest['version'] != '0.8.5' or ifcopenshell.version != '0.8.5':
        raise RuntimeError('unsupported runtime; initial native adapter requires Blender 5.2.3 / Bonsai 0.8.5 / IfcOpenShell 0.8.5')
    addon_utils.enable('bonsai', default_set=True, persistent=True)
    import bonsai.tool as tool
    import bonsai.core.drawing as core
    if 'bonsai' not in bpy.context.preferences.addons or not hasattr(bpy.ops.bim, 'load_project'):
        raise RuntimeError('Bonsai did not register its native IFC operators')
    (out / 'runtime-probe.json').write_text(json.dumps(runtime, indent=2) + '\n')
    if mode == 'probe':
        return
    from oabm.model import BuildingModel
    from oabm.ifc import to_ifc, from_ifc, canonical_id_to_ifc_guid, round_trip_differences
    from oabm.native_bonsai.api import validate_request, _json, canonical_index, space_label_placement, _local_coordinate, LOCAL_COORDINATE_LIMIT_M, elevation_frame
    import ifcopenshell.api
    import ifcopenshell.util.element as element
    import ifcopenshell.util.unit
    from mathutils import Vector, Matrix

    input_names = ('model.json', 'request.json', 'source.ifc') if mode == 'export-ifc' else ('model.json', 'request.json')
    inputs = {name: (out / 'inputs' / name).read_bytes() for name in input_names}
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()}
    if hashes != json.loads((out / 'input-hashes.json').read_text()):
        raise RuntimeError('snapshot hashes changed before worker read')
    model = BuildingModel.from_dict(_json(inputs['model.json']))
    source_proof = None
    native_diagnostics = []
    bound_source = False
    bound_identities = None
    read_canonical = from_ifc
    if mode == 'export-ifc':
        from oabm.native_bonsai.api import validate_ifc_request
        from oabm.native_bonsai.extensions import source_snapshot, verify_source_snapshot, prepare_render_source, merge_render_additions
        from oabm.ifc.adapter import _from_ifc_canonical_subset
        original_native = ifcopenshell.file.from_string(inputs['source.ifc'].decode('utf-8'))
        from oabm.native_bonsai.source_bindings import VERSION as BOUND_VERSION, binding_contract, verify_source_hash
        bound_source = _json(inputs['request.json']).get('version') == BOUND_VERSION
        verify_source_hash(_json(inputs['request.json']), inputs['source.ifc'])
        request = validate_ifc_request(_json(inputs['request.json']), model, original_native)
        source_proof = source_snapshot(original_native)
        native, render_baseline, native_diagnostics = prepare_render_source(original_native, model, request if bound_source else None)
        if bound_source: bound_identities = binding_contract(request, model, native)
        read_canonical = _from_ifc_canonical_subset
    else:
        request = validate_request(_json(inputs['request.json']), model)
        native = to_ifc(model)  # Existing guards remain active.
    differences = () if bound_source else round_trip_differences(model, read_canonical(native))
    if differences:
        raise ValueError(f'IFC adapter roundtrip mismatch: {differences}')
    canonical = {}
    omissions = []
    if bound_source:
        for cid, row in bound_identities.items():
            item = native.by_guid(row['source_guid'])
            canonical[cid] = {'ifc_guid': item.GlobalId, 'ifc_class': item.is_a(),
                              'canonical_kind': 'source_bound', 'geometry_authority': 'original_native_source'}
            if not item.is_a('IfcBuildingStorey') and (not item.Representation or not any(r.RepresentationIdentifier == 'Body' for r in item.Representation.Representations)):
                omissions.append({'canonical_id': cid, 'ifc_guid': item.GlobalId, 'reason': 'Original source has no Body; no geometry invented.'})
    for item in native.by_type('IfcRoot'):
        if bound_source: break
        ps = element.get_pset(item, 'OABM_Canonical')
        if not ps:
            continue
        payload = json.loads(ps['CanonicalJson'])
        if 'id' not in payload:
            continue
        cid = payload['id']
        if cid in canonical:
            raise ValueError('multiple IFC root mappings for canonical ID unsupported')
        canonical[cid] = {'ifc_guid': item.GlobalId, 'ifc_class': item.is_a(), 'canonical_kind': ps['CanonicalKind']}
        body = element.get_pset(item, 'OABM_Adapter') or {}
        if body.get('Body') == 'no':
            omissions.append({'canonical_id': cid, 'ifc_guid': item.GlobalId, 'reason': body.get('BodyReason')})
    unmapped = sorted(set(canonical_index(model)) - set(canonical))
    if unmapped:
        raise ValueError(f'canonical entities without native IFC roots unsupported: {unmapped}')
    if {o['canonical_id'] for o in omissions} != set(request['acknowledged_body_omissions']):
        (out / 'body-omissions.json').write_text(json.dumps(omissions, indent=2) + '\n')
        raise ValueError('acknowledged_body_omissions must exactly match actual IFC Body omissions; see body-omissions.json')
    for view in request['views']:
        for cid in view['exclude_ids']:
            if cid not in canonical or not native.by_guid(canonical[cid]['ifc_guid']).is_a('IfcProduct'):
                raise ValueError(f'view exclusion has no single native product: {cid}')
    # Validate every native product, including excluded/off-view products, before
    # loading: any distant object can trigger Bonsai's automatic false origin.
    import ifcopenshell.geom
    import ifcopenshell.util.placement
    for point in native.by_type('IfcCartesianPoint'):
        for coordinate in point.Coordinates:
            _local_coordinate(coordinate, 'native Cartesian point')
    settings = ifcopenshell.geom.settings()
    settings.set('use-world-coords', True)
    for product in native.by_type('IfcProduct'):
        if product.ObjectPlacement:
            placement = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
            for coordinate in placement[:3, 3]:
                _local_coordinate(float(coordinate), 'native world placement')
        representation = getattr(product, 'Representation', None)
        if not representation or not any(r.RepresentationIdentifier == 'Body' for r in representation.Representations):
            if source_proof is not None and product.is_a() in ('IfcPipeSegment', 'IfcPipeFitting', 'IfcSanitaryTerminal', 'IfcWasteTerminal'):
                # Bounded Curve3D-only plumbing is explicitly acknowledged, not
                # a fabricated physical diameter. Inspect composed world extents.
                import numpy as np
                placement = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
                for rep in representation.Representations if representation else ():
                    if rep.RepresentationType != 'Curve3D' or not rep.Items:
                        raise ValueError('unsupported native plumbing geometry without Body')
                    for item in rep.Items:
                        if not item.is_a('IfcPolyline') or len(item.Points) < 2:
                            raise ValueError('curve-only plumbing requires explicit polyline')
                        for point in item.Points:
                            if len(point.Coordinates) != 3:
                                raise ValueError('curve-only plumbing requires 3D points')
                            for coordinate in (placement @ np.array((*point.Coordinates, 1.0)))[:3]:
                                _local_coordinate(float(coordinate), 'native world curve extent')
            continue
        # Failure to inspect a Body is not permission to omit its bounds.
        shape = ifcopenshell.geom.create_shape(settings, product)
        if not shape.geometry.verts:
            raise ValueError('local-coordinate scope: native Body has no inspectable vertices')
        for coordinate in shape.geometry.verts:
            _local_coordinate(coordinate, 'native world Body extent')
    path = out / 'model.ifc'
    native.write(str(path))
    if bpy.ops.bim.load_project(filepath=str(path), should_start_fresh_session=True) != {'FINISHED'}:
        raise RuntimeError('native IFC load failed')
    if tool.Georeference.get_georeference_props().has_blender_offset:
        raise ValueError('local-coordinate scope: unexpected native false origin; export refused')
    f = tool.Ifc.get()
    if ifcopenshell.util.unit.calculate_unit_scale(f) != 1.0:
        raise RuntimeError('native IFC must remain in canonical metres')
    resources = out / 'resources'
    resources.mkdir()
    devices = {d.id: d for d in (*model.electrical_devices, *model.electrical_equipment)}
    views = {}
    ns = '{http://www.w3.org/2000/svg}'

    for spec in request['views']:
        point_annotations = list(spec['annotations'])
        annotation_sources = {('canonical', cid): (canonical[cid]['ifc_guid'], (d.pose.position.x, d.pose.position.y)) for cid, d in devices.items()}
        for annotation in spec.get('native_annotations', []):
            product = f.by_guid(annotation['source_guid'])
            xyz = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)[:3, 3]
            anchor_basis='native_placement'
            if bound_source:
                from oabm.native_bonsai.source_bindings import native_label_anchor
                xyz,anchor_basis=native_label_anchor(product)
            annotation_sources[('native', product.GlobalId)] = (product.GlobalId, (float(xyz[0]), float(xyz[1])))
            point_annotations.append({'source_id': product.GlobalId, 'kind': 'text', 'text': annotation['text'], 'label_offset_mm': annotation['label_offset_mm'], 'native_source': True, 'anchor_basis':anchor_basis})
        elevation = spec['target_view'] == 'ELEVATION_VIEW'
        position = spec['camera_xyz_m'] if elevation else (*spec['center_m'], spec['cut_height_m'])
        bpy.context.scene.cursor.location = position
        hint = ({'+X': 'WEST', '-X': 'EAST', '+Y': 'SOUTH', '-Y': 'NORTH'}[spec['direction']]
                if elevation else f.by_guid(canonical[spec['level_id']]['ifc_guid']).id())
        # Avoid creating file paths from untrusted canonical storey names.
        original_name_generator = tool.Drawing.__dict__['generate_drawing_name']
        tool.Drawing.generate_drawing_name = classmethod(lambda cls, *args, **kwargs: spec['id'])
        try:
            core.add_drawing(tool.Ifc, tool.Collector, tool.Drawing, target_view=spec['target_view'],
                             location_hint=hint)
        finally:
            tool.Drawing.generate_drawing_name = original_name_generator
        drawing = [e for e in f.by_type('IfcAnnotation') if e.ObjectType == 'DRAWING'][-1]
        bpy.ops.bim.activate_drawing(drawing=drawing.id(), should_view_from_camera=False)
        camera = bpy.context.scene.camera
        camera.location = position
        camera.data.clip_start = spec['near_m'] if elevation else .002
        camera.data.clip_end = spec['far_m'] if elevation else 10.0
        if elevation:
            frame = elevation_frame(spec)
            expected_axes = [tuple(-getattr(frame.right, a) for a in 'xyz'),
                             tuple(getattr(frame.up, a) for a in 'xyz'),
                             tuple(-getattr(frame.normal, a) for a in 'xyz')]
            for index, expected_axis in enumerate(expected_axes):
                if max(abs(camera.matrix_world.col[index][j] - expected_axis[j]) for j in range(3)) > 1e-6:
                    raise ValueError('native elevation orientation disagrees with projection frame')
            if max(abs(camera.location[j] - position[j]) for j in range(3)) > 1e-5:
                raise ValueError('native elevation camera precision exceeds 0.01 mm')
        cp = tool.Drawing.get_camera_props(camera)
        cp.diagram_scale = 'CUSTOM'
        cp.custom_scale_numerator = '1'
        cp.custom_scale_denominator = str(spec['scale_denominator'])
        cp.width, cp.height = spec['width_m'], spec['height_m']
        rx, ry = cp.update_camera_resolution()
        if max(rx, ry) > 30000 or min(rx, ry) < 1:
            raise ValueError('requested native paper extent exceeds bounded raster/aspect resolution')
        bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = rx, ry
        bpy.context.view_layer.update()
        if elevation:
            # Validate the actual quantized native camera, not only its request.
            native_width = camera.data.ortho_scale * min(1.0, rx / ry)
            native_height = camera.data.ortho_scale * min(1.0, ry / rx)
            for depth in (0.0, camera.data.clip_end):
                for x in (-native_width / 2, native_width / 2):
                    for y in (-native_height / 2, native_height / 2):
                        for coordinate in camera.matrix_world @ Vector((x, y, -depth)):
                            _local_coordinate(float(coordinate), 'native elevation complete camera frustum')
        query = ','.join([canonical[cid]['ifc_guid'] for cid in spec['exclude_ids']]+spec.get('native_exclude_guids',[]))
        ps = element.get_pset(drawing, 'EPset_Drawing')
        library = resources / ('symbols-' + spec['id'] + '.svg')
        ifcopenshell.api.run('pset.edit_pset', f, pset=f.by_id(ps['id']),
                            properties={'Exclude': query, 'Symbols': library.relative_to(out).as_posix(),
                                        **({'GlobalReferencing': False} if elevation or bound_source else {})})
        if elevation or bound_source:
            # Isolated details have no automatic cross-elevation/storey callouts.
            references = [e.GlobalId for e in f.by_type('IfcAnnotation') if e.ObjectType == 'DRAWING' and e != drawing]
            references += [e.GlobalId for e in f.by_type('IfcBuildingStorey')]
            ifcopenshell.api.run('pset.edit_pset', f, pset=f.by_id(ps['id']),
                                properties={'Exclude': ','.join(filter(None, [query, *references]))})
        svg_library = ET.Element(ns + 'svg')
        for index, annotation in enumerate(point_annotations):
            group = ET.SubElement(svg_library, ns + 'g', {'id': 'annotation-' + str(index)})
            kind = annotation['kind']
            if kind in ('luminaire', 'receptacle'):
                ET.SubElement(group, ns + 'circle', {'r': '2', 'style': 'fill:white;stroke:black;stroke-width:0.22'})
            if kind == 'switch':
                ET.SubElement(group, ns + 'circle', {'r': '2', 'style': 'fill:white;stroke:none'})
            paths = {'luminaire': 'M-1.4,-1.4 L1.4,1.4 M-1.4,1.4 L1.4,-1.4',
                     'receptacle': 'M-0.6,-2.7 L-0.6,2.7 M0.6,-2.7 L0.6,2.7',
                     'switch': 'M1.2,-1.5 C-2,-2.5 -2,0 0,0 C2,0 2,2.5 -1.2,1.5'}
            if kind in paths:
                ET.SubElement(group, ns + 'path', {'d': paths[kind], 'style': 'fill:none;stroke:black;stroke-width:0.22'})
            dx, dy = annotation['label_offset_mm']
            ET.SubElement(group, ns + 'text', {'x': str(dx), 'y': str(dy), 'data-type': 'text-template',
                                               'style': 'font-size:3px;text-anchor:' + ('end' if dx < 0 else 'start')}).text = 'LABEL'
        for index, annotation in enumerate(spec.get('space_annotations', [])):
            group = ET.SubElement(svg_library, ns + 'g', {'id': 'space-' + str(index)})
            ET.SubElement(group, ns + 'text', {'x': '0', 'y': '0', 'data-type': 'text-template',
                                               'style': 'font-size:3px;text-anchor:middle'}).text = 'LABEL'
            ET.SubElement(group, ns + 'text', {'x': '0', 'y': '4',
                                               'style': 'font-size:2.4px;text-anchor:middle'}).text = 'SOURCE POLYGON; NET AREA UNVERIFIED'
        ET.ElementTree(svg_library).write(library, encoding='utf-8', xml_declaration=True)
        annotations = []
        for index, annotation in enumerate(point_annotations):
            source_id = annotation['source_id']
            source_guid, source_xy = annotation_sources[('native' if annotation.get('native_source') else 'canonical', source_id)]
            product = f.by_guid(source_guid)
            obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing,
                                      object_type='TEXT', relating_type=None, enable_editing=False)
            ann = tool.Ifc.get_entity(obj)
            ann.Name = annotation['text']
            obj.location.x, obj.location.y = source_xy
            if max(abs(obj.location.x - source_xy[0]), abs(obj.location.y - source_xy[1])) > 1e-5:
                raise ValueError('native annotation placement precision exceeds 0.01 mm')
            tool.Drawing.ensure_annotation_in_drawing_plane(obj)
            tool.Drawing.sync_object_placement(obj)
            for literal in tool.Drawing.get_text_literal(obj, return_list=True):
                literal.Literal = annotation['text']
            ps = ifcopenshell.api.run('pset.add_pset', f, product=ann, name='EPset_Annotation')
            ifcopenshell.api.run('pset.edit_pset', f, pset=ps, properties={'Symbol': 'annotation-' + str(index)})
            ps = ifcopenshell.api.run('pset.add_pset', f, product=ann, name='OABM_Presentation')
            ifcopenshell.api.run('pset.edit_pset', f, pset=ps, properties={('NativeSourceGuid' if annotation.get('native_source') else 'CanonicalId'): source_id,
                                'PresentationOnly': True, 'Kind': annotation['kind'], 'PhysicalSizeInferred': False})
            ifcopenshell.api.run('drawing.assign_product', f, relating_product=product, related_object=ann)
            local = camera.matrix_world.inverted() @ Vector(obj.location)
            inside = abs(local.x) <= cp.width / 2 and abs(local.y) <= cp.height / 2
            annotations.append({'source_id': source_id, 'product_guid': product.GlobalId, 'annotation_guid': ann.GlobalId,
                                'kind': annotation['kind'], 'text': annotation['text'], ('native_source_xy_m' if annotation.get('native_source') else 'canonical_xy_m'): list(source_xy),
                                'native_annotation_xy_m': [obj.location.x, obj.location.y],
                                'presentation_plane_z_m': obj.location.z, 'anchor_inside_view_xy': inside,
                                'label_offset_mm': annotation['label_offset_mm'], 'physical_size_inferred': False})
            if annotation.get('native_source'):annotations[-1]['anchor_basis']=annotation['anchor_basis']
        space_results = []
        space_rects = []
        spaces = {space.id: space for space in model.spaces}
        if any(a['shade'] for a in spec.get('space_annotations', [])):
            css = out / 'drawings/assets/default.css'
            with css.open('a') as stream:
                stream.write('\n.oabm-space-area { fill:#e6f1f7; fill-opacity:0.45; stroke:#426b83; stroke-width:0.18; }\n')
        for index, annotation in enumerate(spec.get('space_annotations', [])):
            space = spaces[annotation['source_id']]
            area, centroid, anchor, rect = space_label_placement(space, annotation, spec, devices, space_rects)
            space_rects.append(rect)
            product = f.by_guid(canonical[space.id]['ifc_guid'])
            displayed = area if annotation['area_unit'] == 'm2' else area / .09290304
            label = f"{annotation['label']}: {displayed:.2f} {annotation['area_unit']}"
            fill_guid = None
            if annotation['shade']:
                fill = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing,
                                           object_type='FILL_AREA', relating_type=None, enable_editing=False)
                fill_ann = tool.Ifc.get_entity(fill)
                fill_ann.Name = 'Source polygon area'
                tool.Drawing.ensure_annotation_in_drawing_plane(fill)
                plane_z = fill.location.z
                fill.matrix_world = Matrix.Identity(4)
                fill.data.clear_geometry()
                fill.data.from_pydata([(p.x, p.y, plane_z) for p in space.footprint.points], [],
                                      [list(range(len(space.footprint.points)))])
                fill.data.update()
                for vertex, point in zip(fill.data.vertices, space.footprint.points):
                    if max(abs(vertex.co.x - point.x), abs(vertex.co.y - point.y)) > 1e-5:
                        raise ValueError('native space fill precision exceeds 0.01 mm')
                tool.Drawing.sync_object_placement(fill)
                if bpy.ops.bim.update_representation(obj=fill.name) != {'FINISHED'}:
                    raise RuntimeError('native space fill representation failed')
                ifcopenshell.api.run('drawing.assign_product', f, relating_product=product, related_object=fill_ann)
                ps = ifcopenshell.api.run('pset.add_pset', f, product=fill_ann, name='EPset_Annotation')
                ifcopenshell.api.run('pset.edit_pset', f, pset=ps, properties={'Classes': 'oabm-space-area'})
                fill_guid = fill_ann.GlobalId
            obj = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing,
                                      object_type='TEXT', relating_type=None, enable_editing=False)
            ann = tool.Ifc.get_entity(obj)
            ann.Name = label
            obj.location.x, obj.location.y = anchor
            if max(abs(obj.location.x - anchor[0]), abs(obj.location.y - anchor[1])) > 1e-5:
                raise ValueError('native space annotation precision exceeds 0.01 mm')
            tool.Drawing.ensure_annotation_in_drawing_plane(obj)
            tool.Drawing.sync_object_placement(obj)
            for literal in tool.Drawing.get_text_literal(obj, return_list=True):
                literal.Literal = label
            ps = ifcopenshell.api.run('pset.add_pset', f, product=ann, name='EPset_Annotation')
            ifcopenshell.api.run('pset.edit_pset', f, pset=ps, properties={'Symbol': 'space-' + str(index)})
            ifcopenshell.api.run('drawing.assign_product', f, relating_product=product, related_object=ann)
            space_results.append({'source_id': space.id, 'product_guid': product.GlobalId,
                                  'annotation_guid': ann.GlobalId, 'fill_guid': fill_guid,
                                  'source_polygon_area_m2': area, 'display_area_unit': annotation['area_unit'],
                                  'label': label, 'centroid_xy_m': centroid, 'anchor_xy_m': anchor, 'net_area_verified': False,
                                  'provenance': model.to_dict()['spaces'][next(i for i, s in enumerate(model.spaces) if s.id == space.id)]['provenance']})
        selected = {e.GlobalId for e in tool.Drawing.get_drawing_elements(drawing) if hasattr(e, 'GlobalId')}
        coverage = [{'canonical_id': cid, **mapping,
                     'selection': ('explicitly_excluded' if cid in spec['exclude_ids'] or mapping['ifc_guid'] in spec.get('native_exclude_guids',[]) else
                                   'native_selected' if mapping['ifc_guid'] in selected else 'not_native_selected')}
                    for cid, mapping in sorted(canonical.items())]
        props = tool.Drawing.get_document_props()
        props.should_use_annotation_cache = props.should_use_linework_cache = False
        if bpy.ops.bim.create_drawing(open_viewer=False) != {'FINISHED'}:
            raise RuntimeError('native drawing generation failed')
        # The operator rebuilds RNA camera data; never reuse stale camera props.
        camera = bpy.context.scene.camera
        cp = tool.Drawing.get_camera_props(camera)
        if elevation:
            if abs(camera.data.clip_start - .002) > 1e-8 or abs(camera.data.clip_end - spec['far_m']) > 1e-5:
                raise ValueError('native elevation camera clipping did not survive drawing regeneration')
        uri = Path(tool.Drawing.get_document_uri(tool.Drawing.get_drawing_document(drawing))).resolve()
        if not uri.is_relative_to(out.resolve()):
            raise ValueError('drawing output escaped delivery directory')
        svg = ET.parse(uri).getroot()
        width, height = float(svg.attrib['width'].removesuffix('mm')), float(svg.attrib['height'].removesuffix('mm'))
        if svg.attrib['data-scale'] != '1:' + str(spec['scale_denominator']):
            raise ValueError('native SVG scale mismatch')
        # Native camera integer pixel-aspect quantization is disclosed, never hidden by SVG scaling.
        expected = [spec['width_m'] * 1000 / spec['scale_denominator'], spec['height_m'] * 1000 / spec['scale_denominator']]
        if max(abs(width - expected[0]), abs(height - expected[1])) > .5:
            raise ValueError('native camera/SVG paper aspect mismatch beyond 0.5 mm')
        views[spec['id']] = {'svg': uri.relative_to(out).as_posix(), 'target_view': spec['target_view'],
                             'drawing_guid': drawing.GlobalId, 'scale': '1/' + str(spec['scale_denominator']),
                             'native_svg_mm': [width, height], 'requested_paper_mm': expected,
                             'render_resolution': [rx, ry], 'clip_depth_m': [spec['near_m'], spec['far_m']] if elevation else [.002, 10.0],
                             **({'camera_xyz_m': list(position), 'direction': spec['direction'],
                                 'automatic_reference_markers': False, 'native_drawing_depth_m': [0.0, spec['far_m']]} if elevation else {}),
                             'annotations': annotations, 'coverage': coverage,
                             **({'native_extension_coverage': [{'ifc_guid': row['ifc_guid'], 'ifc_class': row['ifc_class'], 'selection': 'explicitly_excluded' if row['ifc_guid'] in spec.get('native_exclude_guids',[]) else ('native_selected' if row['ifc_guid'] in selected else 'not_native_selected')} for row in native_diagnostics if row.get('status') == 'preserved_native_only']} if source_proof is not None else {}), **({'space_annotations': space_results} if 'space_annotations' in spec else {}),
                             **({'native_exclude_guids': spec.get('native_exclude_guids',[])} if bound_source else {}),
                             'exclude_ids': spec['exclude_ids'], 'symbol_resource': library.relative_to(out).as_posix()}
    if bpy.ops.bim.save_project(filepath=str(path)) != {'FINISHED'}:
        raise RuntimeError('native save failed')
    saved = ifcopenshell.open(str(path))
    if source_proof is not None:
        saved = merge_render_additions(original_native, render_baseline, saved)
        saved.write(str(path))
        saved = ifcopenshell.open(str(path))
        verify_source_snapshot(source_proof, saved, allow_drawing_context_additions=True)
    restored = None if bound_source else read_canonical(saved)
    if bound_source:
        # The actual source graph is independently proven immutable above.
        # Recheck identity/fact bindings; do not claim a canonical geometry
        # roundtrip for native-authoritative meshes and symbolic source bodies.
        binding_contract(request, model, saved)
    differences = () if bound_source else round_trip_differences(model, restored)
    if differences:
        raise ValueError(f'native saved roundtrip mismatch: {differences}')
    # Resource values must be portable local descendants, not URLs or external file paths.
    for drawing in saved.by_type('IfcAnnotation'):
        if drawing.ObjectType != 'DRAWING' or drawing.GlobalId not in {view['drawing_guid'] for view in views.values()}:
            continue
        ps = element.get_pset(drawing, 'EPset_Drawing')
        for key in ('Stylesheet', 'Markers', 'Patterns', 'Symbols', 'ShadingStyles'):
            resource = Path(ps[key])
            if resource.is_absolute() or not (out / resource).resolve().is_relative_to(out.resolve()) or not (out / resource).is_file():
                raise ValueError('non-portable native resource: ' + key)
    receipt = {'status': 'review_only_success', 'input_sha256': hashes, 'runtime': runtime,
               'canonical_roundtrip': ({'performed': False, 'reason': 'Explicit source-bound native geometry; original IFC preservation is proven separately.'} if bound_source else {'exact_equal': model.to_dict() == restored.to_dict(), 'numerical_differences': list(differences)}),
               **({'source_bindings': bound_identities} if bound_source else {}),
               'units': {'canonical_length': 'm', 'ifc_length_scale_to_m': ifcopenshell.util.unit.calculate_unit_scale(saved), 'svg_length': 'mm'},
               'local_coordinate_scope': {'limit_m': LOCAL_COORDINATE_LIMIT_M, 'native_false_origin': False, 'all_body_extents_checked': True},
               'appearance_boundary': ('Bounded original IFC retained; no external resources or canonical plumbing conversion.' if source_proof is not None else 'Canonical adapter geometry only; no external appearance meshes or existing IFC intake.'),
               **({'native_source_preservation': {'verified': True, 'invariant': 'All original entities unchanged except append-only verified project Plan contexts; original context references and order retained.', 'existing_presentation_resources': 'Opaque references retained, never loaded; existing resource bytes are not copied or validated.', 'diagnostics': native_diagnostics, 'acknowledged_native_body_omissions': request.get('acknowledged_native_body_omissions', [])}} if source_proof is not None else {}),
               'canonical_identity': canonical, 'body_omissions': omissions, 'views': views,
               'limitations': ['Native selection is not a pixel visibility/occlusion guarantee.',
                               'PLAN/RCP use fixed 10 m depth. Elevation native drawing depth is camera plane zero to explicit far_m; near_m=0.002 is the pinned nominal viewport clip only. level_id is not an exclusive storey filter.',
                               'Annotation collision/label clipping and professional sheet composition require visual review.',
                               'No dimensions, circuits, engineering, photometry or physical sizes are inferred.',
                               'Native annotation/drawing GUIDs and file ordering may vary; canonical product identity is stable.',
                               'No elevation annotations/dimensions, PDF, arbitrary IFC intake, custom symbol resources, grouped callouts or physical assembly opt-in in v1.']}
    receipt['files'] = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(out.rglob('*')) if p.is_file() and '.runtime' not in p.parts
                        and p.suffix not in ('.log', '.h5') and p.name != 'receipt.json'}
    (out / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
