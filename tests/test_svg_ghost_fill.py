"""Ghost styling must preserve outline/open-path geometry when actually rendered."""
import io
import xml.etree.ElementTree as ET

import pytest
from PIL import Image
from oabm.drawings.model import Bounds2, DrawingPrimitive, DrawingView, LineStyle, Point2
from oabm.drawings.svg import view_to_svg


def ghost_view():
    style=LineStyle(stroke='ghost')
    return DrawingView('ghost-fill','plan','Synthetic ghost outline',Bounds2(0,0,6,3),50,(
        DrawingPrimitive('outline','polygon','test',('source:outline',),
            (Point2(.5,.5),Point2(2.5,.5),Point2(2.5,2.5),Point2(.5,2.5)),closed=True,style=style),
        DrawingPrimitive('open','polyline','test',('source:open',),
            (Point2(3.5,.5),Point2(3.5,2.5),Point2(5.5,2.5)),style=style),
        DrawingPrimitive('label','text','test',('source:label',),(Point2(4.,.2),),text='Ghost',style=style),
    ))


def test_ghost_svg_retains_fill_none_for_open_and_outline_geometry():
    svg=view_to_svg(ghost_view())
    nodes={node.get('id'):node for node in ET.fromstring(svg).iter()}
    for identifier in ('outline','open'):
        assert nodes[identifier].get('fill')=='none'
        assert 'fill:' not in nodes[identifier].get('style','')
    assert 'fill:#a6adb8' in nodes['label'].get('style','')


def test_rendered_ghost_interiors_stay_empty_and_open_path_does_not_close(tmp_path):
    cairosvg=pytest.importorskip('cairosvg')
    svg=view_to_svg(ghost_view())
    png=cairosvg.svg2png(bytestring=svg.encode(),output_width=600,output_height=300)
    (tmp_path/'ghost-outlines.svg').write_text(svg)
    (tmp_path/'ghost-outlines.png').write_bytes(png)
    rendered=Image.open(io.BytesIO(png)).convert('RGBA')
    assert rendered.getpixel((150,150))[3]==0  # rectangle interior
    assert rendered.getpixel((400,100))[3]==0  # formerly filled open triangle
    assert rendered.getpixel((450,150))[3]==0  # no implicit closing diagonal
    assert rendered.getpixel((50,150))[3]>0   # actual outline still renders
    assert rendered.getpixel((350,150))[3]>0  # actual open-path segment still renders
