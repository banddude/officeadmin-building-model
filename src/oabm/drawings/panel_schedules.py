"""Explicit fact-report adapter; default model drawing generation is untouched."""
from oabm.model import ContractError, stable_id
from oabm.reports import PanelScheduleReport
from .model import DrawingSchedule, ScheduleColumn, ScheduleRow


def _value(value):
    return 'unknown' if value is None else str(value)


def panel_report_to_drawing_schedules(report: PanelScheduleReport) -> tuple[DrawingSchedule,...]:
    if not isinstance(report,PanelScheduleReport):raise ContractError('adapter requires a panel fact report')
    report.to_dict()  # Refuse stale/malformed nested state before making a view.
    columns=tuple(ScheduleColumn(key,title,unit) for key,title,unit in (
        ('circuit_id','Circuit ID',None),('number','Printed circuit label',None),
        ('source_port','Actual source port',None),('source_owner','Actual source owner',None),
        ('load_ports','Load ports',None),('poles','Supplied poles',None),('phase','Supplied phase',None),
        ('voltage','Supplied voltage','V'),('connected_load','Supplied connected load','VA'),
        ('routes','Route IDs',None),('missing','Missing supplied facts',None),
        ('assignment','Source assignment',None),('source_confidence','Circuit source confidence',None)))
    def row(fact):
        circuit=fact.circuit.entity
        return ScheduleRow(source_id=fact.circuit.id,cells=(fact.circuit.id,
            _value(circuit['circuit_number']),fact.source_port.id,fact.source_owner.id,
            ', '.join(circuit['load_port_ids']),_value(circuit['poles']),_value(circuit['phase']),
            _value(circuit['voltage_v']),_value(circuit['load_va']),', '.join(circuit['route_ids']),
            ', '.join(fact.missing_fields),fact.assignment_status,str(circuit['confidence'])))
    schedules=[]
    for group in report.panels:
        title=f'Panel {group.panel.id}: supplied circuit facts'
        if group.label_conflicts:title+=' (duplicate printed labels)'
        schedules.append(DrawingSchedule(id=stable_id('schedule',f'panel-facts:{group.panel.id}'),
            title=title,columns=columns,rows=tuple(row(fact) for fact in group.circuits)))
        summary_columns=(ScheduleColumn('panel','Panel ID'),ScheduleColumn('known','Known connected subtotal','VA'),
            ScheduleColumn('complete','All listed circuit loads known'),ScheduleColumn('unknown','Unknown-load circuit IDs'),
            ScheduleColumn('conflicts','Conflicting opaque labels'),ScheduleColumn('verification','Code verification'))
        schedules.append(DrawingSchedule(id=stable_id('schedule',f'panel-fact-summary:{group.panel.id}'),
            title=f'Panel {group.panel.id}: supplied connected-load completeness',columns=summary_columns,
            rows=(ScheduleRow(source_id=group.panel.id,cells=(group.panel.id,str(group.known_connected_va),
                str(group.all_listed_loads_known),', '.join(group.unknown_load_circuit_ids),
                ', '.join(conflict.label for conflict in group.label_conflicts),'not performed')),)))
    if report.unassigned_circuits:
        schedules.append(DrawingSchedule(id=stable_id('schedule',f'unassigned-panel-facts:{report.source_model_id}'),
            title='Unsupported source owners: unassigned circuit facts',columns=columns,
            rows=tuple(row(fact) for fact in report.unassigned_circuits)))
    return tuple(schedules)
