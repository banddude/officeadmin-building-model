"""Generate a synthetic three-sheet set through the shared company renderer."""
from pathlib import Path
import argparse
from oabm.drawings.details import create_detail_sheet_set


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True,type=Path)
    parser.add_argument('--job',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--details',type=Path,help='Explicit detail request; defaults to the three-sheet fixture')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]/'fixtures'/'detail_sheets'
    print(create_detail_sheet_set(root/'model.json',args.details or root/'details.json',args.profile,args.job,output_directory=args.output))


if __name__=='__main__': main()
