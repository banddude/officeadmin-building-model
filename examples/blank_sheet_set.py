"""Synthetic job metadata to an ordered, populated-titleblock blank sheet set.

Run: PYTHONPATH=src python examples/blank_sheet_set.py /tmp/blank-set-demo
Add --svg-only for a standard-library-only demo without the optional cad extra.
This serializes vector sheets; no renderer or external service is run.
"""
import argparse
import hashlib
import json
from pathlib import Path

from oabm.drawings.blank_set import create_blank_sheet_set

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('destination',type=Path)
parser.add_argument('--svg-only',action='store_true')
args=parser.parse_args()
args.destination.mkdir(parents=True)
fixture=Path(__file__).resolve().parents[1]/'fixtures/blank_sheets/v1/config.json'
config=json.loads(fixture.read_bytes())
if args.svg_only:config['formats']=['svg']
path=args.destination/'config.json'
path.write_text(json.dumps(config,sort_keys=True,indent=2)+'\n')
trees=[]
for name in ('first','rebuild'):
    root=create_blank_sheet_set(path,output_directory=args.destination/name)
    manifest=json.loads((root/'MANIFEST.json').read_bytes())
    for row in manifest['files']:
        data=(root/row['path']).read_bytes()
        assert len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256']
    trees.append({p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()})
assert trees[0]==trees[1]
print('delivery:',args.destination/'first/delivery')
print('sheet order:',', '.join(manifest['sheet_order']))
print('formats:',', '.join(manifest['formats']))
print('verified files:',len(manifest['files']),'; complete rebuild byte-identical')
print('model:',manifest['model_id'],'; status:',manifest['status'])
print('PDF:',manifest['pdf']['status'])
if 'requirement' in manifest['pdf']:print(manifest['pdf']['requirement'])
