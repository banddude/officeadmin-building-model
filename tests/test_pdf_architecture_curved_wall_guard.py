"""Public source-evidence byte guard against main 99d0f47 for #226."""

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from oabm.importers.pdf_architecture import sheet_wall_evidence
from oabm.importers.pdf_architecture.extract import extract_pdf
from oabm.importers.pdf_architecture.importer import classify_page

BASELINE = {
    "adjacent-room-label-pairs.pdf": "7260095f4c71ddc43440c991c8fda804dc437caa3e9b62d825ea7f09666b8095",
    "cad-export-geometry-only.pdf": "2bdc0cb4f7ef10b5658a4d5f9e7bd6d6000130ae54e07ecab513d4b5263d7668",
    "cad-export-geometry-plus-text.pdf": "fc487c07a16467917e30b6d084faadb6b0d8a97aac87d58b232775fd1798e3d0",
    "cad-export-scale-no-registration.pdf": "ca17ac9197bdad149d4a93d474aebf401c6b1c1eaebf9dda9f092ec3efd9c227",
    "cad-wall-primitive-families.pdf": "2bdc0cb4f7ef10b5658a4d5f9e7bd6d6000130ae54e07ecab513d4b5263d7668",
    "dense-room-labels.pdf": "cd2b3708c64c4ace79df543842d0d43bcb3f945907858c899f1fe5d8bb276375",
    "simple-floor-plan.pdf": "9f4c72cb620b72c34852025d3d938d41ad523a5770e53a25cc1c713dc19180af",
}


@pytest.mark.parametrize("filename", sorted(BASELINE))
def test_existing_architectural_source_evidence_unchanged(filename):
    path = (
        Path(__file__).resolve().parents[1] / "fixtures/pdf_architecture/v1" / filename
    )
    source = extract_pdf(path, source_id="synthetic:straight-guard")
    payload = {
        "classification": [
            dataclasses.asdict(classify_page(page)) for page in source.pages
        ],
        "evidence": [
            dataclasses.asdict(sheet_wall_evidence(page)) for page in source.pages
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert digest == BASELINE[filename]
