from __future__ import annotations

import argparse
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
COST_BPS = [30, 50, 70]
DEFAULT_BASE_NOTIONAL_CNY = 100_000.0
DEFAULT_LOT_SIZE = 100
SIDES = ["long_residual", "short_residual"]
BARS_PER_DAY = 16
TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Config:
    train_window: int = 200
    lookback: int = 400
    estimators: int = 5
    entry_low: float = 0.05
    exit_low: float = 0.75
    entry_high: float = 0.95
    exit_high: float = 0.25
    base_notional_cny: float = DEFAULT_BASE_NOTIONAL_CNY
    lot_size: int = DEFAULT_LOT_SIZE


def load_merged(merged_dir: Path) -> pd.DataFrame:
    cols = [
        "date",
        "name",
        "a_code",
        "h_code",
        "bar_end",
        "Buy1Px",
        "Buy1Qty",
        "Sell1Px",
        "Sell1Qty",
        "Buy1px",
        "buy1qty",
        "sell1px",
        "sell1qty",
        "a_mid",
        "h_mid_hkd",
        "USDCNH_SPOT_EST",
        "USDHKD",
        "a_bar_notional",
        "h_bar_notional",
    ]
    frames = [pd.read_parquet(p, columns=cols) for p in sorted(merged_dir.glob("ah_merged_fx_ratio_15m_*.parquet"))]
    if not frames:
        raise SystemExit(f"no merged parquet files found in {merged_dir}")
    df = pd.concat(frames, ignore_index=True)
    df["a_code"] = df["a_code"].astype(str).str.zfill(6)
    df["h_code"] = df["h_code"].astype(str).str.zfill(5)
    df["date"] = df["date"].astype(str)
    df["Time"] = pd.to_datetime(df["bar_end"])
    return df


def add_adjustments(df: pd.DataFrame, a_adj_path: Path, h_yahoo_path: Path) -> pd.DataFrame:
    a_adj = pd.read_csv(a_adj_path, dtype={"a_code": str, "trade_date": str})
    a_adj["a_code"] = a_adj["a_code"].astype(str).str.zfill(6)
    a_adj["trade_date"] = a_adj["trade_date"].astype(str)
    a_adj = a_adj.rename(columns={"trade_date": "date", "adj_factor": "a_adj_factor"})
    a_anchor = (
        a_adj.dropna(subset=["a_adj_factor"])
        .sort_values("date")
        .groupby("a_code", as_index=False)
        .tail(1)[["a_code", "a_adj_factor"]]
        .rename(columns={"a_adj_factor": "a_anchor_factor"})
    )
    a_adj = a_adj.merge(a_anchor, on="a_code", how="left")
    a_adj["a_norm_adj_factor"] = a_adj["a_adj_factor"] / a_adj["a_anchor_factor"]

    h_yahoo = pd.read_csv(h_yahoo_path, dtype={"h_code": str, "trade_date": str})
    h_yahoo["h_code"] = h_yahoo["h_code"].astype(str).str.zfill(5)
    h_yahoo["trade_date"] = h_yahoo["trade_date"].astype(str)
    h_yahoo = h_yahoo.rename(columns={"trade_date": "date", "adjclose": "h_yahoo_adjclose"})

    daily_h_close = (
        df.sort_values("Time")
        .groupby(["h_code", "date"], as_index=False)
        .tail(1)[["h_code", "date", "h_mid_hkd"]]
        .rename(columns={"h_mid_hkd": "h_raw_daily_close"})
    )
    h_factor = daily_h_close.merge(h_yahoo[["h_code", "date", "h_yahoo_adjclose"]], on=["h_code", "date"], how="left")
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

    out = df.merge(a_adj[["a_code", "date", "a_norm_adj_factor"]], on=["a_code", "date"], how="left")
    out = out.merge(h_factor[["h_code", "date", "h_norm_adj_factor"]], on=["h_code", "date"], how="left")
    fx = out["USDCNH_SPOT_EST"] / out["USDHKD"]
    out["a_bid_adj"] = out["Buy1Px"] * out["a_norm_adj_factor"]
    out["a_ask_adj"] = out["Sell1Px"] * out["a_norm_adj_factor"]
    out["a_mid_adj"] = out["a_mid"] * out["a_norm_adj_factor"]
    out["h_bid_adj_cny"] = out["Buy1px"] * out["h_norm_adj_factor"] * fx
    out["h_ask_adj_cny"] = out["sell1px"] * out["h_norm_adj_factor"] * fx
    out["h_mid_adj_cny"] = out["h_mid_hkd"] * out["h_norm_adj_factor"] * fx
    out["A"] = out["a_mid_adj"]
    out["H"] = out["h_mid_adj_cny"]
    out["NotionalFeature"] = out.groupby(["a_code", "date"])["a_bar_notional"].cumsum()
    out["notionalFeature"] = out.groupby(["h_code", "date"])["h_bar_notional"].cumsum()
    out["weekday"] = out["Time"].dt.weekday
    iso = out["Time"].dt.isocalendar()
    out["week"] = iso.week.astype(float)
    out["year"] = out["Time"].dt.year
    out["month"] = out["Time"].dt.month
    return out.replace([np.inf, -np.inf], np.nan)


def rolling_rf_residual(df: pd.DataFrame, cfg: Config) -> pd.Series:
    vals = np.full(len(df), np.nan)
    x = df[FEATURES]
    y = df["H"]
    for i in range(cfg.train_window, len(df)):
        model = RandomForestRegressor(
            n_estimators=cfg.estimators,
            random_state=42,
            min_samples_leaf=3,
            n_jobs=1,
        )
        model.fit(x.iloc[i - cfg.train_window : i], y.iloc[i - cfg.train_window : i])
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


def fill_prices(row: pd.Series, fill_mode: str, direction: str, action: str) -> tuple[float, float]:
    if fill_mode == "mid_fill":
        return float(row["a_mid_adj"]), float(row["h_mid_adj_cny"])
    if fill_mode != "cross_fill":
        raise ValueError(fill_mode)

    long_a_short_h = direction == "long_residual"
    if action == "entry":
        if long_a_short_h:
            return float(row["a_ask_adj"]), float(row["h_bid_adj_cny"])
        return float(row["a_bid_adj"]), float(row["h_ask_adj_cny"])
    if action == "exit":
        if long_a_short_h:
            return float(row["a_bid_adj"]), float(row["h_ask_adj_cny"])
        return float(row["a_ask_adj"]), float(row["h_bid_adj_cny"])
    raise ValueError(action)


def round_qty(notional: float, price: float, lot_size: int) -> float:
    if price <= 0 or not np.isfinite(price):
        return np.nan
    qty = round(notional / price / lot_size) * lot_size
    return float(max(qty, lot_size))


def pair_pnl_cny(direction: str, a_qty: float, h_qty: float, entry_a: float, entry_h: float, cur_a: float, cur_h: float) -> float:
    if direction == "long_residual":
        return a_qty * (cur_a - entry_a) - h_qty * (cur_h - entry_h)
    if direction == "short_residual":
        return -a_qty * (cur_a - entry_a) + h_qty * (cur_h - entry_h)
    raise ValueError(direction)


def max_drawdown(values) -> float:
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return 0.0
    return float(np.min(arr - np.maximum.accumulate(arr)))


def max_percentage_drawdown(values) -> float:
    arr = np.asarray(values, dtype=float)
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


def curve_performance(values) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return {
            "final_return": 0.0,
            "annualized_return": 0.0,
            "annualized_vol": 0.0,
            "sharpe": np.nan,
            "sortino": np.nan,
            "calmar": np.nan,
            "max_drawdown": 0.0,
        }
    diffs = np.diff(arr, prepend=0.0)
    years = len(arr) / BARS_PER_DAY / TRADING_DAYS_PER_YEAR
    ann_return = float(arr[-1] / years) if years > 0 else np.nan
    ann_vol = float(np.std(diffs, ddof=1) * np.sqrt(BARS_PER_DAY * TRADING_DAYS_PER_YEAR)) if len(diffs) > 1 else np.nan
    downside = diffs[diffs < 0]
    downside_vol = float(np.std(downside, ddof=1) * np.sqrt(BARS_PER_DAY * TRADING_DAYS_PER_YEAR)) if len(downside) > 1 else np.nan
    mdd = max_drawdown(arr)
    pct_mdd = max_percentage_drawdown(arr)
    return {
        "final_return": float(arr[-1]),
        "annualized_return": ann_return,
        "annualized_vol": ann_vol,
        "sharpe": float(ann_return / ann_vol) if ann_vol and np.isfinite(ann_vol) else np.nan,
        "sortino": float(ann_return / downside_vol) if downside_vol and np.isfinite(downside_vol) else np.nan,
        "calmar": float(ann_return / abs(mdd)) if mdd < 0 else np.nan,
        "calmar_pct_dd": float(ann_return / abs(pct_mdd)) if pct_mdd < 0 else np.nan,
        "return_over_abs_maxdd": float(arr[-1] / abs(mdd)) if mdd < 0 else np.nan,
        "return_over_abs_max_pct_dd": float(arr[-1] / abs(pct_mdd)) if pct_mdd < 0 else np.nan,
        "max_drawdown": mdd,
        "max_pct_drawdown": pct_mdd,
    }


def trade_stats(trades: list[dict], prefix: str = "") -> dict[str, float]:
    if not trades:
        return {
            f"{prefix}profit_factor_gross": np.nan,
            f"{prefix}profit_factor_net50": np.nan,
            f"{prefix}avg_win_gross_pnl_cny": np.nan,
            f"{prefix}avg_loss_gross_pnl_cny": np.nan,
            f"{prefix}largest_win_gross_pnl_cny": np.nan,
            f"{prefix}largest_loss_gross_pnl_cny": np.nan,
            f"{prefix}win_rate_gross": np.nan,
            f"{prefix}win_rate_net50": np.nan,
        }
    g = pd.DataFrame(trades)
    gross = g["gross_pnl_cny"]
    net50 = g["net_pnl_cny_50bp"]
    gross_wins = gross[gross > 0]
    gross_losses = gross[gross < 0]
    net50_wins = net50[net50 > 0]
    net50_losses = net50[net50 < 0]
    return {
        f"{prefix}profit_factor_gross": float(gross_wins.sum() / abs(gross_losses.sum())) if gross_losses.sum() < 0 else np.nan,
        f"{prefix}profit_factor_net50": float(net50_wins.sum() / abs(net50_losses.sum())) if net50_losses.sum() < 0 else np.nan,
        f"{prefix}avg_win_gross_pnl_cny": float(gross_wins.mean()) if len(gross_wins) else np.nan,
        f"{prefix}avg_loss_gross_pnl_cny": float(gross_losses.mean()) if len(gross_losses) else np.nan,
        f"{prefix}largest_win_gross_pnl_cny": float(gross.max()),
        f"{prefix}largest_loss_gross_pnl_cny": float(gross.min()),
        f"{prefix}win_rate_gross": float((gross > 0).mean()),
        f"{prefix}win_rate_net50": float((net50 > 0).mean()),
    }


def backtest_rf_real_pnl(symbol: str, df: pd.DataFrame, fill_mode: str, cfg: Config):
    df = df.sort_values("Time").dropna(
        subset=FEATURES
        + [
            "A",
            "H",
            "a_bid_adj",
            "a_ask_adj",
            "a_mid_adj",
            "h_bid_adj_cny",
            "h_ask_adj_cny",
            "h_mid_adj_cny",
        ]
    ).reset_index(drop=True)
    if len(df) < cfg.train_window + cfg.lookback + 50:
        return None
    metric = rolling_rf_residual(df, cfg)
    pct = rolling_percentile(metric, cfg.lookback).to_numpy(float)

    realized = {bp: 0.0 for bp in COST_BPS}
    curves = {bp: [] for bp in COST_BPS}
    gross_curve = []
    side_gross_realized = {side: 0.0 for side in SIDES}
    side_realized = {side: {bp: 0.0 for bp in COST_BPS} for side in SIDES}
    side_gross_curves = {side: [] for side in SIDES}
    side_curves = {side: {bp: [] for bp in COST_BPS} for side in SIDES}
    position_curve = []
    trades = []
    position = ""
    entry_i = None
    entry_a = entry_h = np.nan
    a_qty = h_qty = np.nan
    entry_a_notional = entry_h_notional = np.nan
    entry_metric = entry_pct = np.nan
    gross_realized = 0.0
    inventory_a_qty = round_qty(cfg.base_notional_cny, float(df.loc[0, "a_mid_adj"]), cfg.lot_size)
    inventory_h_qty = round_qty(cfg.base_notional_cny, float(df.loc[0, "h_mid_adj_cny"]), cfg.lot_size)
    inventory_entry_a = float(df.loc[0, "a_mid_adj"])
    inventory_entry_h = float(df.loc[0, "h_mid_adj_cny"])

    for i, row in df.iterrows():
        p = pct[i]
        exit_now = False
        exit_reason = ""
        if position and np.isfinite(p):
            if position == "long_residual" and p >= cfg.exit_low:
                exit_now = True
                exit_reason = "percentile_exit"
            elif position == "short_residual" and p <= cfg.exit_high:
                exit_now = True
                exit_reason = "percentile_exit"

        if exit_now:
            exit_a, exit_h = fill_prices(row, fill_mode, position, "exit")
            gross_pnl = pair_pnl_cny(position, a_qty, h_qty, entry_a, entry_h, exit_a, exit_h)
            gross_return = gross_pnl / cfg.base_notional_cny
            gross_realized += gross_return
            side_gross_realized[position] += gross_return
            for bp in COST_BPS:
                realized[bp] += gross_return - bp / 10000.0
                side_realized[position][bp] += gross_return - bp / 10000.0
            trades.append(
                {
                    "symbol": symbol,
                    "method": "rf5_w200_adj",
                    "fill_mode": fill_mode,
                    "direction": position,
                    "entry_time": str(df.loc[entry_i, "Time"]),
                    "exit_time": str(row["Time"]),
                    "entry_index": int(entry_i),
                    "exit_index": int(i),
                    "holding_bars": int(i - entry_i),
                    "holding_days": float((i - entry_i) / BARS_PER_DAY),
                    "entry_metric": float(entry_metric),
                    "exit_metric": float(metric.iloc[i]),
                    "entry_percentile": float(entry_pct),
                    "exit_percentile": float(p),
                    "entry_A_fill": float(entry_a),
                    "entry_H_fill": float(entry_h),
                    "exit_A_fill": float(exit_a),
                    "exit_H_fill": float(exit_h),
                    "a_qty": float(a_qty),
                    "h_qty": float(h_qty),
                    "entry_a_notional_cny": float(entry_a_notional),
                    "entry_h_notional_cny": float(entry_h_notional),
                    "entry_notional_ratio_a_over_h": float(entry_a_notional / entry_h_notional) if entry_h_notional else np.nan,
                    "gross_pnl_cny": float(gross_pnl),
                    "net_pnl_cny_30bp": float(gross_pnl - cfg.base_notional_cny * 30 / 10000.0),
                    "net_pnl_cny_50bp": float(gross_pnl - cfg.base_notional_cny * 50 / 10000.0),
                    "net_pnl_cny_70bp": float(gross_pnl - cfg.base_notional_cny * 70 / 10000.0),
                    "gross_return": float(gross_return),
                    "net_return_30bp": float(gross_return - 30 / 10000.0),
                    "net_return_50bp": float(gross_return - 50 / 10000.0),
                    "net_return_70bp": float(gross_return - 70 / 10000.0),
                    "exit_reason": exit_reason,
                }
            )
            position = ""
            entry_i = None

        if not position and np.isfinite(p):
            if p <= cfg.entry_low:
                position = "long_residual"
            elif p >= cfg.entry_high:
                position = "short_residual"
            if position:
                entry_i = i
                entry_a, entry_h = fill_prices(row, fill_mode, position, "entry")
                a_qty = round_qty(cfg.base_notional_cny, entry_a, cfg.lot_size)
                h_qty = round_qty(cfg.base_notional_cny, entry_h, cfg.lot_size)
                entry_a_notional = a_qty * entry_a
                entry_h_notional = h_qty * entry_h
                entry_metric = metric.iloc[i]
                entry_pct = p

        unrealized = 0.0
        pos_num = 0
        if position:
            unrealized = pair_pnl_cny(position, a_qty, h_qty, entry_a, entry_h, row["a_mid_adj"], row["h_mid_adj_cny"]) / cfg.base_notional_cny
            pos_num = 1 if position == "long_residual" else -1
        gross_curve.append(gross_realized + unrealized)
        position_curve.append(pos_num)
        for bp in COST_BPS:
            curves[bp].append(realized[bp] + unrealized)
        for side in SIDES:
            side_unrealized = unrealized if position == side else 0.0
            side_gross_curves[side].append(side_gross_realized[side] + side_unrealized)
            for bp in COST_BPS:
                side_curves[side][bp].append(side_realized[side][bp] + side_unrealized)

    if position and entry_i is not None and entry_i < len(df) - 1:
        row = df.iloc[-1]
        exit_a, exit_h = fill_prices(row, fill_mode, position, "exit")
        gross_pnl = pair_pnl_cny(position, a_qty, h_qty, entry_a, entry_h, exit_a, exit_h)
        gross_return = gross_pnl / cfg.base_notional_cny
        gross_realized += gross_return
        side_gross_realized[position] += gross_return
        for bp in COST_BPS:
            realized[bp] += gross_return - bp / 10000.0
            side_realized[position][bp] += gross_return - bp / 10000.0
        trades.append(
            {
                "symbol": symbol,
                "method": "rf5_w200_adj",
                "fill_mode": fill_mode,
                "direction": position,
                "entry_time": str(df.loc[entry_i, "Time"]),
                "exit_time": str(row["Time"]),
                "entry_index": int(entry_i),
                "exit_index": int(len(df) - 1),
                "holding_bars": int(len(df) - 1 - entry_i),
                "holding_days": float((len(df) - 1 - entry_i) / BARS_PER_DAY),
                "entry_metric": float(entry_metric),
                "exit_metric": float(metric.iloc[-1]),
                "entry_percentile": float(entry_pct),
                "exit_percentile": float(pct[-1]) if np.isfinite(pct[-1]) else np.nan,
                "entry_A_fill": float(entry_a),
                "entry_H_fill": float(entry_h),
                "exit_A_fill": float(exit_a),
                "exit_H_fill": float(exit_h),
                "a_qty": float(a_qty),
                "h_qty": float(h_qty),
                "entry_a_notional_cny": float(entry_a_notional),
                "entry_h_notional_cny": float(entry_h_notional),
                "entry_notional_ratio_a_over_h": float(entry_a_notional / entry_h_notional) if entry_h_notional else np.nan,
                "gross_pnl_cny": float(gross_pnl),
                "net_pnl_cny_30bp": float(gross_pnl - cfg.base_notional_cny * 30 / 10000.0),
                "net_pnl_cny_50bp": float(gross_pnl - cfg.base_notional_cny * 50 / 10000.0),
                "net_pnl_cny_70bp": float(gross_pnl - cfg.base_notional_cny * 70 / 10000.0),
                "gross_return": float(gross_return),
                "net_return_30bp": float(gross_return - 30 / 10000.0),
                "net_return_50bp": float(gross_return - 50 / 10000.0),
                "net_return_70bp": float(gross_return - 70 / 10000.0),
                "exit_reason": "end_of_sample",
            }
        )
        gross_curve[-1] = gross_realized
        for bp in COST_BPS:
            curves[bp][-1] = realized[bp]
        side_gross_curves[position][-1] = side_gross_realized[position]
        for bp in COST_BPS:
            side_curves[position][bp][-1] = side_realized[position][bp]
        position_curve[-1] = 0

    gross_arr = np.asarray(gross_curve)
    side_trade_counts = {side: sum(1 for t in trades if t["direction"] == side) for side in SIDES}
    total_hold_bars = sum(t["holding_bars"] for t in trades)
    net50_perf = curve_performance(curves[50])
    summary = {
        "symbol": symbol,
        "method": "rf5_w200_adj",
        "fill_mode": fill_mode,
        "rows": int(len(df)),
        "trades": int(len(trades)),
        "base_notional_cny": float(cfg.base_notional_cny),
        "lot_size": int(cfg.lot_size),
        "gross_pnl_cny": float(gross_realized * cfg.base_notional_cny),
        "net_pnl_cny_30bp": float(realized[30] * cfg.base_notional_cny),
        "net_pnl_cny_50bp": float(realized[50] * cfg.base_notional_cny),
        "net_pnl_cny_70bp": float(realized[70] * cfg.base_notional_cny),
        "gross_return": float(gross_realized),
        "net_return_30bp": float(realized[30]),
        "net_return_50bp": float(realized[50]),
        "net_return_70bp": float(realized[70]),
        "long_residual_trades": int(side_trade_counts["long_residual"]),
        "short_residual_trades": int(side_trade_counts["short_residual"]),
        "long_gross_pnl_cny": float(side_gross_realized["long_residual"] * cfg.base_notional_cny),
        "short_gross_pnl_cny": float(side_gross_realized["short_residual"] * cfg.base_notional_cny),
        "long_net_pnl_cny_50bp": float(side_realized["long_residual"][50] * cfg.base_notional_cny),
        "short_net_pnl_cny_50bp": float(side_realized["short_residual"][50] * cfg.base_notional_cny),
        "long_net_return_50bp": float(side_realized["long_residual"][50]),
        "short_net_return_50bp": float(side_realized["short_residual"][50]),
        "max_drawdown": max_drawdown(gross_arr),
        "net50_max_drawdown": net50_perf["max_drawdown"],
        "net50_max_pct_drawdown": net50_perf["max_pct_drawdown"],
        "annualized_return_net50": net50_perf["annualized_return"],
        "annualized_vol_net50": net50_perf["annualized_vol"],
        "sharpe_net50": net50_perf["sharpe"],
        "sortino_net50": net50_perf["sortino"],
        "calmar_net50": net50_perf["calmar"],
        "calmar_pct_dd_net50": net50_perf["calmar_pct_dd"],
        "return_over_abs_maxdd_net50": net50_perf["return_over_abs_maxdd"],
        "return_over_abs_max_pct_dd_net50": net50_perf["return_over_abs_max_pct_dd"],
        "long_max_drawdown": max_drawdown(side_gross_curves["long_residual"]),
        "short_max_drawdown": max_drawdown(side_gross_curves["short_residual"]),
        "avg_hold_bars": float(np.mean([t["holding_bars"] for t in trades])) if trades else 0.0,
        "avg_hold_days": float(np.mean([t["holding_days"] for t in trades])) if trades else 0.0,
        "median_hold_bars": float(np.median([t["holding_bars"] for t in trades])) if trades else 0.0,
        "median_hold_days": float(np.median([t["holding_days"] for t in trades])) if trades else 0.0,
        "max_hold_bars": int(max([t["holding_bars"] for t in trades])) if trades else 0,
        "max_hold_days": float(max([t["holding_days"] for t in trades])) if trades else 0.0,
        "exposure_bars": int(total_hold_bars),
        "exposure_days": float(total_hold_bars / BARS_PER_DAY),
        "exposure_time_pct": float(total_hold_bars / len(df)) if len(df) else 0.0,
    }
    summary.update(trade_stats(trades))
    pnl = pd.DataFrame(
        {
            "Time": df["Time"],
            "symbol": symbol,
            "method": "rf5_w200_adj",
            "fill_mode": fill_mode,
            "base_notional_cny": cfg.base_notional_cny,
            "position": position_curve,
            "gross_curve": gross_curve,
            "net_curve_30bp": curves[30],
            "net_curve_50bp": curves[50],
            "net_curve_70bp": curves[70],
            "metric": metric,
            "percentile": pct,
        }
    )
    pnl["gross_curve_cny"] = pnl["gross_curve"] * cfg.base_notional_cny
    pnl["net_curve_cny_30bp"] = pnl["net_curve_30bp"] * cfg.base_notional_cny
    pnl["net_curve_cny_50bp"] = pnl["net_curve_50bp"] * cfg.base_notional_cny
    pnl["net_curve_cny_70bp"] = pnl["net_curve_70bp"] * cfg.base_notional_cny
    for side in SIDES:
        prefix = "long_side" if side == "long_residual" else "short_side"
        pnl[f"{prefix}_gross_curve"] = side_gross_curves[side]
        pnl[f"{prefix}_gross_curve_cny"] = pnl[f"{prefix}_gross_curve"] * cfg.base_notional_cny
        for bp in COST_BPS:
            pnl[f"{prefix}_net_curve_{bp}bp"] = side_curves[side][bp]
            pnl[f"{prefix}_net_curve_cny_{bp}bp"] = pnl[f"{prefix}_net_curve_{bp}bp"] * cfg.base_notional_cny
    pnl["a_inventory_curve"] = inventory_a_qty * (df["a_mid_adj"] - inventory_entry_a) / cfg.base_notional_cny
    pnl["ah_spread_inventory_curve"] = [
        pair_pnl_cny("long_residual", inventory_a_qty, inventory_h_qty, inventory_entry_a, inventory_entry_h, a, h) / cfg.base_notional_cny
        for a, h in zip(df["a_mid_adj"], df["h_mid_adj_cny"])
    ]
    pnl["short_theoretical_net_curve_50bp"] = pnl["short_side_net_curve_50bp"]
    pnl["short_with_a_inventory_net_curve_50bp"] = pnl["a_inventory_curve"] + pnl["short_side_net_curve_50bp"]
    pnl["short_with_ah_inventory_net_curve_50bp"] = pnl["ah_spread_inventory_curve"] + pnl["short_side_net_curve_50bp"]
    for col in [
        "a_inventory_curve",
        "ah_spread_inventory_curve",
        "short_theoretical_net_curve_50bp",
        "short_with_a_inventory_net_curve_50bp",
        "short_with_ah_inventory_net_curve_50bp",
    ]:
        pnl[f"{col}_cny"] = pnl[col] * cfg.base_notional_cny
    return summary, trades, pnl


def run_pair(task):
    symbol, df, cfg = task
    out = []
    for fill_mode in ["mid_fill", "cross_fill"]:
        result = backtest_rf_real_pnl(symbol, df.copy(), fill_mode, cfg)
        if result is not None:
            out.append(result)
    return symbol, out


def plot_pnl(pnl: pd.DataFrame, out_dir: Path) -> None:
    chart_dir = out_dir / "pnl_charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    for (symbol, fill_mode), g in pnl.groupby(["symbol", "fill_mode"]):
        g = g.sort_values("Time")
        fig, ax = plt.subplots(figsize=(11.5, 4.2), dpi=130)
        ax.plot(g["Time"], g["gross_curve"], label="gross", linewidth=0.8)
        ax.plot(g["Time"], g["net_curve_30bp"], label="net 30bp", linewidth=0.75)
        ax.plot(g["Time"], g["net_curve_50bp"], label="net 50bp", linewidth=0.75)
        ax.plot(g["Time"], g["net_curve_70bp"], label="net 70bp", linewidth=0.75)
        ax.set_title(f"{symbol} {fill_mode} RF5 real pair PnL")
        ax.grid(True, linewidth=0.35, alpha=0.35)
        ax.legend(fontsize=8)
        fig.tight_layout()
        fig.savefig(chart_dir / f"pnl_{symbol.replace('/', '_')}_{fill_mode}.png")
        plt.close(fig)


def summarize_side_trades(trades_df: pd.DataFrame, summary_df: pd.DataFrame) -> pd.DataFrame:
    if trades_df.empty:
        return pd.DataFrame()
    rows_lookup = {
        (r["symbol"], r["fill_mode"]): int(r["rows"])
        for _, r in summary_df.iterrows()
    }
    rows = []
    for (symbol, fill_mode, direction), g in trades_df.groupby(["symbol", "fill_mode", "direction"], sort=True):
        sample_rows = rows_lookup.get((symbol, fill_mode), 0)
        hold_bars = float(g["holding_bars"].sum())
        gross = g["gross_pnl_cny"]
        net50 = g["net_pnl_cny_50bp"]
        gross_wins = gross[gross > 0]
        gross_losses = gross[gross < 0]
        net50_wins = net50[net50 > 0]
        net50_losses = net50[net50 < 0]
        row = {
            "symbol": symbol,
            "fill_mode": fill_mode,
            "direction": direction,
            "trades": int(len(g)),
            "gross_pnl_cny": float(g["gross_pnl_cny"].sum()),
            "net_pnl_cny_30bp": float(g["net_pnl_cny_30bp"].sum()),
            "net_pnl_cny_50bp": float(g["net_pnl_cny_50bp"].sum()),
            "net_pnl_cny_70bp": float(g["net_pnl_cny_70bp"].sum()),
            "gross_return": float(g["gross_return"].sum()),
            "net_return_30bp": float(g["net_return_30bp"].sum()),
            "net_return_50bp": float(g["net_return_50bp"].sum()),
            "net_return_70bp": float(g["net_return_70bp"].sum()),
            "win_rate_gross": float((g["gross_pnl_cny"] > 0).mean()),
            "win_rate_net50": float((g["net_pnl_cny_50bp"] > 0).mean()),
            "profit_factor_gross": float(gross_wins.sum() / abs(gross_losses.sum())) if gross_losses.sum() < 0 else np.nan,
            "profit_factor_net50": float(net50_wins.sum() / abs(net50_losses.sum())) if net50_losses.sum() < 0 else np.nan,
            "avg_win_gross_pnl_cny": float(gross_wins.mean()) if len(gross_wins) else np.nan,
            "avg_loss_gross_pnl_cny": float(gross_losses.mean()) if len(gross_losses) else np.nan,
            "largest_win_gross_pnl_cny": float(gross.max()),
            "largest_loss_gross_pnl_cny": float(gross.min()),
            "avg_trade_gross_pnl_cny": float(g["gross_pnl_cny"].mean()),
            "median_trade_gross_pnl_cny": float(g["gross_pnl_cny"].median()),
            "min_trade_gross_pnl_cny": float(g["gross_pnl_cny"].min()),
            "max_trade_gross_pnl_cny": float(g["gross_pnl_cny"].max()),
            "avg_hold_bars": float(g["holding_bars"].mean()),
            "avg_hold_days": float(g["holding_days"].mean()),
            "median_hold_bars": float(g["holding_bars"].median()),
            "median_hold_days": float(g["holding_days"].median()),
            "max_hold_bars": float(g["holding_bars"].max()),
            "max_hold_days": float(g["holding_days"].max()),
            "exposure_bars": hold_bars,
            "exposure_days": float(hold_bars / BARS_PER_DAY),
            "exposure_time_pct": float(hold_bars / sample_rows) if sample_rows else np.nan,
            "avg_entry_notional_ratio_a_over_h": float(g["entry_notional_ratio_a_over_h"].mean()),
            "min_entry_notional_ratio_a_over_h": float(g["entry_notional_ratio_a_over_h"].min()),
            "max_entry_notional_ratio_a_over_h": float(g["entry_notional_ratio_a_over_h"].max()),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_short_inventory_variants(pnl_df: pd.DataFrame) -> pd.DataFrame:
    if pnl_df.empty:
        return pd.DataFrame()
    variant_cols = [
        "short_theoretical_net_curve_50bp",
        "short_with_a_inventory_net_curve_50bp",
        "short_with_ah_inventory_net_curve_50bp",
    ]
    rows = []
    for (symbol, fill_mode), g in pnl_df.groupby(["symbol", "fill_mode"], sort=True):
        g = g.sort_values("Time")
        for col in variant_cols:
            vals = g[col].to_numpy(float)
            cny_vals = g[f"{col}_cny"].to_numpy(float)
            rows.append(
                {
                    "symbol": symbol,
                    "fill_mode": fill_mode,
                    "variant": col.replace("_net_curve_50bp", ""),
                    "final_return_50bp": float(vals[-1]),
                    "final_pnl_cny_50bp": float(cny_vals[-1]),
                    "max_drawdown": max_drawdown(vals),
                }
            )
    return pd.DataFrame(rows)


def summarize_curve_metrics(pnl_df: pd.DataFrame) -> pd.DataFrame:
    if pnl_df.empty:
        return pd.DataFrame()
    curve_cols = {
        "total_gross": "gross_curve",
        "total_net30": "net_curve_30bp",
        "total_net50": "net_curve_50bp",
        "total_net70": "net_curve_70bp",
        "long_side_net50": "long_side_net_curve_50bp",
        "short_side_net50": "short_side_net_curve_50bp",
        "short_theoretical_net50": "short_theoretical_net_curve_50bp",
        "short_with_a_inventory_net50": "short_with_a_inventory_net_curve_50bp",
        "short_with_ah_inventory_net50": "short_with_ah_inventory_net_curve_50bp",
    }
    rows = []
    for (symbol, fill_mode), g in pnl_df.groupby(["symbol", "fill_mode"], sort=True):
        g = g.sort_values("Time")
        for label, col in curve_cols.items():
            perf = curve_performance(g[col])
            rows.append(
                {
                    "symbol": symbol,
                    "fill_mode": fill_mode,
                    "curve": label,
                    "rows": int(len(g)),
                    "sample_days_16bar": float(len(g) / BARS_PER_DAY),
                    **perf,
                }
            )
    return pd.DataFrame(rows)


def summarize_monthly_pnl(pnl_df: pd.DataFrame) -> pd.DataFrame:
    if pnl_df.empty:
        return pd.DataFrame()
    curve_cols = {
        "total_net50": "net_curve_50bp",
        "long_side_net50": "long_side_net_curve_50bp",
        "short_side_net50": "short_side_net_curve_50bp",
        "short_theoretical_net50": "short_theoretical_net_curve_50bp",
        "short_with_a_inventory_net50": "short_with_a_inventory_net_curve_50bp",
        "short_with_ah_inventory_net50": "short_with_ah_inventory_net_curve_50bp",
    }
    rows = []
    for (symbol, fill_mode), g in pnl_df.groupby(["symbol", "fill_mode"], sort=True):
        g = g.sort_values("Time").copy()
        g["month"] = pd.to_datetime(g["Time"]).dt.to_period("M").astype(str)
        for label, col in curve_cols.items():
            inc = g[col].diff()
            inc.iloc[0] = g[col].iloc[0]
            tmp = pd.DataFrame({"month": g["month"], "return_increment": inc})
            for month, mg in tmp.groupby("month", sort=True):
                ret = float(mg["return_increment"].sum())
                rows.append(
                    {
                        "symbol": symbol,
                        "fill_mode": fill_mode,
                        "curve": label,
                        "month": month,
                        "monthly_return": ret,
                        "monthly_pnl_cny": ret * DEFAULT_BASE_NOTIONAL_CNY,
                    }
                )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--merged-dir", type=Path, required=True)
    parser.add_argument("--a-adj", type=Path, required=True)
    parser.add_argument("--h-yahoo", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--base-notional-cny", type=float, default=DEFAULT_BASE_NOTIONAL_CNY)
    parser.add_argument("--lot-size", type=int, default=DEFAULT_LOT_SIZE)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    df = add_adjustments(load_merged(args.merged_dir), args.a_adj, args.h_yahoo)
    df["symbol"] = df["a_code"] + "/" + df["h_code"]
    selected = [p.strip() for p in args.pairs.split(",") if p.strip()]
    df = df[df["symbol"].isin(selected)].copy()
    cfg = Config(base_notional_cny=args.base_notional_cny, lot_size=args.lot_size)

    summaries = []
    trades = []
    pnl_frames = []
    tasks = [(symbol, g.copy(), cfg) for symbol, g in df.groupby("symbol", sort=True)]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(run_pair, task) for task in tasks]):
            symbol, results = fut.result()
            print(f"DONE {symbol}")
            for summary, trade_rows, pnl in results:
                summaries.append(summary)
                trades.extend(trade_rows)
                pnl_frames.append(pnl)

    summary_df = pd.DataFrame(summaries)
    trades_df = pd.DataFrame(trades)
    pnl_df = pd.concat(pnl_frames, ignore_index=True) if pnl_frames else pd.DataFrame()
    summary_df.to_csv(args.out_dir / "real_fill_smoke_summary.csv", index=False)
    trades_df.to_csv(args.out_dir / "real_fill_smoke_trades.csv", index=False)
    pnl_df.to_parquet(args.out_dir / "real_fill_smoke_pnl.parquet", index=False)
    side_summary_df = summarize_side_trades(trades_df, summary_df)
    side_summary_df.to_csv(args.out_dir / "real_fill_smoke_side_summary.csv", index=False)
    variant_summary_df = summarize_short_inventory_variants(pnl_df)
    variant_summary_df.to_csv(args.out_dir / "real_fill_smoke_short_inventory_variants.csv", index=False)
    curve_metrics_df = summarize_curve_metrics(pnl_df)
    curve_metrics_df.to_csv(args.out_dir / "real_fill_smoke_curve_metrics.csv", index=False)
    monthly_pnl_df = summarize_monthly_pnl(pnl_df)
    monthly_pnl_df.to_csv(args.out_dir / "real_fill_smoke_monthly_pnl.csv", index=False)
    if not summary_df.empty:
        agg = summary_df.groupby(["method", "fill_mode"]).agg(
            symbols=("symbol", "count"),
            trades=("trades", "sum"),
            gross_pnl_cny=("gross_pnl_cny", "sum"),
            net_pnl_cny_30bp=("net_pnl_cny_30bp", "sum"),
            net_pnl_cny_50bp=("net_pnl_cny_50bp", "sum"),
            net_pnl_cny_70bp=("net_pnl_cny_70bp", "sum"),
            gross_return=("gross_return", "sum"),
            net_return_30bp=("net_return_30bp", "sum"),
            net_return_50bp=("net_return_50bp", "sum"),
            net_return_70bp=("net_return_70bp", "sum"),
            median_symbol_net_50bp=("net_return_50bp", "median"),
            avg_hold_bars=("avg_hold_bars", "mean"),
        ).reset_index()
        agg.to_csv(args.out_dir / "real_fill_smoke_aggregate.csv", index=False)
        print(agg.to_string(index=False))
    if not pnl_df.empty:
        plot_pnl(pnl_df, args.out_dir)
    print(f"out_dir={args.out_dir}")


if __name__ == "__main__":
    main()
