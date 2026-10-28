"""Supplied drawing annotations on canonical entities; no electrical design.

Only this drawing consumer interprets the namespaced presentation attributes.
Canonical ports/circuits remain the exclusive source of connectivity.
"""
from __future__ import annotations
import math

STATUSES={'existing','new','unknown'}
KINDS={'power','neutral','grounding'}
PROTECTION={'breaker','fuse','disconnect','fused_disconnect'}


def _positive(value,label):
    if type(value) not in (int,float) or not math.isfinite(value) or value<=0:
        raise ValueError(label+': supply a positive finite explicit value')
    return value


def equipment_facts(entity):
    value=entity.attributes.get('single_line_drafting',{})
    allowed={'status','bus_current_a','neutral_ground_bond','notes'}
    if not isinstance(value,dict) or set(value)-allowed:
        raise ValueError(f'{entity.id}: single_line_drafting equipment fields must be {sorted(allowed)}')
    status=value.get('status','unknown')
    if not isinstance(status,str) or status not in STATUSES:raise ValueError(f'{entity.id}: unsupported supplied installation status')
    current=value.get('bus_current_a')
    if current is not None:_positive(current,entity.id+'.bus_current_a')
    bond=value.get('neutral_ground_bond','unknown')
    if not isinstance(bond,str) or bond not in {'isolated','bonded','unknown'}:raise ValueError(f'{entity.id}: unsupported supplied neutral/ground state')
    notes=value.get('notes',[])
    if not isinstance(notes,list) or any(not isinstance(note,str) for note in notes):
        raise ValueError(f'{entity.id}: drafting notes must be a list of supplied strings')
    return {'status':status,'bus_current_a':current,'neutral_ground_bond':bond,'notes':notes}


def circuit_facts(entity):
    value=entity.attributes.get('single_line_drafting',{})
    allowed={'status','connection_kind','protection','notes'}
    if not isinstance(value,dict) or set(value)-allowed:
        raise ValueError(f'{entity.id}: single_line_drafting circuit fields must be {sorted(allowed)}')
    status=value.get('status','unknown')
    kind=value.get('connection_kind','power')
    if not isinstance(status,str) or status not in STATUSES or not isinstance(kind,str) or kind not in KINDS:
        raise ValueError(f'{entity.id}: unsupported supplied status or connection_kind')
    protection=value.get('protection')
    if protection is not None:
        if not isinstance(protection,dict) or set(protection)-{'kind','rating_a','poles','interrupting_rating_a'} or not isinstance(protection.get('kind'),str) or protection.get('kind') not in PROTECTION:
            raise ValueError(f'{entity.id}: unsupported explicit protection annotation')
        for key in ('rating_a','interrupting_rating_a'):
            if protection.get(key) is not None:_positive(protection[key],entity.id+'.protection.'+key)
        poles=protection.get('poles')
        if poles is not None and (type(poles) is not int or poles<1):
            raise ValueError(f'{entity.id}: protection poles must be a positive integer')
        if entity.poles is not None and poles is not None and entity.poles!=poles:
            raise ValueError(f'{entity.id}: protection pole annotation conflicts with canonical circuit.poles')
    notes=value.get('notes',[])
    if not isinstance(notes,list) or any(not isinstance(note,str) for note in notes):
        raise ValueError(f'{entity.id}: drafting notes must be a list of strings')
    if kind!='power' and protection is not None:
        raise ValueError(f'{entity.id}: neutral/grounding links cannot carry an inferred phase protection symbol')
    return {'status':status,'connection_kind':kind,'protection':protection,'notes':notes}


def status_tag(status):
    return {'existing':'(E)','new':'(N)','unknown':'(?)'}[status]


def protection_label(facts):
    protection=facts['protection']
    if protection is None:return 'PROTECTION NOT SUPPLIED'
    rating='? A' if protection.get('rating_a') is None else f'{protection["rating_a"]:g} A'
    poles='?P' if protection.get('poles') is None else f'{protection["poles"]}P'
    label=protection['kind'].replace('_',' ').upper()+' '+rating+' / '+poles
    if protection.get('interrupting_rating_a') is not None:label+=f'; AIC {protection["interrupting_rating_a"]:g} A'
    return label


def feeder_labels(edge,tag):
    facts=edge['drafting'];kind=facts['connection_kind']
    voltage='VOLTAGE NOT SUPPLIED' if edge['voltage_v'] is None else f'{edge["voltage_v"]:g} V'
    lines=[status_tag(facts['status'])+' '+tag+' / '+(voltage if kind=='power' else kind.upper())]
    if facts['protection'] is not None:
        p=facts['protection'];rating='? A' if p.get('rating_a') is None else f'{p["rating_a"]:g} A'
        poles='?P' if p.get('poles') is None else f'{p["poles"]}P'
        lines.append(rating+' / '+poles+' '+p['kind'].replace('_',' ').upper())
    if edge['load_role'] in {'normal','alternate'}:lines.append(edge['load_role'].upper()+' SOURCE')
    if edge.get('wire_labels'):lines.extend(edge['wire_labels'])
    return lines
