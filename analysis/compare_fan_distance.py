"""Six-file, trial-level comparison; explicit user labels override stale CSV metadata.

No source data are changed. Invalid channel-1 values are excluded, not replaced by zero.
Fixed windows and fixed shrinkage classifier are identical for all fan placements.
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from compare_20260829 import fit_lda, predict_lda

BASE = Path(r'D:/聊天文件/微信/xwechat_files/wxid_1dpbeiucwjug22_2a10/msg/file/2026-08')
OUT = Path(__file__).resolve().parent / 'output'
FILES = {
    'far': ['20260830T081019', '20260830T081210'],
    'middle': ['20260829T103639', '20260829T103808'],
    'near': ['20260829T113635', '20260829T113758'],
}
GRID = np.arange(300, 1881, 10, dtype=float)


def curve(t):
    a = t.groupby('position_mm', as_index=False).pressure_pa_1.mean()
    x, p = a.position_mm.to_numpy(), a.pressure_pa_1.to_numpy()
    z = np.interp(GRID, x, p, left=np.nan, right=np.nan)
    right = np.searchsorted(x, GRID, side='left').clip(1, len(x)-1)
    gap = x[right] - x[right-1]
    z[(gap > 50) & (GRID != x[right])] = np.nan
    return z


def mean(t, lo, hi):
    p = t.loc[(t.position_mm >= lo) & (t.position_mm < hi), 'pressure_pa_1']
    return float(p.mean()) if len(p) >= 3 else np.nan


def cv(a, b, blocked=False):
    x = np.vstack([a, b]); y = np.repeat([0, 1], [len(a), len(b)])
    if not np.isfinite(x).all(): return {'error': 'missing window; no imputation'}
    groups = [np.arange(0, 5), np.arange(5, 10)] if blocked else [np.array([i]) for i in range(10)]
    prediction = np.zeros(len(y), dtype=int)
    for group in groups:
        test = np.r_[group, len(a)+group]
        train = np.setdiff1d(np.arange(len(x)), test)
        prediction[test] = predict_lda(fit_lda(x[train], y[train]), x[test])
    return {'correct': int((prediction == y).sum()), 'total': len(y),
            'obstacle_recall': float(prediction[y == 1].mean()),
            'clear_specificity': float((prediction[y == 0] == 0).mean())}


def univariate(a, b):
    a, b = np.array(a), np.array(b)
    variance = ((len(a)-1)*a.var(ddof=1)+(len(b)-1)*b.var(ddof=1))/(len(a)+len(b)-2)
    auc = float(((b[:, None] > a) + .5*(b[:, None] == a)).mean())
    # Direction and threshold come from training trials only.
    correct = 0
    for i in range(10):
        m0, m1 = np.delete(a, i).mean(), np.delete(b, i).mean()
        direction = 1 if m1 >= m0 else -1
        threshold = (m0+m1)/2
        correct += int(direction*(a[i]-threshold) <= 0) + int(direction*(b[i]-threshold) > 0)
    return {'clear_mean': float(a.mean()), 'obstacle_mean': float(b.mean()),
            'clear_sd': float(a.std(ddof=1)), 'obstacle_sd': float(b.std(ddof=1)),
            'difference_pa': float(b.mean()-a.mean()),
            'signed_cohen_d': float((b.mean()-a.mean())/np.sqrt(variance)),
            'auc_obstacle_higher': auc, 'threshold_cv_correct': correct, 'total': 20,
            'clear_values': a.tolist(), 'obstacle_values': b.tolist()}


def main():
    output = {'method': 'channel 1; valid_mask bit0; moving=1; each trial equal weight; no extra filtering',
              'caution': 'One recording batch per label per placement: batch and label are confounded. CV is within-batch separability, not deployment accuracy.',
              'groups': {}}
    all_curves = {}
    for group, names in FILES.items():
        arrays, group_report = [], {'files': []}
        for label, name in enumerate(names):
            df = pd.read_csv(BASE / (name+'_batch_001.csv'))
            flags = (df.valid_mask.astype(int) & 1) != 0
            entry = {'file':name, 'user_label':label, 'csv_labels':df.label.unique().tolist(),
                     'trials':int(df.trial_id.nunique()), 'rows':len(df),
                     'valid_percent':float(flags.mean()*100),
                     'settings':df[['commanded_speed_mm_s','accel_ms','decel_ms','sensor_test_mode']].drop_duplicates().to_dict('records')}
            features, curves, trial_info = [], [], []
            for tid, raw in df.groupby('trial_id', sort=True):
                valid = ((raw.valid_mask.astype(int)&1)!=0) & raw.moving.eq(1) & raw.phase.eq('measure')
                t = raw[valid & np.isfinite(raw.pressure_pa_1) & np.isfinite(raw.position_mm)].sort_values('position_mm')
                values = {f'bin_{lo}':mean(t,lo,lo+100) for lo in range(300,1800,100)}
                values.update(late_level=mean(t,1500,1800), terminal_level=mean(t,1825,1880),
                              ramp=mean(t,1700,1800)-mean(t,1500,1600),
                              ramp_early=mean(t,1500,1600)-mean(t,1300,1400),
                              late_vs_early=mean(t,1650,1800)-mean(t,300,600))
                features.append(values); curves.append(curve(t))
                late = raw[raw.moving.eq(1) & raw.position_mm.between(1200,1800)]
                trial_info.append({'trial':tid, 'valid_1200_1800_pct':float(((late.valid_mask.astype(int)&1)!=0).mean()*100),
                                   'missing_bins':[k for k,v in values.items() if not np.isfinite(v)]})
            entry['trial_quality'] = trial_info
            group_report['files'].append(entry)
            arrays.append(pd.DataFrame(features))
            all_curves[group,label] = np.vstack(curves)
        a,b = arrays
        group_report['features'] = {k:univariate(a[k],b[k]) for k in ['late_level','ramp','ramp_early','late_vs_early','terminal_level']}
        group_report['classifiers'] = {}
        for title,lo,hi in [('constant_speed_300_1800',300,1800),('late_1200_1800',1200,1800),('early_300_1200',300,1200),('late_1500_1800',1500,1800)]:
            columns = [f'bin_{x}' for x in range(lo,hi,100)]
            x0,x1 = a[columns].to_numpy(), b[columns].to_numpy()
            group_report['classifiers'][title] = {'leave_one_each_class':cv(x0,x1), 'half_batch_blocked':cv(x0,x1,True)}
            if np.isfinite(x0).all() and np.isfinite(x1).all():
                diff = x1.mean(axis=0)-x0.mean(axis=0)
                noise = np.sqrt((x0.var(axis=0,ddof=1)+x1.var(axis=0,ddof=1))/2)
                group_report['classifiers'][title].update(rms_class_gap_pa=float(np.sqrt(np.mean(diff**2))),
                    rms_within_class_sd_pa=float(np.sqrt(np.mean(noise**2))),
                    gap_to_repeat_sd=float(np.sqrt(np.mean(diff**2)/np.mean(noise**2))))
        output['groups'][group] = group_report
    OUT.mkdir(exist_ok=True)
    (OUT/'fan_distance_comparison.json').write_text(json.dumps(output,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(output,ensure_ascii=False,indent=2))
    plot(all_curves)


def plot(curves):
    # All individual trials, no across-trial median/mean replacement and no extra filter.
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="820" viewBox="0 0 1440 820">',
           '<rect width="1440" height="820" fill="white"/>',
           '<style>text{font-family:"Microsoft YaHei",sans-serif;fill:#263242;font-size:14px}</style>',
           '<text x="50" y="36" style="font-size:24px;font-weight:bold">风扇远 / 中 / 近：单通道有无障碍对比</text>',
           '<text x="50" y="64">每条细实线为一次实验；蓝色无障碍，红色有障碍；按距离对齐，不额外滤波</text>']
    titles={'far':'远 · 081019 / 081210','middle':'中 · 103639 / 103808','near':'近 · 113635 / 113758'}
    colors=['#2469b5','#d44343']
    for row,(low,high) in enumerate([(300,1880),(1200,1800)]):
        shown = np.concatenate([c[:,(GRID>=low)&(GRID<=high)].ravel() for c in curves.values()])
        ymin = np.floor(np.nanmin(shown)*2)/2
        ymax = np.ceil(np.nanmax(shown)*2)/2
        for col,group in enumerate(FILES):
            x0,y0,w,h=65+col*475,115+row*330,400,235
            xx=lambda p:x0+(p-low)/(high-low)*w
            yy=lambda p:y0+h-(p-ymin)/(ymax-ymin)*h
            clip=f'p{row}{col}'
            svg.append(f'<text x="{x0}" y="{y0-18}">{titles[group]} · {"300–1880 mm" if row==0 else "减速前放大"}</text>')
            svg.append(f'<defs><clipPath id="{clip}"><rect x="{x0}" y="{y0}" width="{w}" height="{h}"/></clipPath></defs>')
            if high > 1824.875:
                svg.append(f'<rect x="{xx(1824.875):.2f}" y="{y0}" width="{xx(high)-xx(1824.875):.2f}" height="{h}" fill="#fff0d8"/>')
            svg.append(f'<text x="{x0-43}" y="{y0-4}" style="font-size:12px">Pa</text>')
            for val in np.arange(ymin,ymax+.01,.5):
                svg.append(f'<path d="M{x0} {yy(val):.1f}h{w}" stroke="#e7eaee"/><text x="{x0-8}" y="{yy(val)+5:.1f}" text-anchor="end">{val:g}</text>')
            for val in ([300,600,900,1200,1500,1800] if row==0 else [1200,1400,1600,1800]):
                svg.append(f'<text x="{xx(val):.1f}" y="{y0+h+22}" text-anchor="middle">{val}</text>')
            for label in [0,1]:
                for z in curves[group,label]:
                    commands=[]; drawing=False
                    for distance,p in zip(GRID,z):
                        if distance<low or distance>high or not np.isfinite(p): drawing=False;continue
                        commands.append(f'{"L" if drawing else "M"}{xx(distance):.1f},{yy(p):.1f}');drawing=True
                    svg.append(f'<path d="{" ".join(commands)}" fill="none" stroke="{colors[label]}" stroke-width="0.8" opacity="0.65" clip-path="url(#{clip})"/>')
            svg.append(f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="none" stroke="#8894a1"/>')
            svg.append(f'<text x="{x0+w/2}" y="{y0+h+44}" text-anchor="middle">滑台位置 (mm)</text>')
    svg += ['<text x="50" y="776">橙色：旧固件推算的减速区（1824.875 mm起）；无效采样不当作0，超过50 mm的缺口断开</text>',
            '<text x="50" y="802">每组无障碍10次、有障碍10次；跨批次与跨日期因素未排除，不能据此认定真实部署准确率</text>', '</svg>']
    (OUT/'fan_distance_comparison.svg').write_text('\n'.join(svg),encoding='utf-8')


if __name__=='__main__': main()
