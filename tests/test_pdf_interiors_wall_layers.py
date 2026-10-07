"""Interiors wall layers use the same explicit authority as architectural walls."""
import pytest
from oabm.importers.pdf_architecture.layered_rooms import _wall_layer_lines
from oabm.importers.pdf_architecture.extract import extract_pdf, _is_wall_source_layer
from oabm.importers.pdf_architecture.importer import import_observations
from oabm.importers.pdf_architecture.types import ImportOptions, ScaleOverride
from oabm.model import validate_model
from test_pdf_architecture_importer import _write_layered_wall_source


@pytest.mark.parametrize('layer', ['I-WALL', 'fixture|I-WALL', '_i-wall', 'I-WALL-PATT'])
def test_visible_interiors_wall_layer_is_explicit_source_evidence(tmp_path, layer):
    path = tmp_path/'synthetic-interiors-walls.pdf'
    _write_layered_wall_source(path, wall_layer=layer)
    doc = extract_pdf(path)
    scale = .03386666666666666
    assert _is_wall_source_layer(layer)
    assert len(_wall_layer_lines(doc.pages[0])) == 4
    assert all(line.source_layers == (layer,) for line in _wall_layer_lines(doc.pages[0]))
    model = import_observations(doc, options=ImportOptions(default_wall_height_m=3., scale_overrides=(ScaleOverride(1, scale),)))
    validate_model(model)
    assert len(model.walls) == 2
    assert not model.spaces
    assert all(w.attributes['pdf_architecture']['source_layers'] == [layer] for w in model.walls)


def test_hidden_interiors_wall_layer_keeps_the_existing_fail_closed_boundary(tmp_path):
    path = tmp_path/'synthetic-hidden-interiors.pdf'
    _write_layered_wall_source(path, wall_layer='I-WALL', hidden_wall=True, duplicate_visible=True)
    doc = extract_pdf(path)
    assert doc.pages[0].hidden_wall_source_present
    assert all('I-WALL' not in line.source_layers for line in doc.pages[0].lines)
    model = import_observations(doc, options=ImportOptions(default_wall_height_m=3., scale_overrides=(ScaleOverride(1, .03386666666666666),)))
    validate_model(model)
    assert not model.walls


@pytest.mark.parametrize('layer', ['I-FURN', 'I-WALL-DEMO', 'ID-WALL', 'I-ANNO', 'OTHER-I-WALL'])
def test_interiors_support_does_not_promote_other_or_demolition_layers(layer):
    assert not _is_wall_source_layer(layer)
