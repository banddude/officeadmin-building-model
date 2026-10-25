"""Synthetic mixed-input packaging must fail before any SVG/DXF render."""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from oabm.model import ContractError
from oabm.pipeline.review_drawings import generate_review_drawings
from oabm.pipeline.review_sheet_delivery import generate_sheet_delivery
from test_review_electrical_plans import case
from test_review_sheet_delivery import config


@pytest.fixture
def handoff(tmp_path):
    model, _, _ = case(tmp_path)
    cfg = config(formats=('svg',))
    captured = {}

    def capture(drawing, cfg, payloads, status, **kwargs):
        captured.update(drawing=drawing, config=cfg, payloads=payloads.copy(), status=deepcopy(status))
        return {}

    with patch('oabm.pipeline.review_sheet_delivery.generate_sheet_delivery', capture):
        generate_review_drawings(model, cfg)
    return captured


def mutate_json(handoff, key, mutate):
    document = json.loads(handoff['payloads'][key])
    mutate(document)
    handoff['payloads'][key] = json.dumps(document).encode()


def render_forbidden(*args, **kwargs):
    raise AssertionError('renderer must not run on incoherent inputs')


def refuse(handoff, match):
    with patch('oabm.drawings.sheet_svg.render_sheet_svg', render_forbidden), \
         patch('oabm.drawings.sheet_dxf.sheet_to_dxf', render_forbidden):
        with pytest.raises(ContractError, match=match):
            generate_sheet_delivery(**handoff)


def test_valid_package_deterministic_and_inputs_untouched(handoff):
    before = deepcopy(handoff)
    first = generate_sheet_delivery(**handoff)
    assert first == generate_sheet_delivery(**handoff)
    assert handoff == before


def test_equivalent_json_and_optional_provenance_defaults(handoff):
    # JSON whitespace/key order and optional canonical provenance defaults may
    # differ. The package retains supplied bytes instead of rewriting them.
    for key in ('drawing_set', 'panel_facts'):
        document = json.loads(handoff['payloads'][key])
        if key == 'drawing_set':
            for source in document['drawing_set']['source_index']:
                for provenance in source['provenance']:
                    for name in ('source_element_id', 'page', 'method', 'derivation'):
                        if provenance.get(name) is None:
                            provenance.pop(name, None)
        handoff['payloads'][key] = json.dumps(document, indent=4, sort_keys=True).encode()
    before = deepcopy(handoff)
    generate_sheet_delivery(**handoff)
    assert handoff == before


def test_mixed_panel_revision_refused(handoff):
    mutate_json(handoff, 'panel_facts', lambda d: d['review'].update(canonical_model_sha256='0'*64))
    refuse(handoff, 'panel facts belong to a different model revision')


def test_mixed_status_refused(handoff):
    handoff['status']['canonical_model_sha256'] = '0'*64
    refuse(handoff, 'drawing review differs')


def test_mixed_titleblock_revision_refused(handoff):
    cfg = handoff['config']
    sheet = cfg.sheets[0]
    handoff['config'] = replace(cfg, sheets=(replace(sheet, paper=replace(sheet.paper, revision='STALE')),))
    refuse(handoff, 'configuration differs')


def test_mixed_typed_drawing_refused(handoff):
    drawing = handoff['drawing']
    handoff['drawing'] = replace(drawing, views=(replace(drawing.views[0], title='STALE'),))
    refuse(handoff, 'typed drawing geometry')


def test_mixed_view_binding_refused(handoff):
    drawing = handoff['drawing']
    view = drawing.views[0]
    metadata = tuple((k, '0'*64 if k == 'canonical_model_sha256' else v) for k,v in view.metadata)
    handoff['drawing'] = replace(drawing, views=(replace(view, metadata=metadata),))
    refuse(handoff, 'drawing view has a missing or conflicting')


def test_panel_schedule_difference_refused_even_when_both_drawing_copies_match(handoff):
    drawing = handoff['drawing']
    handoff['drawing'] = replace(drawing, schedules=())
    mutate_json(handoff, 'drawing_set', lambda d: d['drawing_set'].update(schedules=[]))
    refuse(handoff, 'panel facts differ from drawing schedules')


def test_provenance_projection_difference_refused(handoff):
    mutate_json(handoff, 'drawing_set', lambda d: d['drawing_set']['source_index'][0].update(confidence=.125))
    refuse(handoff, 'source identity or confidence mismatch')


@pytest.mark.parametrize('key', ['drawing_set', 'panel_facts'])
@pytest.mark.parametrize('data', [b'{bad', b'[]', b'{"review":{},"review":{}}', b'{"x":NaN}', b'\xff', None])
def test_malformed_documents_refused_before_render(handoff, key, data):
    handoff['payloads'][key] = data
    refuse(handoff, 'sheet input coherence')


def test_boolean_is_not_numeric_false_equivalent(handoff):
    mutate_json(handoff, 'panel_facts', lambda d: d['review'].update(permit_ready=0))
    refuse(handoff, 'panel facts belong')


def test_boolean_is_not_numeric_confidence_equivalent(handoff):
    mutate_json(handoff, 'drawing_set', lambda d: d['drawing_set']['source_index'][0].update(confidence=True))
    refuse(handoff, 'source identity or confidence mismatch')


def test_json_numeric_spelling_is_equivalent(handoff):
    mutate_json(handoff, 'drawing_set', lambda d: d['drawing_set']['source_index'][0].update(confidence=1))
    generate_sheet_delivery(**handoff)


def test_invalid_typed_panel_report_has_coherence_diagnostic(handoff):
    mutate_json(handoff, 'panel_facts', lambda d: d.update(panel_facts=[]))
    refuse(handoff, 'sheet input coherence: invalid panel fact report')


def test_matching_revision_labels_do_not_hide_changed_panel_facts(handoff, tmp_path):
    from oabm.reports import generate_panel_schedule_report
    model, _, _ = case(tmp_path / 'other')
    revised = replace(model, circuits=(replace(model.circuits[0], circuit_number='OTHER'),))
    facts = generate_panel_schedule_report(revised).to_dict()
    mutate_json(handoff, 'panel_facts', lambda d: d.update(panel_facts=facts))
    refuse(handoff, 'panel facts differ from drawing schedules')


def test_overflowed_float_in_enriched_provenance_is_refused(handoff):
    data = json.loads(handoff['payloads']['drawing_set'])
    data['drawing_set']['source_index'][0]['provenance'][0]['attributes'] = {'overflow': 'SENTINEL'}
    handoff['payloads']['drawing_set'] = json.dumps(data).replace('"SENTINEL"', '1e400').encode()
    refuse(handoff, 'sheet input coherence')


def test_guard_does_not_change_valid_export_bytes(handoff):
    with patch('oabm.pipeline.sheet_coherence.validate_sheet_inputs', lambda *args: None):
        original_bytes = generate_sheet_delivery(**handoff)
    assert generate_sheet_delivery(**handoff) == original_bytes
