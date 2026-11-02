"""Synthetic four-edit as-built delivery. No customer data or engineering claim.

Run only when the coordinated execution window opens:
  python examples/as_built_quick_edit.py /absolute/fresh/output
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile

from oabm.model import (BuildingModel, CatalogProduct, ElectricalBox, Point3,
                        Provenance, RuleReference, Size3, upgrade_to_v1_2)
from oabm.electrical import (BoxCatalogEntry, BoxIntent, RacewayCatalogEntry,
                             RacewayIntent, RacewayRule, select_electrical_boxes,
                             select_raceways)
from oabm.pipeline.as_built import (VERSION, _sha, apply_transaction, preview_transaction,
                                   record_sha256, replay_history, snapshot, start_history)
from oabm.pipeline.as_built_delivery import create_as_built_delivery

ROOT = Path(__file__).resolve().parents[1]


def synthetic_inputs():
    model = upgrade_to_v1_2(BuildingModel.load(ROOT / "fixtures/model/v1/garage-route.json"))
    evse = model.electrical_devices[0]
    spare = replace(evse, id="device:spare", name="Synthetic unrelated device",
                    pose=replace(evse.pose, position=Point3(x=1, y=.2, z=1.2)))
    model = replace(model, electrical_devices=(*model.electrical_devices, spare))
    provenance = (Provenance(source_kind="synthetic", source_id="fixture:as-built",
                            derivation="user"),)
    box_product = CatalogProduct(catalog_id="synthetic", catalog_revision="1", product_id="box-A")
    model = select_electrical_boxes(model, intents=(BoxIntent(intent_id="intent:spare-box",
        occupant_ids=(spare.id,), actor_id="synthetic-installer", product=box_product,
        pose=spare.pose, host_id=spare.host_id, mounting="surface"),),
        catalog=(BoxCatalogEntry(product=box_product, box_type="junction",
                                 size=Size3(x=.3, y=.3, z=.6), provenance=provenance),))
    route = model.routes[0]
    product = CatalogProduct(catalog_id="synthetic", catalog_revision="1", product_id="emt-A")
    basis = RuleReference(authority="synthetic-only", revision="1", rule_id="explicit-product")
    model = select_raceways(model, intents=(RacewayIntent(intent_id="intent:route",
        route_id=route.id, actor_id="synthetic-installer", product=product, basis=basis),),
        catalog=(RacewayCatalogEntry(product=product, route_type="emt", wiring_method="raceway",
            designation_kind="trade_size", designation="synthetic trade A",
            nominal_diameter_m=route.nominal_diameter_m, provenance=provenance),),
        rules=(RacewayRule(reference=basis, product=product, route_type="emt", provenance=provenance),))
    history = start_history(model)
    moved = replace(evse, pose=replace(evse.pose, position=Point3(x=5.6, y=.2, z=1.2)))
    port = next(port for port in model.ports if port.owner_id == evse.id)
    moved_port = replace(port, pose=moved.pose)
    points = (*route.centerline.points[:-1], moved.pose.position)
    revised_route = replace(route, route_type="cable", nominal_diameter_m=None,
                            fitting_ids=(), centerline=replace(route.centerline, points=points))
    cable = CatalogProduct(catalog_id="synthetic", catalog_revision="1", product_id="cable-B")
    selection = replace(model.raceway_selections[0], product=cable, route_type="cable",
        wiring_method="cable", designation_kind="listed_assembly",
        designation="synthetic listed cable B", nominal_diameter_m=None,
        basis=RuleReference(authority="synthetic-only", revision="1", rule_id="explicit-cable-B"))
    outcome = replace(model.raceway_selection_outcomes[0], requested_product=cable,
                       requested_basis=selection.basis)
    box_outcome = next(row for row in model.box_selection_outcomes if evse.id in row.occupant_ids)
    box = ElectricalBox(id="box:field-added", intent_id=box_outcome.intent_id,
        box_type="junction", pose=moved.pose, size=Size3(x=.3, y=.3, z=.6),
        level_id=evse.level_id, host_id=evse.host_id, mounting="surface",
        occupant_ids=(evse.id,), product=box_product)
    selected_box_outcome = replace(box_outcome, status="selected", selected_box_id=box.id,
                                   requested_product=box_product, reason_codes=())
    revised = replace(model,
        electrical_devices=tuple(moved if row.id == evse.id else row for row in model.electrical_devices),
        ports=tuple(moved_port if row.id == port.id else row for row in model.ports),
        routes=(revised_route,), route_fittings=(), conductors=(),
        electrical_boxes=(*model.electrical_boxes, box),
        box_selection_outcomes=tuple(selected_box_outcome if row.id == box_outcome.id else row
                                     for row in model.box_selection_outcomes),
        raceway_selections=(selection,), raceway_selection_outcomes=(outcome,))
    before, after = model.to_dict(), revised.to_dict()
    edits = []
    for collection in ("electrical_devices", "ports", "routes", "route_fittings", "conductors",
                       "electrical_boxes", "box_selection_outcomes", "raceway_selections",
                       "raceway_selection_outcomes"):
        old = {row["id"]: row for row in before[collection]}
        new = {row["id"]: row for row in after[collection]}
        for identity in sorted(set(old) | set(new)):
            if old.get(identity) == new.get(identity):
                continue
            edits.append({"collection": collection, "id": identity,
                "expected_sha256": record_sha256(old[identity]) if identity in old else None,
                "record": deepcopy(new.get(identity)), "evidence_ids": ["field:note"]})
    reconciliation = {"route_id": route.id}
    for collection in ("conductors", "route_fittings", "raceway_selections", "raceway_selection_outcomes",
                       "fitting_selections", "fitting_interfaces", "fitting_selection_outcomes"):
        reconciliation[collection] = sorted({row["id"] for row in before.get(collection, [])
            if row.get("route_id") == route.id or route.id in row.get("route_ids", [])})
    statement = ("Synthetic field testimony: moved EVSE; rerouted listed cable; removed separate "
                 "conductors/elbows; added box. Checked the resulting devices, panel, both boxes "
                 "and explicit listed cable product against this installation.")
    request = {"version": VERSION, "transaction_id": "field:synthetic-four-edits",
        "actor": "synthetic-installer", "reason": statement, "design_sha256": history["design_sha256"],
        "parent_sha256": _sha(snapshot(model)), "evidence": [{"id": "field:note", "kind": "quick_edit",
        "reference": "synthetic-inline-note", "sha256": _sha(statement), "statement": statement}],
        "edits": edits, "method_reconciliations": [reconciliation], "installed_assertions": []}
    preview = preview_transaction(history, request).to_dict()
    for collection in ("electrical_devices", "electrical_equipment", "routes", "electrical_boxes", "raceway_selections"):
        request["installed_assertions"].extend({"collection": collection, "id": row["id"],
            "record_sha256": record_sha256(row), "evidence_ids": ["field:note"]}
            for row in preview[collection])
    return history, request


def create_synthetic_delivery(output_directory):
    """Generate this independent public synthetic model's receipted delivery.

    The reference production example can retain it as a companion. Its model,
    quantities and evidence never become part of that other example's model.
    """
    history, request = synthetic_inputs()
    with tempfile.TemporaryDirectory(prefix="as-built-inputs-") as temporary:
        temporary = Path(temporary)
        (temporary / "history.json").write_text(json.dumps(history))
        (temporary / "transaction.json").write_text(json.dumps(request))
        return create_as_built_delivery(temporary / "history.json", temporary / "transaction.json",
                                        output_directory=output_directory)


def main():
    create_synthetic_delivery(sys.argv[1])


if __name__ == "__main__":
    main()
