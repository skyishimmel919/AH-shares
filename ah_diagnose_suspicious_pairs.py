from __future__ import annotations
import argparse
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import sys

sys.path.insert(0, str(Path.home()/'temp/ah_shares_run/scripts'))
from ah_plot_adjusted_ratio_samples import load_merged, load_a_adj, load_h_yahoo, add_adjusted_ratio

PAIRS = ['601869/06869','688331/09995','002202/02208']

def plot_pnl(pkg: Path, pair: str, out: Path):
    safe = pair.replace('/','_')
    p = pkg/'pairs'/safe/'pnl.parquet'
    df = pd.read_parquet(p)
    g = df[df['fill_mode']=='cross_fill'].sort_values('Time').reset_index(drop=True)
    x = range(len(g))
    fig, axes = plt.subplots(3,1,figsize=(14,10),dpi=140,sharex=True)
    axes[0].plot(x, g['long_side_net_curve_cny_50bp'], label='long side net50', linewidth=.8)
    axes[0].plot(x, g['short_side_net_curve_cny_50bp'], label='short theoretical net50', linewidth=.8)
    axes[0].plot(x, g['net_curve_cny_50bp'], label='raw total net50', linewidth=.8, alpha=.65)
    axes[0].set_title(f'{pair} cross_fill spread PnL curves')
    axes[0].set_ylabel('CNY')
    axes[0].grid(True, linewidth=.35, alpha=.35)
    axes[0].legend(fontsize=8)

    axes[1].plot(x, g['a_inventory_curve_cny'], label='A inventory only', linewidth=.8)
    axes[1].plot(x, g['short_theoretical_net_curve_50bp_cny'], label='short theoretical', linewidth=.8)
    axes[1].plot(x, g['short_with_a_inventory_net_curve_50bp_cny'], label='2b short + A inventory', linewidth=.9)
    axes[1].set_title('2b decomposition')
    axes[1].set_ylabel('CNY')
    axes[1].grid(True, linewidth=.35, alpha=.35)
    axes[1].legend(fontsize=8)

    axes[2].plot(x, g['ah_spread_inventory_curve_cny'], label='AH spread inventory only', linewidth=.8)
    axes[2].plot(x, g['short_theoretical_net_curve_50bp_cny'], label='short theoretical', linewidth=.8)
    axes[2].plot(x, g['short_with_ah_inventory_net_curve_50bp_cny'], label='2c short + AH inventory', linewidth=.9)
    axes[2].set_title('2c decomposition')
    axes[2].set_ylabel('CNY')
    axes[2].grid(True, linewidth=.35, alpha=.35)
    axes[2].legend(fontsize=8)
    ticks = list(range(0,len(g),max(len(g)//10,1)))
    axes[2].set_xticks(ticks)
    axes[2].set_xticklabels([str(g.loc[i,'Time'])[:10] for i in ticks], rotation=30, ha='right')
    fig.tight_layout()
    fig.savefig(out/f'{safe}_pnl_diagnostic.png')
    plt.close(fig)


def plot_ratio(adjusted: pd.DataFrame, pair: str, out: Path):
    a,h = pair.split('/')
    g = adjusted[(adjusted.a_code==a)&(adjusted.h_code==h)].sort_values('bar_end').dropna(subset=['ah_ratio_adj']).reset_index(drop=True)
    g['x'] = range(len(g))
    fig, axes = plt.subplots(3,1,figsize=(14,10),dpi=140,sharex=True)
    axes[0].plot(g.x, g.ah_ratio_adj, linewidth=.7, label='adjusted A/H ratio')
    axes[0].set_title(f'{pair} adjusted A/H ratio')
    axes[0].set_ylabel('A / H CNY')
    axes[0].grid(True, linewidth=.35, alpha=.35)
    axes[0].legend(fontsize=8)
    axes[1].plot(g.x, g.a_mid_adj, linewidth=.65, label='A adjusted CNY')
    axes[1].plot(g.x, g.h_mid_adj_cny, linewidth=.65, label='H adjusted CNY')
    axes[1].set_title('Adjusted leg prices')
    axes[1].grid(True, linewidth=.35, alpha=.35)
    axes[1].legend(fontsize=8)
    axes[2].plot(g.x, g.a_norm_adj_factor, linewidth=.65, label='A adj factor')
    axes[2].plot(g.x, g.h_norm_adj_factor, linewidth=.65, label='H adj factor')
    axes[2].set_title('Adjustment factors')
    axes[2].grid(True, linewidth=.35, alpha=.35)
    axes[2].legend(fontsize=8)
    day_first = g.groupby('date', sort=True).head(1)
    step=max(len(day_first)//10,1)
    axes[2].set_xticks(day_first.x.to_numpy()[::step])
    axes[2].set_xticklabels(day_first.date.to_numpy()[::step], rotation=30, ha='right')
    fig.tight_layout()
    safe=pair.replace('/','_')
    fig.savefig(out/f'{safe}_ratio_diagnostic.png')
    plt.close(fig)
    g[['date','bar_end','a_mid_adj','h_mid_adj_cny','ah_ratio_adj','a_norm_adj_factor','h_norm_adj_factor']].to_csv(out/f'{safe}_ratio_diagnostic.csv', index=False)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--merged-dir', type=Path, required=True)
    ap.add_argument('--a-adj', type=Path, required=True)
    ap.add_argument('--h-yahoo', type=Path, required=True)
    ap.add_argument('--package-dir', type=Path, required=True)
    ap.add_argument('--out-dir', type=Path, required=True)
    args=ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    merged=load_merged(args.merged_dir)
    adjusted,_=add_adjusted_ratio(merged, load_a_adj(args.a_adj), load_h_yahoo(args.h_yahoo))
    for pair in PAIRS:
        plot_ratio(adjusted, pair, args.out_dir)
        plot_pnl(args.package_dir, pair, args.out_dir)
    print(args.out_dir)

if __name__ == '__main__':
    main()
