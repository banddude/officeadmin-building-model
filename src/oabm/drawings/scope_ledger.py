"""Explicit scope-ledger view; never part of default drawings."""
from oabm.model import ContractError, stable_id
from oabm.reports.scope_ledger import ScopeLedger
from .model import DrawingSchedule, ScheduleColumn, ScheduleRow


def scope_ledger_to_drawing_schedules(ledger: ScopeLedger) -> tuple[DrawingSchedule, ...]:
    if not isinstance(ledger, ScopeLedger):
        raise ContractError('scope adapter requires a ScopeLedger')
    ledger.to_dict()
    columns = tuple(ScheduleColumn(key, title) for key, title in (
        ('id', 'Canonical object ID'), ('collection', 'Collection'), ('type', 'Canonical type'),
        ('original', 'Original source scope'), ('effective', 'Effective scope'),
        ('reason', 'Unresolved/source reason'), ('derivation', 'Decision basis'),
        ('confidence', 'Effective confidence'), ('page', 'Source page'), ('rule', 'Caller rule/revision')))
    rows = []
    for row in ledger.rows:
        lane = row.source.entity['attributes'].get('pdf_electrical')
        lane = lane if isinstance(lane, dict) else {}
        rows.append(ScheduleRow(source_id=row.source.id, cells=(row.source.id, row.source.collection,
            row.canonical_type, str(lane.get('scope_status', 'unknown')), row.effective_status,
            row.diagnostic_reason or '', row.derivation, str(row.effective_confidence),
            str(lane.get('source_page', 'unknown')), f'{ledger.policy.basis.rule_id}@{ledger.policy.basis.revision}')))
    summary_columns = tuple(ScheduleColumn(key, title) for key, title in (
        ('collection', 'Collection'), ('type', 'Canonical type'), ('scope', 'Scope'),
        ('count', 'Object count'), ('ids', 'Contributing object IDs')))
    return (DrawingSchedule(id=stable_id('schedule', f'scope-ledger:{ledger.model_source_context.model_id}'),
        title='Object scope facts and explicit caller decisions', columns=columns, rows=tuple(rows)),
        DrawingSchedule(id=stable_id('schedule', f'scope-ledger-counts:{ledger.model_source_context.model_id}'),
        title='Object scope counts (not procurement or labor)', columns=summary_columns,
        rows=tuple(ScheduleRow(source_id=stable_id('scope-group', f'{g.collection}:{g.canonical_type}:{g.status}'),
            cells=(g.collection, g.canonical_type, g.status, str(g.count), ', '.join(g.object_ids))) for g in ledger.groups)))
