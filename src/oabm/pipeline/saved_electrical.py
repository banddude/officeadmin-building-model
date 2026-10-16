"""Local, saved-input electrical delivery: python -m oabm.pipeline.saved_electrical."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

from oabm.model import BuildingModel, ContractError
from oabm.proposals.symbols import SymbolRun
from oabm.importers.pdf_electrical.symbol_stage import SymbolReview, SymbolDecision, SymbolRegistration
from oabm.importers.pdf_electrical.placement_assertions import PlacementAssertionReview
from oabm.reports.scope_ledger import ScopePolicy
from oabm.qa.confidence_review import ReviewConfig
from .reviewed_electrical import KINDS, deliver_reviewed_electrical, ReviewedElectricalDeliveryError

VERSION = "saved-electrical-input-v1"
DRAWING_VERSION = "saved-electrical-input-v2"
FILES = {"canonical_model": "model.json", "quantities": "quantities.json",
         "scope_ledger": "scope-ledger.json", "confidence_review": "confidence-review.json",
         "source_map": "source-map.json", "glb": "model.glb",
         "review_bundle": "review-bundle.json", "ifc": "model.ifc"}
REQUIRED = {"version", "delivery_id", "model", "review", "runs", "documents", "images",
            "journals", "decisions", "registrations", "assertion_review", "evidence_documents",
            "scope_policy", "review_config", "proposal_groups", "canonical_groups",
            "source_references", "glb_options", "include_ifc", "coverage_report"}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False).encode()


def _decode(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ContractError("nonfinite JSON number")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _read(path):
    # No URL, shell expansion, special files or symlink following at the leaf.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ContractError("input must be a regular local file")
        return stream.read()


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class SavedDeliveryError(ContractError):
    """A failed attempt is retained, never published as a delivery."""
    def __init__(self, attempt_directory, stage, cause):
        self.attempt_directory = attempt_directory
        self.stage = stage
        super().__init__(f"saved delivery failed at {stage}; attempt retained at {attempt_directory}")


def persist_saved_electrical(manifest_path, *, output_root, run_id):
    """Reserve a fresh run ID; publish its entire delivery directory by rename.

    Existing attempts are never resumed, overwritten or deleted. Retry with a
    new run ID. Input hashes bind exact bytes; journals are snapshotted privately
    so downstream rereads use those same bytes. No provider or network API.
    """
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id) is None:
        raise ContractError("run ID must be 1-80 ASCII letters, digits, underscores or hyphens")
    root = Path(output_root).resolve()
    attempt = root / run_id
    pending = attempt / ".pending"
    stage = "prepare_output_root"
    reserved = False
    committed = False
    try:
        # Sync the whole containing chain after reservation. A directory that
        # already exists may have been left by a failed earlier attempt before
        # its parent link was synced; existence alone does not prove durability.
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        stage = "reserve_attempt"
        attempt.mkdir(mode=0o700)  # exclusive reservation, including concurrent callers
        reserved = True
        stage = "sync_output_root"
        for directory in (root, *root.parents):
            _sync_dir(directory)
        stage = "prepare_attempt"
        pending.mkdir(mode=0o700)
        _write(attempt / "attempt.json", _json({"version": VERSION, "run_id": run_id,
            "state": "reserved", "completion_rule": "delivery/COMPLETED.json plus verified file hashes"}))
        stage = "load_inputs"
        manifest_path = Path(manifest_path).absolute()
        manifest_bytes = _read(manifest_path)
        manifest = _decode(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ContractError("saved input manifest must be an object")
        version = manifest.get("version")
        required = REQUIRED if version == VERSION else REQUIRED | {"review_drawings"}
        if version not in (VERSION, DRAWING_VERSION) or set(manifest) != required:
            raise ContractError("saved input manifest requires exact versioned keys")
        if manifest["coverage_report"] is not None:
            raise ContractError("coverage_report must explicitly be null")
        receipts = []
        def read_ref(ref, label):
            if (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
                    or not isinstance(ref["path"], str) or not ref["path"].strip()
                    or "://" in ref["path"] or "\x00" in ref["path"]
                    or not isinstance(ref["sha256"], str)
                    or re.fullmatch("[0-9a-f]{64}", ref["sha256"]) is None):
                raise ContractError("input reference requires local path and SHA-256")
            path = Path(ref["path"])
            if not path.is_absolute():
                path = manifest_path.parent / path
            data = _read(path)
            if _sha(data) != ref["sha256"]:
                raise ContractError("saved input hash mismatch: " + label)
            receipts.append({"input": label, "sha256": _sha(data), "bytes": len(data)})
            return data
        def doc(key):
            return _decode(read_ref(manifest[key], key))
        def sequence(key, cls):
            rows = doc(key)
            if not isinstance(rows, list):
                raise ContractError(key + " must be a JSON array")
            return tuple(cls.from_dict(row) for row in rows)
        def buffers(key):
            refs = manifest[key]
            if not isinstance(refs, dict) or any(not isinstance(k, str) or not k for k in refs):
                raise ContractError(key + " must map explicit identities to file references")
            return {k: read_ref(ref, key + "/" + k) for k, ref in sorted(refs.items())}
        model = BuildingModel.from_dict(doc("model"))
        review = SymbolReview.from_dict(doc("review"))
        runs = sequence("runs", SymbolRun)
        decisions = sequence("decisions", SymbolDecision)
        registrations = sequence("registrations", SymbolRegistration)
        assertion = PlacementAssertionReview.from_dict(doc("assertion_review"))
        policy = ScopePolicy.from_dict(doc("scope_policy"))
        config = ReviewConfig.from_dict(doc("review_config"))
        drawing_config = None
        if version == DRAWING_VERSION and manifest["review_drawings"] is not None:
            from .review_drawings import ReviewDrawingConfig
            drawing_config = ReviewDrawingConfig.from_dict(doc("review_drawings"))
        documents, images, evidence = buffers("documents"), buffers("images"), buffers("evidence_documents")
        journals = buffers("journals")
        journal_dir = pending / "journals"
        journal_dir.mkdir(mode=0o700)
        journal_paths = {}
        for index, (key, data) in enumerate(sorted(journals.items())):
            path = journal_dir / (str(index) + ".jsonl")
            _write(path, data)
            journal_paths[key] = path
        _sync_dir(journal_dir)
        sources = manifest["source_references"]
        if not isinstance(sources, list):
            raise ContractError("source_references must be explicit rows")
        registry = {}
        for row in sources:
            if not isinstance(row, dict) or set(row) != {"source_kind", "source_id", "reference"}:
                raise ContractError("source reference row requires kind, ID and reference")
            key = (row["source_kind"], row["source_id"])
            if key in registry:
                raise ContractError("duplicate source reference identity")
            registry[key] = row["reference"]
        if not isinstance(manifest["include_ifc"], bool):
            raise ContractError("include_ifc must be explicit boolean")
        kinds = tuple(KINDS) + (("ifc",) if manifest["include_ifc"] else ())
        filenames = dict(FILES)
        if drawing_config is not None:
            kinds += drawing_config.artifact_kinds
            filenames.update(drawing_set="drawing-set.json", panel_facts="panel-facts.json")
            for index, plan in enumerate(sorted(drawing_config.plans, key=lambda p: p.id), 1):
                filenames["review_plan:" + plan.id] = f"review-plan-{index:03d}.svg"
        final = attempt / "delivery"
        refs = {k: str(final / filenames[k]) for k in kinds}
        _write(pending / "input-receipt.json", _json({"version": VERSION,
            "manifest_sha256": _sha(manifest_bytes), "files": receipts}))
        stage = "compute_delivery"
        result = deliver_reviewed_electrical(model, review, delivery_id=manifest["delivery_id"],
            runs=runs, documents=documents, images=images, journal_paths=journal_paths,
            decisions=decisions, registrations=registrations, assertion_review=assertion,
            evidence_documents=evidence, scope_policy=policy,
            proposal_groups=manifest["proposal_groups"], canonical_groups=manifest["canonical_groups"],
            review_config=config, source_references=registry, artifact_references=refs,
            glb_options=manifest["glb_options"], include_ifc=manifest["include_ifc"], coverage_report=None,
            review_drawings=drawing_config)
        stage = "persist_delivery"
        if set(result.artifacts) != set(kinds) or result.completion["delivery_ready"] is not True:
            raise ContractError("runner returned an incomplete payload set")
        for kind in kinds:
            data = result.artifacts[kind]
            identity = result.completion["artifacts"][kind]
            if identity != {"reference": refs[kind], "sha256": _sha(data), "bytes": len(data)}:
                raise ContractError("runner artifact identity mismatch")
            _write(pending / filenames[kind], data)
        _write(pending / "completion-report.json", result.completion_json.encode())
        entries = []
        for path in sorted(pending.rglob("*")):
            if path.is_file():
                data = _read(path)
                entries.append({"path": path.relative_to(pending).as_posix(), "sha256": _sha(data), "bytes": len(data)})
        _write(pending / "COMPLETED.json", _json({"version": VERSION, "state": "persisted",
            "run_id": run_id, "delivery_id": manifest["delivery_id"], "files": entries,
            "meaning": "artifact persistence only; consult completion-report.json for source and physical limits"}))
        _sync_dir(pending)
        stage = "publish_directory"
        os.rename(pending, final)
        committed = True
        _sync_dir(attempt)
        return final
    except Exception as exc:
        if stage == "reserve_attempt" and isinstance(exc, FileExistsError) and not reserved:
            raise  # never write a receipt into an attempt owned by another caller
        # Never remove partial bytes; no success marker is visible in delivery/.
        # If rename already committed, a directory-fsync failure is reported as
        # durability unknown, not as an uncommitted or safe-to-overwrite attempt.
        failure = {"version": VERSION, "state": "durability_unknown" if committed else "failed",
                   "stage": stage, "error_type": type(exc).__name__, "directory_committed": committed}
        if isinstance(exc, ReviewedElectricalDeliveryError):
            failure["runner_completion"] = exc.completion
        if reserved:
            try:
                _write(attempt / "failure.json", _json(failure))
                _sync_dir(attempt)
            except OSError:
                pass  # original failure remains the cause, e.g. exhausted storage
        raise SavedDeliveryError(attempt, stage, exc) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", help="explicit local saved-input JSON manifest")
    parser.add_argument("--output-root", required=True, help="private local output parent")
    parser.add_argument("--run-id", required=True, help="fresh version label; never overwritten")
    args = parser.parse_args(argv)
    try:
        final = persist_saved_electrical(args.manifest, output_root=args.output_root, run_id=args.run_id)
    except (ContractError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(str(final))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
