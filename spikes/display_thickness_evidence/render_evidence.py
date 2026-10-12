"""Issue #88 evidence: a wall drawn at its display thickness is a wall.

Offline inspection output, outside the OABM runtime package. It builds a
small **synthetic** scan-like room (three walls recorded at the 0.001 m
default with ``inferred`` provenance, one wall measured at 0.2 m, one slab),
exports it twice with ``oabm.exports.gltf.to_glb`` — once faithfully, once
with ``min_display_wall_thickness_m`` — parses both GLB files back with the
standard library, and renders an orthographic view of each beside the element
panel data read out of the **actual exported node extras**.

The render is the acceptance evidence issue #88 asks for: a scanned wall
reads as a wall, and the panel states plainly that the drawn thickness is a
drawing aid while the model keeps what it honestly knows. Run from the
repository root:

    python spikes/display_thickness_evidence/render_evidence.py out.png

``OABM_EVIDENCE_SCALE`` scales the PNG (default 1). Needs only the installed
package plus numpy and Pillow (both core dependencies). No network access,
no viewer application.
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

from oabm.exports.gltf import to_glb
from oabm.model import (
    BuildingModel,
    Level,
    Point3,
    Polyline3D,
    Provenance,
    Slab,
    Wall,
)

#: The display thickness the operator has chosen for this drawing.
MIN_DISPLAY_THICKNESS_M = 0.1

#: The recorded thickness a RoomPlan capture honestly carries (issue #75).
SCAN_THICKNESS_M = 0.001

_BACKGROUND = (255, 255, 255)
_PANEL_KEY_COLOUR = (40, 40, 46)
_PANEL_VALUE_COLOUR = (70, 70, 78)
_PANEL_TITLE_COLOUR = (20, 20, 24)


def synthetic_scan_room() -> BuildingModel:
    """One room: three scan-recorded walls, one measured wall, one slab.

    The scan-recorded walls carry the 0.001 m default and ``inferred``
    provenance exactly as the RoomPlan importer records them (issue #75);
    the measured wall and the slab are observed and draw opaque.
    """

    level = Level(id="level:main", elevation_m=0.0, height_m=2.7)
    scan_provenance = (
        Provenance(
            source_kind="capture",
            source_id="scan:synthetic-room",
            method="captured-room-json",
            confidence=0.9,
            derivation="inferred",
        ),
    )
    observed_provenance = (
        Provenance(
            source_kind="drawing",
            source_id="plan:synthetic-room",
            method="scaled-drawing",
            confidence=0.95,
            derivation="observed",
        ),
    )

    def wall(wall_id: str, start: tuple[float, float], end: tuple[float, float],
             thickness_m: float, provenance: tuple) -> Wall:
        return Wall(
            id=wall_id,
            level_id=level.id,
            centerline=Polyline3D(points=(
                Point3(x=start[0], y=start[1], z=0.0),
                Point3(x=end[0], y=end[1], z=0.0),
            )),
            thickness_m=thickness_m,
            height_m=2.7,
            provenance=provenance,
        )

    slab = Slab(
        id="slab:floor",
        level_id=level.id,
        footprint=Polyline3D(points=(
            Point3(x=0.0, y=0.0, z=0.0),
            Point3(x=6.0, y=0.0, z=0.0),
            Point3(x=6.0, y=4.0, z=0.0),
            Point3(x=0.0, y=4.0, z=0.0),
        )),
        thickness_m=0.1,
        provenance=observed_provenance,
    )
    return BuildingModel(
        model_id="model:display-thickness-evidence",
        levels=(level,),
        walls=(
            wall("wall:scan-south", (0.0, 0.0), (6.0, 0.0), SCAN_THICKNESS_M, scan_provenance),
            wall("wall:scan-west", (0.0, 0.0), (0.0, 4.0), SCAN_THICKNESS_M, scan_provenance),
            wall("wall:scan-east", (6.0, 0.0), (6.0, 4.0), SCAN_THICKNESS_M, scan_provenance),
            wall("wall:measured-north", (0.0, 4.0), (6.0, 4.0), 0.2, observed_provenance),
        ),
        slabs=(slab,),
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


def node_faces(gltf: dict, bin_chunk: bytes, node: dict) -> tuple[np.ndarray, np.ndarray]:
    """(triangles, colours) for one node: triangle soup plus its material RGBA.

    Line-mode nodes (glazed outlines) yield empty arrays; the evidence model
    has none, but the renderer stays honest about what it can draw.
    """

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


def render_view(
    gltf: dict,
    bin_chunk: bytes,
    size: tuple[int, int],
    focus: tuple[float, float, float] | None = None,
    extent_m: float | None = None,
) -> Image.Image:
    """Orthographic painter's-algorithm render of one GLB document.

    View direction is fixed (from +x, -y, +z down into the room). Faces sort
    far to near; translucent faces composite over what is behind them by
    their material alpha, so inferred geometry reads as translucent exactly
    as a real viewer would draw it. ``focus`` (a canonical-space point) with
    ``extent_m`` zooms the framing onto that point instead of fitting the
    whole model, so a corner crop can show a wall's drawn thickness.
    """

    width, height = size
    image = Image.new("RGB", size, _BACKGROUND)

    view_dir = np.array([0.7, -0.7, 2.8])
    view_dir /= np.linalg.norm(view_dir)
    up_world = np.array([0.0, 0.0, 1.0])
    right = np.cross(view_dir, up_world)
    right /= np.linalg.norm(right)
    up_cam = np.cross(right, view_dir)

    all_triangles: list[np.ndarray] = []
    all_colours: list[np.ndarray] = []
    for node in gltf["nodes"]:
        if "mesh" not in node:
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
    if focus is not None and extent_m is not None:
        centre = np.asarray(focus, dtype=np.float64)
        scale = (width - 40) / extent_m
        origin_x = width / 2.0 - scale * float(centre @ right)
        origin_y = height / 2.0 + scale * float(centre @ up_cam)
    else:
        min_x, max_x = screen_x.min(), screen_x.max()
        min_y, max_y = screen_y.min(), screen_y.max()
        scale = min(
            (width - 40) / (max_x - min_x),
            (height - 40) / (max_y - min_y),
        )
        origin_x = (width - scale * (max_x + min_x)) / 2.0
        origin_y = (height + scale * (max_y + min_y)) / 2.0

    tri_count = len(triangles)
    order = np.argsort(depth.reshape(tri_count, 3).mean(axis=1))
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
        # Composite each face over what is already drawn, so a translucent
        # inferred wall shows the room behind it exactly as a viewer would.
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).polygon(points, fill=fill)
        canvas = Image.alpha_composite(canvas, overlay)
    return canvas.convert("RGB")


def element_panel_lines(gltf: dict, node_name: str) -> list[str]:
    """The element-panel text for one node, from its actual exported extras.

    This is the disclosure the issue requires to be visible in the element
    panel, not a footnote: the recorded thickness, the drawn thickness when
    they differ, and where the drawn value came from.
    """

    node = next(item for item in gltf["nodes"] if item["name"] == node_name)
    extras = node.get("extras", {})
    lines = [node_name]
    for key in sorted(extras):
        lines.append(f"  {key}  {extras[key]}")
    return lines


def render_evidence(output_path: str | Path) -> dict[str, object]:
    """Export the synthetic room both ways, render and annotate, and report.

    Returns the summary dict the test asserts on: both export summaries, the
    element panel lines read back from the thickened GLB, and the image path.
    The image layout is fixed, so the same inputs render the same bytes.
    """

    model = synthetic_scan_room()
    scale = float(os.environ.get("OABM_EVIDENCE_SCALE", "1") or 1)
    work_dir = Path(output_path).resolve().parent
    work_dir.mkdir(parents=True, exist_ok=True)

    faithful_path = work_dir / "display-thickness-faithful.glb"
    thickened_path = work_dir / "display-thickness-drawn.glb"
    faithful = to_glb(model, faithful_path)
    drawn = to_glb(
        model,
        thickened_path,
        min_display_wall_thickness_m=MIN_DISPLAY_THICKNESS_M,
    )

    faithful_gltf, faithful_bin = parse_glb(faithful_path)
    drawn_gltf, drawn_bin = parse_glb(thickened_path)

    view_size = (int(560 * scale), int(420 * scale))
    # Row 1: the whole room. Row 2: a 2.4 m top-down crop at the south-west
    # corner, where a recorded 0.001 m wall is a hairline and the drawn wall
    # is a band — the difference the issue is about, at the scale it lives at.
    corner_focus = (0.9, 0.9, 0.6)
    corner_extent = 2.4
    views = [
        ("Recorded faithfully: three scanned walls at 0.001 m",
         render_view(faithful_gltf, faithful_bin, view_size)),
        (f"Drawn at min_display_wall_thickness_m = {MIN_DISPLAY_THICKNESS_M}",
         render_view(drawn_gltf, drawn_bin, view_size)),
        ("Faithful, zoomed to the south-west corner",
         render_view(faithful_gltf, faithful_bin, view_size, corner_focus, corner_extent)),
        ("Drawn, same corner: the scanned wall reads as a wall",
         render_view(drawn_gltf, drawn_bin, view_size, corner_focus, corner_extent)),
    ]

    panel_lines = element_panel_lines(drawn_gltf, "wall:scan-south")
    panel_line_height = int(18 * scale)
    panel_height = panel_line_height * (len(panel_lines) + 3)
    title_height = int(22 * scale)
    row_height = view_size[1] + title_height + int(8 * scale)
    width = view_size[0] * 2 + int(40 * scale)
    height = row_height * 2 + panel_height + int(20 * scale)
    image = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    for index, (title, view) in enumerate(views):
        column, row = index % 2, index // 2
        x = int(10 * scale) + column * (view_size[0] + int(20 * scale))
        y = int(4 * scale) + row * row_height
        draw.text((x + int(4 * scale), y), title, fill=_PANEL_TITLE_COLOUR)
        image.paste(view, (x, y + title_height))

    panel_top = row_height * 2 + int(12 * scale)
    draw.text(
        (int(14 * scale), panel_top),
        "Element panel: wall:scan-south (extras read back from the exported GLB)",
        fill=_PANEL_TITLE_COLOUR,
    )
    for index, line in enumerate(panel_lines, start=1):
        key, _, value = line.partition("  ")
        if value:
            draw.text(
                (int(20 * scale), panel_top + index * panel_line_height),
                key,
                fill=_PANEL_KEY_COLOUR,
            )
            draw.text(
                (int(280 * scale), panel_top + index * panel_line_height),
                value,
                fill=_PANEL_VALUE_COLOUR,
            )
        else:
            draw.text(
                (int(20 * scale), panel_top + index * panel_line_height),
                key,
                fill=_PANEL_TITLE_COLOUR,
            )

    target = Path(output_path)
    image.save(target, format="PNG")
    return {
        "faithful": faithful,
        "drawn": drawn,
        "panel_lines": panel_lines,
        "image": str(target),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    report = render_evidence(argv[1])
    for line in report["panel_lines"]:
        print(line)
    print(f"written: {report['image']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
