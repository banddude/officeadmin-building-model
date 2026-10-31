"""Read-only bend preflight for canonical sharp-vertex route centerlines.

Radii are explicit caller design inputs, never inferred from nominal diameter.
No geometry, connectivity, catalog decisions or engineering approvals are changed.
"""
from __future__ import annotations

from oabm.model import reject_physical_routes
import argparse
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from typing import Mapping

from oabm.model import BuildingModel, ContractError, validate_model


@dataclass(frozen=True)
class BendDiagnostic:
    code: str
    route_id: str
    index: int  # vertex index, or segment start index for short_straight
    fitting_ids: tuple[str, ...] = ()
    available_m: float | None = None
    required_m: float | None = None


@dataclass(frozen=True)
class BendCheck:
    route_id: str
    vertex_index: int
    angle_radians: float
    radius_m: float | None
    tangent_setback_m: float | None


@dataclass(frozen=True)
class BendReport:
    bends: tuple[BendCheck, ...]
    diagnostics: tuple[BendDiagnostic, ...]
    tolerance_m: float
    angle_tolerance_rad: float

    @property
    def ready(self) -> bool:
        """Only the bounded geometric checks passed; not engineering approval."""
        return not self.diagnostics

    def to_json(self) -> str:
        return json.dumps({"version": "route-bend-preflight-v1", "ready": self.ready,
                           **asdict(self)}, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _coords(p):
    return p.x, p.y, p.z


def _distance(a, b):
    return math.dist(_coords(a), _coords(b))


def check_route_bends(model: BuildingModel, *, radii_m: Mapping[str, float],
                      tolerance_m: float = 1e-6, angle_tolerance_rad: float = 1e-6) -> BendReport:
    """Check elbow inventory and tangent-space budgets on every canonical route.

    Each route radius is a caller-supplied *centerline* radius in metres. A
    missing radius remains unresolved for routes with turns. At turn angle a,
    a circular fillet consumes r*tan(a/2) on EACH neighboring straight. Both
    ends of each shared straight are charged, including non-axis-aligned 3D
    turns. This necessary condition does not prove swept clearance or fit.
    Collinear redundant vertices are coalesced for length budgets only; report
    indices always refer to original canonical vertices. Fitting matches use
    canonical vertex poses, as emitted by the router, not tangent endpoints.
    Supports interior turns within each route only. A valid consolidation
    junction elbow at a straight trunk endpoint reports elbow_off_turn; its
    cross-route bend budget is unsupported, not evidence of invalid topology.
    """
    reject_physical_routes(model, "check_route_bends")
    validate_model(model)
    for value in (tolerance_m, angle_tolerance_rad):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ContractError("bend preflight tolerances must be finite and positive")
    if angle_tolerance_rad >= math.pi / 2:
        raise ContractError("bend preflight angle tolerance must be less than pi/2")
    if not isinstance(radii_m, Mapping):
        raise ContractError("bend preflight radii must be a mapping")
    route_ids = {r.id for r in model.routes}
    if set(radii_m) - route_ids:
        raise ContractError("bend preflight radius references an unknown route")
    for radius in radii_m.values():
        if type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0:
            raise ContractError("bend preflight radii must be finite and positive")
    fittings = {f.id: f for f in model.route_fittings}
    bends, diagnostics = [], []
    for route in sorted(model.routes, key=lambda r: r.id):
        points = route.centerline.points
        turns = []
        matched_at = {}
        for i in range(1, len(points) - 1):
            u = tuple(b-a for a, b in zip(_coords(points[i-1]), _coords(points[i])))
            v = tuple(b-a for a, b in zip(_coords(points[i]), _coords(points[i+1])))
            lu, lv = math.hypot(*u), math.hypot(*v)
            if not all(math.isfinite(length) and length > 0 for length in (lu, lv)):
                raise ContractError("bend preflight segment length overflow or underflow")
            # atan2 is stable for small angles, unlike acos(dot).
            u, v = tuple(a/lu for a in u), tuple(a/lv for a in v)
            cross = (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
            angle = math.atan2(math.hypot(*cross), sum(a*b for a,b in zip(u,v)))
            if angle <= angle_tolerance_rad:
                continue
            radius = radii_m.get(route.id)
            reverse = math.pi - angle <= angle_tolerance_rad
            setback = None if radius is None or reverse else radius * math.tan(angle / 2)
            if setback is not None and not math.isfinite(setback):
                raise ContractError("bend preflight tangent setback overflow")
            bends.append(BendCheck(route.id, i, angle, radius, setback))
            turns.append((i, setback))
            if reverse:
                diagnostics.append(BendDiagnostic("route_reversal", route.id, i))
            if radius is None:
                diagnostics.append(BendDiagnostic("radius_unknown", route.id, i))
            matches = sorted((fittings[fid] for fid in route.fitting_ids
                              if fittings[fid].fitting_type.startswith("elbow")
                              and _distance(fittings[fid].pose.position, points[i]) <= tolerance_m), key=lambda f:f.id)
            ids = tuple(f.id for f in matches)
            for fid in ids:
                if fid in matched_at:
                    diagnostics.append(BendDiagnostic("elbow_reused", route.id, i, (fid,)))
                matched_at[fid] = i
            if len(matches) != 1:
                diagnostics.append(BendDiagnostic("elbow_missing" if not matches else "elbow_ambiguous", route.id, i, ids))
            elif matches[0].angle_radians is None or abs(matches[0].angle_radians-angle) > angle_tolerance_rad:
                diagnostics.append(BendDiagnostic("elbow_angle_mismatch", route.id, i, ids))
            if len(matches) == 1:
                named_angle = {"elbow-90": math.pi / 2, "elbow-45": math.pi / 4}.get(matches[0].fitting_type)
                if named_angle is not None and abs(named_angle - angle) > angle_tolerance_rad:
                    diagnostics.append(BendDiagnostic("elbow_type_mismatch", route.id, i, ids))
        turn_indices = {i for i, _ in turns}
        for fid in sorted(route.fitting_ids):
            f = fittings[fid]
            if f.fitting_type.startswith("elbow") and not any(_distance(f.pose.position, points[i]) <= tolerance_m for i in turn_indices):
                diagnostics.append(BendDiagnostic("elbow_off_turn", route.id, -1, (fid,)))
        # Merge collinear pieces into one straight budget without editing model.
        boundaries = [(0, 0.0), *turns, (len(points)-1, 0.0)]
        for (a, left), (b, right) in zip(boundaries, boundaries[1:]):
            if left is None or right is None:
                continue
            available = sum(_distance(points[i], points[i+1]) for i in range(a, b))
            if not math.isfinite(available) or not math.isfinite(left + right):
                raise ContractError("bend preflight straight budget overflow")
            if left + right > available + tolerance_m:
                diagnostics.append(BendDiagnostic("short_straight", route.id, a,
                                                  available_m=available, required_m=left + right))
    return BendReport(tuple(bends), tuple(sorted(diagnostics, key=lambda d:(d.route_id,d.index,d.code,d.fitting_ids))),
                      tolerance_m, angle_tolerance_rad)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("radii", type=Path, help="JSON object mapping canonical route IDs to centerline radius in metres")
    args = parser.parse_args(argv)
    try:
        radii = json.loads(args.radii.read_text())
        if not isinstance(radii, dict):
            raise ContractError("radii must be a JSON object")
        report = check_route_bends(BuildingModel.load(args.model), radii_m=radii)
    except (ContractError, OSError, ValueError) as exc:
        parser.exit(2, f"bend preflight: {exc}\n")
    print(report.to_json(), end="")
    return 0 if report.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
