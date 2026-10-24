"""Independent synthetic model -> drawing -> paper-space DXF QA example.

Run from repository root: python spikes/physical_sheet_dxf/demo.py /tmp/cad-demo
The optional --preview uses ezdxf's SVG backend (no browser or CAD application).
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import io
from pathlib import Path

from oabm.drawings import Bounds2, PlanSpec, SheetSpec, ViewPlacement, generate_drawing_set
from oabm.drawings.sheet_dxf import sheet_to_dxf
from oabm.model import BuildingModel, Level, Point3, Polyline3D, Provenance, Wall


def synthetic_model():
    provenance = (Provenance(source_kind="synthetic", source_id="physical-dxf-demo", method="independently authored fixture"),)
    return BuildingModel(model_id="synthetic-dxf-demo", provenance=provenance,
        levels=(Level(id="ground", name="Ground", elevation_m=0, height_m=3, provenance=provenance),),
        walls=tuple(Wall(id=identity, level_id="ground", centerline=Polyline3D(points=(Point3(x=a[0], y=a[1], z=0), Point3(x=b[0], y=b[1], z=0))), thickness_m=.2, height_m=3, provenance=provenance)
                    for identity, a, b in (("south", (0, 0), (6, 0)), ("east", (6, 0), (6, 4)), ("north", (6, 4), (0, 4)), ("west", (0, 4), (0, 0)))))


def make_demo(model=None):
    model = synthetic_model() if model is None else model
    drawings = generate_drawing_set(model, plans=(PlanSpec(id="plan", level_id="ground", bounds=Bounds2(-1, -1, 7, 5), scale=50),), dimension_decimals=2)
    # Explicit presentation spacing for this tiny synthetic proof: move only
    # dimension offset lines, retaining canonical endpoints/values/source IDs.
    # At 1:50, 0.7 model metres reserves 14 paper millimetres for labels.
    drawings = replace(drawings, views=tuple(replace(view, dimensions=tuple(replace(d, offset_m=.7) for d in view.dimensions)) for view in drawings.views))
    spec = SheetSpec("S-1", 297, 210, titleblock_height_mm=35, title="Synthetic wall plan", revision="A", titleblock_fields=(("Scale", "1:50"),))
    return sheet_to_dxf(drawings, spec, (ViewPlacement("main", "plan", 20, 20),))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    artifact = make_demo()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "sheet.dxf").write_bytes(artifact.dxf)
    (args.output / "sheet.manifest.json").write_text(artifact.manifest_json, encoding="utf-8")
    if args.preview:
        import ezdxf
        from ezdxf.addons.drawing import Frontend, RenderContext, config, layout, svg
        from ezdxf.math import BoundingBox2d
        doc = ezdxf.read(io.StringIO(artifact.dxf.decode("utf-8")))
        backend = svg.SVGBackend()
        Frontend(RenderContext(doc), backend, config=config.Configuration(background_policy=config.BackgroundPolicy.WHITE)).draw_layout(doc.layouts.get("Layout1"), finalize=True)
        preview = backend.get_string(layout.Page(297, 210), settings=layout.Settings(fit_page=False, scale=1, max_stroke_width=0.01), render_box=BoundingBox2d([(0, 0), (297, 210)]))
        (args.output / "sheet.readback.svg").write_text(preview, encoding="utf-8")
    print(f"Wrote synthetic CAD artifacts to {args.output}")


if __name__ == "__main__":
    main()
