"""Opt-in review of the voltage-drop facts in a validated v1 sizing proposal.

This is a presentation of provider-reported values, not a new calculation,
engineering certification, approval, or mutation of the canonical model.
"""
import json
from importlib.resources import files
from jsonschema import Draft202012Validator, ValidationError

_SCHEMA=Draft202012Validator(json.loads(files("oabm.electrical._sizing_contracts").joinpath("voltage-drop-review-v1.schema.json").read_text()))
from oabm.electrical.sizing_review import preview
from oabm.electrical.sizing_provider import _portable_json
from oabm.routing.physical_capture import capture, SOURCE_BYTES
from oabm.routing.physical_verify_types import VerificationStop


def summarize_voltage_drop(model, proposal):
    """Validate the proposal against the current model, then expose its drop.

    The provider output is editable and unauthenticated. A passing comparison
    against a caller's limit is not a code-compliance determination.
    """
    try:
        snapshot=capture(model, byte_limit=SOURCE_BYTES)
        captured=capture(_portable_json(capture(proposal, byte_limit=8*1024*1024)),byte_limit=8*1024*1024)
    except (TypeError, ValueError, RecursionError, VerificationStop):
        raise ValueError('invalid or oversized model or voltage-drop proposal') from None
    checked=preview(snapshot,captured)
    inputs=captured['inputs']; result=captured['result']
    output = {
        'version':'voltage-drop-review-v1', 'purpose':'review_only',
        'model_id':checked['model_id'], 'model_sha256':checked['model_sha256'],
        'circuit_id':checked['circuit_id'], 'request_digest':checked['request_digest'],
        'provider':checked['provider'], 'phase':inputs['phase'],
        'voltage_v':inputs['voltage_v'], 'operating_amps':result['operating_amps'],
        'load_amps_input':inputs.get('load_amps'), 'load_va_input':inputs.get('load_va'),
        'proposed_wire_size':result['wire_size'], 'material':inputs['material'],
        'one_way_length_ft':inputs['length_ft'], 'length_basis':inputs['length_basis'],
        'source_route_id':inputs.get('source_route_id'),
        'voltage_drop_percent':result['voltage_drop_percent'],
        'user_limit_percent':inputs['vd_limit_pct'],
        'comparison':'within_user_limit',
        'provider_output_authentication':'not_verified', 'applied':False,
        'provider_notes_untrusted':captured.get('limitations',[]),
        'limitations':[
            'Provider-reported values are unauthenticated and have not been independently recalculated.',
            'Comparison with the supplied limit is not a code-compliance determination.',
            'Source facts, conductor suitability, and field conditions remain unverified.',
            'One-way length may exclude leads and slack; installation allowances need review.',
            'The calculation method and power factor are not specified by this contract.',
        ],
    }

    try:
        _SCHEMA.validate(output)
    except ValidationError:
        raise ValueError('invalid voltage-drop review') from None
    return output


def validate_voltage_drop_review(model, proposal, report):
    """Compare a saved report to a fresh reconstruction; never authenticate it."""
    try:
        submitted=capture(_portable_json(capture(report, byte_limit=8*1024*1024)),byte_limit=8*1024*1024)
        _SCHEMA.validate(submitted)
    except (VerificationStop, ValidationError, TypeError, ValueError, RecursionError):
        raise ValueError('invalid voltage-drop review') from None
    expected=summarize_voltage_drop(model,proposal)
    if submitted != expected:
        raise ValueError('voltage-drop review differs from current model and proposal')
    return json.dumps(expected,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False)
