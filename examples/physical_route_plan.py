"""Connected synthetic detour, 3D preview, schedule and explicit XY review."""
from pathlib import Path
import sys

from physical_route_schedule import create_example
from oabm.drawings.physical_routes import main as draw_plan


def create_plan_example(output):
    output=Path(output)
    output.mkdir(exist_ok=False)
    create_example(output/'route')
    draw_plan([str(output/'route'/'model.json'),str(output/'drawing'),'--scale','20'])


if __name__=='__main__':
    if len(sys.argv)!=2:
        raise SystemExit('Usage: python examples/physical_route_plan.py FRESH_OUTPUT_DIRECTORY')
    create_plan_example(sys.argv[1])
