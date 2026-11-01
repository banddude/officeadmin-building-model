"""Presentation adapters over the same complete saved-preset resolution.

Never filter or rewrite the canonical model. Drawings omit hidden annotations
and primitives; IFC retains all geometry with standard presentation layer flags.
"""
from dataclasses import replace

from oabm.model.layers import _universe
from .layers import _validate_partition


def selection_states(model, resolution, *, include_obstacles=False):
    _validate_partition(model, resolution, include_obstacles=include_obstacles)
    return {entity_id: state for state, ids in (
        ('visible', resolution.visible_ids), ('hidden', resolution.hidden_ids),
        ('ghosted', resolution.ghosted_ids)) for entity_id in ids}


def apply_layer_selection(view, model, resolution, *, include_obstacles=False):
    """Return a derived DrawingView usable by SVG, physical sheets and DXF.

    Assembly cut primitives carry child then parent IDs: the first source owns
    the geometry. Its already resolved state includes inherited membership.
    Dimensions spanning several objects are omitted if any source is hidden or ghosted.
    """
    states = selection_states(model, resolution, include_obstacles=include_obstacles)
    from oabm.drawings.model import Point2
    primitives = []
    for primitive in view.primitives:
        state = states.get(primitive.source_ids[0], 'visible')
        if state == 'hidden':
            continue
        if state == 'ghosted':
            primitive = replace(primitive, style=replace(primitive.style, stroke='ghost', weight='thin'),
                metadata=primitive.metadata + (('layer_state', state),))
        if primitive.kind == 'symbol' and primitive.symbol == 'support_attachment':
            # Explicit paper-sized semantic attachment cross, not hardware geometry.
            anchor = primitive.points[0]
            half = .001 * view.scale
            style = replace(primitive.style, stroke='ghost' if state == 'ghosted' else 'electrical')
            for suffix, endpoints in (('h', ((-half,0),(half,0))), ('v', ((0,-half),(0,half)))):
                primitives.append(replace(primitive, id=primitive.id+':'+suffix, kind='line',
                    points=tuple(Point2(anchor.x+x,anchor.y+y) for x,y in endpoints),
                    symbol=None,text=None,style=style))
            if primitive.text:
                primitives.append(replace(primitive,id=primitive.id+':label',kind='text',symbol=None,
                    points=(Point2(anchor.x+2*half,anchor.y+2*half),),style=style))
        else:
            primitives.append(primitive)
    dimensions = tuple(d for d in view.dimensions
        if all(states.get(i, 'visible') == 'visible' for i in d.source_ids))
    return replace(view, primitives=tuple(primitives), dimensions=dimensions,
        metadata=view.metadata + (('layer_preset_id', resolution.preset_id),
            ('layer_hidden_count', str(len(resolution.hidden_ids))),
            ('layer_ghosted_count', str(len(resolution.ghosted_ids)))))


def assign_ifc_layer_selection(ifc, model, resolution, entity_ifc):
    """Write native IFC4 layer flags and actual translucent ghost materials.

    A viewer must honor IfcPresentationLayerWithStyle for initial hiding. All
    geometry, groups, canonical shadows and quantities remain in the file.
    Derived route segments follow their route; canonical fittings remain
    individually selectable, even when their owning route has another state.
    """
    import ifcopenshell.api.style
    states = selection_states(model, resolution)
    ghost = ifc.create_entity('IfcSurfaceStyle', Name='OABM layer ghost', Side='BOTH', Styles=[
        ifc.create_entity('IfcSurfaceStyleRendering',
            SurfaceColour=ifc.create_entity('IfcColourRgb', Name=None, Red=.65, Green=.68, Blue=.72),
            Transparency=.82, ReflectanceMethod='NOTDEFINED')])
    canonical = set(_universe(model))
    products_by_state = {state: {} for state in ('visible', 'hidden', 'ghosted')}
    route_ids = {route.id for route in model.routes}
    for entity_id, state in sorted(states.items()):
        product = entity_ifc.get(entity_id)
        if product is None:
            raise ValueError(f'IFC layer selection has no product for {entity_id}')
        products = [product]
        if entity_id in route_ids:
            products.extend(p for key,p in entity_ifc.items()
                if key not in canonical and key.startswith(entity_id+'#segment:'))
        for item in products:
            products_by_state[state][item.id()] = item
    for state, products in products_by_state.items():
        representations = []
        for product in products.values():
            shape = getattr(product, 'Representation', None)
            for rep in shape.Representations if shape else ():
                representations.append(rep)
                if state == 'ghosted' and rep.RepresentationIdentifier == 'Body':
                    for item in rep.Items:
                        ifcopenshell.api.style.assign_item_style(ifc, item=item, style=ghost)
        if representations:
            ifc.create_entity('IfcPresentationLayerWithStyle',
                Name=f'layer:{resolution.preset_id}:{state}',
                Description='Saved OABM selection; geometry and quantities retained',
                AssignedItems=representations, LayerOn=state!='hidden',
                LayerFrozen=state=='hidden', LayerBlocked=False,
                LayerStyles=[ghost] if state=='ghosted' else [])
