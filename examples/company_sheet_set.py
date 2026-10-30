"""Executable synthetic shared-titleblock demo; creates no customer content."""
import argparse
import json
from pathlib import Path
from PIL import Image, ImageDraw
from oabm.drawings.company_sheets import create_company_sheet_set


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    args=parser.parse_args();args.output.mkdir()
    fixture=Path(__file__).parents[1]/'fixtures/company_sheets/v1'
    profile=json.loads((fixture/'profile.json').read_bytes())
    # Clearly synthetic logo components, never extracted company assets.
    for name,size,label in [('icon',(160,120),'DEMO'),('wordmark',(400,60),'EXAMPLE ELECTRIC')]:
        image=Image.new('RGB',size,'white');draw=ImageDraw.Draw(image)
        draw.rectangle((1,1,size[0]-2,size[1]-2),outline='black',width=2)
        draw.text((10,20),label,fill='black');image.save(args.output/(name+'.png'))
    profile['logos']={'icon':'icon.png','wordmark':'wordmark.png'}
    (args.output/'profile.json').write_text(json.dumps(profile,indent=2)+'\n')
    (args.output/'job.json').write_bytes((fixture/'job.json').read_bytes())
    root=create_company_sheet_set(args.output/'profile.json',args.output/'job.json',output_directory=args.output/'attempt')
    print(root)

if __name__=='__main__':main()
