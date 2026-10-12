"""Native PDF rectangles must retain the same eligible material as path strips."""
from dataclasses import replace

from oabm.importers.pdf_architecture.fill_paint import wall_geometry_page
from oabm.importers.pdf_architecture.types import PdfRectObservation
from test_pdf_architecture_filled_targets import FIXTURE, MPP, _plan, _document
from oabm.importers.pdf_architecture import sheet_wall_evidence
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.model import validate_model


def _native_plan(gray=.4):
    return replace(_plan(excluded=False), lines=(), rects=tuple(
        PdfRectObservation(f'native-wall-{i}', tuple(box), filled=True,
                           fill_grays=(gray,), stroke_present=False)
        for i, box in enumerate(FIXTURE['plan_a_strips'])
    ))


def test_native_filled_rectangles_supply_the_same_bounded_plan_evidence():
    page = _native_plan()
    evidence = sheet_wall_evidence(page, meters_per_point=MPP)
    assert len(evidence.drawings) == 1
    assert len(evidence.drawings[0][1]) >= 8
    model = import_observations(_document(page, 'arch'))
    validate_model(model)
    assert len(model.walls) >= 4
    assert model.to_json() == import_observations(_document(page, 'arch')).to_json()


def test_native_fill_expansion_is_idempotent_and_leaves_observations_intact():
    page = _native_plan()
    view = wall_geometry_page(page, expand_native_fills=True)
    assert len(view.lines) == 4 * len(page.rects)
    assert wall_geometry_page(view, expand_native_fills=True) == view
    assert not page.lines


def test_white_native_rectangles_never_supply_material_edges():
    page = _native_plan(1)
    assert not wall_geometry_page(page, expand_native_fills=True).lines
    assert not import_observations(_document(page, 'arch')).walls


def test_equal_number_of_removed_masks_and_added_edges_does_not_return_old_page():
    from oabm.importers.pdf_architecture.extract import _rect_edge_segments
    page = _native_plan()
    rect = page.rects[0]
    white = replace(rect, element_id='unrelated-white-mask', bbox_pt=(10, 10, 20, 20), fill_grays=(1.,))
    page = replace(page, rects=(rect,), lines=_rect_edge_segments((white,), page.page_number))
    view = wall_geometry_page(page, expand_native_fills=True)
    assert len(view.lines) == len(page.lines) == 4
    assert all(line.fill_grays == (.4,) for line in view.lines)
    assert view != page


def test_unknown_or_conflicting_native_fill_never_expands():
    page = _native_plan()
    for paints in ((), (None,), (.4, 1.), (.4, None)):
        source = replace(page, rects=tuple(replace(rect, fill_grays=paints) for rect in page.rects))
        assert not wall_geometry_page(source, expand_native_fills=True).lines


def test_duplicate_native_and_path_boundaries_do_not_inflate_walls():
    native = _native_plan()
    source = replace(native, lines=tuple(replace(line, source_layers=(), fill_grays=(.4,))
                                        for line in _plan(excluded=False).lines))
    a = import_observations(_document(native, 'arch'))
    b = import_observations(_document(source, 'arch'))
    assert len(a.walls) == len(b.walls)
    signature = lambda wall: repr((wall.thickness_m, wall.centerline.points))
    assert sorted(map(signature, a.walls)) == sorted(map(signature, b.walls))


def test_explicit_nonwall_layer_and_unfilled_rectangles_are_not_promoted():
    page = _native_plan()
    furniture = replace(page, rects=tuple(replace(rect, source_layer='A-FURN') for rect in page.rects))
    assert not wall_geometry_page(furniture, expand_native_fills=True).lines
    outlines = replace(page, rects=tuple(replace(rect, filled=False, stroke_present=True) for rect in page.rects))
    assert not wall_geometry_page(outlines, expand_native_fills=True).lines


def test_compact_native_fill_does_not_become_a_wall():
    page = replace(_native_plan(), rects=(PdfRectObservation('compact-symbol', (200, 250, 260, 310),
                                                           filled=True, fill_grays=(.4,), stroke_present=False),))
    assert not import_observations(_document(page, 'arch')).walls


def test_unclassified_rectangles_keep_the_existing_normalization_path():
    page = _native_plan()
    assert wall_geometry_page(page) == page
    no_role = replace(page, texts=tuple(text for text in page.texts if text.element_id != 'title'))
    assert not sheet_wall_evidence(no_role, meters_per_point=MPP).drawings


def test_native_rectangle_cannot_recreate_coincident_mask_path_edges():
    from oabm.importers.pdf_architecture.extract import _rect_edge_segments
    page = _native_plan()
    for paints in ((1.,), (None,), (.4, 1.), (.4, None)):
        masks = tuple(replace(rect, element_id=f"mask-{rect.element_id}",
                              fill_grays=paints) for rect in page.rects)
        source = replace(page, lines=_rect_edge_segments(masks, page.page_number))
        view = wall_geometry_page(source, expand_native_fills=True)
        assert not any(line.fill_grays == (.4,) for line in view.lines)
        assert not import_observations(_document(source, 'arch')).walls
        assert wall_geometry_page(view, expand_native_fills=True) == view


def test_coincident_mask_keeps_independent_stroke_without_promoting_its_fill():
    from oabm.importers.pdf_architecture.extract import _rect_edge_segments
    page = _native_plan()
    masks = tuple(replace(rect, element_id=f"mask-{rect.element_id}",
                          fill_grays=(1.,), stroke_present=True) for rect in page.rects)
    source = replace(page, lines=_rect_edge_segments(masks, page.page_number))
    view = wall_geometry_page(source, expand_native_fills=True)
    assert view.lines == source.lines
    assert all(line.stroke_present and line.fill_grays == (1.,) for line in view.lines)
