"""Source-bound geometric route schedules, not a material or code takeoff."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path

from oabm.model import BuildingModel, ContractError, PhysicalRoute, physical_segment_facts
from oabm.routing.physical_capture import capture, bounded_source_json, SOURCE_BYTES
from oabm.routing.physical_verify_types import VerificationStop

NANO = 1_000_000_000
KIND = "physical_route_schedule"
VERSION = "1.0.0"
BINDING = "normalized-canonical-model-json-v1"
LIMITATIONS = (
    "Geometric centerline measurement only; not a complete model quantity takeoff.",
    "Quarter-bend counts are geometry, not purchased elbow or fitting counts.",
    "No stock lengths, cut allowances, slack, couplings, supports, waste, pricing or code approval are inferred.",
    "Live route clearance and source authenticity are not verified by this report.",
    "Existing quantity and review-bundle consumer limitations remain unchanged.",
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _snapshot(model):
    if type(model) is not BuildingModel:
        raise ContractError("physical route schedule requires a canonical BuildingModel")
    try:
        detached = capture(model, byte_limit=SOURCE_BYTES)
        canonical = BuildingModel.from_json(detached.to_json(indent=None))
        text = canonical.to_json(indent=None)
        bounded_source_json(text)
        return canonical, text
    except VerificationStop as exc:
        raise ContractError(f"physical route schedule {exc.status}: {exc.reason}") from exc
    except (OverflowError, TypeError, AttributeError, KeyError) as exc:
        raise ContractError("malformed physical route schedule source") from exc


def _length(straight_ticks, pi_coefficient_ticks):
    coefficient = Fraction(pi_coefficient_ticks)
    return {
        "straight_ticks": straight_ticks,
        "pi_coefficient_ticks": {"numerator": coefficient.numerator, "denominator": coefficient.denominator},
        "straight_m": straight_ticks / NANO,
        "curved_m_approx": float(coefficient / NANO) * math.pi,
        "total_m_approx": math.fsum((straight_ticks / NANO, float(coefficient / NANO) * math.pi)),
    }


def _text_cell(value):
    text = "" if value is None else str(value)
    # CSV quoting does not prevent spreadsheet formula execution. Authoritative
    # JSON remains unchanged; dangerous text cells get a presentation apostrophe.
    text = "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else
        f"\\x{ord(char):02x}" if ord(char) < 32 and char not in "\t\r\n" else char
        for char in text
    )
    offset = 0
    while offset < len(text) and (text[offset].isspace() or text[offset] == "\ufeff"):
        offset += 1
    visible = text[offset:]
    if (visible and visible[0] in "=+-@") or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _metres(value):
    return "" if value is None else format(value, ".9f")


@dataclass(frozen=True)
class PhysicalRouteSchedule:
    _source_model_json: str
    _report_json: str

    @property
    def source_model_json(self):
        return self._source_model_json

    def to_dict(self):
        return json.loads(self._report_json)

    def to_json(self, *, indent=None):
        if indent is None:
            return self._report_json
        if type(indent) is not int or indent not in (0, 2, 4):
            raise ContractError("report indentation must be None, 0, 2 or 4")
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, ensure_ascii=True, allow_nan=False)

    def validate_against_model(self, model):
        expected = generate_physical_route_schedule(model)
        if self._report_json != expected._report_json or self._source_model_json != expected._source_model_json:
            raise ContractError("physical route schedule is stale or its measurements were changed")

    def routes_csv(self):
        document = self.to_dict()
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("status", "route_id", "route_name", "route_type", "start_port_id", "end_port_id",
                         "actual_outside_diameter_m", "centerline_radius_m", "straight_centerline_m",
                         "curved_centerline_m_approx", "total_centerline_m_approx", "quarter_bend_count",
                         "absolute_turn_degrees", "source_model_sha256"))
        for row in document["routes"]:
            writer.writerow(("measured_physical_route", *(_text_cell(row[key]) for key in
                ("route_id", "route_name", "route_type", "start_port_id", "end_port_id")),
                _metres(row["outside_diameter_m"]), _metres(row["centerline_radius_m"]),
                _metres(row["length"]["straight_m"]), _metres(row["length"]["curved_m_approx"]),
                _metres(row["length"]["total_m_approx"]), row["quarter_bend_count"],
                row["absolute_turn_degrees"], document["source"]["sha256"]))
        for row in document["excluded_legacy_routes"]:
            writer.writerow(("excluded_legacy_route", *(_text_cell(row[key]) for key in
                ("route_id", "route_name", "route_type", "start_port_id", "end_port_id")),
                "", "", "", "", "", "", "", document["source"]["sha256"]))
        return output.getvalue()

    def segments_csv(self):
        document = self.to_dict()
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("route_id", "segment_index", "segment_id", "kind", "start_joint_id", "end_joint_id",
                         "straight_ticks", "pi_coefficient_ticks_numerator", "pi_coefficient_ticks_denominator",
                         "centerline_length_m_approx", "absolute_turn_degrees", "source_model_sha256"))
        for row in document["segments"]:
            length = row["length"]
            writer.writerow((_text_cell(row["route_id"]), row["segment_index"], *(_text_cell(row[key]) for key in
                ("segment_id", "kind", "start_joint_id", "end_joint_id")), length["straight_ticks"],
                length["pi_coefficient_ticks"]["numerator"], length["pi_coefficient_ticks"]["denominator"],
                _metres(length["total_m_approx"]), row["absolute_turn_degrees"], document["source"]["sha256"]))
        return output.getvalue()


def generate_physical_route_schedule(model):
    """Measure all physical routes, explicitly excluding every legacy route."""
    model, source_json = _snapshot(model)
    source = model.to_dict()
    source_routes = {row["id"]: row for row in source["routes"]}
    routes, segments, excluded = [], [], []
    total_straight, total_pi, total_bends = 0, Fraction(0), 0
    for route in sorted(model.routes, key=lambda item: item.id):
        identity = {"route_id": route.id, "route_name": route.name, "route_type": route.route_type,
                    "start_port_id": route.start_port_id, "end_port_id": route.end_port_id}
        if not isinstance(route, PhysicalRoute):
            excluded.append(identity)
            continue
        retained = source_routes[route.id]
        joints = {row["id"]: row for row in retained["joints"]}
        straight, coefficient, bends = 0, Fraction(0), 0
        for index, (segment, fact, original) in enumerate(zip(route.segments, physical_segment_facts(route), retained["segments"])):
            line_ticks = fact["length_ticks"] if segment.kind == "line" else 0
            arc_ticks = Fraction(fact["radius_ticks"], 2) if segment.kind == "quarter_arc" else Fraction(0)
            turn = 90 if segment.kind == "quarter_arc" else 0
            segments.append({"route_id": route.id, "segment_index": index, "segment_id": segment.id,
                "kind": segment.kind, "start_joint_id": segment.start_joint_id, "end_joint_id": segment.end_joint_id,
                "source_segment": original, "source_start_joint": joints[segment.start_joint_id],
                "source_end_joint": joints[segment.end_joint_id], "length": _length(line_ticks, arc_ticks),
                "absolute_turn_degrees": turn})
            straight += line_ticks; coefficient += arc_ticks; bends += int(turn != 0)
        routes.append({**identity, "geometry_kind": route.geometry_kind, "requirements_id": route.requirements.id,
            "outside_diameter_m": route.requirements.outside_diameter_m,
            "centerline_radius_m": route.requirements.centerline_radius_m,
            "source_requirements": retained["requirements"], "source_provenance": retained["provenance"],
            "generation_request_sha256": route.context.request_sha256,
            "length": _length(straight, coefficient), "quarter_bend_count": bends,
            "absolute_turn_degrees": 90*bends, "segment_count": len(route.segments)})
        total_straight += straight; total_pi += coefficient; total_bends += bends
    document = {"kind": KIND, "version": VERSION,
        "source": {"binding_profile": BINDING, "sha256": hashlib.sha256(source_json.encode("utf-8")).hexdigest(),
                   "model_id": model.model_id, "schema_version": model.schema_version,
                   "coordinate_frame_id": model.coordinate_system.frame_id},
        "scope": {"measured_physical_route_ids": [row["route_id"] for row in routes],
                  "excluded_legacy_route_ids": [row["route_id"] for row in excluded],
                  "other_model_entities_measured": False},
        "routes": routes, "segments": segments, "excluded_legacy_routes": excluded,
        "totals": {"measured_route_count": len(routes), "excluded_legacy_route_count": len(excluded),
                   "length": _length(total_straight, total_pi), "quarter_bend_count": total_bends,
                   "absolute_turn_degrees": total_bends*90},
        "live_clearance_verification_performed": False, "limitations": list(LIMITATIONS),
        "csv_text_policy": "Formula-like text is apostrophe-prefixed; unsupported controls/surrogates are escaped. Authoritative JSON preserves source text."}
    text = _json(document)
    try:
        bounded_source_json(text)
    except VerificationStop as exc:
        raise ContractError(f"physical route schedule output {exc.status}: {exc.reason}") from exc
    return PhysicalRouteSchedule(source_json, text)


def validate_physical_route_schedule(payload, model):
    """Regenerate measurements; a matching saved source hash alone is insufficient."""
    try:
        text = bounded_source_json(payload)
        def unique(rows):
            result = {}
            for key, value in rows:
                if key in result:
                    raise ContractError("duplicate report JSON key")
                result[key] = value
            return result
        actual = json.loads(text, object_pairs_hook=unique)
        expected = generate_physical_route_schedule(model)
        if _json(actual) != expected.to_json():
            raise ContractError("physical route schedule is stale or its measurements were changed")
        return expected
    except VerificationStop as exc:
        raise ContractError(f"physical route schedule input {exc.status}: {exc.reason}") from exc
    except (ValueError, TypeError, OverflowError) as exc:
        raise ContractError("invalid physical route schedule JSON") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write a geometry-only physical route schedule from canonical JSON.")
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path, help="Fresh output directory; existing paths are never overwritten.")
    args = parser.parse_args(argv)
    try:
        with args.model.open("rb") as handle:
            raw = handle.read(SOURCE_BYTES+1)
        text = bounded_source_json(raw)
        report = generate_physical_route_schedule(BuildingModel.from_json(text))
        contents = {"model.json": report.source_model_json, "report.json": report.to_json(),
                    "routes.csv": report.routes_csv(), "segments.csv": report.segments_csv()}
        prepared = {name: content.encode("utf-8") for name, content in contents.items()}
        args.output.mkdir(parents=False, exist_ok=False)
        for name, content in prepared.items():
            (args.output/name).write_bytes(content)
        print(_json({"output_directory": str(args.output), "source_model_sha256": report.to_dict()["source"]["sha256"],
                     "measured_route_count": report.to_dict()["totals"]["measured_route_count"],
                     "excluded_legacy_route_count": report.to_dict()["totals"]["excluded_legacy_route_count"]}))
        return 0
    except (ContractError, VerificationStop, OSError, ValueError) as exc:
        parser.exit(2, f"physical route schedule: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
