"""Synthetic regression for the radius-bounded legend leader lookup (#218)."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from oabm.importers.pdf_electrical.importer import (
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfSymbolObservation,
    PdfTextObservation,
    PdfVectorPathObservation,
    _LegendRow,
    _VectorCluster,
    _VectorEndpointGrid,
    _leader_connects_heading_to_rows,
    _paths_touch,
    _topology_candidate_pairs,
    extract_pdf,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures/pdf_electrical/geometry-only-power-sheet-with-legend.pdf"
)
# Deliberate digest update for #276: every import now states its
# branch_wall_evidence guard status in pdf_electrical attributes ("absent"
# with the #76 note when no handoff was supplied, "applied" with chain and
# rejection counts otherwise). Removing exactly that attribute from the new
# output reproduces the previous digests byte for byte, so the attribute is
# the sole intended delta on these fixtures.
BASELINE_DENSE_SHA256 = "5f27f16b01a170ee688fd7a7c575c6120ad20debe27b99a8d4b583a1e2295a87"
BASELINE_FIXTURE_SHA256 = {
    "geometry-only-power-sheet-with-legend.pdf": "3979e782b9bb48b742608f00c5fafb27353ee0cbab7b9ca0a03ea922ab1f1b92",
    "two-page-sheet-local-legend.pdf": "c0fd1ae7a3564bfb8ce9d7ab335e94afb67bdd608a1e353c62f935cce18b5177",
    "geometry-only-power-sheet-real-cad-glyphs.pdf": "311c8c52faf2fb936990b147560501abec4bf62a397a700695ae3d68f8c32ba2",
}


@pytest.mark.parametrize("filename", sorted(BASELINE_FIXTURE_SHA256))
def test_existing_fixture_import_bytes_match_prechange_output(filename: str) -> None:
    fixture = FIXTURE.parent / filename
    source = extract_pdf(fixture, source_id="synthetic:byte-identity")
    model = ElectricalPdfImporter().import_document(source)
    assert hashlib.sha256(model.to_json(indent=None).encode()).hexdigest() == (
        BASELINE_FIXTURE_SHA256[filename]
    )


def test_endpoint_grid_preserves_leader_decisions_and_order() -> None:
    heading = PdfTextObservation("heading", 1, "LEGEND", 0.0, 0.0)
    label = PdfTextObservation("label", 1, "DUPLEX", 200.0, 200.0)
    glyph = PdfVectorPathObservation("glyph", 1, ((198.0, 198.0), (202.0, 202.0)))
    row = _LegendRow(
        cluster=_VectorCluster(1, (glyph,), (198.0, 198.0, 202.0, 202.0),
                               (200.0, 200.0), "glyph", "glyph"),
        label=label,
        classification=None,
        classification_candidates=(),
        orientation=0,
        horizontal_gap_pt=0.0,
    )
    vectors = (
        PdfVectorPathObservation("far", 1, ((-100.0, -100.0), (200.0, 200.0))),
        PdfVectorPathObservation("near", 1, ((54.0, 0.0), (200.0, 200.0))),
        PdfVectorPathObservation("reverse", 1, ((200.0, 200.0), (0.0, 54.0))),
        PdfVectorPathObservation("other-page", 2, ((0.0, 0.0), (200.0, 200.0))),
    )
    grid = _VectorEndpointGrid(vectors)
    for candidate_heading in (
        heading,
        PdfTextObservation("miss", 1, "LEGEND", -54.01, 0.0),
        PdfTextObservation("other-page-heading", 2, "LEGEND", 0.0, 0.0),
    ):
        assert _leader_connects_heading_to_rows(candidate_heading, (row,), vectors) == (
            _leader_connects_heading_to_rows(
                candidate_heading, (row,), vectors, endpoints=grid
            )
        )
    # The grid's conservative cell search never reorders the source vectors.
    assert grid.candidates(1, 0.0, 0.0) == (
        (vectors[1].points_pt[0], vectors[1].points_pt[-1]),
        (vectors[2].points_pt[0], vectors[2].points_pt[-1]),
    )


def test_topology_bounds_keep_every_touching_pair() -> None:
    paths = (
        PdfVectorPathObservation("long", 1, ((-100.0, 0.0), (100.0, 0.0))),
        PdfVectorPathObservation("crossing", 1, ((0.0, -100.0), (0.0, 100.0))),
        PdfVectorPathObservation("endpoint", 1, ((99.5, 0.0), (125.0, 0.0))),
        PdfVectorPathObservation("near", 1, ((0.0, 1.9), (0.0, 20.0))),
        PdfVectorPathObservation("far", 1, ((500.0, 500.0), (510.0, 510.0))),
        PdfVectorPathObservation("other-page", 2, ((0.0, 0.0), (100.0, 0.0))),
    )
    for tolerance in (0.0, 2.0):
        candidates = set(_topology_candidate_pairs(paths, tolerance))
        touching = {
            (first, second)
            for first in range(len(paths))
            for second in range(first + 1, len(paths))
            if _paths_touch(paths[first], paths[second], tolerance_pt=tolerance)
        }
        assert touching <= candidates
        assert candidates == set(_topology_candidate_pairs(paths, tolerance))


def test_dense_page_import_is_fast_and_byte_identical_to_baseline() -> None:
    source = extract_pdf(FIXTURE, source_id="synthetic:dense-benchmark")
    vectors = tuple(
        PdfVectorPathObservation(
            element_id=f"dense:v:{index:05d}",
            page=1,
            points_pt=(
                (1000.0 + (index % 100) * 20, 1000.0 + (index // 100) * 20),
                (1001.0 + (index % 100) * 20, 1000.0 + (index // 100) * 20),
            ),
        )
        for index in range(30_000)
    )
    texts = tuple(
        PdfTextObservation(
            element_id=f"dense:t:{index:04d}",
            page=1,
            text=f"NOTE {index:04d}",
            x_pt=1000.0 + (index % 20) * 100,
            y_pt=1000.0 + (index // 20) * 100,
            font_size_pt=9.0,
        )
        for index in range(1_600)
    )
    symbols = tuple(
        PdfSymbolObservation(
            element_id=f"dense:s:{index:04d}",
            page=1,
            name="generic",
            x_pt=1000.0 + (index % 20) * 100,
            y_pt=1800.0 + (index // 20) * 100,
        )
        for index in range(200)
    )
    document = PdfElectricalDocument(
        source_id=source.source_id,
        page_count=source.page_count,
        texts=source.texts + texts,
        symbols=source.symbols + symbols,
        vectors=source.vectors + vectors,
        page_provenance=source.page_provenance,
    )
    start = time.perf_counter()
    model = ElectricalPdfImporter().import_document(document)
    elapsed = time.perf_counter() - start
    assert elapsed < 20.0, f"dense synthetic page import took {elapsed:.2f} s"
    assert hashlib.sha256(model.to_json(indent=None).encode()).hexdigest() == (
        BASELINE_DENSE_SHA256
    )
