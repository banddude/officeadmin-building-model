"""Read-only comparison of two completed, same-source saved-review deliveries."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import stat
from pathlib import Path, PurePosixPath
import sys

from oabm.model import BuildingModel, ContractError
from oabm.model.model import _entity_collections
from oabm.qa import build_revision_diff, RevisionDiff
from oabm.qa.confidence_review import ConfidenceReviewManifest, ReviewConfig, build_confidence_review
from oabm.quantities import extract_quantities
from oabm.reports.scope_ledger import ScopeLedger, generate_scope_ledger
from oabm.exports.review_bundle import build_review_bundle, canonical_model_sha256
from oabm.importers.pdf_electrical.symbol_stage import SymbolReview
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
from oabm.proposals.symbols import SymbolRun, AttemptJournal
from oabm.proposals.contracts import fingerprint
from .saved_electrical import VERSION as INPUT_VERSION, FILES, _read, _decode, _sha
from .reviewed_electrical import KINDS, STAGES, VERSION as RUNNER_VERSION

VERSION = "saved-review-revision-v1"


def _require(condition, reason):
    if not condition:
        raise ContractError(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True, slots=True)
class SavedRevisionComparison:
    diff_json: str
    context_json: str

    @property
    def diff(self):
        return RevisionDiff.from_dict(json.loads(self.diff_json))

    @property
    def context(self):
        return json.loads(self.context_json)

    def to_json(self):
        return _json({"context": self.context, "diff": json.loads(self.diff_json)})


def _load(directory):
    root = Path(directory).absolute()
    _require(root.name == "delivery" and ".pending" not in root.parts,
             "select the published delivery directory, never a pending attempt")
    _require(not any(p.is_symlink() for p in (root, *root.parents)), "symlink delivery paths unsupported")
    _require(not (root.parent / "failure.json").exists(), "failed or durability-unknown attempt refused")
    marker_bytes = _read(root / "COMPLETED.json")
    marker = _decode(marker_bytes)
    _require(isinstance(marker, dict) and set(marker) == {"version", "state", "run_id", "delivery_id", "files", "meaning"},
             "unsupported completion marker")
    _require(marker["version"] == INPUT_VERSION and marker["state"] == "persisted"
             and marker["run_id"] == root.parent.name, "uncommitted completion marker")
    attempt = _decode(_read(root.parent / "attempt.json"))
    _require(attempt.get("run_id") == marker["run_id"] and attempt.get("version") == INPUT_VERSION,
             "attempt reservation contradicts completion marker")
    _require(isinstance(marker["files"], list), "completion manifest must list files")
    payloads = {}
    for row in marker["files"]:
        _require(isinstance(row, dict) and set(row) == {"path", "sha256", "bytes"}, "invalid manifest row")
        name = row["path"]
        _require(isinstance(name, str) and name and not name.startswith("/") and "\\" not in name
                 and all(p not in ("", ".", "..") for p in name.split("/"))
                 and str(PurePosixPath(name)) == name and name != "COMPLETED.json", "unsafe manifest path")
        _require(name not in payloads, "duplicate manifest file")
        path = root / name
        cursor = path
        while cursor != root:
            _require(not cursor.is_symlink(), "symlink manifest path")
            cursor = cursor.parent
        data = _read(path)
        _require(isinstance(row["bytes"], int) and not isinstance(row["bytes"], bool)
                 and row["bytes"] == len(data) and row["sha256"] == _sha(data), "manifest file hash/size mismatch")
        payloads[name] = data
    actual, directories = set(), set()
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        _require(not stat.S_ISLNK(mode), "symlink payload refused")
        name = path.relative_to(root).as_posix()
        if stat.S_ISREG(mode):
            actual.add(name)
        elif stat.S_ISDIR(mode):
            directories.add(name)
        else:
            raise ContractError("unsupported filesystem entry in delivery")
    expected_directories = {str(parent) for name in payloads for parent in PurePosixPath(name).parents if str(parent) != "."}
    _require(actual == set(payloads) | {"COMPLETED.json"} and directories == expected_directories,
             "manifest does not cover the full published file set")
    _require(_read(root / "COMPLETED.json") == marker_bytes, "completion marker changed during read")
    report = _decode(payloads["completion-report.json"])
    _require(report.get("version") == RUNNER_VERSION and report.get("delivery_ready") is True
             and report.get("delivery_id") == marker["delivery_id"], "delivery is not ready/bound to marker")
    claims = report.get("claims")
    _require(isinstance(claims, dict) and set(claims) == {"complete_source_takeoff", "complete_buildable_model",
             "code_compliance_verified", "production_live"} and all(v is False for v in claims.values()),
             "unsupported completeness claims")
    _require(report["source_coverage"]["status"] == "unavailable"
             and report["physical_completeness"]["status"] == "not_verified"
             and report["quantity_eligibility"]["base_bid"] == "not_established"
             and report["quantity_eligibility"]["source_complete"] is False, "unsupported completeness limits")
    artifacts = report["artifacts"]
    circuit_enabled = "circuit_design" in artifacts
    drawing_enabled = "drawing_set" in artifacts
    kinds = set(KINDS) | ({"ifc"} if "ifc" in artifacts else set())
    filenames = dict(FILES)
    circuit_paths = set()
    if circuit_enabled:
        from .saved_circuit_revisions import circuit_inputs
        kinds.add("circuit_design")
        filenames["circuit_design"] = "circuit-design.json"
        _, _, circuit_paths = circuit_inputs(payloads)
    drawing_config = None
    drawing_paths = set()
    if drawing_enabled:
        from .review_drawings import read_review_drawing_config, review_drawing_filenames
        drawing_paths.add("drawing-inputs/config.json")
        drawing_config = read_review_drawing_config(_decode(payloads["drawing-inputs/config.json"]))
        kinds.update(drawing_config.artifact_kinds)
        filenames.update(review_drawing_filenames(drawing_config))
    _require(set(artifacts) == kinds and report["requested_outputs"] == sorted(kinds), "unsupported artifact request set")
    expected_paths = {filenames[k] for k in kinds} | circuit_paths | drawing_paths | {"completion-report.json", "input-receipt.json"}
    _require(all(n in expected_paths or (n.startswith("journals/") and n.endswith(".jsonl")) for n in payloads),
             "unexpected payload outside the saved-review format")
    for kind in kinds:
        data = payloads[filenames[kind]]
        _require(artifacts[kind] == {"reference": str(root / filenames[kind]), "sha256": _sha(data), "bytes": len(data)},
                 "artifact/model report binding mismatch")
    stages = (*STAGES, *(("circuit_design",) if circuit_enabled else ()), *(("review_drawings",) if drawing_enabled else ()))
    _require(set(report["stages"]) == set(stages), "unsupported runner stages")
    for stage in stages:
        expected = "not_requested" if stage == "ifc" and "ifc" not in kinds else "passed"
        _require(report["stages"][stage]["status"] == expected, "incomplete runner stage")
    model = BuildingModel.from_json(payloads[FILES["canonical_model"]].decode())
    model_sha = canonical_model_sha256(model)
    _require(model_sha == report["canonical_output_sha256"] == _sha(payloads[FILES["canonical_model"]]),
             "canonical snapshot binding mismatch")
    history = model.attributes["ai_symbol_stage_runs"]
    _require(isinstance(history, list) and bool(history), "saved source lineage missing")
    latest = history[-1]
    review = SymbolReview.from_dict(latest["manifest"])
    _require(review.fingerprint == report["input_bindings"]["symbol_review_fingerprint"]
             and list(review.coverage) == report["saved_run_results"]
             and latest["dispositions"] == report["dispositions"], "saved review/report binding mismatch")
    _require(report["counts"]["dispositions"] == dict(sorted(Counter(d["status"] for d in report["dispositions"]).items()))
             and report["counts"]["source_proposals"] == len(review.proposals)
             and report["counts"]["canonical_devices"] == len(model.electrical_devices)
             and report["counts"]["canonical_equipment"] == len(model.electrical_equipment), "canonical/review counts mismatch")
    placement = model.attributes["placement_assertion_reviews"][-1]
    assertion = PlacementAssertionReview.from_dict(placement["review"])
    _require(assertion.fingerprint == placement["fingerprint"]
             == report["input_bindings"]["placement_assertion_review_fingerprint"]
             and assertion.symbol_review_fingerprint == review.fingerprint
             and placement["results"] == report["placement_assertion_results"],
             "placement review/report binding mismatch")
    sources = set()
    for entry in history:
        manifest = SymbolReview.from_dict(entry["manifest"])
        for item in manifest.coverage:
            run = SymbolRun.from_dict(item["run"])
            _require(run.fingerprint == item["run_fingerprint"] and run.status == item["status"], "saved run identity mismatch")
            sources.update((im.source.source_id, im.source.document_sha256, im.source.page) for im in run.request.images)
    _require(bool(sources), "explicit source identities unavailable")
    # Journal receipt files are inside the committed manifest; never read original input paths.
    current = {SymbolRun.from_dict(i["run"]).request.fingerprint: SymbolRun.from_dict(i["run"])
               for i in review.coverage}
    receipts = _decode(payloads["input-receipt.json"])
    _require(isinstance(receipts, dict) and set(receipts) == {"version", "manifest_sha256", "files"}
             and receipts["version"] == INPUT_VERSION and isinstance(receipts["files"], list),
             "unsupported input receipt format")
    fingerprint(receipts["manifest_sha256"], "original input manifest digest")
    scalar_inputs = {"model", "review", "runs", "decisions", "registrations", "assertion_review", "scope_policy", "review_config"}
    if circuit_enabled:
        scalar_inputs.add("circuit_design")
    else:
        _require("circuit_design" not in report and "circuit_design_fingerprint" not in report["input_bindings"],
                 "circuit lineage without circuit artifact")
    if drawing_enabled:
        scalar_inputs.add("review_drawings")
    else:
        _require("review_drawings" not in report
                 and "review_drawings_fingerprint" not in report["input_bindings"],
                 "drawing lineage without drawing artifacts")
    prefixes = {"documents", "images", "evidence_documents", "journals"}
    receipt_rows = {}
    for row in receipts["files"]:
        _require(isinstance(row, dict) and set(row) == {"input", "sha256", "bytes"}, "invalid input receipt row")
        label = row["input"]
        _require(isinstance(label, str) and bool(label) and label not in receipt_rows, "duplicate or invalid input receipt identity")
        fingerprint(row["sha256"], "input receipt digest")
        _require(isinstance(row["bytes"], int) and not isinstance(row["bytes"], bool) and row["bytes"] >= 0,
                 "invalid input receipt byte size")
        if label not in scalar_inputs:
            prefix, separator, identity = label.partition("/")
            _require(separator and prefix in prefixes and bool(identity), "unsupported input receipt identity")
            if prefix in ("documents", "evidence_documents", "journals"):
                fingerprint(identity, "input receipt identity digest")
            if prefix in ("documents", "evidence_documents"):
                _require(row["sha256"] == identity, "input receipt contradicts source document digest")
        elif row["bytes"] == 0:
            raise ContractError("empty scalar input receipt")
        receipt_rows[label] = row
    _require(scalar_inputs <= receipt_rows.keys(), "missing required scalar input receipts")
    current_images = {}
    for run in current.values():
        for image in run.request.images:
            _require(image.id not in current_images or current_images[image.id] == image.sha256,
                     "contradictory current source image identity")
            current_images[image.id] = image.sha256
            row = receipt_rows.get("documents/" + image.source.document_sha256)
            _require(row is not None and row["sha256"] == image.source.document_sha256 and row["bytes"] > 0,
                     "missing or contradictory current PDF receipt")
    for identity, expected in current_images.items():
        row = receipt_rows.get("images/" + identity)
        _require(row is not None and row["sha256"] == expected and row["bytes"] > 0,
                 "missing or contradictory current image receipt")
    for fact in assertion.assertions:
        for evidence in fact.evidence:
            identity = evidence.document_sha256
            available = [receipt_rows[label] for label in ("documents/" + identity, "evidence_documents/" + identity)
                         if label in receipt_rows]
            _require(bool(available), "missing placement evidence document receipt")
            _require(all(1 <= row["bytes"] <= 128_000_000 for row in available)
                     and len({row["bytes"] for row in available}) == 1,
                     "cited placement evidence receipt has unsupported or contradictory byte size")
    journal_records = {label[len("journals/"):]: row for label, row in receipt_rows.items() if label.startswith("journals/")}
    _require(set(current) == set(journal_records), "journal request receipt mismatch")
    journal_names = {"journals/" + str(i) + ".jsonl" for i in range(len(current))}
    _require({n for n in payloads if n.startswith("journals/")} == journal_names, "journal file set mismatch")
    for index, key in enumerate(sorted(current)):
        name = "journals/" + str(index) + ".jsonl"
        _require(journal_records[key]["sha256"] == _sha(payloads[name])
                 and journal_records[key]["bytes"] == len(payloads[name])
                 and AttemptJournal.read(root / name) == current[key].attempts, "saved journal/run mismatch")
    circuit_lineage = None
    if circuit_enabled:
        from .saved_circuit_revisions import verify_circuit_revision
        circuit_lineage = verify_circuit_revision(model, report, review, assertion, payloads, receipt_rows)
    if drawing_enabled:
        config_data = payloads["drawing-inputs/config.json"]
        config_receipt = receipt_rows["review_drawings"]
        _require(config_receipt["sha256"] == _sha(config_data)
                 and config_receipt["bytes"] == len(config_data)
                 and report["input_bindings"]["review_drawings_fingerprint"] == _sha(_json(drawing_config.to_dict()).encode()),
                 "retained drawing configuration binding mismatch")
        from .review_drawings import generate_review_drawings
        drawing_payloads, drawing_status = generate_review_drawings(model, drawing_config,
            source_proposals_not_admitted=[dict(d) for d in report["dispositions"] if d["canonical_id"] is None])
        drawing_status["source_proposals_not_admitted"] = [dict(d) for d in report["dispositions"] if d["canonical_id"] is None]
        _require(drawing_status == report["review_drawings"]
                 and set(drawing_payloads) == set(drawing_config.artifact_kinds)
                 and all(data == payloads[filenames[kind]] for kind, data in drawing_payloads.items()),
                 "drawing/model replay mismatch")
    groups = report["quantity_eligibility"]["group_bindings"]["resolved"]
    takeoff = extract_quantities(model, groups=groups)
    _require(takeoff.to_json().encode() == payloads[FILES["quantities"]]
             and report["counts"]["quantity_lines"] == len(takeoff.items)
             and report["quantity_warnings"] == [w.to_dict() for w in takeoff.warnings], "quantity/model binding mismatch")
    ledger = ScopeLedger.from_json(payloads[FILES["scope_ledger"]].decode())
    _require(generate_scope_ledger(model, policy=ledger.policy).to_json() == ledger.to_json()
             and ledger.policy.fingerprint == report["input_bindings"]["scope_policy_fingerprint"], "scope/model binding mismatch")
    confidence = ConfidenceReviewManifest.from_dict(_decode(payloads[FILES["confidence_review"]]))
    config = ReviewConfig(confidence_threshold=confidence.confidence_threshold)
    _require(build_confidence_review(model, config).to_dict() == confidence.to_dict()
             and _sha(config.to_json(indent=None).encode()) == report["input_bindings"]["review_config_sha256"],
             "confidence/model binding mismatch")
    source_rows = _decode(payloads[FILES["source_map"]])
    registry = {(r["source_kind"], r["source_id"]): r["reference"] for r in source_rows}
    _require(len(registry) == len(source_rows), "duplicate source-map identities")
    bundle_doc = _decode(payloads[FILES["review_bundle"]])
    bundle = build_review_bundle(model, glb_bytes=payloads[FILES["glb"]],
        model_reference=artifacts["canonical_model"]["reference"], glb_reference=artifacts["glb"]["reference"],
        confidence_manifest=confidence, confidence_reference=artifacts["confidence_review"]["reference"],
        source_reference_mapping_reference=artifacts["source_map"]["reference"], source_references=registry,
        glb_options=bundle_doc["artifacts"]["glb"]["export_options"])
    _require(bundle.to_dict() == bundle_doc, "review bundle/model binding mismatch")
    if "ifc" in kinds:
        from oabm.ifc.adapter import from_ifc, to_ifc
        _require(to_ifc(model).to_string().encode() == payloads[FILES["ifc"]], "IFC/model binding mismatch")
        restored = from_ifc(root / FILES["ifc"])
        identities = lambda m: {e.id for collection in _entity_collections(m) for e in collection}
        _require(restored.model_id == model.model_id and restored.schema_version == model.schema_version
                 and restored.coordinate_system == model.coordinate_system
                 and identities(restored) == identities(model) and restored.attributes == model.attributes,
                 "IFC reload identity/frame mismatch")
    _require(all(_read(root / name) == data for name, data in payloads.items())
             and _read(root / "COMPLETED.json") == marker_bytes, "delivery changed during validation")
    metadata = {"directory": str(root), "completion_marker_sha256": _sha(marker_bytes),
        "canonical_model_sha256": model_sha, "verified_files": len(payloads),
        "retained_review_base_fingerprint": assertion.model_fingerprint}
    if circuit_lineage is not None:
        metadata["circuit_design_lineage"] = circuit_lineage
    if drawing_enabled:
        metadata["review_drawings_config_fingerprint"] = report["input_bindings"]["review_drawings_fingerprint"]
    return model, report, tuple(sorted(sources)), metadata


def compare_saved_reviews(base_directory, target_directory):
    """Return the unchanged canonical diff plus separate inherited limitations."""
    try:
        base, br, bs, bi = _load(base_directory)
        target, tr, ts, ti = _load(target_directory)
        _require(bs == ts, "source identities differ; changed-PDF identity reconciliation unavailable")
        _require(base.model_id == target.model_id, "different canonical model lineage")
        _require(base.coordinate_system == target.coordinate_system, "different coordinate frame or units")
        _require(base.schema_version == target.schema_version, "different canonical schema versions")
        _require(br["input_bindings"]["base_model_canonical_json_sha256"] == tr["input_bindings"]["base_model_canonical_json_sha256"],
                 "different review base snapshots; this consumer only compares reviews of one saved base")
        _require(bi["retained_review_base_fingerprint"] == ti["retained_review_base_fingerprint"],
                 "different review base fingerprints in retained placement evidence")
        diff = build_revision_diff(base, target)
        def limits(report):
            return {k: report[k] for k in ("claims", "source_coverage", "physical_completeness", "quantity_eligibility", "unresolved")}
        context = {"version": VERSION, "scope": "same-source same-base reviewed revisions", "base": bi, "target": ti,
            "source_identities": [{"source_id": s, "document_sha256": d, "page": p} for s,d,p in bs],
            "inherited_limits": {"base": limits(br), "target": limits(tr)}, "permit_ready": False,
            "pricing": "not_performed", "identity_reconciliation": "unavailable",
            "source_pixel_validation": "not_repeated; recorded source identity and committed local artifacts checked"}
        return SavedRevisionComparison(diff.to_json(), _json(context))
    except ContractError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, ImportError, RuntimeError) as error:
        raise ContractError("invalid completed saved-review delivery: " + str(error)) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("target")
    args = parser.parse_args(argv)
    try:
        print(compare_saved_reviews(args.base, args.target).to_json())
    except ContractError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
