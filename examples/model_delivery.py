"""Rebuild a synthetic model delivery twice and verify every packaged byte.

Run: PYTHONPATH=src python examples/model_delivery.py /tmp/synthetic-delivery-demo
The destination must be fresh. No renderer or external service is used.
"""
import argparse
import hashlib
import json
from pathlib import Path

from oabm.exports.model_delivery import deliver_model

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("destination", type=Path)
args = parser.parse_args()
args.destination.mkdir(parents=True)  # retain existing examples rather than overwrite
fixture = Path(__file__).resolve().parents[1] / "fixtures/model_delivery/v1"
outputs = []
for name in ("first", "rebuild"):
    root = deliver_model(fixture/"model.json", fixture/"options.json",
                         output_directory=args.destination/name)
    manifest = json.loads((root/"MANIFEST.json").read_bytes())
    for row in manifest["files"]:
        data = (root/row["path"]).read_bytes()
        assert len(data) == row["bytes"]
        assert hashlib.sha256(data).hexdigest() == row["sha256"]
    outputs.append({p.relative_to(root).as_posix():p.read_bytes()
                    for p in root.rglob("*") if p.is_file()})
assert outputs[0] == outputs[1]
print(f"delivery: {args.destination/'first/delivery'}")
print(f"status: {manifest['status']['outputs']}; bend check: {manifest['status']['bend_check']}")
print(f"canonical model SHA-256: {manifest['canonical_model_sha256']}")
print(f"verified {len(manifest['files'])} input/output files; byte-identical rebuild")
print("outputs: plans/plan-01.svg, preview.glb, drawing-set.json, bend-report.json, MANIFEST.json")
