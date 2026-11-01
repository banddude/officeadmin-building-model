"""Synthetic supplied endpoint edit -> fresh generation -> comparison + scaled plans."""
from pathlib import Path
import sys

from physical_route_schedule import create_example
from oabm.drawings.physical_routes import generate_physical_route_plan
from oabm.model import BuildingModel
from oabm.reports.physical_route_revisions import main as compare, validate_physical_route_comparison


def create_comparison_example(output):
    output = Path(output)
    output.mkdir(exist_ok=False)
    for label, endpoint_x in (('before', 4), ('after', 4.5)):
        create_example(output / label, endpoint_x=endpoint_x)
        model = BuildingModel.from_json((output / label / 'model.json').read_text())
        plan = generate_physical_route_plan(model, scale=20)
        # Label the actual adapter output; keep its mm dimensions/viewBox and
        # supplied 1:20 scale. Never merge snapshots into invented geometry.
        svg = plan.svg.replace('Source-bound physical route XY review',
                               f'{label.upper()} snapshot: source-bound physical route XY review')
        svg = svg.replace('PHYSICAL ROUTE / XY REVIEW', f'{label.upper()} / PHYSICAL ROUTE XY REVIEW')
        (output / label / 'plan.svg').write_text(svg)
    compare([str(output / 'before' / 'model.json'), str(output / 'after' / 'model.json'), str(output / 'comparison')])
    result = validate_physical_route_comparison((output / 'comparison' / 'comparison.json').read_text())
    (output / 'README.txt').write_text('SYNTHETIC ONLY. Before endpoint X=4 m; after endpoint X=4.5 m. Both routes freshly generated from retained source.json/request hashes. Plans are separate actual adapter outputs at 1:20. No current engineering or code approval.\n')
    return result


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python examples/physical_route_revisions.py FRESH_OUTPUT_DIRECTORY')
    create_comparison_example(sys.argv[1])
