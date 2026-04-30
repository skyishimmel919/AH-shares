from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
import pandas as pd


PRIMARY_FILL_MODE = "cross_fill"
PRIMARY_COST = "50bp"


def max_drawdown(values: pd.Series) -> float:
    arr = values.to_numpy(float)
    if len(arr) == 0:
        return 0.0
    return float(np.min(arr - np.maximum.accumulate(arr)))


def max_percentage_drawdown(values: pd.Series) -> float:
    arr = values.to_numpy(float)
    if len(arr) == 0:
        return 0.0
    equity = 1.0 + arr
    peak = np.maximum.accumulate(equity)
    valid = peak > 0
    if not np.any(valid):
        return np.nan
    dd = np.full(len(equity), np.nan)
    dd[valid] = equity[valid] / peak[valid] - 1.0
    return float(np.nanmin(dd))


def curve_perf(values: pd.Series) -> dict[str, float]:
    arr = values.to_numpy(float)
    if len(arr) == 0:
        return {
            "final_return": np.nan,
            "final_pnl_cny": np.nan,
            "sharpe": np.nan,
            "max_drawdown": np.nan,
        }
    diff = np.diff(arr, prepend=0.0)
    years = len(arr) / 16 / 252
    ann_ret = arr[-1] / years if years else np.nan
    ann_vol = np.std(diff, ddof=1) * np.sqrt(16 * 252) if len(diff) > 1 else np.nan
    mdd = max_drawdown(pd.Series(arr))
    pct_mdd = max_percentage_drawdown(pd.Series(arr))
    return {
        "final_return": float(arr[-1]),
        "final_pnl_cny": float(arr[-1] * 100000.0),
        "sharpe": float(ann_ret / ann_vol) if ann_vol and np.isfinite(ann_vol) else np.nan,
        "max_drawdown": mdd,
        "max_pct_drawdown": pct_mdd,
        "return_over_abs_maxdd": float(arr[-1] / abs(mdd)) if mdd < 0 else np.nan,
        "return_over_abs_max_pct_dd": float(arr[-1] / abs(pct_mdd)) if pct_mdd < 0 else np.nan,
        "calmar_pct_dd": float(ann_ret / abs(pct_mdd)) if pct_mdd < 0 else np.nan,
    }


def safe_float(value) -> float:
    if pd.isna(value):
        return np.nan
    return float(value)


def load_inputs(run_dir: Path) -> dict[str, pd.DataFrame]:
    return {
        "summary": pd.read_csv(run_dir / "real_fill_smoke_summary.csv"),
        "side": pd.read_csv(run_dir / "real_fill_smoke_side_summary.csv"),
        "curve": pd.read_csv(run_dir / "real_fill_smoke_curve_metrics.csv"),
        "monthly": pd.read_csv(run_dir / "real_fill_smoke_monthly_pnl.csv"),
        "variants": pd.read_csv(run_dir / "real_fill_smoke_short_inventory_variants.csv"),
        "trades": pd.read_csv(run_dir / "real_fill_smoke_trades.csv"),
        "pnl": pd.read_parquet(run_dir / "real_fill_smoke_pnl.parquet"),
    }


def build_ranking(d: dict[str, pd.DataFrame]) -> pd.DataFrame:
    summary = d["summary"]
    side = d["side"]
    curve = d["curve"]
    variants = d["variants"]

    symbols = sorted(summary["symbol"].unique())
    rows = []
    for symbol in symbols:
        row: dict[str, object] = {"symbol": symbol}
        s_cross = summary[(summary["symbol"] == symbol) & (summary["fill_mode"] == PRIMARY_FILL_MODE)]
        if not s_cross.empty:
            s = s_cross.iloc[0]
            row.update(
                {
                    "rows": int(s["rows"]),
                    "total_trades": int(s["trades"]),
                    "total_cross_net50_pnl_cny": safe_float(s["net_pnl_cny_50bp"]),
                    "total_cross_net50_return": safe_float(s["net_return_50bp"]),
                    "total_cross_sharpe_net50": safe_float(s.get("sharpe_net50", np.nan)),
                    "total_cross_max_drawdown": safe_float(s.get("net50_max_drawdown", s.get("max_drawdown", np.nan))),
                    "avg_entry_notional_ratio_a_over_h": np.nan,
                }
            )

        long_side = side[
            (side["symbol"] == symbol)
            & (side["fill_mode"] == PRIMARY_FILL_MODE)
            & (side["direction"] == "long_residual")
        ]
        if not long_side.empty:
            ls = long_side.iloc[0]
            row.update(
                {
                    "long_cross_trades": int(ls["trades"]),
                    "long_cross_net50_pnl_cny": safe_float(ls["net_pnl_cny_50bp"]),
                    "long_cross_net50_return": safe_float(ls["net_return_50bp"]),
                    "long_cross_win_rate_net50": safe_float(ls["win_rate_net50"]),
                    "long_cross_profit_factor_net50": safe_float(ls["profit_factor_net50"]),
                    "long_cross_avg_hold_days": safe_float(ls["avg_hold_days"]),
                    "long_cross_max_hold_days": safe_float(ls["max_hold_days"]),
                    "long_cross_exposure_time_pct": safe_float(ls["exposure_time_pct"]),
                    "long_cross_notional_ratio_mean": safe_float(ls["avg_entry_notional_ratio_a_over_h"]),
                    "long_cross_notional_ratio_min": safe_float(ls["min_entry_notional_ratio_a_over_h"]),
                    "long_cross_notional_ratio_max": safe_float(ls["max_entry_notional_ratio_a_over_h"]),
                }
            )

        long_curve = curve[
            (curve["symbol"] == symbol)
            & (curve["fill_mode"] == PRIMARY_FILL_MODE)
            & (curve["curve"] == "long_side_net50")
        ]
        if not long_curve.empty:
            lc = long_curve.iloc[0]
            row.update(
                {
                    "long_cross_annualized_return": safe_float(lc["annualized_return"]),
                    "long_cross_annualized_vol": safe_float(lc["annualized_vol"]),
                    "long_cross_sharpe": safe_float(lc["sharpe"]),
                    "long_cross_sortino": safe_float(lc["sortino"]),
                    "long_cross_calmar": safe_float(lc["calmar"]),
                    "long_cross_max_drawdown": safe_float(lc["max_drawdown"]),
                }
            )

        for variant, prefix in [
            ("short_with_a_inventory", "short_a_inv"),
            ("short_with_ah_inventory", "short_ah_inv"),
        ]:
            v = variants[
                (variants["symbol"] == symbol)
                & (variants["fill_mode"] == PRIMARY_FILL_MODE)
                & (variants["variant"] == variant)
            ]
            if not v.empty:
                vv = v.iloc[0]
                row.update(
                    {
                        f"{prefix}_cross_net50_pnl_cny": safe_float(vv["final_pnl_cny_50bp"]),
                        f"{prefix}_cross_net50_return": safe_float(vv["final_return_50bp"]),
                        f"{prefix}_cross_max_drawdown": safe_float(vv["max_drawdown"]),
                    }
                )
            vc = curve[
                (curve["symbol"] == symbol)
                & (curve["fill_mode"] == PRIMARY_FILL_MODE)
                & (curve["curve"] == f"{variant}_net50")
            ]
            if not vc.empty:
                cc = vc.iloc[0]
                row.update(
                    {
                        f"{prefix}_cross_sharpe": safe_float(cc["sharpe"]),
                        f"{prefix}_cross_sortino": safe_float(cc["sortino"]),
                        f"{prefix}_cross_calmar": safe_float(cc["calmar"]),
                        f"{prefix}_cross_max_pct_drawdown": safe_float(cc.get("max_pct_drawdown", np.nan)),
                        f"{prefix}_cross_return_over_abs_maxdd": safe_float(cc.get("return_over_abs_maxdd", np.nan)),
                        f"{prefix}_cross_return_over_abs_max_pct_dd": safe_float(cc.get("return_over_abs_max_pct_dd", np.nan)),
                        f"{prefix}_cross_calmar_pct_dd": safe_float(cc.get("calmar_pct_dd", np.nan)),
                        f"{prefix}_cross_annualized_return": safe_float(cc["annualized_return"]),
                        f"{prefix}_cross_annualized_vol": safe_float(cc["annualized_vol"]),
                    }
                )

        rows.append(row)

    return pd.DataFrame(rows)


def monthly_concentration(monthly: pd.DataFrame, symbol: str, curve: str) -> float:
    g = monthly[
        (monthly["symbol"] == symbol)
        & (monthly["fill_mode"] == PRIMARY_FILL_MODE)
        & (monthly["curve"] == curve)
    ]
    if g.empty:
        return np.nan
    vals = g["monthly_pnl_cny"].abs()
    denom = vals.sum()
    if denom <= 0:
        return np.nan
    return float(vals.max() / denom)


def build_comments(ranking: pd.DataFrame, monthly: pd.DataFrame) -> pd.DataFrame:
    comments = []

    def add(symbol: str, category: str, severity: str, comment: str) -> None:
        comments.append({"symbol": symbol, "category": category, "severity": severity, "comment": comment})

    for _, r in ranking.iterrows():
        symbol = r["symbol"]
        rows = r.get("rows", np.nan)
        long_trades = r.get("long_cross_trades", np.nan)
        long_mdd = r.get("long_cross_max_drawdown", np.nan)
        long_exp = r.get("long_cross_exposure_time_pct", np.nan)
        ratio_min = r.get("long_cross_notional_ratio_min", np.nan)
        ratio_max = r.get("long_cross_notional_ratio_max", np.nan)

        if pd.isna(rows) or rows < 2500:
            add(symbol, "rows too few", "high", f"rows={rows}; sample may be too short")
        if pd.isna(long_trades) or long_trades < 15:
            add(symbol, "trades too few", "high", f"long_cross_trades={long_trades}")
        if pd.notna(long_mdd) and long_mdd < -0.35:
            add(symbol, "max drawdown too large", "high", f"long_cross_max_drawdown={long_mdd:.3f}")
        elif pd.notna(long_mdd) and long_mdd < -0.20:
            add(symbol, "max drawdown large", "medium", f"long_cross_max_drawdown={long_mdd:.3f}")
        if pd.notna(long_exp) and long_exp < 0.05:
            add(symbol, "exposure too low", "medium", f"long_cross_exposure_time_pct={long_exp:.3f}")
        if pd.notna(long_exp) and long_exp > 0.75:
            add(symbol, "exposure too high", "medium", f"long_cross_exposure_time_pct={long_exp:.3f}")
        if pd.notna(ratio_min) and ratio_min < 0.85:
            add(symbol, "notional ratio rounding far from 1", "medium", f"min A/H notional ratio={ratio_min:.3f}")
        if pd.notna(ratio_max) and ratio_max > 1.15:
            add(symbol, "notional ratio rounding far from 1", "medium", f"max A/H notional ratio={ratio_max:.3f}")

        for curve, label in [
            ("long_side_net50", "long side"),
            ("short_with_a_inventory_net50", "short with A inventory"),
            ("short_with_ah_inventory_net50", "short with AH inventory"),
            ("short_blend_50_50_net50", "short blend 50/50"),
        ]:
            conc = monthly_concentration(monthly, symbol, curve)
            if pd.notna(conc) and conc > 0.45:
                add(symbol, "monthly pnl too concentrated", "medium", f"{label} max abs monthly pnl share={conc:.2%}")

    return pd.DataFrame(comments)


def add_curve_metrics_from_pnl(ranking: pd.DataFrame, pnl: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for symbol, g0 in pnl[pnl["fill_mode"] == PRIMARY_FILL_MODE].groupby("symbol", sort=True):
        g = g0.sort_values("Time")
        curves = {
            "long": g["long_side_net_curve_50bp"],
            "short_a_inv": g["short_with_a_inventory_net_curve_50bp"],
            "short_ah_inv": g["short_with_ah_inventory_net_curve_50bp"],
            "short_blend_50_50": 0.5 * g["short_with_a_inventory_net_curve_50bp"]
            + 0.5 * g["short_with_ah_inventory_net_curve_50bp"],
        }
        row = {"symbol": symbol}
        for prefix, series in curves.items():
            p = curve_perf(series)
            row.update(
                {
                    f"{prefix}_cross_net50_pnl_cny": p["final_pnl_cny"],
                    f"{prefix}_cross_net50_return": p["final_return"],
                    f"{prefix}_cross_sharpe": p["sharpe"],
                    f"{prefix}_cross_max_drawdown": p["max_drawdown"],
                    f"{prefix}_cross_max_pct_drawdown": p["max_pct_drawdown"],
                    f"{prefix}_cross_return_over_abs_maxdd": p["return_over_abs_maxdd"],
                    f"{prefix}_cross_return_over_abs_max_pct_dd": p["return_over_abs_max_pct_dd"],
                    f"{prefix}_cross_calmar_pct_dd": p["calmar_pct_dd"],
                }
            )
        rows.append(row)
    metrics = pd.DataFrame(rows)
    overlap = [c for c in metrics.columns if c in ranking.columns and c != "symbol"]
    if overlap:
        ranking = ranking.drop(columns=overlap)
    return ranking.merge(metrics, on="symbol", how="left")


def write_pair_folders(run_dir: Path, package_dir: Path, d: dict[str, pd.DataFrame]) -> None:
    pair_root = package_dir / "pairs"
    pair_root.mkdir(parents=True, exist_ok=True)
    chart_dir = run_dir / "pnl_charts"
    symbols = sorted(d["summary"]["symbol"].unique())

    for symbol in symbols:
        safe_symbol = symbol.replace("/", "_")
        out = pair_root / safe_symbol
        charts_out = out / "charts"
        charts_out.mkdir(parents=True, exist_ok=True)

        d["summary"][d["summary"]["symbol"] == symbol].to_csv(out / "summary.csv", index=False)
        d["side"][d["side"]["symbol"] == symbol].to_csv(out / "side_summary.csv", index=False)
        d["curve"][d["curve"]["symbol"] == symbol].to_csv(out / "curve_metrics.csv", index=False)
        d["monthly"][d["monthly"]["symbol"] == symbol].to_csv(out / "monthly_pnl.csv", index=False)
        d["variants"][d["variants"]["symbol"] == symbol].to_csv(out / "short_inventory_variants.csv", index=False)
        d["trades"][d["trades"]["symbol"] == symbol].to_csv(out / "trades.csv", index=False)
        d["pnl"][d["pnl"]["symbol"] == symbol].to_parquet(out / "pnl.parquet", index=False)

        for mode in ["cross_fill", "mid_fill"]:
            src = chart_dir / f"pnl_{safe_symbol}_{mode}.png"
            if src.exists():
                shutil.copy2(src, charts_out / src.name)


def write_portfolio_inputs(package_dir: Path, pnl: pd.DataFrame) -> None:
    out = package_dir / "portfolio_inputs"
    out.mkdir(parents=True, exist_ok=True)
    pnl = pnl.copy()
    pnl["short_blend_50_50_net_curve_50bp"] = (
        0.5 * pnl["short_with_a_inventory_net_curve_50bp"]
        + 0.5 * pnl["short_with_ah_inventory_net_curve_50bp"]
    )
    pnl["short_blend_50_50_net_curve_50bp_cny"] = (
        0.5 * pnl["short_with_a_inventory_net_curve_50bp_cny"]
        + 0.5 * pnl["short_with_ah_inventory_net_curve_50bp_cny"]
    )
    cols = [
        "Time",
        "symbol",
        "fill_mode",
        "long_side_net_curve_50bp",
        "short_with_a_inventory_net_curve_50bp",
        "short_with_ah_inventory_net_curve_50bp",
        "short_blend_50_50_net_curve_50bp",
        "long_side_net_curve_cny_50bp",
        "short_with_a_inventory_net_curve_50bp_cny",
        "short_with_ah_inventory_net_curve_50bp_cny",
        "short_blend_50_50_net_curve_50bp_cny",
    ]
    keep = [c for c in cols if c in pnl.columns]
    pnl.loc[pnl["fill_mode"] == PRIMARY_FILL_MODE, keep].to_parquet(out / "cross_fill_portfolio_curves.parquet", index=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    args = parser.parse_args()

    if args.package_dir.exists():
        shutil.rmtree(args.package_dir)
    args.package_dir.mkdir(parents=True)

    d = load_inputs(args.run_dir)
    ranking = build_ranking(d)
    ranking = add_curve_metrics_from_pnl(ranking, d["pnl"])
    comments = build_comments(ranking, d["monthly"])

    ranking.to_csv(args.package_dir / "universe_ranking.csv", index=False)
    comments.to_csv(args.package_dir / "universe_comments.csv", index=False)
    d["summary"].to_csv(args.package_dir / "universe_summary.csv", index=False)
    d["side"].to_csv(args.package_dir / "universe_side_summary.csv", index=False)
    d["curve"].to_csv(args.package_dir / "universe_curve_metrics.csv", index=False)
    d["monthly"].to_csv(args.package_dir / "universe_monthly_pnl.csv", index=False)
    d["variants"].to_csv(args.package_dir / "universe_short_inventory_variants.csv", index=False)

    rankings_dir = args.package_dir / "rankings"
    rankings_dir.mkdir()
    ranking.sort_values("long_cross_net50_pnl_cny", ascending=False).to_csv(
        rankings_dir / "long_side_cross_fill_net50.csv", index=False
    )
    ranking.sort_values("short_a_inv_cross_net50_pnl_cny", ascending=False).to_csv(
        rankings_dir / "short_with_a_inventory_cross_fill_net50.csv", index=False
    )
    ranking.sort_values("short_ah_inv_cross_net50_pnl_cny", ascending=False).to_csv(
        rankings_dir / "short_with_ah_inventory_cross_fill_net50.csv", index=False
    )
    ranking.sort_values("short_blend_50_50_cross_net50_pnl_cny", ascending=False).to_csv(
        rankings_dir / "short_blend_50_50_cross_fill_net50.csv", index=False
    )

    write_pair_folders(args.run_dir, args.package_dir, d)
    write_portfolio_inputs(args.package_dir, d["pnl"])

    print(f"package_dir={args.package_dir}")
    print(f"symbols={ranking['symbol'].nunique()}")
    print(f"comments={len(comments)}")
    print("top_long_cross")
    print(ranking.sort_values("long_cross_net50_pnl_cny", ascending=False).head(10)[
        ["symbol", "long_cross_net50_pnl_cny", "long_cross_sharpe", "long_cross_max_drawdown", "long_cross_trades"]
    ].to_string(index=False))


if __name__ == "__main__":
    main()
