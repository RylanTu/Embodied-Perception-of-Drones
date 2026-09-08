"""Annotate the existing scientific SVG without modifying pressure curves."""
from pathlib import Path
import xml.etree.ElementTree as ET

BASE = Path(__file__).parent / 'output'
SOURCE = BASE / '20260830_081019_081210_comparison.svg'
OUT = BASE / '20260830_081019_081210_deceleration.svg'
NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', NS)

def element(tag, **attrs):
    return ET.Element(f'{{{NS}}}{tag}', {k.replace('_','-'):str(v) for k,v in attrs.items()})

def label(x,y,value,size=17):
    item=element('text',x=x,y=y,font_family='Microsoft YaHei, Segoe UI, sans-serif',font_size=size,fill='#92400e')
    item.text=value
    return item

def main():
    # servo.c: dp = floor((command_frequency + minimum_frequency) * decel_ms / 2000).
    # CSV records 1500 mm/s, 100 ms and a 1900 mm endpoint. PPM and minimum
    # frequency are the current firmware defaults, not telemetry measurements.
    ppm=80
    minimum_hz=200
    speed_mm_s=1500
    decel_ms=100
    end_mm=1900
    distance_pulses=(speed_mm_s*ppm+minimum_hz)*decel_ms//2000
    begin_mm=end_mm-distance_pulses/ppm
    # Coordinates of the existing figure: range 20..1880 mm and 172..1477 px.
    sx=lambda mm:172+(mm-20)/(1880-20)*1305
    start=sx(begin_mm)
    root=ET.parse(SOURCE).getroot()
    children=list(root)
    for item in children:
        tag=item.tag.split('}')[-1]
        if tag=='text' and (item.text or '').startswith('最大均值差'):
            root.remove(item)
        elif tag=='line' and item.get('stroke')=='#8d97a8':
            root.remove(item)
        elif tag=='rect' and item.get('y')=='225' and item.get('width')=='300':
            root.remove(item)
    # Insert shading after the panel background, before grid and curve paths.
    panel=next(item for item in root if item.tag.endswith('rect') and item.get('x')=='80' and item.get('y')=='155')
    root.insert(list(root).index(panel)+1,element('rect',x=f'{start:.2f}',y=210,width=f'{1477-start:.2f}',height=490,fill='#f59e0b',opacity='0.22'))
    root.append(element('line',x1=f'{start:.2f}',x2=f'{start:.2f}',y1=210,y2=700,stroke='#d97706',stroke_width=2))
    root.append(element('rect',x=1050,y=225,width=420,height=75,rx=9,fill='#fffbeb',stroke='#fbbf24'))
    root.append(label(1070,253,'减速区 ≈1825–1900 mm',20))
    root.append(label(1070,281,'橙色为图内部分；原图仅画到1880 mm',16))
    root.append(element('line',x1=f'{start:.2f}',x2=f'{start:.2f}',y1=300,y2=331,stroke='#d97706',stroke_width=1.5))
    root.append(label(80,870,'按当前固件估算：减速距离75.125 mm；非编码器实测边界，脉冲分块和位置上报会带来偏差。',15))
    ET.ElementTree(root).write(OUT,encoding='utf-8',xml_declaration=True)
    print(f'deceleration: {begin_mm:.3f}..{end_mm} mm; {OUT}')

if __name__=='__main__':main()
