"""Synthetic real-font Unicode sheet proof; no customer identity or assets."""
import argparse
import json
from pathlib import Path
from oabm.drawings.company_sheets import create_company_sheet_set


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    args=parser.parse_args();args.directory.mkdir()
    fixture=Path(__file__).parents[1]/'fixtures/company_sheets/v1'
    profile=json.loads((fixture/'profile.json').read_bytes())
    profile['fields']['company']='ÉXAMPLE ÉLECTRIC'
    job=json.loads((fixture/'job.json').read_bytes())
    job['set_id']='synthetic-unicode-text'
    job['fields']['client']='José Peña — synthetic text'
    job['sheets']=[{'id':'U1','title':'ÉCLAIRAGE – 45° Ø'}]
    p=args.directory/'profile.json';j=args.directory/'job.json'
    p.write_text(json.dumps(profile,ensure_ascii=False,indent=2)+'\n')
    j.write_text(json.dumps(job,ensure_ascii=False,indent=2)+'\n')
    body='<g><text x="20" y="30" font-family="Liberation Sans" font-size="4">José Peña — 45° Ø “Éclairage”</text><text x="20" y="42" font-family="DejaVu Sans" font-size="4">José Peña — 45° Ø “Éclairage”</text></g>'
    print(create_company_sheet_set(p,j,output_directory=args.directory/'attempt',bodies={'U1':body}))

if __name__=='__main__':main()
