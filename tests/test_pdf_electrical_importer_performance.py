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
BASELINE_DENSE_SHA256 = "31dc13f2f270efe29b45237d5162fe646fe47e03146e9753deda4db18c6409c0"
BASELINE_FIXTURE_SHA256 = {
    "geometry-only-power-sheet-with-legend.pdf": "7571bb35281e1db129fd0fc7f7be921341c3aececd60cd62803f7d0cfd57acc9",
    "two-page-sheet-local-legend.pdf": "6ff0d4dc2f2c04e6f241214aa1700673b3a7a4238ff5aead8ff4cff031478c0c",
    "geometry-only-power-sheet-real-cad-glyphs.pdf": "f6c5790517d71992b314d91904d549d0a92711dfc672a061be79d66fb1ef51ec",
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
