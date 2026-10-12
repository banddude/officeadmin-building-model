"""Anonymous native IFC4 inputs, authored independently of every OABM adapter.

All lengths accepted by this helper are metres. Native geometry oracles use
IfcOpenShell's geometry engine, never the canonical importer or exporter.
"""
from __future__ import annotations

import math
import uuid

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.shape


def _guid(label: str) -> str:
    return ifcopenshell.guid.compress(uuid.uuid5(uuid.NAMESPACE_URL, "synthetic-native-ifc/" + label).hex)


PROJECT_GUID = _guid("project")
SITE_GUID = _guid("site")
BUILDING_GUID = _guid("building")
STOREY_GUID = _guid("storey")
WALL_GUID = _guid("wall")
OPENING_GUID = _guid("opening")
WINDOW_GUID = _guid("window")


def build_native_ifc_fixture(
    *, millimetres: bool = False, yaw_rad: float = 0.0,
    translation_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rectangle_profile: bool = False, edge_axis: bool = True,
    backward_axis: bool = False, reversed_winding: bool = False,
    opening_axis: str = "vertical", with_window: bool = True,
) -> ifcopenshell.file:
    """Build a 3×0.3×2 wall and a 1×0.3×1 hosted void, without metadata shadows.

    The void occupies X=1..2 and Z=0.5..1.5 in the wall frame. An edge
    axis uses Y=0 with Body Y=0..0.3; otherwise Body is centered on Y=0.
    Parent translation/yaw affect the entire spatial and geometry hierarchy.
    """
    if opening_axis not in {"vertical", "horizontal", "host-normal"}:
        raise ValueError("opening_axis must be vertical, horizontal or host-normal")
    f = ifcopenshell.file(schema="IFC4")
    f.header.file_name.name = "anonymous-native-wall.ifc"
    f.header.file_name.time_stamp = "1970-01-01T00:00:00"
    f.header.file_name.author = ("Anonymous synthetic fixture",)
    f.header.file_name.organization = ("Anonymous synthetic fixture",)
    f.header.file_name.preprocessor_version = "anonymous native fixture"
    f.header.file_name.originating_system = "anonymous native fixture"
    f.header.file_name.authorization = ""
    scale = 1000.0 if millimetres else 1.0

    def point(coords):
        return f.create_entity("IfcCartesianPoint", Coordinates=tuple(float(v) * scale for v in coords))

    def direction(coords):
        return f.create_entity("IfcDirection", DirectionRatios=tuple(float(v) for v in coords))

    def frame(location=(0.0, 0.0, 0.0), axis=None, ref=None):
        return f.create_entity("IfcAxis2Placement3D", Location=point(location),
                               Axis=None if axis is None else direction(axis),
                               RefDirection=None if ref is None else direction(ref))

    def placement(parent=None, location=(0.0, 0.0, 0.0), yaw=0.0):
        return f.create_entity("IfcLocalPlacement", PlacementRelTo=parent,
                               RelativePlacement=frame(location, axis=(0, 0, 1), ref=(math.cos(yaw), math.sin(yaw), 0.0)))

    context = f.create_entity("IfcGeometricRepresentationContext", ContextIdentifier=None,
                              ContextType="Model", CoordinateSpaceDimension=3,
                              Precision=1e-8 * scale, WorldCoordinateSystem=frame(), TrueNorth=None)
    length = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Prefix="MILLI" if millimetres else None, Name="METRE")
    units = f.create_entity("IfcUnitAssignment", Units=(length,))
    project = f.create_entity("IfcProject", GlobalId=PROJECT_GUID, Name="Synthetic project",
                              RepresentationContexts=(context,), UnitsInContext=units)
    site = f.create_entity("IfcSite", GlobalId=SITE_GUID, Name="Synthetic site",
                           ObjectPlacement=placement(location=translation_m), CompositionType="ELEMENT")
    building = f.create_entity("IfcBuilding", GlobalId=BUILDING_GUID, Name="Synthetic building",
                               ObjectPlacement=placement(site.ObjectPlacement, yaw=yaw_rad), CompositionType="ELEMENT")
    storey = f.create_entity("IfcBuildingStorey", GlobalId=STOREY_GUID, Name="Synthetic storey",
                             ObjectPlacement=placement(building.ObjectPlacement), CompositionType="ELEMENT", Elevation=0.0)
    for label, parent, child in (("project-site", project, site), ("site-building", site, building), ("building-storey", building, storey)):
        f.create_entity("IfcRelAggregates", GlobalId=_guid(label), RelatingObject=parent, RelatedObjects=(child,))

    def profile(width, depth, center=(0.0, 0.0)):
        if rectangle_profile:
            pos = f.create_entity("IfcAxis2Placement2D", Location=point(center))
            return f.create_entity("IfcRectangleProfileDef", ProfileType="AREA", Position=pos,
                                   XDim=width * scale, YDim=depth * scale)
        corners = [(center[0] - width / 2, center[1] - depth / 2),
                   (center[0] + width / 2, center[1] - depth / 2),
                   (center[0] + width / 2, center[1] + depth / 2),
                   (center[0] - width / 2, center[1] + depth / 2)]
        if reversed_winding:
            corners.reverse()
        points = [point(c) for c in corners]
        curve = f.create_entity("IfcPolyline", Points=tuple(points + [points[0]]))
        return f.create_entity("IfcArbitraryClosedProfileDef", ProfileType="AREA", OuterCurve=curve)

    def body(swept, depth, position=None):
        solid = f.create_entity("IfcExtrudedAreaSolid", SweptArea=swept,
                                Position=frame() if position is None else position,
                                ExtrudedDirection=direction((0, 0, 1)), Depth=depth * scale)
        return f.create_entity("IfcShapeRepresentation", ContextOfItems=context,
                               RepresentationIdentifier="Body", RepresentationType="SweptSolid", Items=(solid,))

    center_y = 0.15 if edge_axis else 0.0
    axis_points = [point((0, 0, 0)), point((3, 0, 0))]
    if backward_axis:
        axis_points.reverse()
    axis = f.create_entity("IfcShapeRepresentation", ContextOfItems=context,
                           RepresentationIdentifier="Axis", RepresentationType="Curve3D",
                           Items=(f.create_entity("IfcPolyline", Points=tuple(axis_points)),))
    wall_body = body(profile(3.0, 0.3, (1.5, center_y)), 2.0)
    wall = f.create_entity("IfcWall", GlobalId=WALL_GUID, Name="Synthetic wall",
                           ObjectPlacement=placement(storey.ObjectPlacement),
                           Representation=f.create_entity("IfcProductDefinitionShape", Representations=(axis, wall_body)),
                           PredefinedType="NOTDEFINED")
    f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_guid("wall-containment"),
                    RelatingStructure=storey, RelatedElements=(wall,))
    if opening_axis == "vertical":
        opening_body = body(profile(1.0, 0.3), 1.0)
        opening_location = (1.5, center_y, 0.5)
    else:
        # Local solid Z=-world Y, local solid Y=world Z: exact same box.
        opening_body = body(profile(1.0, 1.0), 0.3,
                            frame(axis=(0, -1, 0), ref=(1, 0, 0)))
        opening_location = (1.5, center_y + 0.15, 1.0)
    opening = f.create_entity("IfcOpeningElement", GlobalId=OPENING_GUID, Name="Synthetic opening",
                              ObjectPlacement=placement(wall.ObjectPlacement, opening_location),
                              Representation=f.create_entity("IfcProductDefinitionShape", Representations=(opening_body,)),
                              PredefinedType="OPENING")
    f.create_entity("IfcRelVoidsElement", GlobalId=_guid("wall-void"), RelatingBuildingElement=wall, RelatedOpeningElement=opening)
    if with_window:
        window = f.create_entity("IfcWindow", GlobalId=WINDOW_GUID, Name="Synthetic filling",
                                 ObjectPlacement=placement(wall.ObjectPlacement, (1.5, center_y, 0.5)),
                                 Representation=f.create_entity("IfcProductDefinitionShape", Representations=(body(profile(1, 0.2), 1),)),
                                 OverallHeight=1.0 * scale, OverallWidth=1.0 * scale,
                                 PredefinedType="WINDOW", PartitioningType="NOTDEFINED")
        f.create_entity("IfcRelFillsElement", GlobalId=_guid("opening-fill"), RelatingOpeningElement=opening, RelatedBuildingElement=window)
        f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=_guid("window-containment"), RelatingStructure=storey, RelatedElements=(window,))
    return f


def author_fixture(*, mm=False, nested=False, edge_axis=False, reverse_axis=False,
                   poly_profile=False, horizontal_opening=False, window=False,
                   reversed_winding=False):
    """Short option aliases for parameterized importer integration tests."""
    return build_native_ifc_fixture(
        millimetres=mm, yaw_rad=math.pi / 2 if nested else 0.0,
        translation_m=(7.0, -2.0, 0.4) if nested else (0.0, 0.0, 0.0),
        rectangle_profile=not poly_profile, edge_axis=edge_axis,
        backward_axis=reverse_axis, reversed_winding=reversed_winding,
        opening_axis="horizontal" if horizontal_opening else "vertical", with_window=window)


def _native_shape(product, subtract_openings):
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    settings.set("disable-opening-subtractions", not subtract_openings)
    return ifcopenshell.geom.create_shape(settings, product)


def native_world_vertices(product, *, subtract_openings=False):
    """Native tessellated world vertices in metres, with optional boolean voids."""
    shape = _native_shape(product, subtract_openings)
    values = shape.geometry.verts
    return tuple(tuple(values[i:i + 3]) for i in range(0, len(values), 3))


def native_volume(product, *, subtract_openings=True):
    """Independently evaluated native volume in cubic metres."""
    shape = _native_shape(product, subtract_openings)
    return ifcopenshell.util.shape.get_volume(shape.geometry)


def native_void_volume(file, wall_guid=WALL_GUID):
    return native_volume(file.by_guid(wall_guid), subtract_openings=True)
