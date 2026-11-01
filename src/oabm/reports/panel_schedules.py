"""Read-only panel facts and supplied connected-load arithmetic, never demand."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import Any, get_args, get_origin, get_type_hints

from oabm.model import (BuildingModel, ContractError, Entity, CoordinateSystem, Provenance,
                        Route,
                        FITTING_SCHEMA_VERSION, STRUCTURAL_SCHEMA_VERSION,
                        SUPPORTED_SCHEMA_VERSIONS, validate_model)
from oabm.model.common import _finite, _validate_id, _validate_json_value
from oabm.model.model import _decode_dataclass, _encode
from oabm.model.physical_routes import PhysicalRoute, _physical_json_float, validate_physical_json_numbers

REPORT_VERSION = "1.0.0"
PANEL_TYPES = frozenset({"panelboard"})
BINDING_ALGORITHM = "canonical-entity-order-and-scope-sets-v1"
ENTITY_TYPES = {name: get_args(tp)[0] for name, tp in get_type_hints(BuildingModel).items()
                if get_origin(tp) is tuple and get_args(tp) and isinstance(get_args(tp)[0], type)
                and issubclass(get_args(tp)[0], Entity)}
# Collection membership is shared by both canonical route variants. Decode
# the actual discriminator rather than treating physical facts as legacy lines.
ENTITY_TYPES["routes"] = Route
OPTIONAL_FACTS = ("circuit_number", "poles", "phase", "voltage_v", "load_va")


def _minimum_source_schema(collection: str) -> str:
    """Return the first canonical version that may contain this source row.

    RouteFitting objects predate schema 1.5; only the three product-decision
    collections are new to that version.
    """
    if collection in {"fitting_selections", "fitting_interfaces",
                      "fitting_selection_outcomes"}:
        return FITTING_SCHEMA_VERSION
    if collection == "structural_members":
        return STRUCTURAL_SCHEMA_VERSION
    if collection.startswith("support_"):
        return "1.3.0"
    if collection.startswith("raceway_"):
        return "1.2.0"
    if collection in {"electrical_boxes", "box_selection_outcomes"}:
        return "1.1.0"
    return "1.0.0"


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _portable(value):
    return json.loads(_json(_encode(value)))


def _array(value, kind, label):
    if not isinstance(value, tuple) or any(not isinstance(item, kind) for item in value):
        raise ContractError(f"{label} must be a tuple of {kind.__name__} values")


def _ids(value, label):
    _array(value, str, label)
    for identity in value:
        _validate_id(identity, label)
    if len(set(value)) != len(value):
        raise ContractError(f"{label} contains duplicate identities")


def _scopes(data):
    # Only typed provenance qualifiers are sets. Arbitrary attributes/arrays and
    # chronological provenance record ordering remain exactly as supplied.
    for record in data.get("provenance", []):
        if record.get("scope_paths") is not None:
            record["scope_paths"] = sorted(record["scope_paths"])
    return data


def _scope_exists(owner, path):
    for part in path.split("."):
        if is_dataclass(owner) and part in {f.name for f in fields(owner)}:
            owner = getattr(owner, part)
        elif isinstance(owner, dict) and part in owner:
            owner = owner[part]
        else:
            return False
    return True


def _faithful_fields(original, encoded):
    """Typed defaults/legacy null omissions may normalize; supplied facts may not."""
    if isinstance(original,dict) and isinstance(encoded,dict):
        return all((value is None if key not in encoded else _faithful_fields(value,encoded[key]))
                   for key,value in original.items())
    if isinstance(original,list) and isinstance(encoded,list):
        return len(original)==len(encoded) and all(_faithful_fields(a,b) for a,b in zip(original,encoded))
    return original == encoded


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelSourceContext:
    """Header evidence remains relative to the original BuildingModel owner."""
    model_id: str
    schema_version: str
    name: str | None
    coordinate_system: CoordinateSystem
    provenance: tuple[Provenance,...]
    attributes: dict[str,Any]
    content_fingerprint: str

    def __post_init__(self):
        _validate_id(self.model_id,'model source owner')
        if not isinstance(self.schema_version,str) or self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ContractError('unsupported model source context version')
        if self.name is not None and not isinstance(self.name,str):raise ContractError('model source name must be text or null')
        if not isinstance(self.coordinate_system,CoordinateSystem):raise ContractError('model source coordinates must be typed')
        _array(self.provenance,Provenance,'model source provenance')
        if not isinstance(self.attributes,dict):raise ContractError('model source attributes must be an object')
        _validate_json_value(self.attributes)
        for record in self.provenance:
            _decode_dataclass(Provenance,_portable(record))
            if not isinstance(record.attributes,dict):raise ContractError('source provenance attributes must be objects')
            for path in record.scope_paths or ():
                root,*rest=path.split('.')
                if root not in get_type_hints(BuildingModel):raise ContractError('unknown model-owner evidence scope')
                if rest and (root in ENTITY_TYPES or root=='provenance' or not _scope_exists(self,path)):
                    raise ContractError('unknown model-owner evidence scope')
        data=_portable(self);data.pop('content_fingerprint')
        data=_scopes(data)
        digest=hashlib.sha256(_json(data).encode()).hexdigest()
        if self.content_fingerprint != digest:raise ContractError('model source context content binding is inconsistent')
        object.__setattr__(self,'attributes',json.loads(_json(self.attributes)))
        object.__setattr__(self,'provenance',tuple(_decode_dataclass(Provenance,p) for p in data['provenance']))

    @classmethod
    def capture(cls,data):
        header=_scopes({key:data[key] for key in ('model_id','schema_version','name','coordinate_system','provenance','attributes')})
        digest=hashlib.sha256(_json(header).encode()).hexdigest()
        return _decode_dataclass(cls,{**header,'content_fingerprint':digest})


def _decode_source_entity(collection, data):
    if (collection == "routes" and isinstance(data, dict)
            and data.get("geometry_kind") == "line_arc_v1"):
        validate_physical_json_numbers({"routes": [data]})
        return _decode_dataclass(PhysicalRoute, data)
    return _decode_dataclass(ENTITY_TYPES[collection], data)


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceSnapshot:
    """A canonical owner snapshot; qualifiers stay relative to this owner."""
    collection: str
    entity: dict[str, Any]
    content_fingerprint: str

    def __post_init__(self):
        if not isinstance(self.collection,str) or self.collection not in ENTITY_TYPES or not isinstance(self.entity, dict):
            raise ContractError("source snapshot requires a registered collection and object")
        _validate_json_value(self.entity)
        decoded = _decode_source_entity(self.collection, self.entity)
        data = _portable(decoded)
        if not _faithful_fields(self.entity,data):
            raise ContractError("source snapshot cannot roundtrip its canonical field values")
        for record in decoded.provenance:
            for path in record.scope_paths or ():
                if not _scope_exists(decoded, path):
                    raise ContractError("source evidence names an unknown owner-relative field")
        data = _scopes(data)
        digest = hashlib.sha256(_json({"collection":self.collection,"entity":data}).encode()).hexdigest()
        if not isinstance(self.content_fingerprint,str) or self.content_fingerprint != digest:
            raise ContractError('source snapshot content binding is inconsistent')
        object.__setattr__(self, "entity", data)

    @property
    def id(self):
        return self.entity["id"]

    @classmethod
    def capture(cls, collection, entity):
        return cls.from_source_data(collection,_portable(entity))

    @classmethod
    def from_source_data(cls,collection,data):
        normalized=_scopes(_portable(_decode_source_entity(collection,data)))
        digest=hashlib.sha256(_json({'collection':collection,'entity':normalized}).encode()).hexdigest()
        return cls(collection=collection,entity=normalized,content_fingerprint=digest)


def _missing(circuit):
    return tuple(name for name in OPTIONAL_FACTS if circuit[name] is None or
                 (name in {"circuit_number", "phase"} and not circuit[name].strip()))


@dataclass(frozen=True, slots=True, kw_only=True)
class CircuitFactRow:
    circuit: SourceSnapshot
    source_port: SourceSnapshot
    source_owner: SourceSnapshot
    missing_fields: tuple[str, ...]
    assignment_status: str

    def __post_init__(self):
        if any(not isinstance(s, SourceSnapshot) for s in (self.circuit,self.source_port,self.source_owner)):
            raise ContractError("circuit row requires typed canonical source snapshots")
        if self.circuit.collection != "circuits" or self.source_port.collection != "ports":
            raise ContractError("circuit/source-port snapshot collection mismatch")
        if (self.circuit.entity["source_port_id"] != self.source_port.id or
                self.source_port.entity["owner_id"] != self.source_owner.id):
            raise ContractError("circuit row contradicts actual source-port ownership")
        if self.source_port.id==self.source_owner.id or self.source_owner.collection in {
            'box_selection_outcomes','raceway_selections','raceway_selection_outcomes',
            'support_assemblies','support_components','support_interfaces','support_selection_outcomes',
            'fitting_selections','fitting_interfaces','fitting_selection_outcomes'}:
            raise ContractError('nonphysical/self source owner cannot own a circuit source port')
        _array(self.missing_fields, str, "missing_fields")
        if self.missing_fields != _missing(self.circuit.entity):
            raise ContractError("missing fields contradict supplied circuit facts")
        expected = ('panel' if self.source_owner.collection=='electrical_equipment' and
                    self.source_owner.entity['equipment_type'] in PANEL_TYPES else 'unsupported_source_owner')
        if self.assignment_status != expected:
            raise ContractError('source assignment diagnostic contradicts actual owner type')


def _conflicts(rows):
    labels = {}
    for row in rows:
        label = row.circuit.entity["circuit_number"]
        if label is not None and label.strip():
            labels.setdefault(label, []).append(row.circuit.id)
    return tuple(LabelConflict(label=label,circuit_ids=tuple(sorted(identities)))
                 for label,identities in sorted(labels.items()) if len(identities)>1)


@dataclass(frozen=True, slots=True, kw_only=True)
class LabelConflict:
    label: str
    circuit_ids: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.label,str) or not self.label.strip():
            raise ContractError("label conflict requires an explicit opaque label")
        _ids(self.circuit_ids,"conflict circuit_ids")
        if len(self.circuit_ids)<2 or self.circuit_ids != tuple(sorted(self.circuit_ids)):
            raise ContractError("label conflict requires sorted distinct circuit identities")


def _subtotal(rows):
    known = [r.circuit.entity["load_va"] for r in rows if r.circuit.entity["load_va"] is not None]
    try:
        total = math.fsum(known)
    except (OverflowError,ValueError) as exc:
        raise ContractError("known connected-VA subtotal exceeds finite output range") from exc
    return _finite(total,"known connected-VA subtotal")


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelFactGroup:
    panel: SourceSnapshot
    circuits: tuple[CircuitFactRow, ...]
    known_connected_va: float
    unknown_load_circuit_ids: tuple[str, ...]
    all_listed_loads_known: bool
    label_conflicts: tuple[LabelConflict, ...]
    subtotal_derivation: str
    subtotal_source_circuit_ids: tuple[str, ...]
    diagnostic_codes: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.panel,SourceSnapshot) or self.panel.collection != "electrical_equipment" or self.panel.entity["equipment_type"] not in PANEL_TYPES:
            raise ContractError("panel group requires explicitly typed panelboard equipment")
        _array(self.circuits,CircuitFactRow,"panel circuits")
        _array(self.label_conflicts,LabelConflict,"label conflicts")
        _ids(self.unknown_load_circuit_ids,"unknown_load_circuit_ids")
        _ids(self.subtotal_source_circuit_ids,'subtotal_source_circuit_ids')
        _array(self.diagnostic_codes,str,'diagnostic_codes')
        identities=tuple(r.circuit.id for r in self.circuits)
        if identities != tuple(sorted(set(identities))):
            raise ContractError("panel rows require sorted unique circuit identities")
        if any(r.source_owner != self.panel for r in self.circuits):
            raise ContractError("panel row source owner contradicts its panel group")
        total=_finite(self.known_connected_va,"known_connected_va")
        if total != _subtotal(self.circuits):
            raise ContractError("known connected-VA subtotal contradicts listed circuits")
        unknown=tuple(r.circuit.id for r in self.circuits if r.circuit.entity["load_va"] is None)
        if self.unknown_load_circuit_ids != unknown:
            raise ContractError("unknown load identities contradict listed circuits")
        if not isinstance(self.all_listed_loads_known,bool) or self.all_listed_loads_known != (not unknown):
            raise ContractError("listed-load completeness contradicts unknown load facts")
        if self.label_conflicts != _conflicts(self.circuits):
            raise ContractError("label conflicts contradict opaque circuit labels")
        known=tuple(r.circuit.id for r in self.circuits if r.circuit.entity['load_va'] is not None)
        if self.subtotal_derivation != 'inferred' or self.subtotal_source_circuit_ids != known:
            raise ContractError('subtotal basis contradicts supplied connected-load facts')
        codes=tuple(sorted(code for code,present in (
            ('no_listed_circuits',not self.circuits),('unknown_connected_loads',bool(unknown)),
            ('duplicate_circuit_number',bool(self.label_conflicts))) if present))
        if self.diagnostic_codes != codes:
            raise ContractError('panel diagnostics contradict listed facts')
        object.__setattr__(self,"known_connected_va",total)


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelScheduleReport:
    version: str
    source_model_id: str
    source_model_version: str
    binding_algorithm: str
    source_model_fingerprint: str
    model_source_context: ModelSourceContext
    panels: tuple[PanelFactGroup, ...]
    unassigned_circuits: tuple[CircuitFactRow, ...]
    code_verification_performed: bool

    def __post_init__(self):
        _validate_id(self.source_model_id,"source_model_id")
        if self.version != REPORT_VERSION or not isinstance(self.source_model_version,str) or self.source_model_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ContractError("unsupported panel report/model version")
        if self.binding_algorithm != BINDING_ALGORITHM or not isinstance(self.source_model_fingerprint,str) or re.fullmatch(r'[0-9a-f]{64}',self.source_model_fingerprint) is None:
            raise ContractError("invalid semantic source-model binding")
        if self.code_verification_performed is not False:
            raise ContractError("panel fact reports cannot claim code verification")
        if (not isinstance(self.model_source_context,ModelSourceContext) or
            self.model_source_context.model_id != self.source_model_id or
            self.model_source_context.schema_version != self.source_model_version):
            raise ContractError('model source context contradicts report binding')
        _array(self.panels,PanelFactGroup,"panels")
        _array(self.unassigned_circuits,CircuitFactRow,"unassigned_circuits")
        ids=tuple(p.panel.id for p in self.panels)
        if ids != tuple(sorted(set(ids))):
            raise ContractError("panel groups require sorted distinct identities")
        unassigned=tuple(r.circuit.id for r in self.unassigned_circuits)
        if unassigned != tuple(sorted(set(unassigned))):
            raise ContractError("unassigned circuits require sorted distinct identities")
        all_rows=tuple(r for p in self.panels for r in p.circuits)+self.unassigned_circuits
        circuit_ids=[r.circuit.id for r in all_rows]
        if len(circuit_ids)!=len(set(circuit_ids)):
            raise ContractError("a canonical circuit may appear only once in the report")
        if any(r.source_owner.collection == 'electrical_equipment' and r.source_owner.entity['equipment_type'] in PANEL_TYPES for r in self.unassigned_circuits):
            raise ContractError("explicit panel circuit cannot be unassigned")
        snapshots={}
        for source in tuple(p.panel for p in self.panels)+tuple(s for r in all_rows for s in (r.circuit,r.source_port,r.source_owner)):
            minimum_version = ('1.7.0' if source.collection == 'routes' and source.entity.get('geometry_kind') == 'line_arc_v1'
                               else _minimum_source_schema(source.collection))
            if self.source_model_version < minimum_version:
                raise ContractError('source collection is unavailable in declared model version')
            previous=snapshots.get(source.id)
            if previous is not None and previous != source:
                raise ContractError("contradictory snapshots for one canonical source identity")
            snapshots[source.id]=source

    @classmethod
    def from_dict(cls,data):
        _validate_json_value(data)
        return _decode_dataclass(cls,data)

    @classmethod
    def from_json(cls,text):
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ContractError('duplicate panel report JSON key')
                result[key]=value
            return result
        def constant(value):raise ContractError('nonfinite panel report JSON number')
        try:
            exact=json.loads(text,object_pairs_hook=pairs,parse_constant=constant,parse_float=_physical_json_float)
            if isinstance(exact,dict):
                rows=list(exact.get('unassigned_circuits', ())) if isinstance(exact.get('unassigned_circuits'),list) else []
                for panel in exact.get('panels', ()) if isinstance(exact.get('panels'),list) else ():
                    if isinstance(panel,dict) and isinstance(panel.get('circuits'),list):rows.extend(panel['circuits'])
                for row in rows:
                    owner=row.get('source_owner') if isinstance(row,dict) else None
                    entity=owner.get('entity') if isinstance(owner,dict) and owner.get('collection')=='routes' else None
                    if isinstance(entity,dict) and entity.get('geometry_kind')=='line_arc_v1':
                        validate_physical_json_numbers({'routes':[entity]})
            data=json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
        except (TypeError,json.JSONDecodeError) as exc:raise ContractError('invalid panel report JSON') from exc
        return cls.from_dict(data)

    def to_dict(self):
        data=_portable(self)
        # Revalidate nested snapshots and arithmetic if a caller mutated a dict.
        self.from_dict(data)
        return data

    def to_json(self):
        return json.dumps(self.to_dict(),sort_keys=True,indent=2,allow_nan=False)

    def validate_against_model(self,model):
        if self.to_dict()!=generate_panel_schedule_report(model).to_dict():
            raise ContractError('panel report does not match supplied source model')


def _source_document(model):
    if not isinstance(model,BuildingModel):raise ContractError('report requires a BuildingModel')
    validate_model(model)
    original=model.to_dict()
    decoded=BuildingModel.from_dict(original)
    data=decoded.to_dict()
    if data!=original:raise ContractError('model facts do not roundtrip through their typed canonical contract')
    _scopes(data)
    for collection in ENTITY_TYPES:
        if collection in data:
            data[collection]=sorted((_scopes(e) for e in data[collection]),key=lambda e:e['id'])
    return data


def generate_panel_schedule_report(model: BuildingModel) -> PanelScheduleReport:
    """No source mutation, source-owner guessing or engineering calculations."""
    document=_source_document(model)
    fingerprint=hashlib.sha256(_json(document).encode()).hexdigest()
    sources={entity['id']:SourceSnapshot.from_source_data(collection,entity)
             for collection in ENTITY_TYPES for entity in document.get(collection,[])}
    panels={source.id:source for source in sources.values() if source.collection=='electrical_equipment' and source.entity['equipment_type'] in PANEL_TYPES}
    grouped={identity:[] for identity in panels};unassigned=[]
    for circuit in (s for s in sources.values() if s.collection=='circuits'):
        port=sources[circuit.entity['source_port_id']];owner=sources[port.entity['owner_id']]
        row=CircuitFactRow(circuit=circuit,source_port=port,source_owner=owner,missing_fields=_missing(circuit.entity),
            assignment_status='panel' if owner.id in panels else 'unsupported_source_owner')
        (grouped[owner.id] if owner.id in panels else unassigned).append(row)
    groups=[]
    for identity,panel in sorted(panels.items()):
        rows=tuple(sorted(grouped[identity],key=lambda r:r.circuit.id))
        unknown=tuple(r.circuit.id for r in rows if r.circuit.entity['load_va'] is None)
        conflicts=_conflicts(rows)
        codes=tuple(sorted(code for code,present in (('no_listed_circuits',not rows),
            ('unknown_connected_loads',bool(unknown)),('duplicate_circuit_number',bool(conflicts))) if present))
        groups.append(PanelFactGroup(panel=panel,circuits=rows,known_connected_va=_subtotal(rows),
            unknown_load_circuit_ids=unknown,all_listed_loads_known=not unknown,label_conflicts=conflicts,
            subtotal_derivation='inferred',subtotal_source_circuit_ids=tuple(r.circuit.id for r in rows if r.circuit.entity['load_va'] is not None),
            diagnostic_codes=codes))
    return PanelScheduleReport(version=REPORT_VERSION,source_model_id=model.model_id,
        source_model_version=model.schema_version,binding_algorithm=BINDING_ALGORITHM,
        source_model_fingerprint=fingerprint,model_source_context=ModelSourceContext.capture(document),panels=tuple(groups),
        unassigned_circuits=tuple(sorted(unassigned,key=lambda r:r.circuit.id)),code_verification_performed=False)
