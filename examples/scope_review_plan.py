"""Five public synthetic scope classes, using supplied canonical poses only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

from oabm.model import (BuildingModel, ElectricalDevice, ElectricalEquipment, Level,
    Point3, Pose, Provenance, RuleReference, Size3)
from oabm.reports.scope_ledger import ScopePolicy
from oabm.pipeline.review_drawings import ReviewDrawingConfig, ReviewPlan, VERSION
from oabm.pipeline.scope_review_plan import generate_scope_review_plan, render_scope_review_artifacts


def synthetic_inputs():
    provenance = (Provenance(source_kind='synthetic', source_id='fixture:scope-plan',
                            method='explicit synthetic scope and geometry', derivation='user'),)
    statuses = ('new', 'existing_to_remain', 'removed', 'relocated', 'unresolved')
    devices = []
    for i, status in enumerate(statuses):
        lane = {'scope_status': status, 'source_page': 1}
        if status == 'unresolved':
            lane['scope_reason'] = 'scope_legend_conflict'
        devices.append(ElectricalDevice(id='device:'+status, device_type='receptacle',
            level_id='level:scope', pose=Pose(position=Point3(x=2+i*4, y=3, z=1)),
            size=Size3(x=.3, y=.3, z=.3), provenance=provenance,
            attributes={'pdf_electrical': lane}))
    # Include equipment in the same supported lane without adding another class.
    equipment = ElectricalEquipment(id='equipment:retained', equipment_type='panelboard',
        level_id='level:scope', pose=Pose(position=Point3(x=2, y=6, z=1)),
        size=Size3(x=.5, y=.3, z=1), provenance=provenance,
        attributes={'pdf_electrical': {'scope_status': 'existing_to_remain'}})
    model = BuildingModel(model_id='model:synthetic-scope-plan', provenance=provenance,
        levels=(Level(id='level:scope', elevation_m=0),),
        electrical_devices=tuple(devices), electrical_equipment=(equipment,))
    policy = ScopePolicy(version='1.0.0', mode='preserve_unresolved',
        basis=RuleReference(authority='synthetic reviewer', revision='1', rule_id='scope-preservation'),
        actor='synthetic reviewer', provenance=provenance)
    config = ReviewDrawingConfig(version=VERSION, pixels_per_model_unit=64,
        plans=(ReviewPlan(id='scope-plan', level_id='level:scope', bounds_m=(0, 0, 22, 8),
            cut_height_m=2, view_depth_below_m=0, view_depth_above_m=0, scale=50,
            show_architecture=True, show_spaces=False, show_routes=False, show_dimensions=False),))
    return model, config, policy


def create_example(output):
    model, config, policy = synthetic_inputs()
    source = model.to_json().encode()
    payloads, _ = generate_scope_review_plan(model, config, policy=policy)
    artifacts = {'model.json': source, 'policy.json': policy.to_json().encode(),
        'config.json': config.to_json().encode(), 'drawing-set.json': payloads['drawing_set'],
        'scope-ledger.json': payloads['scope_ledger'],
        'scope-plan.svg': payloads['review_plan:scope-plan'], 'scope-schedule.svg': payloads['scope_schedule']}
    for key, data in render_scope_review_artifacts(payloads).items():
        stem, kind = key.rsplit(':', 1)
        name = 'scope-plan' if stem.startswith('review_plan:') else 'scope-schedule'
        artifacts[name+'.'+kind] = data
    removed = next(d for d in model.electrical_devices if d.id == 'device:removed')
    retained = replace(removed, attributes={'pdf_electrical': {'scope_status': 'existing_to_remain'}})
    revised = replace(model, electrical_devices=tuple(retained if d.id == removed.id else d for d in model.electrical_devices))
    after, _ = generate_scope_review_plan(revised, config, policy=policy)
    artifacts['revised-model.json'] = revised.to_json().encode()
    artifacts['revised-scope-plan.svg'] = after['review_plan:scope-plan']
    artifacts['revised-scope-ledger.json'] = after['scope_ledger']
    artifacts['revised-scope-schedule.svg'] = after['scope_schedule']
    for key, data in render_scope_review_artifacts(after).items():
        stem, kind = key.rsplit(':', 1)
        name = 'revised-scope-plan' if stem.startswith('review_plan:') else 'revised-scope-schedule'
        artifacts[name+'.'+kind] = data
    assert model.to_json().encode() == source
    output = Path(output)
    output.mkdir(exist_ok=False)
    for name, data in artifacts.items():
        (output/name).write_bytes(data)
    (output/'SHA256SUMS.json').write_text(json.dumps({name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(artifacts.items())}, sort_keys=True, indent=2)+'\n')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python examples/scope_review_plan.py FRESH_OUTPUT_DIRECTORY')
    create_example(sys.argv[1])
