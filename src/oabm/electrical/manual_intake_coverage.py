"""Connect manual device intake to source coverage without guessing mappings."""
from __future__ import annotations
import json
from importlib.resources import files
from jsonschema import Draft202012Validator,ValidationError
from oabm.electrical.sizing_provider import _portable_json
from oabm.electrical.manual_device_intake import admit_manual_devices
from oabm.reports.source_coverage import inventory_fingerprint, model_fingerprint, source_coverage
from oabm.reports.source_coverage_cli import _source_hash
from oabm.model import ContractError
from oabm.routing.physical_capture import capture, CANDIDATE_BYTES
from oabm.routing.physical_verify_types import VerificationStop


_SCHEMA=Draft202012Validator(json.loads(files('oabm.electrical._manual_coverage_contracts').joinpath('manual-intake-coverage-v1.schema.json').read_text()))

def reconcile_manual_intake(base, *, intake_request, source_path, strict=False):
    """Recompute intake, hash the source file, and account for supplied IDs.

    Returns a detached candidate model and report. The tool never extracts IDs
    from the source, verifies mapping semantics or transmits data. In strict
    mode, any unresolved item rejects the handoff; it does not prove that the
    manually supplied inventory contains everything in the source.
    """
    if type(strict) is not bool:raise ValueError('strict must be boolean')
    try: request=capture(intake_request,byte_limit=CANDIDATE_BYTES)
    except VerificationStop as exc:raise ValueError('invalid or oversized intake request') from exc
    if not isinstance(request,dict) or set(request)!={'request_id','source_sha256','base_model_sha256','frame_id','items','request_sha256'}:
        raise ValueError('invalid intake request fields')
    # Check the actual bytes, not a caller-provided hash alone. The source may
    # still change after this local read and its contents are never interpreted.
    try: source_hash=_source_hash(source_path)
    except (OSError,ValueError,TypeError):raise ValueError('source file unavailable or invalid') from None
    if source_hash!=request['source_sha256']:raise ValueError('source file differs from intake assertion')
    candidate, intake=admit_manual_devices(base,**request)
    source_ids=[row['source_item_id'] for row in intake['items']]
    mapping={'inventory_sha256':inventory_fingerprint(source_hash,source_ids),
             'model_sha256':model_fingerprint(candidate),
             'entries':[{'source_id':row['source_item_id'],'canonical_ids':[row['device_id'],row['load_port_id']]}
                        for row in intake['items'] if row['status']=='admitted']}
    try: coverage=source_coverage(candidate,source_sha256=source_hash,object_ids=source_ids,mapping=mapping,strict=strict)
    except ContractError as exc:raise ValueError('invalid, stale, or unresolved source coverage') from exc
    report={'version':'manual-intake-coverage-v1','intake_request_sha256':intake['request_sha256'],
        'candidate_model_sha256':intake['candidate_model_sha256'],'strict':strict,'coverage':coverage,
        'source_authentication':'not_verified','registration':'not_verified','applied_to_original':False,
        'limitations':['Manually supplied source IDs may be incomplete; the source is hashed, not parsed.',
          'Mappings are derived from user-supplied intake, not independently verified observations.',
          'Source authenticity, registration, field conditions and code compliance are not verified.',
          'The report is bound to the intake candidate; later model changes require a fresh audit.',
          'The source path may change during or after reading; the hash identifies only the bytes read.']}
    try:_SCHEMA.validate(report)
    except ValidationError as exc:raise ValueError('invalid manual intake coverage report') from exc
    return candidate,report


def validate_manual_intake_coverage(base, *, intake_request, source_path, report):
    """Recompute a saved report against current inputs; not authentication."""
    try:
        submitted=capture(_portable_json(capture(report,byte_limit=CANDIDATE_BYTES)),byte_limit=CANDIDATE_BYTES)
        _SCHEMA.validate(submitted)
    except (VerificationStop,ValidationError,TypeError,ValueError,RecursionError) as exc:
        raise ValueError('invalid manual intake coverage report') from exc
    candidate,expected=reconcile_manual_intake(base,intake_request=intake_request,source_path=source_path,strict=submitted['strict'])
    canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if canonical(submitted)!=canonical(expected):raise ValueError('manual intake coverage report differs from current inputs')
    return candidate,canonical(expected)
