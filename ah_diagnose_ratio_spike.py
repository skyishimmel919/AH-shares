from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def load_pair(merged_dir: Path, a_code: str, h_code: str) -> pd.DataFrame:
    rows = []
    for path in sorted(merged_dir.glob("ah_merged_fx_ratio_15m_*.parquet")):
        df = pd.read_parquet(path)
        m = (df["a_code"].astype(str).str.zfill(6) == a_code) & (df["h_code"].astype(str).str.zfill(5) == h_code)
        if m.any():
            rows.append(df.loc[m].copy())
    if not rows:
        raise SystemExit(f"pair not found: {a_code}/{h_code}")
    out = pd.concat(rows, ignore_index=True).sort_values("bar_end")
    out["bar_end"] = pd.to_datetime(out["bar_end"])
    out["x"] = range(len(out))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merged-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--a-code", default="002594")
    parser.add_argument("--h-code", default="01211")
    parser.add_argument("--top-n", type=int, default=120)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    df = load_pair(args.merged_dir, args.a_code, args.h_code)
    q = df["ah_ratio"].quantile([0.01, 0.05, 0.5, 0.95, 0.99]).rename("ah_ratio")
    top = df.nlargest(args.top_n, "ah_ratio").copy()
    top.to_csv(args.out_dir / f"top_ratio_rows_{args.a_code}_{args.h_code}.csv", index=False)
    daily = (
        df.groupby("date")
        .agg(
            bars=("ah_ratio", "size"),
            ratio_min=("ah_ratio", "min"),
            ratio_max=("ah_ratio", "max"),
            ratio_mean=("ah_ratio", "mean"),
            a_mid_min=("a_mid", "min"),
            a_mid_max=("a_mid", "max"),
            h_hkd_min=("h_mid_hkd", "min"),
            h_hkd_max=("h_mid_hkd", "max"),
            usdcnh_min=("USDCNH", "min"),
            usdcnh_max=("USDCNH", "max"),
            usd_hkd=("USDHKD", "first"),
            h_notional=("h_bar_notional", "sum"),
            a_notional=("a_bar_notional", "sum"),
        )
        .reset_index()
        .sort_values("ratio_max", ascending=False)
    )
    daily.to_csv(args.out_dir / f"daily_ratio_summary_{args.a_code}_{args.h_code}.csv", index=False)

    worst_date = str(daily.iloc[0]["date"])
    loc = int(df[df["date"].astype(str) == worst_date]["x"].median())
    lo = max(loc - 280, 0)
    hi = min(loc + 280, len(df) - 1)
    zoom = df[(df["x"] >= lo) & (df["x"] <= hi)].copy()
    zoom.to_csv(args.out_dir / f"zoom_rows_{args.a_code}_{args.h_code}_{worst_date}.csv", index=False)

    fig, axes = plt.subplots(5, 1, figsize=(14, 11), dpi=140, sharex=True)
    axes[0].plot(zoom["x"], zoom["ah_ratio"], linewidth=0.9)
    axes[0].set_ylabel("ratio")
    axes[1].plot(zoom["x"], zoom["a_mid"], label="A mid CNY", linewidth=0.9)
    axes[1].set_ylabel("A")
    axes[2].plot(zoom["x"], zoom["h_mid_hkd"], label="H mid HKD", linewidth=0.9)
    axes[2].plot(zoom["x"], zoom["h_mid_cny"], label="H mid CNY", linewidth=0.9)
    axes[2].legend(fontsize=8)
    axes[2].set_ylabel("H")
    axes[3].plot(zoom["x"], zoom["USDCNH"], label="USDCNH", linewidth=0.9)
    axes[3].plot(zoom["x"], zoom["USDHKD"], label="USDHKD", linewidth=0.9)
    axes[3].legend(fontsize=8)
    axes[3].set_ylabel("FX")
    axes[4].bar(zoom["x"], zoom["a_bar_notional"], label="A notional", alpha=0.55)
    axes[4].bar(zoom["x"], zoom["h_bar_notional"], label="H notional", alpha=0.55)
    axes[4].legend(fontsize=8)
    axes[4].set_ylabel("notional")
    day_starts = zoom.groupby("date").head(1)
    step = max(len(day_starts) // 8, 1)
    axes[-1].set_xticks(day_starts["x"].iloc[::step])
    axes[-1].set_xticklabels(day_starts["date"].astype(str).iloc[::step], rotation=30, ha="right")
    for ax in axes:
        ax.grid(True, linewidth=0.3, alpha=0.3)
    fig.suptitle(f"{args.a_code}/{args.h_code} zoom around highest ratio date {worst_date}")
    fig.tight_layout()
    fig.savefig(args.out_dir / f"zoom_{args.a_code}_{args.h_code}_{worst_date}.png")
    plt.close(fig)

    print("quantiles")
    print(q.to_string())
    print("top_daily")
    print(daily.head(12).to_string(index=False))
    print("worst_date", worst_date)
    print("out_dir", args.out_dir)


if __name__ == "__main__":
    main()
