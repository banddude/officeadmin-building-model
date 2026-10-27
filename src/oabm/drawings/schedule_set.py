"""Explicit supplied panel facts to editable physical schedule sheets."""
from __future__ import annotations

import argparse
import io
import xml.etree.ElementTree as ET
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from oabm.model import BuildingModel, stable_id
from oabm.reports import generate_panel_schedule_report
from .blank_set import _decode, _json, _sha
from .generator import _entities, _source_reference
from .model import DrawingSet
from .panel_schedules import panel_report_to_drawing_schedules
from .schedule_layout import ScheduleLayout, paginate_schedules

VERSION = 'panel-schedule-sheet-set-v1'


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
        expected_text.append([t.replace(' ', '') for t in texts if t.strip(' ')])
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
                    runs.append(value.replace(' ', ''))
            actual.extract_text(visitor_text=collect)
            if runs[:len(text)] != text:
                raise ValueError(f'sheet {page.spec.id}: PDF schedule text missing/changed; inspect supported fonts and supplied glyphs')
            if abs(float(actual.mediabox.width) - page.spec.width_mm * 72 / 25.4) > .01 or abs(
                    float(actual.mediabox.height) - page.spec.height_mm * 72 / 25.4) > .01:
                raise ValueError(f'sheet {page.spec.id}: PDF physical dimensions changed')
        pdf_check = {'status': 'all_projected_nonspace_glyph_runs_exact',
                     'spacing': 'exact SVG/source; PDF spaces are positioned gaps, independently pixel-tested', 'page_count': len(pages)}
    return {'svg': 'all_projected_text_exact', 'pdf': pdf_check}


class ScheduleSheetSetError(ValueError):
    pass


def create_panel_schedule_sheet_set(model_path, profile_path, job_path, settings_path, *, output_directory):
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
    if not isinstance(settings, dict) or set(settings) != {'version', 'layout'} or settings['version'] != VERSION:
        raise ValueError('settings: supply exact version panel-schedule-sheet-set-v1 and layout object')
    if not isinstance(settings['layout'], dict) or set(settings['layout']) - set(ScheduleLayout.__dataclass_fields__):
        raise ValueError('settings.layout: unknown property or invalid object')
    layout = ScheduleLayout(**settings['layout'])
    resolved = resolve_company_sheet_set(profile_path, job_path)
    if resolved.profile_json != inputs['profile.json'] or resolved.job_json != inputs['job.json']:
        raise ValueError('profile/job changed during input snapshot; retry with stable inputs')
    if len(resolved.sheets) != 1:
        raise ValueError('job: supply exactly one template sheet; schedule pagination assigns continuation sheet IDs')
    report, drawing = panel_schedule_drawing_set(model)
    pages = paginate_schedules(drawing, resolved.sheets[0], layout=layout, empty_schedule_sources={
        stable_id('schedule', f'panel-facts:{p.panel.id}'): p.panel.id for p in report.panels if not p.circuits})
    rows = [row for page in pages for row in page.row_map]
    expected = [(s.id, n, r.source_id) for s in drawing.schedules for n, r in enumerate(s.rows)]
    if [(r['schedule_id'], r['row_index'], r['source_id']) for r in rows] != expected:
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
            manifest = {'version': VERSION, 'model_id': model.model_id,
                'source_model_fingerprint': report.source_model_fingerprint,
                'status': 'supplied_facts_only', 'engineering_approval': False,
                'sheet_order': [p.spec.id for p in pages], 'row_count': len(rows), 'rows': rows,
                'layout': asdict(layout), 'verification': verification, 'schedule_pages': [
                    {'sheet_id': p.spec.id, 'schedule_id': p.schedule_id,
                     'schedule_page': p.schedule_page, 'schedule_pages': p.schedule_pages} for p in pages],
                'panel_totals': [{'panel_id': p.panel.id, 'known_connected_va': p.known_connected_va,
                    'all_listed_loads_known': p.all_listed_loads_known,
                    'subtotal_source_circuit_ids': list(p.subtotal_source_circuit_ids),
                    'unknown_load_circuit_ids': list(p.unknown_load_circuit_ids)} for p in report.panels],
                'limitations': ['Connected supplied VA only; no demand, service sizing, breaker selection, spare slots or code approval.',
                    'Font/pixel review remains independent; no construction or permit readiness claim.']}
            # Shared MANIFEST remains intact; schedule manifest binds all shared
            # outputs and retained editable sources, without a recursive hash.
            stage = 'verify'
            manifest['files'] = [{'path': p.relative_to(pending).as_posix(), 'bytes': p.stat().st_size,
                'sha256': _sha(p.read_bytes())} for p in sorted(pending.rglob('*')) if p.is_file()]
            (pending / 'SCHEDULE-MANIFEST.json').write_bytes(_json(manifest))
        os.rename(pending, output / 'delivery')
        return output / 'delivery'
    except Exception as exc:
        (output / 'failure.json').write_bytes(_json({'version': VERSION, 'stage': stage,
            'error_type': type(exc).__name__, 'error': str(exc)}))
        raise ScheduleSheetSetError(f'schedule set failed at {stage}: {exc}; attempt retained at {output}') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('model', 'profile', 'job', 'settings', 'output_directory'):
        parser.add_argument(name, type=Path)
    args = parser.parse_args(argv)
    try:
        path = create_panel_schedule_sheet_set(args.model, args.profile, args.job, args.settings,
                                               output_directory=args.output_directory)
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
