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
