from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def load_merged(merged_dir: Path, columns: list[str]) -> pd.DataFrame:
    frames = []
    for path in sorted(merged_dir.glob("ah_merged_fx_ratio_15m_*.parquet")):
        frames.append(pd.read_parquet(path, columns=columns))
    if not frames:
        raise SystemExit(f"no merged parquet files found in {merged_dir}")
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merged-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--top-n", type=int, default=8)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cols = [
        "date",
        "name",
        "a_code",
        "h_code",
        "bar_end",
        "ah_ratio",
        "ah_premium",
        "a_mid",
        "h_mid_cny",
        "h_bar_notional",
        "a_bar_notional",
    ]
    df = load_merged(args.merged_dir, cols)
    df["pair"] = df["a_code"].astype(str).str.zfill(6) + "/" + df["h_code"].astype(str).str.zfill(5)
    df["bar_end"] = pd.to_datetime(df["bar_end"])

    summary = (
        df.groupby(["pair", "name"], dropna=False)
        .agg(
            bars=("ah_ratio", "size"),
            first_date=("date", "min"),
            last_date=("date", "max"),
            h_notional_sum=("h_bar_notional", "sum"),
            a_notional_sum=("a_bar_notional", "sum"),
            ratio_mean=("ah_ratio", "mean"),
            ratio_std=("ah_ratio", "std"),
            ratio_min=("ah_ratio", "min"),
            ratio_max=("ah_ratio", "max"),
        )
        .reset_index()
        .sort_values(["h_notional_sum", "bars"], ascending=False)
    )
    summary.to_csv(args.out_dir / "ratio_sample_pair_liquidity_summary.csv", index=False)
    selected = summary.head(args.top_n)["pair"].tolist()

    for pair in selected:
        g = df[df["pair"] == pair].sort_values("bar_end").copy()
        g["x"] = range(len(g))
        label_name = str(g["name"].iloc[0]) if "name" in g else ""
        fig, ax = plt.subplots(figsize=(14, 5.2), dpi=140)
        ax.plot(g["x"], g["ah_ratio"], color="#1f6f8b", linewidth=0.8, label="AH ratio")
        ax.plot(g["x"], g["ah_ratio"].rolling(80, min_periods=20).mean(), color="#d95f02", linewidth=1.2, label="rolling mean 80 bars")
        q05 = g["ah_ratio"].rolling(400, min_periods=100).quantile(0.05)
        q75 = g["ah_ratio"].rolling(400, min_periods=100).quantile(0.75)
        ax.plot(g["x"], q05, color="#7a9a01", linewidth=0.9, alpha=0.8, label="rolling 5% 400 bars")
        ax.plot(g["x"], q75, color="#984ea3", linewidth=0.9, alpha=0.8, label="rolling 75% 400 bars")
        tick_idx = g.groupby("date", sort=True).head(1).index
        tick_pos = g.loc[tick_idx, "x"].to_numpy()
        tick_labels = g.loc[tick_idx, "date"].astype(str).to_numpy()
        step = max(len(tick_pos) // 8, 1)
        ax.set_xticks(tick_pos[::step])
        ax.set_xticklabels(tick_labels[::step], rotation=30, ha="right")
        ax.set_title(f"{pair} {label_name} AH ratio 15m")
        ax.set_xlabel("Trading bars, gap-compressed")
        ax.set_ylabel("A price / H CNY price")
        ax.grid(True, linewidth=0.4, alpha=0.35)
        ax.legend(loc="best", fontsize=8)
        fig.tight_layout()
        fig.savefig(args.out_dir / f"ratio_{pair.replace('/', '_')}.png")
        plt.close(fig)

    fig, axes = plt.subplots(len(selected), 1, figsize=(14, max(2.0 * len(selected), 8)), dpi=140, sharex=False)
    if len(selected) == 1:
        axes = [axes]
    for ax, pair in zip(axes, selected):
        g = df[df["pair"] == pair].sort_values("bar_end").copy()
        g["x"] = range(len(g))
        ax.plot(g["x"], g["ah_ratio"], linewidth=0.55)
        ax.set_title(pair, loc="left", fontsize=9)
        ax.grid(True, linewidth=0.3, alpha=0.25)
    fig.suptitle("Top H-notional AH ratio samples, 15m, gap-compressed", fontsize=12)
    fig.tight_layout()
    fig.savefig(args.out_dir / "ratio_top_pairs_overview.png")
    plt.close(fig)

    print("selected_pairs")
    print(summary.head(args.top_n).to_string(index=False))
    print(f"out_dir={args.out_dir}")


if __name__ == "__main__":
    main()
