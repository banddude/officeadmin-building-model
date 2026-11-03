"""Source-bound semantic inventories and supplied panel facts on shared sheets."""
from __future__ import annotations

import argparse
import io
import xml.etree.ElementTree as ET
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from oabm.model import BuildingModel, stable_id, validate_model
from oabm.reports import generate_panel_schedule_report
from .blank_set import _decode, _json, _sha
from .generator import _entities, _source_reference, generate_schedules
from .model import DrawingSet
from .panel_schedules import panel_report_to_drawing_schedules
from .schedule_layout import ScheduleLayout, paginate_schedules, draft_panel_schedule_pages

VERSION = 'panel-schedule-sheet-set-v1'
COMBINED_VERSION = 'combined-schedule-sheet-set-v1'


def _schedule_entities(model):
    # Assembly records are canonical owners but intentionally do not inherit
    # Entity, so the panel report's Entity-only collection index is insufficient.
    return {entity.id: entity for entity in (
        *_entities(model), *model.building_assemblies, *model.assembly_components)}


def _schedule_source_records(model):
    from oabm.reports.panel_schedules import ENTITY_TYPES
    document = model.to_dict()
    collections = (*ENTITY_TYPES, 'building_assemblies', 'assembly_components')
    return {entity['id']: {'collection': collection, 'entity': entity}
        for collection in collections for entity in document.get(collection, [])}


def _panel_totals(report):
    return [{'panel_id': panel.panel.id, 'known_connected_va': panel.known_connected_va,
        'all_listed_loads_known': panel.all_listed_loads_known,
        'subtotal_source_circuit_ids': list(panel.subtotal_source_circuit_ids),
        'unknown_load_circuit_ids': list(panel.unknown_load_circuit_ids)}
        for panel in report.panels]


def _page_receipts(pages):
    return {
        'sheet_order': [page.spec.id for page in pages],
        'rows': [row for page in pages for row in page.row_map],
        'schedule_pages': [{'sheet_id': page.spec.id, **section}
            for page in pages for section in (page.sections or (
                {'schedule_id': page.schedule_id, 'schedule_page': page.schedule_page,
                 'schedule_pages': page.schedule_pages},))],
    }


def combined_schedule_drawing_set(model, *, report=None):
    """One validated snapshot, semantic inventories and supplied panel facts.

    Circuit inventory and panel fact rows intentionally refer to the same
    circuits. Selection outcomes describe decisions, not additional products.
    Site inventory remains unsupported and is refused by generate_schedules.
    """
    # Roundtrip to detach mutable source attributes from the caller.
    validate_model(model)
    snapshot = BuildingModel.from_json(model.to_json())
    if snapshot.to_dict() != model.to_dict():
        raise ValueError('source facts do not roundtrip through canonical contract')
    report = generate_panel_schedule_report(snapshot) if report is None else report
    report.validate_against_model(snapshot)
    schedules = (*generate_schedules(snapshot), *panel_report_to_drawing_schedules(report))
    entities = _schedule_entities(snapshot)
    drawing = DrawingSet(snapshot.model_id, (), schedules,
        tuple(_source_reference(e) for _, e in sorted(entities.items())))
    if any(row.source_id not in entities for schedule in schedules for row in schedule.rows):
        raise ValueError('schedule row has no canonical source')
    return report, drawing


def create_combined_schedule_sheet_set(model_path, profile_path, job_path, settings_path, *, output_directory):
    """Deliver all supported semantic schedules and panel facts as vector tables."""
    return _create_schedule_sheet_set(model_path, profile_path, job_path, settings_path,
        output_directory=output_directory, presentation='fact_ledger', combined=True)


def verify_combined_schedule_delivery(directory, *, model_path=None):
    """Check retained source, fact report, editable rows and file receipts.

    Supplying model_path also refuses a delivery from an older source revision.
    This is integrity/readback verification, not engineering or visual approval.
    """
    directory = Path(directory)
    manifest = _decode((directory / 'SCHEDULE-MANIFEST.json').read_bytes())
    if manifest['version'] != COMBINED_VERSION:
        raise ValueError('not a combined schedule delivery')
    expected_paths = {r['path'] for r in manifest['files']}
    actual_paths = {p.relative_to(directory).as_posix() for p in directory.rglob('*')
                    if p.is_file() and p.name != 'SCHEDULE-MANIFEST.json'}
    if len(expected_paths) != len(manifest['files']) or expected_paths != actual_paths:
        raise ValueError('delivery file coverage mismatch')
    for receipt in manifest['files']:
        path = Path(receipt['path'])
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('invalid delivery file path')
        raw = (directory / path).read_bytes()
        if len(raw) != receipt['bytes'] or _sha(raw) != receipt['sha256']:
            raise ValueError(f'delivery file changed: {path}')
    source = (directory / 'schedule-source/model.json').read_bytes()
    if _sha(source) != manifest['source_model_sha256']:
        raise ValueError('retained source binding mismatch')
    if model_path is not None and Path(model_path).read_bytes() != source:
        raise ValueError('delivery does not match supplied source bytes; regenerate')
    model = BuildingModel.from_json(source)
    from oabm.reports import PanelScheduleReport
    report = PanelScheduleReport.from_json((directory / 'schedule-source/panel-facts.json').read_text())
    report, drawing = combined_schedule_drawing_set(model, report=report)
    if drawing.to_dict() != _decode((directory / 'schedule-source/drawing-schedules.json').read_bytes()):
        raise ValueError('editable schedules do not match canonical source')
    if report.source_model_fingerprint != manifest['source_model_fingerprint']:
        raise ValueError('report source binding mismatch')
    sources = _schedule_source_records(model)
    expected_rows = [{'schedule_id': s.id, 'row_index': n, 'source_id': r.source_id,
        'source_model_sha256': _sha(source), **sources[r.source_id]}
        for s in drawing.schedules for n, r in enumerate(s.rows)]
    if manifest['row_sources'] != expected_rows:
        raise ValueError('row source binding mismatch')
    expected_cells = {(s.id, n, r.source_id): list(r.cells)
        for s in drawing.schedules for n, r in enumerate(s.rows)}
    rows = manifest['rows']
    if len(rows) != len(expected_cells) or manifest['row_count'] != len(rows) or {
        (r['schedule_id'], r['row_index'], r['source_id']): r['cells'] for r in rows} != expected_cells:
        raise ValueError('delivered row coverage/cells mismatch')
    # Recompute physical pagination and receipts from retained inputs. A hash
    # list alone cannot protect the manifest that contains that same list.
    from .company_sheets import resolve_company_sheet_set, _body_group
    retained = directory / 'schedule-source'
    settings = _decode((retained / 'settings.json').read_bytes())
    if not isinstance(settings, dict) or set(settings) != {'version', 'layout'} or settings['version'] != COMBINED_VERSION:
        raise ValueError('invalid retained combined schedule settings')
    layout = ScheduleLayout(**settings['layout'])
    profile = _decode((retained / 'profile.json').read_bytes())
    job = _decode((retained / 'job.json').read_bytes())
    # Logos do not affect schedule geometry. Never reopen original external
    # logo paths merely to verify a portable schedule delivery.
    profile['logos'] = {}
    job['logos'] = {}
    job['sheets'] = [{**sheet, 'logos': {}} for sheet in job['sheets']]
    resolved = resolve_company_sheet_set(retained / 'profile.json', retained / 'job.json',
        _profile_raw=_json(profile), _job_raw=_json(job))
    if len(resolved.sheets) != 1 or not {'svg', 'pdf'} <= set(resolved.formats):
        raise ValueError('invalid retained schedule template/formats')
    pages = paginate_schedules(drawing, resolved.sheets[0], layout=layout,
        empty_schedule_sources={stable_id('schedule', f'panel-facts:{p.panel.id}'): p.panel.id
                                for p in report.panels if not p.circuits})
    expected = {**_page_receipts(pages), 'panel_totals': _panel_totals(report),
        'model_id': model.model_id, 'layout': asdict(layout),
        'status': 'supplied_facts_only', 'engineering_approval': False,
        'drafting_grammar': 'fact-ledger-v1'}
    for key, value in expected.items():
        if _json(manifest.get(key)) != _json(value):
            raise ValueError(f'derived schedule receipt mismatch: {key}')
    def xml_content(node):
        return (node.tag, tuple(sorted(node.attrib.items())), node.text or '',
                tuple(xml_content(child) for child in node))
    for number, page in enumerate(pages, 1):
        stored = _decode((retained / f'{page.spec.id}.drawing.json').read_bytes())
        if _json(stored) != _json(page.drawing_set.to_dict()):
            raise ValueError('retained page drawing differs from regenerated pagination')
        body = page.body_svg(layout)
        if (retained / f'{page.spec.id}.body.svg').read_text() != body + '\n':
            raise ValueError('retained page body differs from regenerated pagination')
        svg = ET.fromstring((directory / 'sheets' / f'{number:03d}-{page.spec.id}.svg').read_bytes())
        groups = svg.findall('{http://www.w3.org/2000/svg}g')
        expected_group = _body_group(body, resolved.sheets[0].drawing_bounds_mm)
        if len(groups) != 1 or xml_content(groups[0]) != xml_content(expected_group):
            raise ValueError('rendered schedule geometry differs from regenerated pagination')
    verification = _verify_shared_output(directory, pages, resolved.formats)
    if _json(manifest.get('verification')) != _json(verification):
        raise ValueError('schedule verification receipt mismatch')
    return manifest


def panel_schedule_drawing_set(model):
    """Reuse the fact report and its adapter; do not recalculate supplied VA."""
    report = generate_panel_schedule_report(model)
    drawing = DrawingSet(model.model_id, (), panel_report_to_drawing_schedules(report),
        tuple(_source_reference(entity) for entity in sorted(_entities(model), key=lambda e: e.id)))
    return report, drawing


def _verify_shared_output(directory, pages, formats):
    """Verify every projected text line survived actual shared SVG/PDF output."""
    shared = _decode((directory / 'MANIFEST.json').read_bytes())
    if shared['sheet_order'] != [page.spec.id for page in pages]:
        raise ValueError('shared sheet order differs from paginated schedules')
    svgs = sorted((directory / 'sheets').glob('*.svg'))
    if len(svgs) != len(pages):
        raise ValueError('shared SVG page count differs from paginated schedules')
    expected_text = []
    for path, page in zip(svgs, pages):
        root = ET.fromstring(path.read_bytes())
        groups = root.findall('{http://www.w3.org/2000/svg}g')
        texts = [p.text for p in page.drawing_set.views[0].primitives if p.kind == 'text']
        if len(groups) != 1 or [n.text or '' for n in groups[0].findall('{http://www.w3.org/2000/svg}text')] != texts:
            raise ValueError(f'sheet {page.spec.id}: schedule text missing or changed in shared SVG')
        expected_text.append([''.join(c for c in t if not c.isspace()) for t in texts if not t.isspace() and t])
    pdf_check = {'status': 'not_requested'}
    if 'pdf' in formats:
        import pypdf
        pdf = pypdf.PdfReader(io.BytesIO((directory / 'sheet-set.pdf').read_bytes()), strict=True)
        if len(pdf.pages) != len(pages):
            raise ValueError('PDF page count differs from paginated schedules')
        for actual, page, text in zip(pdf.pages, pages, expected_text):
            runs = []
            def collect(value, *unused):
                # pypdf inserts standalone separators between SVG text nodes.
                # Cairo encodes spaces as positioned gaps; extraction infers
                # one separator irrespective of the gap. Exact spacing remains
                # bound by SVG/source and covered by actual PDF pixel regressions.
                value = value.rstrip('\r\n')
                if value and not value.isspace():
                    runs.append(''.join(c for c in value if not c.isspace()))
            actual.extract_text(visitor_text=collect)
            if not ''.join(runs).startswith(''.join(text)):
                raise ValueError(f'sheet {page.spec.id}: PDF schedule text missing/changed; inspect supported fonts and supplied glyphs')
            if abs(float(actual.mediabox.width) - page.spec.width_mm * 72 / 25.4) > .01 or abs(
                    float(actual.mediabox.height) - page.spec.height_mm * 72 / 25.4) > .01:
                raise ValueError(f'sheet {page.spec.id}: PDF physical dimensions changed')
        pdf_check = {'status': 'all_projected_nonspace_glyph_sequence_exact',
                     'spacing': 'exact SVG/source; printable Unicode spaces may become positioned PDF gaps; extraction cannot preserve space code points/counts', 'page_count': len(pages)}
    return {'svg': 'all_projected_text_exact', 'pdf': pdf_check}


class ScheduleSheetSetError(ValueError):
    pass


def create_panel_schedule_sheet_set(model_path, profile_path, job_path, settings_path, *, output_directory,
                                    presentation='drafted', drafted_width_mm=420.0, drafted_region_mm=None):
    return _create_schedule_sheet_set(model_path, profile_path, job_path, settings_path,
        output_directory=output_directory, presentation=presentation,
        drafted_width_mm=drafted_width_mm, drafted_region_mm=drafted_region_mm)


def _create_schedule_sheet_set(model_path, profile_path, job_path, settings_path, *, output_directory,
                              presentation='drafted', drafted_width_mm=420.0, drafted_region_mm=None,
                              combined=False):
    """Resolve one shared company sheet as a template, paginate, then publish.

    Uses the shared company profile resolver/renderer/exporter. Caller supplies
    exact canonical model, company profile, job metadata and layout settings.
    Pagination expands the single requested sheet; no branding is copied here.
    Failure never leaves a successful delivery. The final manifest binds every
    logical row to a canonical ID, physical page, exact cells and wrapped lines.
    """
    from .company_sheets import resolve_company_sheet_set, create_company_sheet_set
    inputs = {name: Path(path).read_bytes() for name, path in (
        ('model.json', model_path), ('profile.json', profile_path), ('job.json', job_path), ('settings.json', settings_path))}
    model = BuildingModel.from_json(inputs['model.json'])
    settings = _decode(inputs['settings.json'])
    version = COMBINED_VERSION if combined else VERSION
    if not isinstance(settings, dict) or set(settings) != {'version', 'layout'} or settings['version'] != version:
        raise ValueError(f'settings: supply exact version {version} and layout object')
    if not isinstance(settings['layout'], dict) or set(settings['layout']) - set(ScheduleLayout.__dataclass_fields__):
        raise ValueError('settings.layout: unknown property or invalid object')
    layout_values = dict(settings['layout'])
    # Legacy drafted cards were compact when packing was omitted; ledger
    # mode retains its one-table-per-sheet default. Explicit values win.
    if presentation == 'drafted':
        layout_values.setdefault('packing', 'compact')
    layout = ScheduleLayout(**layout_values)
    resolved = resolve_company_sheet_set(profile_path, job_path)
    if combined and not {'svg', 'pdf'} <= set(resolved.formats):
        raise ValueError('combined delivery requires both svg and pdf job formats')
    if resolved.profile_json != inputs['profile.json'] or resolved.job_json != inputs['job.json']:
        raise ValueError('profile/job changed during input snapshot; retry with stable inputs')
    if len(resolved.sheets) != 1:
        raise ValueError('job: supply exactly one template sheet; schedule pagination assigns continuation sheet IDs')
    report, drawing = (combined_schedule_drawing_set(model) if combined else panel_schedule_drawing_set(model))
    if presentation not in {'drafted', 'fact_ledger'}:
        raise ValueError('presentation: use drafted or fact_ledger')
    if presentation == 'drafted':
        pages = draft_panel_schedule_pages(report, drawing, resolved.sheets[0], layout=layout,
            table_width_mm=drafted_width_mm, region_bounds_mm=drafted_region_mm)
    else:
        pages = paginate_schedules(drawing, resolved.sheets[0], layout=layout, empty_schedule_sources={
            stable_id('schedule', f'panel-facts:{p.panel.id}'): p.panel.id for p in report.panels if not p.circuits})
    rows = [row for page in pages for row in page.row_map]
    expected = [(s.id, n, r.source_id) for s in drawing.schedules for n, r in enumerate(s.rows)]
    if sorted((r['schedule_id'], r['row_index'], r['source_id']) for r in rows) != sorted(expected):
        raise ValueError('schedule pagination row coverage mismatch')
    output = Path(output_directory).absolute()
    if output.exists():
        raise FileExistsError(f'output attempt already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    stage = 'snapshot'
    try:
        # Temporary shared production is inside the fresh attempt. Its successful
        # output stays private to this attempt until schedule receipts verify.
        with tempfile.TemporaryDirectory(prefix='.schedule-', dir=output) as temporary:
            temporary = Path(temporary)
            for name, raw in inputs.items():
                (temporary / name).write_bytes(raw)
            profile = _decode(inputs['profile.json'])
            profile['logos'] = {}
            (temporary / 'profile.json').write_bytes(_json(profile))
            job = _decode(inputs['job.json'])
            job['logos'] = {}
            template = {**job['sheets'][0], 'logos': {}}
            assets = {}
            for name, raw in resolved.sheets[0].logos:
                asset = f'assets/{_sha(raw)}.png'
                (temporary / asset).parent.mkdir(exist_ok=True)
                (temporary / asset).write_bytes(raw)
                assets[name] = asset
            template['logos'] = assets
            job['sheets'] = [{**template, 'id': page.spec.id} for page in pages]
            (temporary / 'expanded-job.json').write_bytes(_json(job))
            stage = 'shared company sheet production'
            shared = create_company_sheet_set(temporary / 'profile.json', temporary / 'expanded-job.json',
                output_directory=temporary / 'shared', bodies={p.spec.id: p.body_svg(layout) for p in pages})
            stage = 'schedule text verification'
            verification = _verify_shared_output(shared, pages, resolved.formats)
            pending = output / '.pending'
            os.rename(shared, pending)
            stage = 'schedule source retention'
            retained = pending / 'schedule-source'
            retained.mkdir()
            for name, raw in inputs.items():
                (retained / name).write_bytes(raw)
            (retained / 'expanded-job.json').write_bytes(_json(job))
            (retained / 'portable-profile.json').write_bytes(_json(profile))
            for asset in assets.values():
                (retained / asset).parent.mkdir(exist_ok=True)
                (retained / asset).write_bytes((temporary / asset).read_bytes())
            (retained / 'panel-facts.json').write_text(report.to_json() + '\n')
            (retained / 'drawing-schedules.json').write_text(drawing.to_json() + '\n')
            for page in pages:
                (retained / f'{page.spec.id}.drawing.json').write_text(page.drawing_set.to_json() + '\n')
                (retained / f'{page.spec.id}.body.svg').write_text(page.body_svg(layout) + '\n')
            manifest = {'version': version, 'model_id': model.model_id,
                'source_model_sha256': _sha(inputs['model.json']),
                'source_model_fingerprint': report.source_model_fingerprint,
                'status': 'supplied_facts_only', 'engineering_approval': False,
                'sheet_order': [p.spec.id for p in pages], 'row_count': len(rows), 'rows': rows,
                'layout': asdict(layout), 'drafting_settings': {'presentation': presentation, 'table_width_mm': drafted_width_mm, 'region_bounds_mm': drafted_region_mm},
                'drafting_grammar': ('paired-panel-card-v2' if presentation == 'drafted' else 'fact-ledger-v1'), 'verification': verification, 'schedule_pages': [
                    {'sheet_id': p.spec.id, **section} for p in pages for section in (p.sections or (
                        {'schedule_id': p.schedule_id, 'schedule_page': p.schedule_page,
                         'schedule_pages': p.schedule_pages},))],
                'panel_totals': _panel_totals(report),
                'limitations': ['Connected supplied VA only; no demand, service sizing, breaker selection, spare slots or code approval.',
                    'Font/pixel review remains independent; no construction or permit readiness claim.']}
            if combined:
                manifest['overlap_policy'] = {
                    'circuits': 'Circuit inventory and panel facts are views of the same canonical circuits; do not sum row counts.',
                    'selection_outcomes': 'Decision metadata, not additional physical inventory; selected boxes count only in schedule:electrical-boxes.'}
                manifest['unsupported_inventory'] = 'Site inventory is refused, never silently omitted.'
                # Full canonical source records retain field-qualified provenance
                # beyond the legacy DrawingSet source-index projection.
                sources = _schedule_source_records(model)
                manifest['row_sources'] = [
                    {'schedule_id': schedule.id, 'row_index': index, 'source_id': row.source_id,
                     'source_model_sha256': manifest['source_model_sha256'], **sources[row.source_id]}
                    for schedule in drawing.schedules for index, row in enumerate(schedule.rows)]
            # Shared MANIFEST remains intact; schedule manifest binds all shared
            # outputs and retained editable sources, without a recursive hash.
            stage = 'verify'
            manifest['files'] = [{'path': p.relative_to(pending).as_posix(), 'bytes': p.stat().st_size,
                'sha256': _sha(p.read_bytes())} for p in sorted(pending.rglob('*')) if p.is_file()]
            (pending / 'SCHEDULE-MANIFEST.json').write_bytes(_json(manifest))
            if combined:
                verify_combined_schedule_delivery(pending)
        os.rename(pending, output / 'delivery')
        return output / 'delivery'
    except Exception as exc:
        (output / 'failure.json').write_bytes(_json({'version': version, 'stage': stage,
            'error_type': type(exc).__name__, 'error': str(exc)}))
        raise ScheduleSheetSetError(f'schedule set failed at {stage}: {exc}; attempt retained at {output}') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('model', 'profile', 'job', 'settings', 'output_directory'):
        parser.add_argument(name, type=Path)
    parser.add_argument('--presentation', choices=('drafted', 'fact_ledger'), default='drafted')
    parser.add_argument('--drafted-width-mm', type=float, default=420.0)
    parser.add_argument('--drafted-region-mm', type=float, nargs=4, metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'))
    parser.add_argument('--combined', action='store_true', help='deliver semantic inventories and panel facts; requires combined settings version')
    args = parser.parse_args(argv)
    try:
        path = _create_schedule_sheet_set(args.model, args.profile, args.job, args.settings,
                                               combined=args.combined,
                                               output_directory=args.output_directory, presentation='fact_ledger' if args.combined else args.presentation,
                                               drafted_width_mm=args.drafted_width_mm, drafted_region_mm=args.drafted_region_mm)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
