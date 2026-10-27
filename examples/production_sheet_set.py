"""Synthetic blank-to-populated shared production chain; single-line pending."""
import argparse
import json
from pathlib import Path
import shutil
from oabm.drawings.company_sheets import create_company_sheet_set
from oabm.drawings.production_set import create_production_sheet_set


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    args=parser.parse_args();args.directory.mkdir()
    inputs=args.directory/'inputs'
    shutil.copytree(Path(__file__).parents[1]/'fixtures/production_set/v1',inputs)
    blank=create_company_sheet_set(inputs/'profile.json',inputs/'job.json',output_directory=args.directory/'blank')
    output=create_production_sheet_set(inputs/'production.json',output_directory=args.directory/'production')
    manifest=json.loads((output/'PRODUCTION-MANIFEST.json').read_bytes())
    print(json.dumps({'blank_titleblock_set':str(blank),'production_set':str(output),
        'sheet_order':manifest['sheet_order'],'schedule_rows':manifest['schedule_row_count'],
        'single_line':manifest['single_line']},indent=2))

if __name__=='__main__':main()
