"""Generate only public synthetic architecture SVG, for independent renderer QA.

Run with PYTHONPATH=src python examples/physical_sheet_svg.py /new/output/path.
No network, private input, overwrite, or project publication occurs.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from oabm.drawings import (
    Bounds2, ElevationSpec, PlanSpec, SheetSpec, ViewPlacement, VisibilityPolicy,
    compact_label_provider, generate_drawing_set,
)
from oabm.drawings.sheet_svg import render_sheet_svg
from oabm.model import BuildingModel, Point3, Vector3, Space


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    model = BuildingModel.load(root / 'fixtures/drawings/v1/drawing-model.json')
    visibility = VisibilityPolicy(electrical=False, routes=False)
    drawings = generate_drawing_set(
        model,
        plans=(PlanSpec(id='plan-ground', level_id='level:ground', bounds=Bounds2(-1, -1, 8, 8),
                        scale=50, visibility=visibility),),
        elevations=(ElevationSpec(id='elevation-south', origin=Point3(x=0, y=0, z=0), direction=Vector3(x=0, y=1, z=0),
                                  near_m=-1, far_m=8, bounds=Bounds2(-8, -1, 1, 8),
                                  scale=50, visibility=visibility),),
        include_schedules=False, symbol_provider=lambda entity, view_type: None,
        label_provider=lambda entity, view_type: compact_label_provider(entity, view_type) if isinstance(entity, Space) else None,
        dimension_decimals=2,
        label_overlap='skip_with_dimensions', dimension_overlap='skip_shorter',
    )
    spec = SheetSpec('DEMO-1', 420, 297, titleblock_height_mm=35,
                     title='Synthetic architecture review', revision='A',
                     titleblock_fields=(('views', 'Left: ground plan. Right: south elevation. Scale 1:50.'),
                                        ('status', 'SYNTHETIC DEMONSTRATION - NOT FOR CONSTRUCTION')))
    output = render_sheet_svg(drawings, spec, (ViewPlacement('left', 'plan-ground', 15, 20),
                                              ViewPlacement('right', 'elevation-south', 220, 20)))
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'sheet.svg').write_text(output.svg, encoding='utf-8')
    (args.output / 'sheet-manifest.json').write_text(output.manifest_json + '\n', encoding='utf-8')
    (args.output / 'drawing-set.json').write_text(drawings.to_json() + '\n', encoding='utf-8')
    print(args.output / 'sheet.svg')


if __name__ == '__main__':
    main()
