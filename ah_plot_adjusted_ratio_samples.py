from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd


def load_merged(merged_dir: Path) -> pd.DataFrame:
    cols = [
        "date",
        "name",
        "a_code",
        "h_code",
        "bar_end",
        "a_mid",
        "h_mid_hkd",
        "USDCNH_SPOT_EST",
        "USDHKD",
        "ah_ratio",
        "h_bar_notional",
        "a_bar_notional",
    ]
    frames = []
    for path in sorted(merged_dir.glob("ah_merged_fx_ratio_15m_*.parquet")):
        frames.append(pd.read_parquet(path, columns=cols))
    if not frames:
        raise SystemExit(f"no merged parquet files found in {merged_dir}")
    df = pd.concat(frames, ignore_index=True)
    df["a_code"] = df["a_code"].astype(str).str.zfill(6)
    df["h_code"] = df["h_code"].astype(str).str.zfill(5)
    df["date"] = df["date"].astype(str)
    df["bar_end"] = pd.to_datetime(df["bar_end"])
    return df


def load_a_adj(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"a_code": str, "trade_date": str, "ts_code": str})
    df["a_code"] = df["a_code"].astype(str).str.zfill(6)
    df["trade_date"] = df["trade_date"].astype(str)
    return df[["a_code", "trade_date", "adj_factor"]].rename(columns={"trade_date": "date", "adj_factor": "a_adj_factor"})


def load_h_yahoo(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"h_code": str, "trade_date": str})
    df["h_code"] = df["h_code"].astype(str).str.zfill(5)
    df["trade_date"] = df["trade_date"].astype(str)
    return df[["h_code", "trade_date", "adjclose"]].rename(columns={"trade_date": "date", "adjclose": "h_yahoo_adjclose"})


def add_adjusted_ratio(df: pd.DataFrame, a_adj: pd.DataFrame, h_yahoo: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    daily_h_close = (
        df.sort_values("bar_end")
        .groupby(["h_code", "date"], as_index=False)
        .tail(1)[["h_code", "date", "h_mid_hkd"]]
        .rename(columns={"h_mid_hkd": "h_raw_daily_close"})
    )
    h_factor = daily_h_close.merge(h_yahoo, on=["h_code", "date"], how="left")
    h_factor["h_implied_adj_factor"] = h_factor["h_yahoo_adjclose"] / h_factor["h_raw_daily_close"]
    h_factor = h_factor.dropna(subset=["h_implied_adj_factor"])
    h_anchor = (
        h_factor.sort_values("date")
        .groupby("h_code", as_index=False)
        .tail(1)[["h_code", "h_implied_adj_factor"]]
        .rename(columns={"h_implied_adj_factor": "h_anchor_factor"})
    )
    h_factor = h_factor.merge(h_anchor, on="h_code", how="left")
    h_factor["h_norm_adj_factor"] = h_factor["h_implied_adj_factor"] / h_factor["h_anchor_factor"]

    a_anchor = (
        a_adj.dropna(subset=["a_adj_factor"])
        .sort_values("date")
        .groupby("a_code", as_index=False)
        .tail(1)[["a_code", "a_adj_factor"]]
        .rename(columns={"a_adj_factor": "a_anchor_factor"})
    )
    a_factor = a_adj.merge(a_anchor, on="a_code", how="left")
    a_factor["a_norm_adj_factor"] = a_factor["a_adj_factor"] / a_factor["a_anchor_factor"]

    out = df.merge(a_factor[["a_code", "date", "a_norm_adj_factor"]], on=["a_code", "date"], how="left")
    out = out.merge(h_factor[["h_code", "date", "h_norm_adj_factor", "h_implied_adj_factor", "h_raw_daily_close", "h_yahoo_adjclose"]], on=["h_code", "date"], how="left")

    out["a_mid_adj"] = out["a_mid"] * out["a_norm_adj_factor"]
    out["h_mid_adj_hkd"] = out["h_mid_hkd"] * out["h_norm_adj_factor"]
    out["h_mid_adj_cny"] = out["h_mid_adj_hkd"] * out["USDCNH_SPOT_EST"] / out["USDHKD"]
    out["ah_ratio_adj"] = out["a_mid_adj"] / out["h_mid_adj_cny"]
    return out, h_factor


def plot_pair(df: pd.DataFrame, pair: str, out_dir: Path) -> None:
    a_code, h_code = pair.split("/")
    g = df[(df["a_code"] == a_code) & (df["h_code"] == h_code)].sort_values("bar_end").copy()
    if g.empty:
        print(f"missing pair {pair}")
        return
    g = g.dropna(subset=["ah_ratio", "ah_ratio_adj"])
    if g.empty:
        print(f"missing adjusted data for {pair}")
        return
    g["x"] = range(len(g))
    name = str(g["name"].iloc[0])
    fig, axes = plt.subplots(2, 1, figsize=(14, 7.2), dpi=140, sharex=True)
    axes[0].plot(g["x"], g["ah_ratio"], color="#777777", linewidth=0.65, label="raw AH ratio")
    axes[0].plot(g["x"], g["ah_ratio_adj"], color="#005f73", linewidth=0.75, label="adjusted AH ratio")
    axes[0].set_title(f"{pair} {name} raw vs adjusted AH ratio")
    axes[0].set_ylabel("A / H CNY")
    axes[0].grid(True, linewidth=0.35, alpha=0.35)
    axes[0].legend(loc="best", fontsize=8)

    axes[1].plot(g["x"], g["a_norm_adj_factor"], color="#bb3e03", linewidth=0.75, label="A normalized factor")
    axes[1].plot(g["x"], g["h_norm_adj_factor"], color="#0a9396", linewidth=0.75, label="H normalized factor")
    axes[1].set_ylabel("normalized factor")
    axes[1].grid(True, linewidth=0.35, alpha=0.35)
    axes[1].legend(loc="best", fontsize=8)

    day_first = g.groupby("date", sort=True).head(1)
    step = max(len(day_first) // 9, 1)
    axes[1].set_xticks(day_first["x"].to_numpy()[::step])
    axes[1].set_xticklabels(day_first["date"].to_numpy()[::step], rotation=30, ha="right")
    axes[1].set_xlabel("Trading bars, gap-compressed")
    fig.tight_layout()
    fig.savefig(out_dir / f"adjusted_ratio_{a_code}_{h_code}.png")
    plt.close(fig)

    cols = [
        "date",
        "bar_end",
        "a_mid",
        "h_mid_hkd",
        "a_norm_adj_factor",
        "h_norm_adj_factor",
        "ah_ratio",
        "ah_ratio_adj",
        "h_raw_daily_close",
        "h_yahoo_adjclose",
        "h_implied_adj_factor",
    ]
    g[cols].to_csv(out_dir / f"adjusted_ratio_{a_code}_{h_code}.csv", index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merged-dir", type=Path, required=True)
    parser.add_argument("--a-adj", type=Path, required=True)
    parser.add_argument("--h-yahoo", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--pairs", default="002594/01211,601318/02318,601899/02899,601628/02628,688981/00981")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    merged = load_merged(args.merged_dir)
    adjusted, h_factor = add_adjusted_ratio(merged, load_a_adj(args.a_adj), load_h_yahoo(args.h_yahoo))
    adjusted_summary = (
        adjusted.groupby(["a_code", "h_code", "name"], dropna=False)
        .agg(
            bars=("ah_ratio", "size"),
            adjusted_bars=("ah_ratio_adj", lambda s: s.notna().sum()),
            first_date=("date", "min"),
            last_date=("date", "max"),
            raw_min=("ah_ratio", "min"),
            raw_max=("ah_ratio", "max"),
            adj_min=("ah_ratio_adj", "min"),
            adj_max=("ah_ratio_adj", "max"),
        )
        .reset_index()
    )
    adjusted_summary.to_csv(args.out_dir / "adjusted_ratio_pair_summary.csv", index=False)
    h_factor.to_csv(args.out_dir / "h_implied_yahoo_factor_daily.csv", index=False)

    for pair in [p.strip() for p in args.pairs.split(",") if p.strip()]:
        plot_pair(adjusted, pair, args.out_dir)

    print(f"out_dir={args.out_dir}")
    print(adjusted_summary[adjusted_summary["h_code"].isin(["01211", "02318", "02899", "02628", "00981"])].to_string(index=False))


if __name__ == "__main__":
    main()

