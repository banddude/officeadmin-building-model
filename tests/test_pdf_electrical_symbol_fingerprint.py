"""Tests for Form XObject interior-geometry fingerprinting (issue #259).

When a PDF uses opaque Form XObject resource names (e.g. "Fm0"), the
English-word regexes in DEFAULT_SYMBOL_RULES cannot match. Fingerprinting
the XObject's interior vector geometry with _cluster_shape_signature
identifies the symbol without relying on the name.
"""

import pytest

from oabm.importers.pdf_electrical.importer import (
    ElectricalPdfError,
    PdfSymbolObservation,
    PdfVectorPathObservation,
    SymbolShapeRule,
    _classify_symbol_by_shape,
    _form_interior_shape_signature,
)


def _vector(
    element_id: str,
    points: tuple[tuple[float, float], ...],
    *,
    closed: bool = False,
) -> PdfVectorPathObservation:
    return PdfVectorPathObservation(
        element_id=element_id,
        page=1,
        points_pt=points,
        closed=closed,
    )


def _square_vectors(prefix: str) -> tuple[PdfVectorPathObservation, ...]:
    """Four line segments forming a 10x10 square."""
    return (
        _vector(f"{prefix}:0", ((0.0, 0.0), (10.0, 0.0))),
        _vector(f"{prefix}:1", ((10.0, 0.0), (10.0, 10.0))),
        _vector(f"{prefix}:2", ((10.0, 10.0), (0.0, 10.0))),
        _vector(f"{prefix}:3", ((0.0, 10.0), (0.0, 0.0))),
    )


def _triangle_vectors(prefix: str) -> tuple[PdfVectorPathObservation, ...]:
    """Three line segments forming a triangle."""
    return (
        _vector(f"{prefix}:0", ((0.0, 0.0), (10.0, 0.0))),
        _vector(f"{prefix}:1", ((10.0, 0.0), (5.0, 10.0))),
        _vector(f"{prefix}:2", ((5.0, 10.0), (0.0, 0.0))),
    )


def test_identical_geometry_produces_identical_signature():
    """Two XObjects with the same interior paths share a signature."""
    first = _form_interior_shape_signature(_square_vectors("a"))
    second = _form_interior_shape_signature(_square_vectors("b"))
    assert first is not None
    assert second is not None
    assert first == second


def test_different_geometry_produces_different_signature():
    """A square and a triangle do not share a signature."""
    square = _form_interior_shape_signature(_square_vectors("a"))
    triangle = _form_interior_shape_signature(_triangle_vectors("a"))
    assert square is not None
    assert triangle is not None
    assert square != triangle


def test_empty_interior_returns_none():
    assert _form_interior_shape_signature(()) is None


def test_translated_geometry_produces_identical_signature():
    """The signature is translation-invariant (normalized by bbox center)."""
    shifted = tuple(
        _vector(
            f"s:{i}",
            tuple((x + 100.0, y + 50.0) for x, y in vector.points_pt),
        )
        for i, vector in enumerate(_square_vectors("a"))
    )
    original = _form_interior_shape_signature(_square_vectors("a"))
    moved = _form_interior_shape_signature(shifted)
    assert original == moved


def test_scaled_geometry_produces_identical_signature():
    """The signature is scale-invariant (normalized by bbox extent)."""
    scaled = tuple(
        _vector(
            f"s:{i}",
            tuple((x * 2.5, y * 2.5) for x, y in vector.points_pt),
        )
        for i, vector in enumerate(_square_vectors("a"))
    )
    original = _form_interior_shape_signature(_square_vectors("a"))
    bigger = _form_interior_shape_signature(scaled)
    assert original == bigger


def test_symbol_observation_accepts_shape_signature():
    symbol = PdfSymbolObservation(
        element_id="p1:xobject:00001",
        page=1,
        name="Fm0",
        x_pt=10.0,
        y_pt=20.0,
        shape_signature="abc123",
    )
    assert symbol.shape_signature == "abc123"


def test_symbol_observation_shape_signature_defaults_to_none():
    symbol = PdfSymbolObservation(
        element_id="p1:xobject:00001",
        page=1,
        name="Fm0",
        x_pt=10.0,
        y_pt=20.0,
    )
    assert symbol.shape_signature is None


def test_classify_symbol_by_shape_matches_signature():
    signature = _form_interior_shape_signature(_square_vectors("a"))
    assert signature is not None
    symbol = PdfSymbolObservation(
        element_id="p1:xobject:00001",
        page=1,
        name="Fm0",  # opaque name; regexes cannot match
        x_pt=10.0,
        y_pt=20.0,
        shape_signature=signature,
    )
    rules = (
        SymbolShapeRule(
            shape_signature=signature,
            entity_kind="device",
            canonical_type="receptacle_duplex",
            confidence=0.95,
        ),
    )
    classification, ranked = _classify_symbol_by_shape(
        symbol, rules, ambiguity_margin=0.08
    )
    assert classification == ("device", "receptacle_duplex", 0.95)
    assert len(ranked) == 1


def test_classify_symbol_by_shape_no_signature_returns_none():
    symbol = PdfSymbolObservation(
        element_id="p1:xobject:00001",
        page=1,
        name="Fm0",
        x_pt=10.0,
        y_pt=20.0,
        shape_signature=None,
    )
    rules = (
        SymbolShapeRule(
            shape_signature="abc123",
            entity_kind="device",
            canonical_type="receptacle_duplex",
            confidence=0.95,
        ),
    )
    classification, ranked = _classify_symbol_by_shape(
        symbol, rules, ambiguity_margin=0.08
    )
    assert classification is None
    assert ranked == []


def test_classify_symbol_by_shape_no_match_returns_none():
    symbol = PdfSymbolObservation(
        element_id="p1:xobject:00001",
        page=1,
        name="Fm0",
        x_pt=10.0,
        y_pt=20.0,
        shape_signature="deadbeef",
    )
    rules = (
        SymbolShapeRule(
            shape_signature="abc123",
            entity_kind="device",
            canonical_type="receptacle_duplex",
            confidence=0.95,
        ),
    )
    classification, ranked = _classify_symbol_by_shape(
        symbol, rules, ambiguity_margin=0.08
    )
    assert classification is None
    assert ranked == []


def test_symbol_shape_rule_rejects_blank_signature():
    with pytest.raises(ElectricalPdfError):
        SymbolShapeRule(
            shape_signature="  ",
            entity_kind="device",
            canonical_type="receptacle_duplex",
            confidence=0.95,
        )


def test_symbol_shape_rule_rejects_bad_entity_kind():
    with pytest.raises(ElectricalPdfError):
        SymbolShapeRule(
            shape_signature="abc123",
            entity_kind="widget",
            canonical_type="receptacle_duplex",
            confidence=0.95,
        )


def test_symbol_shape_rule_rejects_bad_confidence():
    with pytest.raises(ElectricalPdfError):
        SymbolShapeRule(
            shape_signature="abc123",
            entity_kind="device",
            canonical_type="receptacle_duplex",
            confidence=1.5,
        )
