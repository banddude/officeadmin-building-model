"""Issue #275 evidence: saved layer selections render, hide, ghost, and tell the truth.

Offline inspection output, outside the OABM runtime package. It builds a small
**synthetic** two-level building (framed and glazed walls, slabs, a space, a
panelboard, receptacles, conduit routes with real conductors so the GLB draws
wires), resolves four saved layer selections from one portable configuration,
exports each with ``oabm.exports.gltf.to_glb`` through
``oabm.exports.layers.layer_glb_plan``, parses the GLB files back with the
standard library, and renders the actual exported meshes honouring the actual
``KHR_node_visibility`` extension and the actual material alphas.

The render is the acceptance evidence issue #275 asks for: hide, isolate and
ghost behaviour on real GLB output, with the ghosted route's conductor wires
visibly translucent beside it and the unsupported architecture ghosts named in
the panel. Run from the repository root:

    python spikes/layer_selections_evidence/render_evidence.py out.png

``OABM_EVIDENCE_SCALE`` scales the PNG (default 1). Needs only the installed
package plus numpy and Pillow (both core dependencies). No network access, no
viewer application.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from oabm.exports import layer_glb_plan, to_glb
from oabm.model.layers import resolve_preset
from oabm.model import (
    BuildingModel,
    CatalogProduct,
    Circuit,
    Conductor,
    ElectricalDevice,
    ElectricalEquipment,
    LayerSelectionConfig,
    Level,
    Point3,
    Polygon3D,
    Polyline3D,
    Port,
    Pose,
    Provenance,
    Route,
    RuleReference,
    Slab,
    Space,
    Vector3,
    Wall,
)

_BACKGROUND = (255, 255, 255)
_PANEL_KEY_COLOUR = (40, 40, 46)
_PANEL_VALUE_COLOUR = (70, 70, 78)
_PANEL_TITLE_COLOUR = (20, 20, 24)
_PROVENANCE = (
    Provenance(source_kind="synthetic", source_id="spike:layers", derivation="user"),
)


def parse_glb(path: Path) -> tuple[dict, bytes]:
    """Read a GLB container back into (json document, BIN chunk bytes)."""

    data = path.read_bytes()
    json_length, json_type = struct.unpack_from("<II", data, 12)
    if json_type != 0x4E4F534A:  # "JSON"
        raise ValueError(f"{path}: not a glTF JSON chunk")
    gltf = json.loads(data[20:20 + json_length])
    offset = 20 + json_length
    bin_chunk = b""
    if offset < len(data):
        bin_length, bin_type = struct.unpack_from("<II", data, offset)
        if bin_type != 0x004E4942:  # "BIN\0"
            raise ValueError(f"{path}: not a glTF BIN chunk")
        bin_chunk = data[offset + 8:offset + 8 + bin_length]
    return gltf, bin_chunk


def synthetic_two_level_model() -> BuildingModel:
    """Two levels, architecture, power, lighting, and a conduit run with wires."""

    def wall(wall_id: str, level_id: str, y: float, construction: str | None) -> Wall:
        return Wall(
            id=wall_id,
            level_id=level_id,
            centerline=Polyline3D(points=(
                Point3(x=0, y=y, z=0), Point3(x=6, y=y, z=0),
            )),
            thickness_m=0.15,
            height_m=3.0,
            construction=construction,
            provenance=_PROVENANCE,
        )

    def slab(slab_id: str, level_id: str, z: float) -> Slab:
        return Slab(
            id=slab_id,
            level_id=level_id,
            footprint=Polygon3D(points=(
                Point3(x=0, y=0, z=z), Point3(x=6, y=0, z=z), Point3(x=6, y=4, z=z),
            )),
            thickness_m=0.2,
            provenance=_PROVENANCE,
        )

    def device(device_id: str, level_id: str, x: float, z: float) -> ElectricalDevice:
        return ElectricalDevice(
            id=device_id,
            device_type="receptacle" if "recp" in device_id else "occupancy_sensor",
            pose=Pose(position=Point3(x=x, y=0.35, z=z)),
            level_id=level_id,
            provenance=_PROVENANCE,
        )

    panel = ElectricalEquipment(
        id="equip:panel",
        equipment_type="panelboard",
        pose=Pose(position=Point3(x=0.6, y=0.35, z=1.5)),
        level_id="level:one",
        provenance=_PROVENANCE,
    )
    devices = (
        device("device:one-recp", "level:one", 3.0, 1.2),
        device("device:one-em", "level:one", 5.0, 2.9),
        device("device:two-recp", "level:two", 3.0, 5.2),
    )
    ports = (
        Port(
            id="port:panel-out", owner_id="equip:panel", domain="electrical",
            role="load", pose=Pose(position=Point3(x=0.6, y=0.35, z=1.4)),
            direction=Vector3(x=0, y=0, z=-1), provenance=_PROVENANCE,
        ),
        Port(
            id="port:recp-in", owner_id="device:one-recp", domain="electrical",
            role="line", pose=Pose(position=Point3(x=3.0, y=0.35, z=1.2)),
            direction=Vector3(x=0, y=0, z=1), provenance=_PROVENANCE,
        ),
        Port(
            id="port:two-in", owner_id="device:two-recp", domain="electrical",
            role="line", pose=Pose(position=Point3(x=3.0, y=0.35, z=5.2)),
            direction=Vector3(x=0, y=0, z=1), provenance=_PROVENANCE,
        ),
    )
    route_one = Route(
        id="route:one", route_type="emt",
        start_port_id="port:panel-out", end_port_id="port:recp-in",
        centerline=Polyline3D(points=(
            Point3(x=0.6, y=0.35, z=1.4),
            Point3(x=0.6, y=0.35, z=2.6),
            Point3(x=3.0, y=0.35, z=2.6),
            Point3(x=3.0, y=0.35, z=1.2),
        )),
        nominal_diameter_m=0.03,
        provenance=_PROVENANCE,
    )
    route_two = Route(
        id="route:two", route_type="emt",
        start_port_id="port:panel-out", end_port_id="port:two-in",
        centerline=Polyline3D(points=(
            Point3(x=0.6, y=0.35, z=1.4),
            Point3(x=0.6, y=0.35, z=5.4),
            Point3(x=3.0, y=0.35, z=5.4),
            Point3(x=3.0, y=0.35, z=5.2),
        )),
        nominal_diameter_m=0.03,
        provenance=_PROVENANCE,
    )
    circuit = Circuit(
        id="circuit:one", source_port_id="port:panel-out",
        load_port_ids=("port:recp-in",), route_ids=("route:one",),
        provenance=_PROVENANCE,
    )
    circuit_two = Circuit(
        id="circuit:two", source_port_id="port:panel-out",
        load_port_ids=("port:two-in",), route_ids=("route:two",),
        provenance=_PROVENANCE,
    )
    conductors = tuple(
        Conductor(
            id=f"conductor:{circuit_key}-{role}", circuit_id=circuit_id, role=role,
            count=1, route_ids=(route_id,), provenance=_PROVENANCE,
        )
        for circuit_key, circuit_id, route_id in (
            ("one", "circuit:one", "route:one"),
            ("two", "circuit:two", "route:two"),
        )
        for role in ("l1", "neutral", "egc")
    )
    return BuildingModel(
        model_id="model:layers-evidence",
        levels=(
            Level(id="level:one", elevation_m=0.0, height_m=3.0),
            Level(id="level:two", elevation_m=4.0, height_m=3.0),
        ),
        walls=(
            wall("wall:one-a", "level:one", 0.0, "framed"),
            wall("wall:one-glass", "level:one", 2.0, "glazed"),
            wall("wall:two-a", "level:two", 0.0, "framed"),
        ),
        slabs=(slab("slab:one", "level:one", 0.0), slab("slab:two", "level:two", 4.0)),
        spaces=(
            Space(
                id="space:one", level_id="level:one",
                footprint=Polygon3D(points=(
                    Point3(x=0, y=0, z=0), Point3(x=6, y=0, z=0), Point3(x=6, y=4, z=0),
                )),
                provenance=_PROVENANCE,
            ),
        ),
        electrical_devices=devices,
        electrical_equipment=(panel,),
        ports=ports,
        routes=(route_one, route_two),
        circuits=(circuit, circuit_two),
        conductors=conductors,
    )


def synthetic_config() -> LayerSelectionConfig:
    """Layers with overlap, an empty named system, and three saved presets."""

    return LayerSelectionConfig.from_dict({
        "schema_version": "1.0.0",
        "layers": [
            {
                "id": "layer:power",
                "name": "Power",
                "member_ids": [
                    "device:one-recp", "device:two-recp", "equip:panel",
                    "route:one", "route:two",
                ],
                "system": "power",
                "trade": "electrical",
            },
            {
                "id": "layer:lighting",
                "name": "Lighting",
                "member_ids": ["device:one-em"],
                "system": "lighting",
            },
            {
                "id": "layer:phase-1",
                "name": "Phase 1",
                "member_ids": ["device:one-recp", "route:one"],
                "phase": "phase-1",
            },
            {
                "id": "layer:arch",
                "name": "Architecture",
                "member_ids": [
                    "wall:one-a", "wall:one-glass", "wall:two-a",
                    "slab:one", "slab:two", "space:one",
                ],
                "trade": "architecture",
            },
            {
                "id": "layer:fire-alarm",
                "name": "Fire alarm",
                "system": "fire_alarm",
            },
        ],
        "presets": [
            {
                "id": "preset:all",
                "name": "All visible",
                "visible_layer_ids": [
                    "layer:power", "layer:lighting", "layer:phase-1",
                    "layer:arch", "layer:fire-alarm",
                ],
            },
            {
                "id": "preset:phase-1",
                "name": "Phase 1 only",
                "visible_layer_ids": ["layer:power", "layer:phase-1"],
                "hidden_layer_ids": ["layer:lighting", "layer:fire-alarm"],
                "ghosted_layer_ids": ["layer:arch"],
            },
            {
                "id": "preset:ghost-power",
                "name": "Power ghosted",
                "visible_layer_ids": ["layer:lighting", "layer:arch"],
                "hidden_layer_ids": ["layer:phase-1", "layer:fire-alarm"],
                "ghosted_layer_ids": ["layer:power"],
            },
        ],
    })


def _hidden_subtree(gltf: dict) -> set[int]:
    """Node indices hidden by the actual KHR_node_visibility extension."""

    hidden_groups = {
        index
        for index, node in enumerate(gltf["nodes"])
        if node.get("extensions", {}).get("KHR_node_visibility", {}).get("visible") is False
    }
    hidden: set[int] = set()
    frontier = list(hidden_groups)
    while frontier:
        index = frontier.pop()
        if index in hidden:
            continue
        hidden.add(index)
        frontier.extend(gltf["nodes"][index].get("children", ()))
    return hidden


def node_faces(gltf: dict, bin_chunk: bytes, node: dict) -> tuple[np.ndarray, np.ndarray]:
    """(triangles, colours) for one mesh node, from the actual GLB buffers."""

    mesh = gltf["meshes"][node["mesh"]]
    primitive = mesh["primitives"][0]
    if primitive.get("mode", 4) != 4:
        return np.zeros((0, 3, 3)), np.zeros((0, 4))
    accessor = gltf["accessors"][primitive["attributes"]["POSITION"]]
    view = gltf["bufferViews"][accessor["bufferView"]]
    values = np.frombuffer(
        bin_chunk, dtype="<f4", count=accessor["count"] * 3,
        offset=view["byteOffset"],
    ).reshape(accessor["count"], 3)
    triangles = values[: len(values) // 3 * 3].reshape(-1, 3, 3)
    material = gltf["materials"][primitive["material"]]
    colour = material["pbrMetallicRoughness"]["baseColorFactor"]
    colours = np.tile(np.asarray(colour, dtype=np.float64), (len(triangles), 1))
    return triangles, colours


def render_view(gltf: dict, bin_chunk: bytes, size: tuple[int, int]) -> Image.Image:
    """Orthographic render honouring the file's own hidden groups and alphas."""

    width, height = size
    image = Image.new("RGB", size, _BACKGROUND)
    hidden = _hidden_subtree(gltf)

    view_dir = np.array([0.35, -0.35, 2.6])
    view_dir /= np.linalg.norm(view_dir)
    up_world = np.array([0.0, 0.0, 1.0])
    right = np.cross(view_dir, up_world)
    right /= np.linalg.norm(right)
    up_cam = np.cross(right, view_dir)

    all_triangles: list[np.ndarray] = []
    all_colours: list[np.ndarray] = []
    for index, node in enumerate(gltf["nodes"]):
        if "mesh" not in node or index in hidden:
            continue
        triangles, colours = node_faces(gltf, bin_chunk, node)
        if len(triangles):
            all_triangles.append(triangles)
            all_colours.append(colours)
    if not all_triangles:
        return image
    triangles = np.concatenate(all_triangles)
    colours = np.concatenate(all_colours)

    flat = triangles.reshape(-1, 3)
    screen_x = flat @ right
    screen_y = flat @ up_cam
    depth = flat @ view_dir
    min_x, max_x = screen_x.min(), screen_x.max()
    min_y, max_y = screen_y.min(), screen_y.max()
    scale = min((width - 40) / (max_x - min_x), (height - 40) / (max_y - min_y))
    origin_x = (width - scale * (max_x + min_x)) / 2.0
    origin_y = (height + scale * (max_y + min_y)) / 2.0

    order = np.argsort(depth.reshape(len(triangles), 3).mean(axis=1))
    light = np.array([0.4, -0.5, 0.75])
    light /= np.linalg.norm(light)

    canvas = image.convert("RGBA")
    for tri_index in order:
        corners = triangles[tri_index]
        normal = np.cross(corners[1] - corners[0], corners[2] - corners[0])
        norm = np.linalg.norm(normal)
        shade = 0.55 + 0.45 * abs(float(np.dot(normal / norm, light))) if norm > 1e-12 else 0.55
        red, green, blue, alpha = colours[tri_index]
        fill = (
            int(min(255, float(red) * shade * 255.0)),
            int(min(255, float(green) * shade * 255.0)),
            int(min(255, float(blue) * shade * 255.0)),
            int(min(255, max(0.0, float(alpha)) * 255.0)),
        )
        points = [
            (
                float(origin_x + scale * screen_x[tri_index * 3 + corner]),
                float(origin_y - scale * screen_y[tri_index * 3 + corner]),
            )
            for corner in range(3)
        ]
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon(points, fill=fill)
        canvas = Image.alpha_composite(canvas, overlay)
    return canvas.convert("RGB")


def node_material_facts(gltf: dict, node_name: str) -> str:
    """One line: the node's actual material name and alpha, from the GLB."""

    node = next(item for item in gltf["nodes"] if item["name"] == node_name)
    material = gltf["materials"][node["mesh"] and gltf["meshes"][node["mesh"]]["primitives"][0]["material"]]
    alpha = material["pbrMetallicRoughness"]["baseColorFactor"][3]
    return f"{node_name}  material={material['name']}  alpha={alpha}"


def render_evidence(output_path: str | Path) -> dict[str, object]:
    """Resolve, export, render and report the layer-selection behaviour."""

    model = synthetic_two_level_model()
    config = synthetic_config()
    scale = float(os.environ.get("OABM_EVIDENCE_SCALE", "1") or 1)
    work_dir = Path(output_path).resolve().parent
    work_dir.mkdir(parents=True, exist_ok=True)

    cases: list[tuple[str, dict[str, object]]] = [
        ("preset:all", {"preset": "preset:all", "unassigned": "visible"}),
        ("preset:phase-1", {"preset": "preset:phase-1", "unassigned": "visible"}),
        ("preset:ghost-power", {"preset": "preset:ghost-power", "unassigned": "visible"}),
        (
            "isolate:power",
            {
                "preset": "preset:all", "unassigned": "visible",
                "isolate": ("layer:power",),
            },
        ),
    ]
    views: list[tuple[str, Image.Image, str]] = []
    resolutions: dict[str, dict[str, object]] = {}
    ghost_gltf = None
    for label, kwargs in cases:
        resolution = resolve_preset(config, model, **kwargs)  # type: ignore[arg-type]
        plan = layer_glb_plan(model, resolution)
        target = work_dir / f"layers-{label.replace(':', '-')}.glb"
        summary = to_glb(
            model, target,
            groups=plan.groups,
            hidden_groups=plan.hidden_groups,
            dimmed_ids=plan.dimmed_ids,
        )
        gltf, bin_chunk = parse_glb(target)
        caption = (
            f"{resolution.preset_name}"
            + (f" + isolate" if kwargs.get("isolate") else "")
        )
        report_line = (
            f"visible {len(resolution.visible_ids)}  hidden {len(resolution.hidden_ids)}  "
            f"ghosted {len(resolution.ghosted_ids)}  dimmed {summary.get('dimmed', 0)}  "
            f"dimmed wires {summary.get('dimmed_conductor_wires', 0)}"
        )
        if plan.unsupported_ghost_ids:
            unsupported = plan.unsupported_ghost_ids
            shown = ", ".join(unsupported[:2])
            more = len(unsupported) - 2
            report_line += (
                f"  unsupported ghosts {len(unsupported)}: {shown}"
                + (f" + {more} more" if more > 0 else "")
            )
        views.append((caption, render_view(gltf, bin_chunk, (int(480 * scale), int(360 * scale))), report_line))
        resolutions[label] = {
            "resolution": resolution.to_dict(),
            "summary": {key: value for key, value in summary.items() if key != "path"},
            "unsupported_ghost_ids": list(plan.unsupported_ghost_ids),
        }
        if label == "preset:ghost-power":
            ghost_gltf = gltf

    assert ghost_gltf is not None
    material_lines = [
        node_material_facts(ghost_gltf, name)
        for name in (
            "route:two",
            "conductor:two-l1#route:two#0",
            "conductor:two-neutral#route:two#0",
            "conductor:two-egc#route:two#0",
        )
    ]

    title_height = int(22 * scale)
    caption_height = int(20 * scale)
    report_height = int(18 * scale)
    view_height = int(360 * scale) + title_height + caption_height + report_height + int(10 * scale)
    width = (int(480 * scale) + int(20 * scale)) * 2 + int(20 * scale)
    height = view_height * 2 + (report_height * (len(material_lines) + 2)) + int(16 * scale)
    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    for index, (caption, view, report_line) in enumerate(views):
        column, row = index % 2, index // 2
        x = int(10 * scale) + column * (int(480 * scale) + int(20 * scale))
        y = int(4 * scale) + row * view_height
        draw.text((x + int(4 * scale), y), caption, fill=_PANEL_TITLE_COLOUR)
        image.paste(view, (x, y + title_height))
        line_y = y + title_height + int(360 * scale) + caption_height
        draw.text((x + int(4 * scale), line_y), report_line, fill=_PANEL_VALUE_COLOUR)

    panel_top = view_height * 2 + int(8 * scale)
    draw.text(
        (int(14 * scale), panel_top),
        "Materials read back from the ghost-power GLB: the route and all three of its wires ghost",
        fill=_PANEL_TITLE_COLOUR,
    )
    for index, line in enumerate(material_lines, start=1):
        draw.text(
            (int(20 * scale), panel_top + index * report_height),
            line,
            fill=_PANEL_KEY_COLOUR,
        )

    target = Path(output_path)
    image.save(target, format="PNG")
    return {
        "resolutions": resolutions,
        "material_lines": material_lines,
        "image": str(target),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = render_evidence(argv[1])
    for line in report["material_lines"]:
        print(line)
    print(f"written: {report['image']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
