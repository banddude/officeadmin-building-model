"""Read-only directed schematic projection of canonical equipment and circuits.

No physical geometry, equipment, ports, circuits or electrical facts are created.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import hashlib
import json
from oabm.model import BuildingModel, validate_model

SUPPORTED = {'source', 'generator', 'meter', 'panelboard', 'switchboard',
             'disconnect', 'transformer', 'transfer_switch'}


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+'\n').encode()


@dataclass(frozen=True)
class Topology:
    model_sha256: str
    equipment: tuple[dict, ...]
    connections: tuple[dict, ...]
    diagnostics: tuple[str, ...]
    source_index: tuple[dict, ...]

    def to_dict(self):
        return asdict(self)


def project_topology(model: BuildingModel) -> Topology:
    """All equipment and all circuit/load pairs, or an actionable error.

    Only directed acyclic radial/branching networks and explicit two-input
    transfer equipment are supported. Routes remain physical source references;
    their geometry does not imply electrical connectivity. Isolated equipment
    is retained with a visible warning. Unknown supplied facts stay unknown.
    """
    validate_model(model)
    equipment = {e.id:e for e in model.electrical_equipment}
    ports = {p.id:p for p in model.ports}
    if not equipment:
        raise ValueError('single-line: no electrical_equipment supplied')
    if model.electrical_devices:
        raise ValueError('single-line: electrical_devices are unsupported; use an equipment-only source model explicitly')
    diagnostics = []
    for e in equipment.values():
        if e.equipment_type not in SUPPORTED:
            raise ValueError(f'{e.id}: unsupported equipment_type {e.equipment_type!r}; supported: {sorted(SUPPORTED)}')
    connections = []
    endpoint_pairs = set()
    used_ports = set()
    conductors = {}
    for wire in model.conductors:
        conductors.setdefault(wire.circuit_id, []).append(wire)
    for circuit in sorted(model.circuits, key=lambda c:c.id):
        source = ports[circuit.source_port_id]
        for load_id in sorted(circuit.load_port_ids):
            load = ports[load_id]
            for p in (source, load):
                if p.owner_id not in equipment or p.domain != 'electrical':
                    raise ValueError(f'{circuit.id}: port {p.id} must belong to supported equipment in electrical domain')
            if source.owner_id == load.owner_id:
                raise ValueError(f'{circuit.id}: self-connection on {source.owner_id} is unsupported')
            pair = (source.id, load.id)
            if pair in endpoint_pairs:
                raise ValueError(f'{circuit.id}: duplicate circuit/load connection {pair}')
            endpoint_pairs.add(pair)
            used_ports.update(pair)
            if circuit.voltage_v is None:
                diagnostics.append(f'{circuit.id}: voltage UNKNOWN')
            wires = sorted(conductors.get(circuit.id, []), key=lambda w:w.id)
            if not wires:
                diagnostics.append(f'{circuit.id}: conductor specification UNKNOWN')
            spec = ' | '.join(f'{w.count}x {w.role}; {w.size or "size UNKNOWN"}; {w.material or "material UNKNOWN"}; {w.insulation or "insulation UNKNOWN"}' for w in wires) or 'Conductor specification UNKNOWN'
            connections.append({'id': circuit.id+'::'+load.id, 'circuit_id':circuit.id,
                'source_port_id':source.id, 'load_port_id':load.id,
                'source_id':source.owner_id, 'load_id':load.owner_id,
                'source_role':source.role, 'load_role':load.role,
                'voltage_v':circuit.voltage_v, 'poles':circuit.poles, 'phase':circuit.phase,
                'conductor_specification':spec,
                'source_ids':[circuit.id,source.id,load.id,*[w.id for w in wires],*circuit.route_ids],
                'route_ids':list(circuit.route_ids), 'conductor_ids':[w.id for w in wires]})
    # Port-only links are not directed circuit facts. Refuse silent omission.
    undirected = {frozenset(pair) for pair in endpoint_pairs}
    for port in ports.values():
        if port.owner_id in equipment:
            for other in port.connected_port_ids:
                if frozenset((port.id,other)) not in undirected:
                    raise ValueError(f'{port.id}: explicit port link {other} lacks a directed circuit; resolve before rendering')
    incoming = {key:[] for key in equipment}
    outgoing = {key:[] for key in equipment}
    for edge in connections:
        incoming[edge['load_id']].append(edge)
        outgoing[edge['source_id']].append(edge)
    # Source role determines direction; never infer from port pose or voltage.
    for e in equipment.values():
        ins, outs = incoming[e.id], outgoing[e.id]
        if e.equipment_type == 'transfer_switch':
            roles = [edge['load_role'] for edge in ins]
            if sorted(roles) != ['alternate','normal'] or not outs or any(x['source_role']!='load' for x in outs):
                raise ValueError(f'{e.id}: transfer requires exactly normal and alternate incoming ports and explicit load-role output(s)')
            if len({x['load_port_id'] for x in ins}) != 2:
                raise ValueError(f'{e.id}: transfer inputs must be distinct ports')
        elif len(ins)>1:
            raise ValueError(f'{e.id}: multiple incoming feeders are ambiguous outside an explicit transfer_switch')
        if e.equipment_type in {'source','generator'} and ins:
            raise ValueError(f'{e.id}: source equipment cannot have an incoming feeder')
        if e.equipment_type == 'transformer' and (len(ins)!=1 or not outs):
            raise ValueError(f'{e.id}: transformer requires one incoming feeder and at least one outgoing feeder')
        voltages = {x['voltage_v'] for x in ins+outs if x['voltage_v'] is not None}
        if e.equipment_type != 'transformer':
            if len(voltages)>1:
                raise ValueError(f'{e.id}: inconsistent supplied circuit voltages {sorted(voltages)}; resolve or explicitly model a transformer')
            if e.rated_voltage_v is not None and any(v!=e.rated_voltage_v for v in voltages):
                raise ValueError(f'{e.id}: circuit voltage differs from supplied rated_voltage_v')
        else:
            if {x['load_port_id'] for x in ins}&{x['source_port_id'] for x in outs}:
                raise ValueError(f'{e.id}: transformer primary and secondary must use distinct ports')
            output_voltages={x['voltage_v'] for x in outs if x['voltage_v'] is not None}
            if len(output_voltages)>1:
                raise ValueError(f'{e.id}: multiple secondary voltages unsupported; model explicit transformer windings')
        if not ins and outs and e.equipment_type not in {'source','generator'}:
            diagnostics.append(f'{e.id}: upstream supply UNKNOWN - no incoming circuit supplied')
        if not ins and not outs:
            diagnostics.append(f'{e.id}: ISOLATED - no explicit circuit connections')
        if e.rated_voltage_v is None:
            diagnostics.append(f'{e.id}: equipment rated voltage UNKNOWN')
    # Deterministic topological ordering. A transfer is two alternate feeds,
    # never a claim that the two upstream sources are electrically paralleled.
    degree={key:len(incoming[key]) for key in equipment}
    queue=sorted(key for key,value in degree.items() if value==0)
    order=[]
    while queue:
        key=queue.pop(0)
        order.append(key)
        for edge in outgoing[key]:
            child=edge['load_id']
            degree[child]-=1
            if degree[child]==0:
                queue.append(child)
                queue.sort()
    if len(order)!=len(equipment):
        raise ValueError('single-line: cyclic/ring topology unsupported; resolve explicit switching states before rendering')
    rows=[]
    for key in order:
        e=equipment[key]
        notes=e.attributes.get('single_line_notes',[])
        if not isinstance(notes,list) or any(not isinstance(n,str) for n in notes):
            raise ValueError(f'{key}: attributes.single_line_notes must be a list of supplied text notes')
        rows.append({'id':key,'name':e.name or key,'equipment_type':e.equipment_type,
                     'rated_voltage_v':e.rated_voltage_v,'supplied_notes':notes,
                     'incoming':[x['id'] for x in incoming[key]],
                     'outgoing':[x['id'] for x in outgoing[key]]})
    selected=set(equipment)|used_ports|{x['circuit_id'] for x in connections}
    selected.update(w.id for w in model.conductors)
    selected.update(r for x in connections for r in x['route_ids'])
    selected.update(r for w in model.conductors for r in w.route_ids)
    doc=model.to_dict()
    source_index=[]
    for collection in ('electrical_equipment','ports','circuits','conductors','routes'):
        for row in doc[collection]:
            if row['id'] in selected:
                source_index.append({'collection':collection,'entity':row})
    return Topology(hashlib.sha256(model.to_json(indent=None).encode()).hexdigest(),
                    tuple(rows),tuple(connections),tuple(sorted(set(diagnostics))),
                    tuple(sorted(source_index,key=lambda row:row['entity']['id'])))
