from __future__ import annotations

import argparse
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor


FEATURES = ["A", "NotionalFeature", "notionalFeature", "weekday", "week", "year", "month"]


@dataclass
class BacktestConfig:
    train_window: int
    lookback: int
    estimators: int
    entry_low: float
    exit_low: float
    entry_high: float
    exit_high: float


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
        "a_bar_notional",
        "h_bar_notional",
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
    df = pd.read_csv(path, dtype={"a_code": str, "trade_date": str})
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
    out["A"] = out["a_mid"] * out["a_norm_adj_factor"]
    out["H"] = out["h_mid_hkd"] * out["h_norm_adj_factor"] * out["USDCNH_SPOT_EST"] / out["USDHKD"]
    out["ah_ratio_adj"] = out["A"] / out["H"]
    out["NotionalFeature"] = out.groupby(["a_code", "date"])["a_bar_notional"].cumsum()
    out["notionalFeature"] = out.groupby(["h_code", "date"])["h_bar_notional"].cumsum()
    out["Time"] = out["bar_end"]
    out["weekday"] = out["Time"].dt.weekday
    iso = out["Time"].dt.isocalendar()
    out["week"] = iso.week.astype(float)
    out["year"] = out["Time"].dt.year
    out["month"] = out["Time"].dt.month
    out = out.replace([np.inf, -np.inf], np.nan)
    return out, h_factor


def rolling_linear_residual(df: pd.DataFrame, train_window: int) -> pd.Series:
    vals = np.full(len(df), np.nan)
    a = df["A"].to_numpy(float)
    h = df["H"].to_numpy(float)
    for i in range(train_window, len(df)):
        x = a[i - train_window : i]
        y = h[i - train_window : i]
        denom = float(np.dot(x, x))
        if denom > 0:
            vals[i] = float(np.dot(x, y) / denom) * a[i] - h[i]
    return pd.Series(vals, index=df.index)


def rolling_rf_residual(df: pd.DataFrame, train_window: int, n_estimators: int) -> pd.Series:
    vals = np.full(len(df), np.nan)
    x = df[FEATURES]
    y = df["H"]
    for i in range(train_window, len(df)):
        model = RandomForestRegressor(
            n_estimators=n_estimators,
            random_state=42,
            n_jobs=1,
            min_samples_leaf=3,
        )
        model.fit(x.iloc[i - train_window : i], y.iloc[i - train_window : i])
        vals[i] = float(model.predict(x.iloc[[i]])[0]) - float(y.iloc[i])
    return pd.Series(vals, index=df.index)


def rolling_percentile(metric: pd.Series, lookback: int) -> pd.Series:
    arr = metric.to_numpy(float)
    out = np.full(len(arr), np.nan)
    for i in range(lookback, len(arr)):
        hist = arr[i - lookback : i]
        hist = hist[np.isfinite(hist)]
        if len(hist) < max(20, lookback // 4) or not np.isfinite(arr[i]):
            continue
        out[i] = (np.sum(hist < arr[i]) + 0.5 * np.sum(hist == arr[i])) / len(hist)
    return pd.Series(out, index=metric.index)


def backtest_metric(symbol: str, method: str, df: pd.DataFrame, metric: pd.Series, normalizer: pd.Series, cfg: BacktestConfig):
    pct = rolling_percentile(metric, cfg.lookback)
    m = metric.to_numpy(float)
    norm = normalizer.to_numpy(float)
    p = pct.to_numpy(float)
    pos = np.zeros(len(m))
    cur = 0.0
    entries = []
    hold_lengths = []
    open_i = None
    open_direction = ""
    exits = []
    for i in range(len(m)):
        if not np.isfinite(p[i]):
            pos[i] = cur
            continue
        if cur == 0:
            if p[i] <= cfg.entry_low:
                cur = 1.0
                entries.append(i)
                open_i = i
                open_direction = "long_residual"
            elif p[i] >= cfg.entry_high:
                cur = -1.0
                entries.append(i)
                open_i = i
                open_direction = "short_residual"
        elif cur > 0 and p[i] >= cfg.exit_low:
            hold_lengths.append(i - open_i)
            exits.append((open_i, i, open_direction, "percentile_exit"))
            cur = 0.0
            open_i = None
            open_direction = ""
        elif cur < 0 and p[i] <= cfg.exit_high:
            hold_lengths.append(i - open_i)
            exits.append((open_i, i, open_direction, "percentile_exit"))
            cur = 0.0
            open_i = None
            open_direction = ""
        pos[i] = cur
    prev_pos = np.roll(pos, 1)
    prev_pos[0] = 0
    dm = np.diff(m, prepend=np.nan)
    gross_bar = prev_pos * dm / norm
    gross_bar[~np.isfinite(gross_bar)] = 0
    turnover = np.abs(pos - prev_pos)

    def curve(cost_bp: float) -> np.ndarray:
        return np.cumsum(gross_bar - turnover * (cost_bp / 2 / 10000))

    gross_curve = np.cumsum(gross_bar)
    curve50 = curve(50)
    curve70 = curve(70)

    if open_i is not None and open_i < len(m) - 1:
        hold_lengths.append(len(m) - 1 - open_i)
        exits.append((open_i, len(m) - 1, open_direction, "end_of_sample"))

    peak = np.maximum.accumulate(gross_curve)
    max_dd = float(np.min(gross_curve - peak)) if len(gross_curve) else 0.0
    summary = {
        "symbol": symbol,
        "method": method,
        "rows": int(np.sum(np.isfinite(metric))),
        "trades": len(entries),
        "gross_return": float(gross_curve[-1]) if len(gross_curve) else 0.0,
        "net_return_50bp": float(curve50[-1]) if len(curve50) else 0.0,
        "net_return_70bp": float(curve70[-1]) if len(curve70) else 0.0,
        "max_drawdown": max_dd,
        "avg_hold_bars": float(np.mean(hold_lengths)) if hold_lengths else 0.0,
    }
    trades = []
    times = df["Time"].reset_index(drop=True)
    a_vals = df["A"].to_numpy(float)
    h_vals = df["H"].to_numpy(float)
    for entry_i, exit_i, direction, reason in exits:
        if exit_i <= entry_i:
            continue
        path = np.cumsum(gross_bar[entry_i + 1 : exit_i + 1])
        gross = float(path[-1]) if len(path) else 0.0
        trades.append(
            {
                "symbol": symbol,
                "method": method,
                "direction": direction,
                "entry_time": str(times.iloc[entry_i]),
                "exit_time": str(times.iloc[exit_i]),
                "entry_index": int(entry_i),
                "exit_index": int(exit_i),
                "holding_bars": int(exit_i - entry_i),
                "entry_metric": float(m[entry_i]),
                "exit_metric": float(m[exit_i]),
                "entry_percentile": float(p[entry_i]),
                "exit_percentile": float(p[exit_i]),
                "entry_A": float(a_vals[entry_i]),
                "exit_A": float(a_vals[exit_i]),
                "entry_H": float(h_vals[entry_i]),
                "exit_H": float(h_vals[exit_i]),
                "gross_return": gross,
                "net_return_50bp": gross - 50 / 10000,
                "net_return_70bp": gross - 70 / 10000,
                "max_adverse_return": float(np.min(path)) if len(path) else 0.0,
                "max_favorable_return": float(np.max(path)) if len(path) else 0.0,
                "exit_reason": reason,
            }
        )
    pnl = pd.DataFrame(
        {
            "Time": df["Time"].to_numpy(),
            "symbol": symbol,
            "method": method,
            "position": pos,
            "gross_bar_return": gross_bar,
            "gross_curve": gross_curve,
            "net_curve_50bp": curve50,
            "net_curve_70bp": curve70,
            "metric": m,
            "percentile": p,
        }
    )
    return summary, trades, pnl


def run_pair(item) -> tuple[list[dict], list[dict], pd.DataFrame, str]:
    symbol, df, cfg = item
    df = df.sort_values("Time").reset_index(drop=True)
    needed = ["A", "H"] + FEATURES
    df = df.dropna(subset=needed).reset_index(drop=True)
    if len(df) < cfg.train_window + cfg.lookback + 50:
        return [], [], pd.DataFrame(), f"SKIP {symbol}: only {len(df)} rows"
    metrics = {
        "raw_ratio_adj": df["A"] / df["H"],
        f"linear_w{cfg.train_window}_adj": rolling_linear_residual(df, cfg.train_window),
        f"rf{cfg.estimators}_w{cfg.train_window}_adj": rolling_rf_residual(df, cfg.train_window, cfg.estimators),
    }
    summaries = []
    trades = []
    pnls = []
    for method, metric in metrics.items():
        summary, method_trades, pnl = backtest_metric(symbol, method, df, metric, df["A"], cfg)
        summaries.append(summary)
        trades.extend(method_trades)
        pnls.append(pnl)
    return summaries, trades, pd.concat(pnls, ignore_index=True), f"DONE {symbol}: {len(df)} rows"


def plot_pnl(pnl: pd.DataFrame, out_dir: Path, method: str) -> None:
    method_dir = out_dir / "pnl_charts" / method
    method_dir.mkdir(parents=True, exist_ok=True)
    for symbol, g in pnl[pnl["method"] == method].groupby("symbol"):
        g = g.sort_values("Time").copy()
        if g.empty:
            continue
        fig, ax = plt.subplots(figsize=(11, 4.2), dpi=120)
        ax.plot(g["Time"], g["gross_curve"], linewidth=0.75, label="gross")
        ax.plot(g["Time"], g["net_curve_50bp"], linewidth=0.75, label="net 50bp")
        ax.plot(g["Time"], g["net_curve_70bp"], linewidth=0.75, label="net 70bp")
        ax.set_title(symbol)
        ax.grid(True, linewidth=0.35, alpha=0.35)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(method_dir / f"pnl_{symbol.replace('/', '_')}.png")
        plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merged-dir", type=Path, required=True)
    parser.add_argument("--a-adj", type=Path, required=True)
    parser.add_argument("--h-yahoo", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--train-window", type=int, default=200)
    parser.add_argument("--lookback", type=int, default=400)
    parser.add_argument("--estimators", type=int, default=5)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--pairs", default="")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cfg = BacktestConfig(args.train_window, args.lookback, args.estimators, 0.05, 0.75, 0.95, 0.25)
    merged = load_merged(args.merged_dir)
    adjusted, h_factor = add_adjusted_ratio(merged, load_a_adj(args.a_adj), load_h_yahoo(args.h_yahoo))
    adjusted.to_parquet(args.out_dir / "ah_merged_fx_ratio_adjusted_15m.parquet", index=False)
    h_factor.to_csv(args.out_dir / "h_implied_yahoo_factor_daily.csv", index=False)

    adjusted["symbol"] = adjusted["a_code"] + "/" + adjusted["h_code"]
    if args.pairs:
        selected = {p.strip() for p in args.pairs.split(",") if p.strip()}
        adjusted = adjusted[adjusted["symbol"].isin(selected)].copy()
    tasks = [(symbol, g.copy(), cfg) for symbol, g in adjusted.groupby("symbol", sort=True)]
    print(f"pairs_to_run={len(tasks)} workers={args.workers}")

    summaries = []
    trades = []
    pnl_frames = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_pair, task) for task in tasks]
        for fut in as_completed(futures):
            pair_summaries, pair_trades, pair_pnl, msg = fut.result()
            print(msg, flush=True)
            summaries.extend(pair_summaries)
            trades.extend(pair_trades)
            if not pair_pnl.empty:
                pnl_frames.append(pair_pnl)

    summary_df = pd.DataFrame(summaries)
    trades_df = pd.DataFrame(trades)
    pnl_df = pd.concat(pnl_frames, ignore_index=True) if pnl_frames else pd.DataFrame()
    summary_df.to_csv(args.out_dir / "ah_adjusted_baseline_summary.csv", index=False)
    trades_df.to_csv(args.out_dir / "ah_adjusted_baseline_trades.csv", index=False)
    pnl_df.to_parquet(args.out_dir / "ah_adjusted_baseline_pnl.parquet", index=False)

    if not summary_df.empty:
        agg = (
            summary_df.groupby("method")
            .agg(
                symbols=("symbol", "count"),
                trades=("trades", "sum"),
                gross_return=("gross_return", "sum"),
                net_return_50bp=("net_return_50bp", "sum"),
                net_return_70bp=("net_return_70bp", "sum"),
                median_symbol_gross=("gross_return", "median"),
                median_symbol_net_50bp=("net_return_50bp", "median"),
                avg_hold_bars=("avg_hold_bars", "mean"),
            )
            .sort_values("net_return_50bp", ascending=False)
        )
        agg.to_csv(args.out_dir / "ah_adjusted_baseline_method_aggregate.csv")
        print("SUMMARY")
        print(agg.to_string())
    if not pnl_df.empty:
        plot_pnl(pnl_df, args.out_dir, f"rf{args.estimators}_w{args.train_window}_adj")

    print(f"out_dir={args.out_dir}")


if __name__ == "__main__":
    main()

