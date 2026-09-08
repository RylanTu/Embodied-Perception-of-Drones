"""Audit missing samples before interpreting the 2026-08-30 pair."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from analyze_20260829_0929_0931 import resample
from compare_20260829 import GRID, moving_average

BASE = Path(r'D:\聊天文件\微信\xwechat_files\wxid_1dpbeiucwjug22_2a10\msg\file\2026-08')
PATHS = [BASE/'20260830T081019_batch_001.csv', BASE/'20260830T081210_batch_001.csv']

def main():
    report = []
    ramps = []
    for path in PATHS:
        df = pd.read_csv(path)
        summary = {'file':path.name, 'labels':df.label.unique().tolist(),
                   'conditions':df.condition.unique().tolist(),
                   'masks':df.valid_mask.value_counts().to_dict(),
                   'settings':df[['commanded_speed_mm_s','accel_ms','decel_ms','sensor_test_mode']].drop_duplicates().to_dict('records'),
                   'trials':[]}
        flags = (df.valid_mask.astype(int) & 1) != 0
        summary['valid_by_distance'] = []
        for lo, hi in [(10,500),(500,1000),(1000,1500),(1500,1700),(1700,1850),(1850,1900.01)]:
            subset = df[(df.position_mm>=lo)&(df.position_mm<hi)]
            summary['valid_by_distance'].append({'range':[lo,hi], 'n':len(subset), 'valid_pct':100*float(((subset.valid_mask.astype(int)&1)!=0).mean())})
        spikes = df[flags & (df.pressure_pa_1.abs()>3)]
        summary['valid_spikes'] = {'count':len(spikes),'position_range':[float(spikes.position_mm.min()),float(spikes.position_mm.max())]}
        rr=[]
        for tid, t in df.groupby('trial_id',sort=True):
            good=(t.valid_mask.astype(int).to_numpy() & 1)!=0
            pos=t.position_mm.to_numpy(float); ts=t.esp_uptime_ms.to_numpy(float)
            longest=0; run=0
            for is_good in good:
                run=0 if is_good else run+1;longest=max(longest,run)
            gdpos=np.sort(np.unique(pos[good])); gp=np.diff(gdpos); j=int(np.argmax(gp))
            dt=np.diff(ts); seq=np.diff(t.sample_sequence.to_numpy(float))
            curve=moving_average(resample(t),5)
            ramp=float(curve[np.argmin(abs(GRID-1770))]-curve[np.argmin(abs(GRID-1600))]);rr.append(ramp)
            summary['trials'].append({'id':tid,'invalid':int((~good).sum()),'n':len(t),
                'longest_bad_run_samples':longest,'max_valid_time_gap_ms':float(np.diff(ts[good]).max()),
                'max_valid_position_gap_mm':float(gp[j]),'gap_range_mm':[float(gdpos[j]),float(gdpos[j+1])],
                'sequence_missing':float(np.maximum(seq-1,0).sum()),
                'received_rate_hz':float((len(t)-1)*1000/(ts[-1]-ts[0])), 'ramp_pa':ramp})
        report.append(summary); ramps.append(np.asarray(rr))
    a,b=ramps; correct=0; errors=[]
    for i in range(len(a)):
        m0=np.delete(a,i).mean();m1=np.delete(b,i).mean();threshold=(m0+m1)/2;direction=np.sign(m1-m0)
        for y, v in enumerate([a[i],b[i]]):
            pred=int(direction*(v-threshold)>0); correct+=pred==y
            if pred!=y: errors.append({'class':y,'trial':i+1})
    print(json.dumps({'audit':report,'ramp':{'means':[float(a.mean()),float(b.mean())], 'correct':correct,'total':len(a)+len(b),'errors':errors}},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
