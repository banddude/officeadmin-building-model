"""Synthetic map acquisition flows through cover pagination and retained assets."""
import hashlib,json,sys
from pathlib import Path
import pytest
from oabm.drawings.production_set import create_production_sheet_set
from test_cover_maps import Client,AddressClient
sys.path.insert(0,str(Path(__file__).parents[1]/'examples'))
from reference_production_set import prepare


def test_explicit_auto_map_reaches_shared_delivery_and_keeps_defaults(tmp_path):
    path=prepare(tmp_path/'inputs',variant='reference',pdf=False)
    request=json.loads(path.read_bytes());request['cover_map']={'mode':'auto','cache_directory':'map-cache','candidate_index':None};path.write_text(json.dumps(request))
    cover=path.parent/'cover.json';config=json.loads(cover.read_bytes());config.pop('location_map',None);cover.write_text(json.dumps(config))
    profile=(path.parent/'profile.json').read_bytes();client=AddressClient(config['project']['location'])
    root=create_production_sheet_set(path,output_directory=tmp_path/'run',map_client=client)
    assert client.calls[:2]==['/tools/site-plan/geocode','/tools/site-plan/imagery']
    assert len(client.calls)==3 and client.calls[2].startswith('/tools/site-plan/tiles/')
    assert (path.parent/'profile.json').read_bytes()==profile
    rows=json.loads((root/'company/delivery/resolved-sheets.json').read_bytes())
    images=[image for row in rows for image in row['images']]
    assert len(images)==1
    record=images[0];raw=(root/'company/delivery'/record['path']).read_bytes()
    assert hashlib.sha256(raw).hexdigest()==record['sha256']
    source=json.loads((root/'derived/cover-map-source.json').read_bytes())
    assert source['sha256']==record['sha256'] and source['provenance']['provider']=='apple'
    svg=''.join(p.read_text() for p in (root/'company/delivery/sheets').glob('*.svg'))
    assert 'data:image/png;base64,' in svg and 'Apple' in svg
    assert (root/'inputs/cover.json').read_bytes()==cover.read_bytes()


def test_nonapple_map_refuses_before_output(tmp_path):
    path=prepare(tmp_path/'inputs',variant='reference',pdf=False)
    request=json.loads(path.read_bytes());request['cover_map']={'mode':'auto','cache_directory':'map-cache','candidate_index':None};path.write_text(json.dumps(request))
    cover=path.parent/'cover.json';config=json.loads(cover.read_bytes());config.pop('location_map',None);cover.write_text(json.dumps(config))
    with pytest.raises(ValueError):create_production_sheet_set(path,output_directory=tmp_path/'run',map_client=Client('esri'))
    assert not (tmp_path/'run').exists()



def test_confirmation_is_forwarded_and_cache_cannot_reuse_changed_identity(tmp_path):
    from oabm.drawings.cover_maps import candidate_fingerprint,MapAmbiguityError
    path=prepare(tmp_path/'inputs',variant='reference',pdf=False)
    cover=path.parent/'cover.json';config=json.loads(cover.read_bytes());config.pop('location_map',None);cover.write_text(json.dumps(config))
    query=config['project']['location'];label='OTHER SYNTHETIC SITE'
    fingerprint=candidate_fingerprint(query,{'label':label,'lat':34.,'lng':-118.})
    request=json.loads(path.read_bytes());request['cover_map']={'mode':'auto','cache_directory':'map-cache','candidate_index':0,'candidate_confirmation':fingerprint};path.write_text(json.dumps(request))
    root=create_production_sheet_set(path,output_directory=tmp_path/'first',map_client=AddressClient(label))
    assert json.loads((root/'derived/cover-map-source.json').read_bytes())['provenance']['resolved_address']==label
    request['cover_map']['mode']='cached';path.write_text(json.dumps(request))
    no_network=Client('esri');create_production_sheet_set(path,output_directory=tmp_path/'cached',map_client=no_network)
    assert not no_network.calls
    receipt=next((path.parent/'map-cache').glob('*.json'));record=json.loads(receipt.read_bytes());record['provenance']['latitude']='34.01';receipt.write_text(json.dumps(record))
    with pytest.raises(MapAmbiguityError):create_production_sheet_set(path,output_directory=tmp_path/'changed',map_client=no_network)
    assert not (tmp_path/'changed').exists() and not no_network.calls
