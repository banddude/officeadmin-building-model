from pathlib import Path

from oabm.importers.pdf_electrical import (
    ElectricalPdfImporter,
    PdfElectricalDocument,
    PdfTextObservation,
    PdfVectorPathObservation,
    extract_pdf,
)
from oabm.importers.pdf_electrical import importer as pdf_electrical_importer


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "pdf_electrical" / "geometry-only-power-sheet-notes-column-legend.pdf"


def test_pr66_north_arrow_near_legend_does_not_match_electrical_type() -> None:
    extracted = extract_pdf(FIXTURE, source_id="review:pr66:north-arrow")
    baseline = ElectricalPdfImporter().import_document(extracted)
    assert len(baseline.electrical_devices) == 16

    arrow_ids = {"review:north-arrow-head", "review:north-arrow-stem"}
    edited = PdfElectricalDocument(
        source_id=extracted.source_id,
        page_count=extracted.page_count,
        texts=(
            *extracted.texts,
            PdfTextObservation(
                element_id="review:north-arrow-n",
                page=1,
                text="N",
                x_pt=556.0,
                y_pt=416.0,
                font_size_pt=6.0,
            ),
        ),
        symbols=extracted.symbols,
        vectors=(
            *extracted.vectors,
            PdfVectorPathObservation(
                element_id="review:north-arrow-head",
                page=1,
                points_pt=((548.0, 424.0), (554.0, 412.0), (542.0, 412.0)),
                closed=True,
                metadata={"paint_operator": "S", "geometry_kind": "straight"},
            ),
            PdfVectorPathObservation(
                element_id="review:north-arrow-stem",
                page=1,
                points_pt=((548.0, 412.0), (548.0, 397.0)),
                closed=False,
                metadata={"paint_operator": "S", "geometry_kind": "straight"},
            ),
        ),
        page_provenance=extracted.page_provenance,
    )

    model = ElectricalPdfImporter().import_document(edited)
    assert len(model.electrical_devices) == 16
    assert not any(
        provenance.source_element_id in arrow_ids
        for device in model.electrical_devices
        for provenance in device.provenance
    )

    unresolved = [
        item
        for item in model.attributes["pdf_electrical"]["unresolved_observations"]
        if arrow_ids.intersection(item.get("source_element_ids", ()))
    ]
    assert len(unresolved) == 1
    item = unresolved[0]
    assert item["reason"] == "glyph cluster has no unique matching type in the sheet legend"
    diagnostics = item["recognition_provenance"]["match_diagnostics"]
    assert diagnostics["nearest_prototype"] is not None
    assert diagnostics["score"] < pdf_electrical_importer._GLYPH_MATCH_SCORE_MIN
    assert diagnostics["second_best"] is not None
    assert diagnostics["non_unique_reason"] == "nearest prototype score is below the match threshold"

# Reviewer-authored acceptance probe only.

