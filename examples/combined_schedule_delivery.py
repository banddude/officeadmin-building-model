"""Fictional building schedule delivery and explicit source-edit regeneration."""
from dataclasses import replace
import argparse
import json
from pathlib import Path

from oabm.model import (BuildingModel, Opening, Pose, Point3, Size3, CatalogProduct,
                        ElectricalBox, BoxSelectionOutcome, upgrade_to_v1_1)
from oabm.drawings.schedule_set import COMBINED_VERSION, create_combined_schedule_sheet_set

ROOT = Path(__file__).resolve().parents[1]


def synthetic_model():
    base = upgrade_to_v1_1(BuildingModel.load(ROOT / 'fixtures/model/v1/garage-route.json'))
    panel = base.electrical_equipment[0]
    port = next(p for p in base.ports if p.owner_id == panel.id)
    panel_b = replace(panel, id='equipment:panel-b', name='Fictional panel B')
    port_b = replace(port, id='port:panel-b', owner_id=panel_b.id)
    device = base.electrical_devices[0]
    device_b = replace(device, id='device:unresolved', name='Fictional unresolved device')
    opening = Opening(id='opening:door', host_id='wall:north', opening_type='door',
        pose=Pose(position=Point3(x=2, y=5, z=1)), size=Size3(x=.9, y=.14, z=2),
        provenance=device.provenance)
    circuit = base.circuits[0]
    circuits = tuple(replace(circuit, id=f'circuit:example-{n}', route_ids=(),
        source_port_id=port_b.id if n == 2 else port.id,
        circuit_number='same printed label' if n < 2 else 'B1',
        load_va=(1800.0, None, 600.0)[n]) for n in range(3))
    product = CatalogProduct(catalog_id='fictional', catalog_revision='1', product_id='box')
    box = ElectricalBox(id='box:selected', intent_id='intent:selected', box_type='device_box',
        pose=device.pose, size=Size3(x=.4, y=.3, z=.6), level_id=device.level_id,
        host_id=device.host_id, mounting='surface', occupant_ids=(device.id,),
        product=product, provenance=device.provenance)
    outcomes = (BoxSelectionOutcome(id='outcome:selected', intent_id=box.intent_id,
        occupant_ids=(device.id,), status='selected', selected_box_id=box.id,
        requested_product=product, provenance=device.provenance),
        BoxSelectionOutcome(id='outcome:unresolved', intent_id='intent:unresolved',
        occupant_ids=(device_b.id,), status='unresolved', reason_codes=('missing_product',),
        provenance=device.provenance))
    return replace(base, model_id='model:fictional-combined-schedules',
        openings=(opening,), electrical_devices=(device, device_b),
        electrical_equipment=(panel, panel_b), ports=(*base.ports, port_b),
        circuits=circuits, routes=(), route_fittings=(), conductors=(),
        electrical_boxes=(box,), box_selection_outcomes=outcomes)


def revised_model(model):
    return replace(model,
        electrical_devices=(replace(model.electrical_devices[0], name='Revised fictional device'),
                            *model.electrical_devices[1:]),
        circuits=(replace(model.circuits[0], load_va=2400.0), *model.circuits[1:]))


def write_inputs(directory, model):
    directory.mkdir(parents=True)
    data = {
        'model': model.to_dict(),
        'profile': {'version': 'company-sheet-profile-v1',
                    'fields': {'company': 'FICTIONAL COMPANY'}, 'template': {}},
        'job': {'version': 'company-sheet-job-v1', 'set_id': 'fictional-combined',
                'date': '2026-11-03', 'revision': 'A', 'formats': ['svg', 'pdf'],
                'fields': {'project': 'FICTIONAL BUILDING', 'issue': 'REVIEW ONLY'},
                'sheets': [{'id': 'S6', 'title': 'INVENTORIES AND PANEL FACTS'}]},
        'settings': {'version': COMBINED_VERSION, 'layout': {'packing': 'compact'}}}
    for name, value in data.items():
        (directory / f'{name}.json').write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    return tuple(directory / f'{name}.json' for name in data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='fresh output directory')
    args = parser.parse_args()
    if args.directory.exists():
        parser.error('directory must be fresh')
    model = synthetic_model()
    for name, source in (('before', model), ('after', revised_model(model))):
        inputs = write_inputs(args.directory / name / 'inputs', source)
        print(create_combined_schedule_sheet_set(*inputs,
            output_directory=args.directory / name / 'output'))


if __name__ == '__main__':
    main()
