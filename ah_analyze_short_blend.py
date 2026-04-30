from __future__ import annotations
import pandas as pd
import numpy as np
from pathlib import Path

BARS_PER_DAY=16
DAYS_PER_YEAR=252

def max_dd(arr):
    arr=np.asarray(arr,float)
    return float(np.min(arr-np.maximum.accumulate(arr))) if len(arr) else 0.0

def max_pct_dd(arr):
    arr=np.asarray(arr,float)
    if not len(arr): return 0.0
    equity=1.0+arr
    peak=np.maximum.accumulate(equity)
    valid=peak>0
    if not np.any(valid): return np.nan
    dd=np.full(len(arr), np.nan)
    dd[valid]=equity[valid]/peak[valid]-1.0
    return float(np.nanmin(dd))

def perf(arr):
    arr=np.asarray(arr,float)
    diff=np.diff(arr, prepend=0.0)
    years=len(arr)/BARS_PER_DAY/DAYS_PER_YEAR
    ann_ret=arr[-1]/years if years>0 else np.nan
    ann_vol=np.std(diff, ddof=1)*np.sqrt(BARS_PER_DAY*DAYS_PER_YEAR) if len(diff)>1 else np.nan
    dd=max_dd(arr)
    pct_dd=max_pct_dd(arr)
    return {
        'final_return_50bp': float(arr[-1]),
        'final_pnl_cny_50bp': float(arr[-1]*100000),
        'annualized_return': float(ann_ret),
        'annualized_vol': float(ann_vol),
        'sharpe': float(ann_ret/ann_vol) if ann_vol and np.isfinite(ann_vol) else np.nan,
        'max_drawdown': dd,
        'max_pct_drawdown': pct_dd,
        'return_over_abs_maxdd': float(arr[-1]/abs(dd)) if dd<0 else np.nan,
        'return_over_abs_max_pct_dd': float(arr[-1]/abs(pct_dd)) if pct_dd<0 else np.nan,
        'calmar': float(ann_ret/abs(dd)) if dd<0 else np.nan,
        'calmar_pct_dd': float(ann_ret/abs(pct_dd)) if pct_dd<0 else np.nan,
    }

pkg=Path.home()/'temp/ah_shares_run/output/real_fill_full_100k_lot100_package_20250401_20260421'
rows=[]
for p in sorted((pkg/'pairs').iterdir()):
    if not p.is_dir():
        continue
    symbol=p.name.replace('_','/')
    df=pd.read_parquet(p/'pnl.parquet')
    g=df[df.fill_mode=='cross_fill'].sort_values('Time')
    if g.empty: continue
    curves={
        'short_theoretical': g['short_theoretical_net_curve_50bp'].to_numpy(float),
        'short_with_a_inventory': g['short_with_a_inventory_net_curve_50bp'].to_numpy(float),
        'short_with_ah_inventory': g['short_with_ah_inventory_net_curve_50bp'].to_numpy(float),
    }
    curves['short_blend_50_50'] = 0.5*curves['short_with_a_inventory'] + 0.5*curves['short_with_ah_inventory']
    for name, arr in curves.items():
        r={'symbol':symbol,'variant':name,'fill_mode':'cross_fill'}
        r.update(perf(arr))
        rows.append(r)

out=pd.DataFrame(rows)
outdir=pkg/'experimental'
outdir.mkdir(exist_ok=True)
out.to_csv(outdir/'short_blend_50_50_comparison.csv', index=False)
wide=out.pivot(index='symbol', columns='variant', values=['final_pnl_cny_50bp','sharpe','max_drawdown']).reset_index()
# flatten
wide.columns=['_'.join([str(x) for x in c if x]) for c in wide.columns]
wide.to_csv(outdir/'short_blend_50_50_wide.csv', index=False)

print('rows', len(out), 'symbols', out.symbol.nunique())
for v in ['short_theoretical','short_with_a_inventory','short_with_ah_inventory','short_blend_50_50']:
    g=out[out.variant==v]
    print('\n',v)
    print(g[['final_pnl_cny_50bp','sharpe','max_drawdown','max_pct_drawdown','return_over_abs_max_pct_dd']].describe(percentiles=[.1,.25,.5,.75,.9]).to_string())
print('\nTOP_BLEND')
print(out[out.variant=='short_blend_50_50'].sort_values('final_pnl_cny_50bp', ascending=False).head(15).to_string(index=False))
# Compare blend between 2b and 2c final pnl exactly count
w=wide
between=((w['final_pnl_cny_50bp_short_blend_50_50']>=np.minimum(w['final_pnl_cny_50bp_short_with_a_inventory'], w['final_pnl_cny_50bp_short_with_ah_inventory'])) & (w['final_pnl_cny_50bp_short_blend_50_50']<=np.maximum(w['final_pnl_cny_50bp_short_with_a_inventory'], w['final_pnl_cny_50bp_short_with_ah_inventory']))).sum()
print('\nblend_between_2b_2c_final_pnl', int(between), '/', len(w))
# more stable drawdown than both? less negative than 2b and 2c
better_dd=((w['max_drawdown_short_blend_50_50']>w['max_drawdown_short_with_a_inventory']) & (w['max_drawdown_short_blend_50_50']>w['max_drawdown_short_with_ah_inventory'])).sum()
print('blend_drawdown_better_than_both', int(better_dd), '/', len(w))
