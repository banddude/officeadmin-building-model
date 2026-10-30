"""Synthetic ordered SVG + combined vector PDF set; no pixel renderer is run.

Install the pdf extra plus native Cairo, then:
PYTHONPATH=src python examples/blank_sheet_pdf.py /tmp/blank-sheet-pdf-demo
"""
import argparse
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader
from oabm.drawings.blank_set import create_blank_sheet_set

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('destination',type=Path)
args=parser.parse_args()
args.destination.mkdir(parents=True)
fixture=Path(__file__).resolve().parents[1]/'fixtures/blank_sheets/v1/config.json'
config=json.loads(fixture.read_bytes());config['formats']=['svg','pdf']
source=args.destination/'config.json';source.write_text(json.dumps(config,sort_keys=True,indent=2)+'\n')
trees=[]
for label in ('first','rebuild'):
    root=create_blank_sheet_set(source,output_directory=args.destination/label)
    manifest=json.loads((root/'MANIFEST.json').read_bytes())
    for row in manifest['files']:
        data=(root/row['path']).read_bytes()
        assert len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256']
    trees.append({p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()})
assert trees[0]==trees[1]
reader=PdfReader(args.destination/'first/delivery/sheet-set.pdf')
assert len(reader.pages)==3
for page,sheet in zip(reader.pages,config['sheets']):
    assert sheet['id'] in page.extract_text()
    assert not list(page.images)
print('PDF:',args.destination/'first/delivery/sheet-set.pdf')
print('sheet order:',', '.join(manifest['sheet_order']))
print('page sizes (pt):',[(float(p.mediabox.width),float(p.mediabox.height)) for p in reader.pages])
print('verified files:',len(manifest['files']),'; complete rebuild byte-identical; no raster images')
