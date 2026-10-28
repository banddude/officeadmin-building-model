"""Public-safe supplied-fact fixtures; electrical values are not design advice."""
from pathlib import Path
from oabm.model import (BuildingModel, ElectricalEquipment, Circuit, Conductor,
                        Port, Pose, Point3, Vector3, Provenance)


def make_model(commercial=False):
    equipment=[]; ports=[]; circuits=[]; conductors=[]
    provenance=(Provenance(source_kind='synthetic',source_id='single-line-demo',derivation='user',method='explicit-demo-facts'),)
    def add(identity,kind,voltage,name=None):
        equipment.append(ElectricalEquipment(id=identity,name=name or identity,equipment_type=kind,
            pose=Pose(position=Point3(x=0,y=0,z=0)),rated_voltage_v=voltage,provenance=provenance,
            attributes={'single_line_notes':['Synthetic supplied rating: 200 A; 22 kAIC. No sizing or code verification performed.']} if identity=='MAIN' else {}))
    def connect(identity,source,load,voltage,load_role='incoming'):
        a,b=identity+'-out',identity+'-in'
        ports.extend([Port(id=a,owner_id=source,domain='electrical',role='load' if source=='ATS' else 'feeder',pose=Pose(position=Point3(x=0,y=0,z=0)),direction=Vector3(x=1,y=0,z=0),provenance=provenance),
                      Port(id=b,owner_id=load,domain='electrical',role=load_role,pose=Pose(position=Point3(x=0,y=0,z=0)),direction=Vector3(x=-1,y=0,z=0),provenance=provenance)])
        circuits.append(Circuit(id=identity,source_port_id=a,load_port_ids=(b,),voltage_v=voltage,poles=3 if voltage==480 else 1 if voltage==120 else 2,phase='ABC' if voltage==480 else 'A' if voltage==120 else 'AB',provenance=provenance))
        conductors.append(Conductor(id=identity+'-wire',circuit_id=identity,role='phase',material='copper',size='4 AWG',count=3 if voltage==480 else 1 if voltage==120 else 2,insulation='THHN',provenance=provenance))
    add('UTILITY','source',480 if commercial else 240,'Synthetic utility supply')
    if not commercial:
        add('METER','meter',240,'Residential meter')
        add('MAIN','panelboard',240,'Main residential panel')
        add('SUB','panelboard',240,'Detached workshop subpanel with a deliberately long supplied equipment label')
        connect('F001','UTILITY','METER',240)
        connect('F002','METER','MAIN',240)
        connect('F003','MAIN','SUB',240)
    else:
        add('GEN','generator',480,'Alternate generator supply')
        add('MAIN','switchboard',480,'Main distribution switchboard')
        add('ATS','transfer_switch',480,'Normal / alternate transfer equipment')
        add('XFMR','transformer',None,'Supplied transformer primary 480 V / secondary 120 V')
        for i in range(1,28):add(f'P{i:02d}','panelboard',480 if i<=15 else 120,f'Panel P{i:02d} / '+('480 V distribution' if i<=15 else '120 V distribution'))
        connect('F001','UTILITY','MAIN',480)
        connect('F002','MAIN','ATS',480,'normal')
        connect('F003','GEN','ATS',480,'alternate')
        connect('F004','ATS','XFMR',480)
        for i in range(1,16):connect(f'H{i:02d}','P01' if 2<=i<=5 else 'ATS',f'P{i:02d}',480)
        for i in range(16,28):connect(f'L{i:02d}','P16' if i>=24 else 'XFMR',f'P{i:02d}',120)
    return BuildingModel(model_id='synthetic-commercial-27' if commercial else 'synthetic-residential',
        electrical_equipment=tuple(equipment),ports=tuple(ports),circuits=tuple(circuits),conductors=tuple(conductors))

if __name__=='__main__':
    folder=Path(__file__).parent
    for label,commercial in [('residential',False),('commercial-27',True)]:
        (folder/(label+'.model.json')).write_text(make_model(commercial).to_json()+'\n')


def make_variant_model(shape, *, transfer_count=2):
    """Synthetic graph-shape mutations preserve explicitly supplied facts."""
    from dataclasses import replace
    model=make_model(True)
    equipment=list(model.electrical_equipment);ports=list(model.ports)
    circuits=list(model.circuits);wires=list(model.conductors)
    port_by_id={p.id:p for p in ports}
    if shape in {'shallow','deep'}:
        changed={}
        for circuit in circuits:
            target=port_by_id[circuit.load_port_ids[0]].owner_id
            if target.startswith('P'):
                number=int(target[1:])
                owner=('ATS' if number<=15 else 'XFMR') if shape=='shallow' or number in {1,16} else f'P{number-1:02d}'
                changed[circuit.source_port_id]=(owner,'load' if owner=='ATS' else 'feeder')
        ports=[replace(p,owner_id=changed[p.id][0],role=changed[p.id][1]) if p.id in changed else p for p in ports]
    else:
        def connect(identity,source,target,voltage,target_role='incoming'):
            base=circuits[0]
            circuits.append(replace(base,id=identity,source_port_id=identity+'-out',load_port_ids=(identity+'-in',),voltage_v=voltage,poles=3,phase='ABC'))
            ports.extend((replace(port_by_id[base.source_port_id],id=identity+'-out',owner_id=source,role='load' if source.startswith('ATS') else 'feeder'),
                          replace(port_by_id[base.load_port_ids[0]],id=identity+'-in',owner_id=target,role=target_role)))
        if shape=='two-transformers':
            base=next(e for e in equipment if e.id=='XFMR')
            equipment.append(replace(base,id='TX2',name='Second explicitly synthetic transformer'))
            connect('TX2-primary','ATS','TX2',480)
            targets={c.source_port_id for c in circuits if port_by_id.get(c.load_port_ids[0]) and port_by_id[c.load_port_ids[0]].owner_id in {'P20','P21','P22','P23'}}
            ports=[replace(p,owner_id='TX2') if p.id in targets else p for p in ports]
        elif shape=='multi-transfer':
            if transfer_count not in {2,3,4}:raise ValueError('synthetic demo supports 2, 3 or 4 explicit transfers')
            base=next(e for e in equipment if e.id=='ATS')
            changes={}
            for i in range(2,transfer_count+1):
                identity=f'ATS{i}'
                equipment.append(replace(base,id=identity,name=f'Synthetic transfer equipment {i}'))
                connect(identity+'-normal','MAIN',identity,480,'normal')
                connect(identity+'-alternate','GEN',identity,480,'alternate')
                circuit=next(c for c in circuits if c.id==f'H{17-i:02d}')
                changes[circuit.source_port_id]=identity
            ports=[replace(p,owner_id=changes[p.id],role='load') if p.id in changes else p for p in ports]
        else:raise ValueError('unknown synthetic graph shape')
    return replace(model,model_id='synthetic-'+shape,electrical_equipment=tuple(equipment),ports=tuple(ports),circuits=tuple(circuits),conductors=tuple(wires))
