"""Canonical classification demonstration using explicitly supplied synthetic facts."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from assembly_layer_selections import synthetic_model
from oabm.model import LayerSelectionConfig,SavedPreset
from oabm.exports import classify_model_layers,build_layer_glb


def run(output):
    output=Path(output);output.mkdir(exist_ok=False)
    model=synthetic_model()
    devices=list(model.electrical_devices)
    for i,system,status in ((0,'power','new'),(1,'lighting','existing_to_remain')):
        devices[i]=replace(devices[i],system=system,attributes={'pdf_electrical':{'scope_status':status}})
    model=replace(model,electrical_devices=tuple(devices))
    result=classify_model_layers(model)
    config=result.configuration
    architecture=tuple(l.id for l in config.layers if l.trade=='architecture')
    lighting=tuple(l.id for l in config.layers if l.system=='lighting')
    config=replace(config,presets=config.presets+(SavedPreset(id='preset:classified-review',name='Classified review',
        ghosted_layer_ids=architecture,hidden_layer_ids=lighting,
        visible_layer_ids=tuple(l.id for l in config.layers if l.id not in architecture+lighting)),))
    config.dump(output/'layers.json');config=LayerSelectionConfig.load(output/'layers.json')
    (output/'classification.json').write_text(result.manifest_json)
    (output/'model.json').write_text(result.source_model_json)
    requests=(('reset','preset:classified-all',(),()),('review','preset:classified-review',(),()),
        ('level','preset:classified-review',(),('level:proof',)),
        ('grid','preset:classified-all',tuple(l.id for l in config.layers if l.system=='ceiling_grid'),()))
    for label,preset,isolate,levels in requests:
        delivery=build_layer_glb(model,config,preset=preset,isolate=isolate,level_ids=levels,unassigned='error')
        (output/f'{label}.glb').write_bytes(delivery.glb_bytes)
        (output/f'{label}.json').write_text(delivery.manifest_json)
    return result

if __name__=='__main__':run(sys.argv[1])
