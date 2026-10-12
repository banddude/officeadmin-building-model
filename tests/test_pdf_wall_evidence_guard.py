"""Source-bound wall-evidence handoff and open-chain rejection (#276, under #76)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from oabm.importers.pdf_architecture.extract import extract_pdf as architecture_extract_pdf
from oabm.importers.pdf_architecture import source_wall_evidence
from oabm.importers.pdf_electrical import (
    ElectricalPdfError,
    ElectricalPdfImporter,
    extract_pdf as electrical_extract_pdf,
)
from oabm.importers.pdf_wall_evidence import (
    SourceWallEvidence,
    WallChainEvidence,
    WallChainSegment,
    WallEvidenceHandoffError,
)

# Displayed sheet points. The panel sits top-left, the device mid-sheet, and
# the classified wall geometry bottom-right, so a lawful branch never comes
# near it while the contact variants end inside it.

PANEL_TEXT = "PANEL LP 120/240V 1PH"
DEVICE_TEXT = 'EVSE-1 +48" AFF WALL MTD'
#: Deliberately tagless: the circuit annotation names panel and circuit but
#: no device, so a circuit can only resolve through the branch run's
#: topology. That keeps the text-link lane out of the guard's test cases.
CIRCUIT_TEXT = "PANEL LP CKT 12 240V 2P"

BRANCH_AWAY = b"72 700 m 200 500 l S\n"
#: A lawful multi-tap branch: one tee, three legs, same tree topology as the
#: open wall chain below, but no architectural geometry anywhere near it.
BRANCH_MULTITAP = (
    b"72 700 m 200 600 l S\n"
    b"200 600 m 280 600 l S\n"
    b"200 600 m 200 530 l S\n"
)
#: A wall-mounted device: the branch ends at its symbol, which sits inside
#: the open wall chain's vertical run (301.5 x 300..400).
BRANCH_INTO_OPEN_CHAIN = b"72 700 m 307 372 l S\n"
#: A device inside the closed wall grid (450..550 x 300..383).
BRANCH_INTO_CLOSED_GRID = b"72 700 m 505 346 l S\n"

OPEN_CHAIN_LINES = (
    b"300 300 m 300 400 l S\n"
    b"303 300 m 303 400 l S\n"
    b"300 400 m 400 400 l S\n"
    b"300 403 m 400 403 l S\n"
)
CLOSED_GRID_LINES = (
    b"450 300 m 550 300 l S\n"
    b"450 303 m 550 303 l S\n"
    b"450 380 m 550 380 l S\n"
    b"450 377 m 550 377 l S\n"
    b"450 300 m 450 380 l S\n"
    b"453 300 m 453 380 l S\n"
    b"550 300 m 550 380 l S\n"
    b"547 300 m 547 380 l S\n"
)


def _write_combined_sheet(
    path: Path,
    *,
    branch: bytes = BRANCH_AWAY,
    walls: bytes = b"",
    device_position: tuple[float, float] = (200, 500),
) -> None:
    """One synthetic sheet: panel, device, homerun annotation, branch, walls."""

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    symbol = DecodedStreamObject()
    symbol.set_data(b"0 0 12 12 re S")
    symbol[NameObject("/Type")] = NameObject("/XObject")
    symbol[NameObject("/Subtype")] = NameObject("/Form")
    symbol[NameObject("/BBox")] = ArrayObject(
        [NumberObject(0), NumberObject(0), NumberObject(12), NumberObject(12)]
    )
    symbol_ref = writer._add_object(symbol)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            NameObject("/XObject"): DictionaryObject({NameObject("/EVSE1"): symbol_ref}),
        }
    )
    device_x, device_y = device_position
    content = DecodedStreamObject()
    content.set_data(
        b"BT /F1 10 Tf 1 0 0 1 72 700 Tm (" + PANEL_TEXT.encode() + b") Tj ET\n"
        b"BT /F1 9 Tf 1 0 0 1 " + str(device_x + 5).encode() + b" "
        + str(device_y + 5).encode() + b" Tm (" + DEVICE_TEXT.encode() + b") Tj ET\n"
        b"BT /F1 8 Tf 1 0 0 1 72 650 Tm (" + CIRCUIT_TEXT.encode() + b") Tj ET\n"
        + branch
        + b"q 1 0 0 1 " + str(device_x).encode() + b" " + str(device_y).encode()
        + b" cm /EVSE1 Do Q\n"
        + b"BT /F1 10 Tf 1 0 0 1 20 760 Tm (SCALE: 1/8\\" + b'" = 1' + b"'-0" + b'") Tj ET\n'
        + walls
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


#: 1/8" = 1'-0", the scale the sheet prints: 96 inches per inch of paper,
#: 72 points per inch, so one displayed point is 0.0254 * 96 / 72 metres.
METERS_PER_POINT = 0.0254 * 96.0 / 72.0


def _handoff(path: Path, confidence: float = 0.9) -> SourceWallEvidence:
    document = architecture_extract_pdf(path)
    return source_wall_evidence(
        document,
        page_number=1,
        confidence=confidence,
        meters_per_point=METERS_PER_POINT,
    )


def _unresolved_reasons(model, code: str) -> list[dict]:
    """Wall-guard records from both circuit-materialization sinks."""

    attributes = model.attributes.get("pdf_electrical", {})
    return [
        item
        for item in attributes.get("unresolved_circuits", ())
        if item.get("reason_code") == code
    ] + [
        item
        for item in attributes.get("unresolved_topology", ())
        if item.get("reason") == code
    ]


# ---------------------------------------------------------------- producer


def test_producer_binds_open_and_closed_chains(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    _write_combined_sheet(source, walls=OPEN_CHAIN_LINES + CLOSED_GRID_LINES)
    handoff = _handoff(source)
    assert handoff.classifier == "pdf_architecture.sheet_wall_evidence"
    assert handoff.page_number == 1
    assert handoff.chains, "the classified wall geometry must reach the handoff"
    assert any(chain.open for chain in handoff.chains), (
        "the L-shaped run is an open chain"
    )
    for chain in handoff.chains:
        for segment in chain.segments:
            assert segment.source_element_ids
            assert handoff.frame_pt[0] <= segment.start_pt[0] <= handoff.frame_pt[2]
    # Chain ids are content-derived and stable across re-extraction.
    again = _handoff(source)
    assert [chain.chain_id for chain in again.chains] == [
        chain.chain_id for chain in handoff.chains
    ]


def test_chain_ids_and_openness_are_permutation_stable(tmp_path: Path) -> None:
    from oabm.importers.pdf_architecture.wall_evidence import _chain_evidence
    from oabm.importers.pdf_architecture.drawing_regions import RegionEvidence

    evidence = (
        RegionEvidence(start_pt=(0.0, 0.0), end_pt=(10.0, 0.0), source_element_ids=("a",)),
        RegionEvidence(start_pt=(10.0, 0.0), end_pt=(20.0, 0.0), source_element_ids=("b",)),
        RegionEvidence(start_pt=(20.0, 0.0), end_pt=(20.0, 10.0), source_element_ids=("c",)),
    )
    first = _chain_evidence(evidence, 0.5)
    second = _chain_evidence(tuple(reversed(evidence)), 0.5)
    assert [chain.chain_id for chain in first] == [chain.chain_id for chain in second]
    assert len(first) == 1
    assert first[0].open is True  # two nodes of degree one: a tree
    loop = evidence + (
        RegionEvidence(start_pt=(20.0, 10.0), end_pt=(0.0, 10.0), source_element_ids=("d",)),
        RegionEvidence(start_pt=(0.0, 10.0), end_pt=(0.0, 0.0), source_element_ids=("e",)),
    )
    closed = _chain_evidence(loop, 0.5)
    assert len(closed) == 1
    assert closed[0].open is False


def test_handoff_type_refuses_out_of_frame_and_bad_ids() -> None:
    with pytest.raises(WallEvidenceHandoffError, match="outside the claimed drawing frame"):
        SourceWallEvidence(
            source_id="sha256:" + "0" * 64,
            content_sha256="0" * 64,
            classifier="pdf_architecture.sheet_wall_evidence",
            classifier_version="1",
            page_number=1,
            drawing_index=0,
            meters_per_point=0.1,
            evidence_kind="paired",
            confidence=0.9,
            frame_pt=(0.0, 0.0, 100.0, 100.0),
            chains=(
                WallChainEvidence(
                    chain_id="wall-chain:x",
                    open=True,
                    segments=(
                        WallChainSegment(
                            start_pt=(10.0, 10.0),
                            end_pt=(10.0, 200.0),
                            source_element_ids=("line-1",),
                        ),
                    ),
                ),
            ),
        )
    with pytest.raises(WallEvidenceHandoffError, match="sha-256"):
        SourceWallEvidence(
            source_id="s",
            content_sha256="nothex",
            classifier="c",
            classifier_version="1",
            page_number=1,
            drawing_index=0,
            meters_per_point=0.1,
            evidence_kind="k",
            confidence=0.9,
            frame_pt=(0.0, 0.0, 10.0, 10.0),
            chains=(),
        )


# ---------------------------------------------------------------- guard


def test_branch_into_classified_open_chain_stays_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    _write_combined_sheet(
        source, branch=BRANCH_INTO_OPEN_CHAIN, walls=OPEN_CHAIN_LINES,
        device_position=(301, 366),
    )
    handoff = _handoff(source)
    assert handoff.chains and any(chain.open for chain in handoff.chains)
    model = ElectricalPdfImporter().import_pdf(source, wall_evidence=handoff)
    rejections = _unresolved_reasons(model, "enters_classified_wall_chain")
    assert len(rejections) == 1
    rejection = rejections[0]
    assert rejection["wall_chain_id"]
    assert rejection["wall_evidence_element_ids"]
    assert rejection["wall_evidence_source_sha256"] == handoff.content_sha256
    assert rejection["status"] == "unresolved"
    # No bogus circuit materialized for the rejected run: the sheet has no
    # other resolvable run, so the model carries no circuits at all.
    assert not model.circuits
    report = model.attributes["pdf_electrical"]["branch_wall_evidence"]
    assert report["status"] == "applied"
    assert report["rejected_runs"] == 1
    assert report["open_chain_count"] >= 1


def test_branch_into_classified_closed_grid_stays_unresolved(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    _write_combined_sheet(
        source, branch=BRANCH_INTO_CLOSED_GRID, walls=CLOSED_GRID_LINES,
        device_position=(494, 334),
    )
    model = ElectricalPdfImporter().import_pdf(source, wall_evidence=_handoff(source))
    rejections = _unresolved_reasons(model, "enters_classified_wall_chain")
    assert len(rejections) == 1
    assert rejections[0]["wall_chain_open"] is False
    assert not model.circuits


def test_lawful_multitap_branch_with_matching_topology_resolves(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    # Same tree topology as the open wall chain, but the branch touches no
    # classified wall geometry: shape must not be a surrogate for evidence.
    _write_combined_sheet(
        source, branch=BRANCH_MULTITAP, walls=OPEN_CHAIN_LINES,
        device_position=(280, 600),
    )
    model = ElectricalPdfImporter().import_pdf(source, wall_evidence=_handoff(source))
    assert not _unresolved_reasons(model, "enters_classified_wall_chain")
    assert model.circuits, "the lawful homerun must resolve"
    report = model.attributes["pdf_electrical"]["branch_wall_evidence"]
    assert report["status"] == "applied"
    assert report["rejected_runs"] == 0


def test_branches_resolve_when_no_wall_evidence_is_supplied(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    _write_combined_sheet(
        source, branch=BRANCH_INTO_OPEN_CHAIN, walls=OPEN_CHAIN_LINES,
    )
    model = ElectricalPdfImporter().import_pdf(source)
    # Without the handoff the guard is absent: the limitation is reported,
    # never silently claimed as covered.
    report = model.attributes["pdf_electrical"]["branch_wall_evidence"]
    assert report["status"] == "absent"
    assert "#76" in report["note"]
    assert not _unresolved_reasons(model, "enters_classified_wall_chain")


def test_stale_source_evidence_refuses(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _write_combined_sheet(first, walls=OPEN_CHAIN_LINES)
    _write_combined_sheet(second, walls=OPEN_CHAIN_LINES, branch=BRANCH_MULTITAP)
    handoff = _handoff(first)
    document = electrical_extract_pdf(second)
    with pytest.raises(ElectricalPdfError, match="different source|stale"):
        ElectricalPdfImporter().import_document(document, wall_evidence=handoff)


def test_wrong_page_evidence_refuses(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    _write_combined_sheet(source, walls=OPEN_CHAIN_LINES)
    handoff = _handoff(source)
    from dataclasses import replace as _replace

    wrong_page = _replace(handoff, page_number=2)
    document = electrical_extract_pdf(source)
    with pytest.raises(ElectricalPdfError, match="outside this document"):
        ElectricalPdfImporter().import_document(document, wall_evidence=wrong_page)


def test_contact_with_the_own_symbol_outline_is_not_a_wall_conflict() -> None:
    """A branch may terminate at its own recognized symbol's strokes.

    The wall classifier shown the same combined sheet can claim those
    strokes; contact with them is the device, not architecture, while the
    same chain contacted anywhere else still conflicts.
    """

    from oabm.importers.pdf_electrical.importer import _wall_chain_conflict
    from oabm.importers.pdf_electrical import PdfVectorPathObservation

    device_position = (300.0, 300.0)
    symbol = PdfVectorPathObservation(
        element_id="p1:vector:00009",
        page=1,
        points_pt=(
            device_position,
            (312.0, device_position[1]),
            (312.0, 312.0),
            (device_position[0], 312.0),
            device_position,
        ),
        closed=True,
    )
    branch = PdfVectorPathObservation(
        element_id="p1:vector:00001",
        page=1,
        points_pt=((72.0, 700.0), (306.0, 306.0)),
    )
    symbol_stroke_chain = WallChainEvidence(
        chain_id="wall-chain:symbol",
        open=False,
        segments=(
            WallChainSegment(
                start_pt=(300.0, 300.0),
                end_pt=(312.0, 300.0),
                source_element_ids=("p1:line:symbol-bottom",),
            ),
        ),
    )
    assert _wall_chain_conflict(
        (branch,), (symbol_stroke_chain,),
        own_symbol_outlines=(symbol,), tolerance_pt=4.0,
    ) is None
    real_wall_chain = WallChainEvidence(
        chain_id="wall-chain:real",
        open=True,
        segments=(
            WallChainSegment(
                start_pt=(301.5, 320.0),
                end_pt=(301.5, 400.0),
                source_element_ids=("p1:line:wall",),
            ),
        ),
    )
    conflict = _wall_chain_conflict(
        (branch,), (real_wall_chain,),
        own_symbol_outlines=(symbol,), tolerance_pt=4.0,
    )
    assert conflict is not None and conflict.chain_id == "wall-chain:real"


def test_guard_never_fires_without_wall_contact(tmp_path: Path) -> None:
    source = tmp_path / "sheet.pdf"
    _write_combined_sheet(source, walls=OPEN_CHAIN_LINES + CLOSED_GRID_LINES)
    model = ElectricalPdfImporter().import_pdf(source, wall_evidence=_handoff(source))
    assert not _unresolved_reasons(model, "enters_classified_wall_chain")
    assert model.circuits
