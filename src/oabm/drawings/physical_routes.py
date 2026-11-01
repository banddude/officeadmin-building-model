"""Explicit XY route-review SVG adapter; never a clearance/installation drawing."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import html
import json
import math
from pathlib import Path

from oabm.model import BuildingModel, ContractError, PhysicalRoute, physical_segment_facts
from oabm.model.physical_route_context import physical_box_bounds
from oabm.reports.physical_routes import PhysicalRouteSchedule, generate_physical_route_schedule
from oabm.routing.physical_capture import SOURCE_BYTES, bounded_source_json
from oabm.routing.physical_verify_types import VerificationStop
from .svg import _fmt
from .company_sheets import _font, measure_sheet_text


NOTES = (
    'XY projection of all route heights; +Y up. Heights are model Z, not height above floor.',
    'Blue: actual OD horizontal sweep. Dashed orange: vertical-plane arc CENTERLINE ONLY; OD not represented.',
    'Vertical lines project to OD discs. Crossings/overlaps do not establish connections or vertical clearance.',
    'Grey: supplied obstacle boxes at all heights, without clearance envelopes. Other building geometry is omitted.',
    'Length/bends are full 3D schedule facts, never projected measurements. No installation or code approval.',
    'No live clearance check is performed; hidden vertical geometry is not established clear.',
)


def _escape(value):
    # XML 1.0 cannot carry arbitrary canonical text controls/surrogates.
    text = str(value)
    return html.escape(''.join(c if c in '\t\n\r' or 32 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF else f'\\u{ord(c):04x}' for c in text), quote=True)


@dataclass(frozen=True)
class PhysicalRoutePlan:
    svg: str
    schedule: PhysicalRouteSchedule

    @property
    def source_model_json(self):
        return self.schedule.source_model_json


def generate_physical_route_plan(model, *, scale=25):
    """Render all physical routes and identity-rotation obstacle boxes at 1:scale.

    No plan cut, visibility filtering, building geometry inference or rerouting.
    Legacy routes are explicitly excluded by the attached source-bound schedule.
    """
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 1 <= scale <= 1000 or not math.isfinite(scale):
        raise ContractError('physical route plan scale must be finite in [1, 1000]')
    schedule = generate_physical_route_schedule(model)
    model = BuildingModel.from_json(schedule.source_model_json)
    routes = sorted((r for r in model.routes if isinstance(r, PhysicalRoute)), key=lambda r: r.id)
    if not routes:
        raise ContractError('physical route plan requires at least one physical route')
    if len(routes) > 32 or sum(len(r.segments) for r in routes) > 4096 or len(model.obstacles) > 512:
        raise ContractError('physical route plan presentation limit: 32 routes, 4096 segments, 512 obstacles')
    boxes = [(o, physical_box_bounds(o.geometry)) for o in sorted(model.obstacles, key=lambda o:o.id)]
    ppu = 1000 / scale  # mm of paper per metre of canonical model
    coords = [(j.position.x, j.position.y) for r in routes for j in r.joints]
    for _, (lo, hi) in boxes:
        coords.extend(((float(lo[0])/1e9,float(lo[1])/1e9),(float(hi[0])/1e9,float(hi[1])/1e9)))
    radius = max(r.requirements.outside_diameter_m/2 for r in routes)
    xmin, xmax = min(x for x,y in coords)-radius, max(x for x,y in coords)+radius
    ymin, ymax = min(y for x,y in coords)-radius, max(y for x,y in coords)+radius
    width = max(280, (xmax-xmin)*ppu+32)
    plot_height = max(70, (ymax-ymin)*ppu+24)
    if width > 1200 or plot_height > 1200:
        raise ContractError('physical route plan exceeds 1200 mm presentation extent; choose a smaller drawing scale')
    x_origin = (width-(xmax-xmin)*ppu)/2
    y_origin = 30+(plot_height-(ymax-ymin)*ppu)/2
    def xy(p): return (x_origin+(p.x-xmin)*ppu, y_origin+(ymax-p.y)*ppu)
    def point(p): return ' '.join(_fmt(v) for v in xy(p))
    report = schedule.to_dict()
    attrs = f'data-source-model-sha256="{report["source"]["sha256"]}"'
    body = []
    try:
        _, font_family, _, _ = _font('DejaVu Sans', 400)
        _font('DejaVu Sans', 700)
    except ValueError as exc:
        raise ContractError(f'physical route plan font: {exc}') from exc
    def measured(value, size=3, weight=400):
        try:
            return measure_sheet_text(value, size, font_family='DejaVu Sans', font_weight=weight)
        except ValueError as exc:
            raise ContractError(f'physical route plan text: {exc}') from exc
    def positive_paper(value, label):
        if float(_fmt(value)) <= 0:
            raise ContractError(f'physical route plan {label} collapses at six-decimal-mm presentation precision; choose a larger drawing scale')
        return _fmt(value)
    def text(x,y,value,**kw):
        extra = ' '.join(f'{k.replace("_","-")}="{_escape(v)}"' for k,v in kw.items())
        body.append(f'<text x="{_fmt(x)}" y="{_fmt(y)}" {extra}>{_escape(value)}</text>')
    def paragraph(y, value, **kw):
        # Wrap the displayed characters with the same resolved font as SVG.
        # Breaking an overlong token is allowed; every identity character remains.
        remaining = html.unescape(_escape(value)).replace('\t', r'\t').replace('\n', r'\n').replace('\r', r'\r')
        weight = 700 if kw.get('font_weight') == 'bold' else 400
        size = kw.get('font_size', 3)
        while remaining:
            lo, hi = 0, len(remaining)
            while lo < hi:
                mid = (lo+hi+1)//2
                if measured(remaining[:mid], size, weight) <= width-32:
                    lo = mid
                else:
                    hi = mid-1
            if not lo:
                raise ContractError('physical route plan text character exceeds available paper width')
            end = lo
            if end < len(remaining):
                space = remaining.rfind(' ', 0, end)
                if space > 0:
                    end = space+1
            text(16,y,remaining[:end],**kw)
            remaining = remaining[end:]
            y += 4.5
            if y > 5000:
                raise ContractError('physical route plan annotations exceed 5000 mm presentation limit')
        return y
    text(16,12,'PHYSICAL ROUTE / XY REVIEW',font_size=5,font_weight='bold')
    text(16,19,f'1:{scale:g} at native print size | metres | +Y up | no depth cut')
    text(16,25,f'Source SHA-256: {report["source"]["sha256"]}',font_size=2.5)
    body.append(f'<rect x="12" y="30" width="{_fmt(width-24)}" height="{_fmt(plot_height)}" fill="#fafcfd" stroke="#d1d9df" stroke-width="0.3"/>')
    for index,(obstacle,(lo,hi)) in enumerate(boxes,1):
        x=x_origin+(float(lo[0])/1e9-xmin)*ppu; y=y_origin+(ymax-float(hi[1])/1e9)*ppu
        box_width = positive_paper(float(hi[0]-lo[0])/1e9*ppu, 'obstacle width')
        box_height = positive_paper(float(hi[1]-lo[1])/1e9*ppu, 'obstacle height')
        body.append(f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{box_width}" height="{box_height}" fill="#dce2e6" stroke="#78858c" stroke-width="0.25" data-source-ids="{_escape(obstacle.id)}"><title>{_escape(obstacle.id)}</title></rect>')
        text(x+1,y+3,f'O{index}',font_size=2.8)
    vertical = []
    for ri,route in enumerate(routes,1):
        joints={j.id:j.position for j in route.joints}
        for si,(seg,fact) in enumerate(zip(route.segments, physical_segment_facts(route)),1):
            a,b=joints[seg.start_joint_id],joints[seg.end_joint_id]
            ids=_escape(' '.join((route.id,seg.id,seg.start_joint_id,seg.end_joint_id,route.requirements.id)))
            od=route.requirements.outside_diameter_m*ppu
            horizontal=a.z==b.z
            positive_paper(od, 'outside diameter')
            vertical_line = seg.kind=='line' and a.x==b.x and a.y==b.y
            if not vertical_line and point(a) == point(b):
                raise ContractError('physical route plan projected segment collapses at six-decimal-mm presentation precision; choose a larger drawing scale')
            if seg.kind=='quarter_arc' and seg.plane_normal.z==1:
                sweep=0 if seg.turn=='positive_quarter' else 1  # model +Y is SVG -Y
                r=fact['radius_ticks']/1e9*ppu
                positive_paper(r, 'arc radius')
                d=f'M {point(a)} A {_fmt(r)} {_fmt(r)} 0 0 {sweep} {point(b)}'
            else:
                d=f'M {point(a)} L {point(b)}'
            if vertical_line:
                x,y=xy(a)
                outside_radius = positive_paper(od/2, 'outside radius')
                body.append(f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="{outside_radius}" fill="#1674ad" data-source-ids="{ids}"/>')
            else:
                style=f'stroke="#1674ad" stroke-width="{_fmt(od)}"' if horizontal else 'stroke="#b75a18" stroke-width="0.4" stroke-dasharray="1.5 1"'
                body.append(f'<path d="{d}" fill="none" {style} stroke-linecap="butt" data-source-ids="{ids}"><title>{_escape(seg.id)}; Z {_fmt(a.z)} to {_fmt(b.z)} m</title></path>')
            if not horizontal:
                tag=f'V{len(vertical)+1}'
                x,y=xy(a);text(x+2,y-2,tag,fill='#91470c',font_size=2.8)
                vertical.append((tag,route.id,seg.id,a.z,b.z,seg.kind))
        for label,joint,port in (('A',route.joints[0],route.start_port_id),('B',route.joints[-1],route.end_port_id)):
            x,y=xy(joint.position)
            body.append(f'<circle cx="{_fmt(x)}" cy="{_fmt(y)}" r="1" fill="white" stroke="#142f43" stroke-width="0.3" data-source-ids="{_escape(route.id+" "+port+" "+joint.id)}"/>')
            text(x+2,y+4,f'R{ri}{label}',font_size=2.8)
    y=36+plot_height
    # A scale bar remains valid even if a viewer scales the entire SVG.
    bar_m=min(1,(width-40)/ppu)
    body.append(f'<path d="M 16 {_fmt(y)} h {_fmt(bar_m*ppu)} m 0 -1 v 2 m {_fmt(-bar_m*ppu)} 0 v -2" fill="none" stroke="#142f43" stroke-width="0.4"/>')
    text(16,y+5,f'{bar_m:g} m XY scale bar');y+=13
    for note in NOTES: y=paragraph(y,note)
    y+=4
    for ri,(route,row) in enumerate(zip(routes,report['routes']),1):
        y=paragraph(y,f'R{ri} | {route.id}',font_weight='bold')
        y=paragraph(y,f'3D centerline approx {row["length"]["total_m_approx"]:.9f} m | {row["quarter_bend_count"]} quarter bends | actual OD {row["outside_diameter_m"]:g} m | CL radius {row["centerline_radius_m"]:g} m')
        for label,joint,port in (('A',route.joints[0],route.start_port_id),('B',route.joints[-1],route.end_port_id)):
            p=joint.position
            y=paragraph(y,f'R{ri}{label} {port} | X {p.x:g}, Y {p.y:g}, Z {p.z:g} m')
        zs=[j.position.z for j in route.joints]
        y=paragraph(y,f'Model Z range {min(zs):g} to {max(zs):g} m. Segment/joint IDs and supplied provenance retained in model.json and schedule.json.')
        y+=3
    for tag,rid,sid,a,b,kind in vertical:
        y=paragraph(y,f'{tag} | {rid} | {sid} | {kind} | Z {a:g} to {b:g} m; delta {b-a:+g} m')
    for index,(obstacle,(lo,hi)) in enumerate(boxes,1):
        y=paragraph(y,f'O{index} | {obstacle.id} | model Z {float(lo[2])/1e9:g} to {float(hi[2])/1e9:g} m')
    excluded=report['scope']['excluded_legacy_route_ids']
    y=paragraph(y,'Excluded legacy routes: '+(', '.join(excluded) if excluded else 'none'))
    omitted={key:len(value) for key,value in model.to_dict().items() if isinstance(value,list) and value and key not in ('routes','obstacles','ports')}
    y=paragraph(y,'Other collections omitted (endpoint ports only are marked): '+json.dumps(omitted,sort_keys=True))
    if y>5000:
        raise ContractError('physical route plan annotations exceed 5000 mm presentation limit')
    svg=(f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}mm" height="{_fmt(y+12)}mm" viewBox="0 0 {_fmt(width)} {_fmt(y+12)}" {attrs}>\n'
         '<title>Source-bound physical route XY review</title>\n'
         f'<rect width="{_fmt(width)}" height="{_fmt(y+12)}" fill="white"/>\n'
         f'<g font-family="{_escape(font_family)}" font-size="3" fill="#142f43">\n'+'\n'.join(body)+'\n</g></svg>\n')
    return PhysicalRoutePlan(svg,schedule)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Explicit physical route XY review SVG with source-bound 3D schedule.')
    parser.add_argument('model',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--scale',type=float,default=25)
    args=parser.parse_args(argv)
    try:
        with args.model.open('rb') as handle: raw=handle.read(SOURCE_BYTES+1)
        plan=generate_physical_route_plan(BuildingModel.from_json(bounded_source_json(raw)),scale=args.scale)
        prepared={k:v.encode('utf-8') for k,v in {'plan.svg':plan.svg,'model.json':plan.source_model_json,'schedule.json':plan.schedule.to_json(),'routes.csv':plan.schedule.routes_csv(),'segments.csv':plan.schedule.segments_csv()}.items()}
        args.output.mkdir(exist_ok=False)
        for name,data in prepared.items(): (args.output/name).write_bytes(data)
        return 0
    except (ContractError,VerificationStop,ValueError,OSError) as exc:
        parser.exit(2,f'physical route plan: {exc}\n')


if __name__=='__main__':
    raise SystemExit(main())
