"""Synthetic before/after label views. No renderer or external service is used.

Run: PYTHONPATH=src python examples/view_label_layout.py /tmp/view-label-demo
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path

from oabm.drawings import (Bounds2, PlanSpec, generate_drawing_set, layout_view_labels,
                          paper_text_height_m, view_to_svg)
from oabm.model import BuildingModel, Point3, Pose

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination",type=Path)
args=parser.parse_args()
args.destination.mkdir(parents=True)
fixture=Path(__file__).resolve().parents[1]/"fixtures/model_delivery/v1/model.json"
model=BuildingModel.load(fixture)
base=model.electrical_equipment[0]
model=replace(model,electrical_equipment=(replace(base,name="Panel A"),
    replace(base,id="equipment:secondary",name="Panel B",pose=Pose(position=Point3(x=.3,y=.1,z=0))),
    replace(base,id="equipment:long",name="Synthetic long equipment label "*8,
            pose=Pose(position=Point3(x=1,y=1,z=0)))))
model.dump(args.destination/"synthetic-model.json")
spec=PlanSpec(id="plan:labels",level_id="level:synthetic",scale=40,
              bounds=Bounds2(min_x=-1,min_y=-1,max_x=4,max_y=3))
before=generate_drawing_set(model,plans=(spec,),include_schedules=False)
height=paper_text_height_m(spec.scale)
after=replace(before,views=tuple(layout_view_labels(v,pixels_per_model_unit=100,
                            text_height_m=height) for v in before.views))
for name,drawings in (("before",before),("after",after)):
    (args.destination/f"{name}.json").write_text(drawings.to_json()+"\n")
    (args.destination/f"{name}.svg").write_text(view_to_svg(drawings.views[0],
                                    pixels_per_model_unit=100,text_height_m=height))
metadata=dict(after.views[0].metadata)
assert metadata["relocated_labels"]=="2"
assert metadata["unplaced_labels_count"]=="1"
report={"relocated_labels":int(metadata["relocated_labels"]),
        "unplaced_labels":json.loads(metadata["unplaced_labels"]),
        "limitations":"Conservative bounded SVG view labels, not a permit-sheet layout or measured font fit."}
(args.destination/"layout-report.json").write_text(json.dumps(report,sort_keys=True,indent=2)+"\n")
print(f"outputs: {args.destination}/before.svg, after.svg, layout-report.json")
print("relocated labels: 2; unplaced labels: 1, full text retained in metadata")
