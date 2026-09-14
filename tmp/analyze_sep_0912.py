from pathlib import Path
import pandas as pd
import numpy as np
import json
import xml.etree.ElementTree as ET

ROOT=Path('C:/Users/Rylan/Downloads')
IDS=['093842','094119','095511','095843','100341']
frames={}
for ident in IDS:
    name=f'20260912_{ident}_001.csv'
    d=pd.read_csv(ROOT/name)
    frames[ident]=d
    dt=d.groupby('trial_id').esp_uptime_ms.diff()
    seq=d.groupby('trial_id').sample_sequence.diff()
    print(name, json.dumps({'rows':len(d),'trials':d.trial_id.nunique(),
        'metadata':{k:d[k].drop_duplicates().tolist() for k in ['condition','label','phase','commanded_speed_mm_s','accel_mm','decel_mm','sensor_test_mode','valid_mask','lidar_connected']},
        'rows_per_trial':d.groupby('trial_id').size().describe().to_dict(),
        'pos_range':[d.position_mm.min(),d.position_mm.max()],
        'dt_ms':dt[dt>0].quantile([0,.5,.95,1]).to_dict(),
        'seq_gaps':int((seq>1).sum()),'seq_missing':float((seq[seq>1]-1).sum()),
        'pressure_ranges':{str(c):[d[f'pressure_pa_{c}'].min(),d[f'pressure_pa_{c}'].max()] for c in range(1,6)}},ensure_ascii=False))

records=[]
for ident,d in frames.items():
    for tid,t in d.groupby('trial_id',sort=False):
        for c in range(1,6):
            p=f'pressure_pa_{c}'
            valid=((t.valid_mask.astype(int)&(1<<(c-1)))!=0)&np.isfinite(t[p])
            v=t[valid & (t.phase=='measure')]
            base=v[v.position_mm<=20][p].mean()
            row={'file':ident,'trial':tid,'ch':c,'label':int(t.label.iloc[0]),'fan':int('有风扇' in t.condition.iloc[0]),'base':base}
            for lo,hi in [(250,600),(600,1000),(1000,1400),(1400,1800),(250,1800)]:
                z=v[v.position_mm.between(lo,hi)][p]
                row[f'mean_{lo}_{hi}']=z.mean()-base
                row[f'std_{lo}_{hi}']=z.std()
            row['ramp']=row['mean_1400_1800']-row['mean_600_1000']
            records.append(row)
features=pd.DataFrame(records)
print('FEATURE GROUP MEAN STD')
print(features.groupby(['file','ch'])[['base','mean_250_1800','ramp','std_250_1800']].agg(['mean','std']).round(4).to_string())
print('SEPARATION per channel: descriptive standardized mean differences, NOT accuracy')
for fan in [0,1]:
    for ch in range(1,6):
        f=features[(features.fan==fan)&(features.ch==ch)]
        a=f[f.label==0];b=f[f.label==1]
        results={}
        for col in ['base','mean_250_1800','mean_1400_1800','ramp']:
            pool=np.sqrt((a[col].var()+b[col].var())/2)
            results[col]={'clear_mean':a[col].mean(),'obstacle_mean':b[col].mean(),'d':(b[col].mean()-a[col].mean())/pool if pool>0 else None,
                          'clear_range':[a[col].min(),a[col].max()],'obstacle_range':[b[col].min(),b[col].max()]}
        print(fan,ch,json.dumps(results))

print('QUALITY')
for ident,d in frames.items():
    print(ident,'invalid_percent', [round(100*((d.valid_mask.astype(int)&(1<<(c-1)))==0).mean(),3) for c in range(1,6)])
    ix=d.pressure_pa_3.abs().nlargest(3).index
    print(d.loc[ix,['trial_id','position_mm','pressure_pa_3','valid_mask','moving']].to_string(index=False))
    text=[e.text for e in ET.parse(ROOT/f'20260912_{ident}_001.svg').getroot().iter() if e.tag.endswith('text')]
    print('svg_text',json.dumps(text[:12],ensure_ascii=True))

print('RAMP SD / AUC descriptive only')
for fan in [0,1]:
    for c in range(1,6):
        a=features[(features.fan==fan)&(features.ch==c)&(features.label==0)].ramp.to_numpy()
        b=features[(features.fan==fan)&(features.ch==c)&(features.label==1)].ramp.to_numpy()
        auc=((b[:,None]>a).mean()+.5*(b[:,None]==a).mean())
        print(fan,c,round(a.mean(),4),round(a.std(ddof=1),4),round(b.mean(),4),round(b.std(ddof=1),4),'AUC magnitude',round(max(auc,1-auc),3))

from PIL import Image, ImageDraw, ImageFont
out=Path('C:/Users/Rylan/.codex/visualizations/2026/08/20/01a01f95-99c9-7291-b51c-5e78be81de3f/pressure_20260912')
out.mkdir(parents=True,exist_ok=True)
im=Image.new('RGB',(1600,1000),'white'); dr=ImageDraw.Draw(im)
fontpath='C:/Windows/Fonts/msyh.ttc'
font=lambda size:ImageFont.truetype(fontpath,size)
def txt(x,y,s,size=19,color='#253247'):dr.text((x,y),s,font=font(size),fill=color)
txt(65,20,'风扇工况辨识度比较 · 第 3 通道',32)
txt(65,67,'上：10–1800 mm 原始有效点（不含减速与停留）；下：逐次形状特征。未滤波、未取中位数。',20)
colors={0:'#397ab5',1:'#cd563b'}; pale={0:'#8cb3d4',1:'#e7a490'}
for fan in [0,1]:
    left=100+fan*780;right=left+680;top=170;bottom=555
    txt(left,116,('无风扇：20 次无障碍 / 10 次有障碍' if fan==0 else '有风扇：10 次无障碍 / 10 次有障碍'),23)
    xmin,xmax=10,1800;ymin,ymax=-1.4,3.6
    X=lambda x:left+(x-xmin)/(xmax-xmin)*(right-left)
    Y=lambda y:bottom-(y-ymin)/(ymax-ymin)*(bottom-top)
    for y in [-1,0,1,2,3]:
        dr.line((left,Y(y),right,Y(y)),fill='#e4e7ec');txt(left-42,Y(y)-12,str(y),17)
    for x in [10,500,1000,1500,1800]:
        txt(X(x)-20,bottom+10,str(x),17)
    for ident,d in frames.items():
        if int('有风扇' in d.condition.iloc[0])!=fan:continue
        for tid,t in d.groupby('trial_id',sort=False):
            # Stop at the first arrival at the endpoint; exclude dwell at same X.
            hits=np.flatnonzero(t.position_mm.to_numpy()>=1900)
            if len(hits):t=t.iloc[:hits[0]+1]
            t=t[t.position_mm<=1800]
            segment=[]
            for row in t.itertuples():
                if (int(row.valid_mask)&4) and np.isfinite(row.pressure_pa_3):
                    segment.append((X(row.position_mm),Y(row.pressure_pa_3)))
                else:
                    if len(segment)>1:dr.line(segment,fill=pale[int(t.label.iloc[0])],width=1)
                    segment=[]
            if len(segment)>1:dr.line(segment,fill=pale[int(t.label.iloc[0])],width=1)
    dr.rectangle((left,top,right,bottom),outline='#adb5c1',width=1)
    txt(left,top-25,'压力 (Pa)',17);txt(right-170,bottom+38,'滑台位置 (mm)',18)
    txt(left+410,top+10,'蓝：无障碍   红：有障碍',16)
    top2,bottom2=690,915
    txt(left,637,'形状特征 = 均值(1400–1800 mm) − 均值(600–1000 mm)',18)
    YY=lambda v:bottom2-(v+.85)/1.2*(bottom2-top2)
    for y in [-.8,-.6,-.4,-.2,0,.2]:
        dr.line((left,YY(y),right,YY(y)),fill='#e4e7ec');txt(left-58,YY(y)-11,f'{y:.1f}',16)
    for label in [0,1]:
        vals=features[(features.fan==fan)&(features.ch==3)&(features.label==label)].ramp.to_numpy()
        center=left+180+label*320
        for i,v in enumerate(vals):
            x=center+((i%5)-2)*13;y=YY(v)
            dr.ellipse((x-5,y-5,x+5,y+5),fill=colors[label])
        dr.line((center-45,YY(vals.mean()),center+45,YY(vals.mean())),fill=colors[label],width=3)
        txt(center-48,bottom2+12,'无障碍' if label==0 else '有障碍',20,colors[label])
    dr.rectangle((left,top2,right,bottom2),outline='#adb5c1')
txt(65,963,'每点代表一次实验；短横线为算术均值。当前批次内分离不等于跨批次测试准确率。',18)
im.save(out/'fan_comparison.png')
print('FIGURE',out/'fan_comparison.png')
