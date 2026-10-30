"""Synthetic two-panel/multipage schedule production; no engineering decisions."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path

from oabm.model import BuildingModel, Provenance
from oabm.drawings.schedule_set import VERSION, create_panel_schedule_sheet_set

ROOT = Path(__file__).resolve().parents[1]


def synthetic_two_panel_model(count=90):
    """Extend only public synthetic facts; explicit zero, unknowns and conflict."""
    base = BuildingModel.load(ROOT / 'fixtures/model/v1/garage-route.json')
    panel = base.electrical_equipment[0]
    source = next(p for p in base.ports if p.owner_id == panel.id)
    second_panel = replace(panel, id='equipment:synthetic-panel-b', name='Synthetic panel B')
    second_source = replace(source, id='port:synthetic-panel-b-source', owner_id=second_panel.id)
    supplied = (Provenance(source_kind='synthetic', source_id='fixture:schedule-sheet-production', derivation='user'),)
    original = base.circuits[0]
    circuits = tuple(replace(original, id=f'circuit:synthetic-{i:03d}',
        name=f'Synthetic supplied circuit {i}', source_port_id=source.id if i < count - 3 else second_source.id,
        circuit_number='duplicate supplied label' if i in (0, 1) else str(i + 1),
        load_va=None if i == 1 else float(i * 10), voltage_v=None if i == 1 else 120.0,
        poles=None if i == 1 else 1, phase=None if i == 1 else 'A',
        route_ids=(), provenance=supplied) for i in range(count))
    return replace(base, model_id='model:synthetic-schedule-sheets',
        electrical_equipment=(panel, second_panel), ports=(*base.ports, second_source),
        circuits=circuits, routes=(), route_fittings=(), conductors=())


def write_example_inputs(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'model.json').write_text(synthetic_two_panel_model().to_json() + '\n')
    (directory / 'settings.json').write_text(json.dumps({'version': VERSION, 'layout': {
        'text_height_mm': 2.5, 'line_height_mm': 4.0, 'padding_mm': 1.5, 'packing': 'compact'}}, indent=2) + '\n')
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path, help='shared company-sheet profile JSON')
    parser.add_argument('job', type=Path, help='shared job JSON with exactly one schedule template sheet')
    parser.add_argument('directory', type=Path, help='fresh synthetic demonstration directory')
    args = parser.parse_args()
    if args.directory.exists():
        parser.error('directory must be fresh')
    inputs = write_example_inputs(args.directory / 'inputs')
    print(create_panel_schedule_sheet_set(inputs / 'model.json', args.profile, args.job,
        inputs / 'settings.json', output_directory=args.directory / 'output'))


if __name__ == '__main__':
    main()
