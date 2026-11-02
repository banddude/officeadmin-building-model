"""Offline, source-bound presentation of the canonical revision diff."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from fractions import Fraction
import hashlib
from html import escape
import io
import json
from pathlib import Path

from oabm.model import BuildingModel, ContractError
from oabm.qa import build_revision_diff
from oabm.reports.physical_routes import _snapshot, _json, _text_cell
from oabm.reports.physical_route_revisions import (
    generate_physical_route_comparison, _TokenFloat, _token_json,
)
from oabm.routing.physical_capture import bounded_source_json, SOURCE_BYTES
from oabm.routing.physical_verify_types import VerificationStop

PRESENTATION_BYTES = 16 * 1024 * 1024
BUNDLE_BYTES = 64 * 1024 * 1024
LIMITATIONS = (
    'Canonical IDs match only within the same model, schema and complete coordinate frame. No plan identity reconciliation.',
    'Quantities are model-derived evidence, not prices, stock orders or engineering approval. Null means an absent side, not zero.',
    'Physical route measurements below explain the canonical takeoff; do not add them to quantity deltas a second time.',
    'Source hashes bind retained normalized snapshots; they do not establish source authenticity, current clearance or code compliance.',
)


class _BoundedText(io.StringIO):
    def __init__(self):
        super().__init__(newline='')
        self.byte_count = 0

    def write(self, text):
        self.byte_count += len(text.encode('utf-8'))
        if self.byte_count > PRESENTATION_BYTES:
            raise ContractError('revision register presentation exceeds byte limit; no rows truncated')
        return super().write(text)


def _csv(headers, rows):
    output = _BoundedText()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(headers)
    for row in rows:
        # JSON encodes null explicitly. All nonnumeric cells use the existing
        # spreadsheet-formula guard, including IDs, paths and serialized values.
        writer.writerow(_text_cell(value) if isinstance(value, str) else value for value in row)
    return output.getvalue()


def _change_rows(diff, hashes):
    for row in diff['changes']:
        common = [row[key] for key in ('change_id', 'collection', 'entity_id', 'change')]
        for field in row['field_changes']:
            yield [*common, ','.join(field['change_classes']), field['path'],
                   _json(field['before']), _json(field['after']), *hashes]
        if not row['field_changes']:
            # Full added/removed canonical document, without fabricated field paths.
            yield [*common, ','.join(row['change_classes']), '',
                   _json(row['document'] if row['change'] == 'removed' else None),
                   _json(row['document'] if row['change'] == 'added' else None), *hashes]


def _quantity_rows(diff, hashes):
    for row in diff['quantity_deltas']:
        yield [*(row[key] for key in ('delta_id', 'category', 'item_type', 'unit')),
               _json(row['variant']), row['status'],
               *(row[key] if row[key] is not None else 'null' for key in ('before', 'after', 'delta')), *hashes]


def _html(document):
    out = _BoundedText()
    def text(value):
        return escape(str(value).encode('utf-8', errors='backslashreplace').decode('utf-8'), quote=True)
    def cell(value):
        return '<td><pre>' + text(value) + '</pre></td>'
    def table(headers, rows):
        out.write('<div class="scroll"><table><thead><tr>' + ''.join('<th scope="col">'+text(h)+'</th>' for h in headers) + '</tr></thead><tbody>')
        count = 0
        for row in rows:
            out.write('<tr>' + ''.join(cell(value) for value in row) + '</tr>')
            count += 1
        out.write('</tbody></table></div>')
        if not count:
            out.write('<p>No changes in this section.</p>')
    diff = document['diff']
    out.write('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Revision change register</title><style>')
    out.write('body{font:16px system-ui,sans-serif;color:#17283a;background:#f5f7fa;margin:0;padding:2rem;max-width:1500px}h1{font-size:2rem}h2{margin-top:2.5rem}a{color:#125b92}table{border-collapse:collapse;background:white;width:100%}th,td{text-align:left;vertical-align:top;padding:.7rem;border:1px solid #c8d1dc}th{background:#e5edf5}pre{font:13px ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;max-width:46rem}.scroll{overflow:auto}details{background:white;padding:.8rem;margin:.7rem 0;border:1px solid #c8d1dc}summary{cursor:pointer;font-weight:600}.hash{overflow-wrap:anywhere;font:13px monospace}.counts{font-size:1.2rem}li{margin:.4rem 0}@media print{body{padding:0;background:white}.scroll{overflow:visible}details{break-inside:avoid}}</style></head><body>')
    out.write('<h1>Revision change register</h1><p>Model <strong>'+text(diff['base_model_id'])+'</strong> · Schema '+text(diff['schema_version'])+'</p>')
    out.write('<p class="counts">'+text(diff['added_count'])+' added · '+text(diff['removed_count'])+' removed · '+text(diff['changed_count'])+' changed objects</p>')
    out.write('<p><a href="changes.csv">Changes CSV</a> · <a href="quantity-deltas.csv">Quantity deltas CSV</a> · <a href="register.json">Complete source-bound JSON</a></p><ul>')
    for line in LIMITATIONS:
        out.write('<li>'+text(line)+'</li>')
    out.write('</ul><nav><a href="#quantities">Quantity deltas</a> · <a href="#objects">Object changes</a> · <a href="#physical">Physical route measurements</a></nav><h2>Snapshot bindings</h2>')
    for side, source in document['sources'].items():
        out.write('<p>'+text(side)+' — normalized-canonical-model-json-v1<br><span class="hash">SHA-256 '+text(source['sha256'])+'</span></p>')
    out.write('<h2 id="quantities">Quantity deltas</h2><p>One row per complete quantity identity. Added and removed product variants remain separate. Null deltas are not arithmetic zero.</p>')
    table(['Delta ID', 'Category', 'Type', 'Unit', 'Variant', 'Status', 'Before', 'After', 'Delta'], _quantity_rows(diff, []))
    out.write('<h2 id="objects">Canonical object changes</h2><p>Expand an object to inspect every changed field or its complete added/removed document.</p>')
    for row in diff['changes']:
        out.write('<details><summary>'+text(row['change'])+' · '+text(row['collection'])+' · '+text(row['entity_id'])+'</summary><p>'+text(row['change_id'])+' · '+text(', '.join(row['change_classes']))+'</p>')
        table(['Classes', 'Field path', 'Before', 'After'], (r[4:8] for r in _change_rows({'changes':[row]}, [])))
        out.write('</details>')
    if not diff['changes']:
        out.write('<p>No object changes.</p>')
    out.write('<h2 id="physical">Physical route measurement evidence</h2><p>Exact length = straight ticks × 10⁻⁹ m + (π coefficient numerator / denominator) × 10⁻⁹ π m. These explain route measurements; they are not additional takeoff rows. Legacy route deltas remain in the canonical quantity table.</p>')
    for row in document['physical_measurements']:
        if row['delta']:
            length = row['delta']['length']
            coefficient = length['pi_coefficient_ticks']
            metres = Fraction(coefficient['numerator'],coefficient['denominator']*1_000_000_000)
            out.write('<p><strong>'+text(row['route_id'])+'</strong>: Δ length = '+
                      text(length['straight_m'])+' m + ('+text(metres)+') π m ≈ '+
                      text(length['total_m_approx'])+' m</p>')
    def measurements():
        for row in document['physical_measurements']:
            delta = row['delta']
            length = delta['length'] if delta else None
            yield [row['route_id'], row['status'], row['path_changed'],
                   _json(row['before_measurement']), _json(row['after_measurement']),
                   _json(length)]
    table(['Canonical route ID','Status','Path changed','Before measurement','After measurement','Exact delta length'], measurements())
    out.write('</body></html>')
    return out.getvalue()


@dataclass(frozen=True)
class RevisionRegister:
    _report_json: str

    def to_json(self):
        return self._report_json

    def to_dict(self):
        return json.loads(self._report_json)

    def validate_against_models(self, before, after):
        if generate_revision_register(before, after).to_json() != self.to_json():
            raise ContractError('revision register does not match supplied snapshots')

    def files(self):
        document = self.to_dict()
        hashes = [document['sources'][side]['sha256'] for side in ('before', 'after')]
        result = {
            'register.json': self.to_json(), 'index.html': _html(document),
            'changes.csv': _csv(['change_id','collection','entity_id','change','classes','path','before_json','after_json','before_source_sha256','after_source_sha256'], _change_rows(document['diff'], hashes)),
            'quantity-deltas.csv': _csv(['delta_id','category','item_type','unit','variant_json','status','before_json','after_json','delta_json','before_source_sha256','after_source_sha256'], _quantity_rows(document['diff'], hashes)),
        }
        if sum(len(value.encode('utf-8')) for value in result.values()) > BUNDLE_BYTES:
            raise ContractError('revision register bundle exceeds byte limit; no rows truncated')
        return result


def generate_revision_register(before: BuildingModel, after: BuildingModel) -> RevisionRegister:
    pairs = [_snapshot(model) for model in (before, after)]
    diff = build_revision_diff(pairs[0][0], pairs[1][0]).to_dict()
    physical = generate_physical_route_comparison(pairs[0][0], pairs[1][0]).to_dict()
    document = {
        'kind': 'canonical_revision_register', 'version': '1.0.0',
        'sources': {side: {'binding_profile': 'normalized-canonical-model-json-v1',
                           'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                           'snapshot': model.to_dict()}
                    for side, (model, raw) in zip(('before','after'), pairs)},
        'diff': diff,
        'physical_measurements': [{key: row[key] for key in
            ('route_id','status','path_changed','before_measurement','after_measurement','delta')}
            for row in physical['routes'] if row['before_kind'] == 'physical' or row['after_kind'] == 'physical'],
        'limitations': list(LIMITATIONS),
    }
    raw = _json(document)
    try:
        bounded_source_json(raw)
    except VerificationStop as exc:
        raise ContractError(f'revision register output {exc.status}: {exc.reason}') from exc
    return RevisionRegister(raw)


def validate_revision_register(payload) -> RevisionRegister:
    """Rebuild every reported value from retained, admitted canonical snapshots."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError('duplicate revision register JSON key')
            result[key] = value
        return result
    try:
        actual = json.loads(bounded_source_json(payload), object_pairs_hook=unique, parse_float=_TokenFloat)
        expected = generate_revision_register(*[
            BuildingModel.from_json(_token_json(actual['sources'][side]['snapshot']))
            for side in ('before','after')])
        if _json(actual) != expected.to_json():
            raise ContractError('revision register is stale or changed')
        return expected
    except (VerificationStop, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ContractError('invalid revision register JSON') from exc


def write_revision_register(report: RevisionRegister, output: Path):
    """Prepare and validate all bytes before reserving a fresh directory."""
    report = validate_revision_register(report.to_json())
    prepared = {name: value.encode('utf-8') for name,value in report.files().items()}
    output = Path(output)
    output.mkdir(exist_ok=False)
    created = []
    try:
        for name, value in prepared.items():
            path = output / name
            with path.open('xb') as handle:
                created.append(path)
                handle.write(value)
    except BaseException:
        # Only our freshly created files; never recursively delete a destination.
        for path in created:
            path.unlink()
        output.rmdir()
        raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline canonical revision register from complete comparable snapshots.')
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('output', type=Path, help='Fresh output directory')
    args = parser.parse_args(argv)
    try:
        models = []
        for path in (args.before,args.after):
            with path.open('rb') as handle:
                models.append(BuildingModel.from_json(bounded_source_json(handle.read(SOURCE_BYTES+1))))
        report = generate_revision_register(*models)
        write_revision_register(report,args.output)
        print(_json({'output_directory': str(args.output), 'changed_count': report.to_dict()['diff']['changed_count']}))
        return 0
    except (ContractError, VerificationStop, OSError, ValueError) as exc:
        parser.exit(2, f'revision register: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
