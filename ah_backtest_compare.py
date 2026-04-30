from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor


# Current production-safe baseline:
# RF5, cumulative notional feature, train_window=200, percentile lookback=400,
# and only the A/H overlapping regular session: 09:30-11:30 and 13:00-15:00.
FEATURES = ["A", "NotionalFeature", "notionalFeature", "weekday", "week", "year", "month"]


@dataclass
class MethodResult:
    symbol: str
    method: str
    rows: int
    trades: int
    gross_return: float
    net_return_50bp: float
    net_return_70bp: float
    max_drawdown: float
    avg_hold_bars: float


@dataclass
class TradeResult:
    symbol: str
    method: str
    direction: str
    entry_time: str
    exit_time: str
    entry_index: int
    exit_index: int
    holding_bars: int
    entry_metric: float
    exit_metric: float
    entry_percentile: float
    exit_percentile: float
    entry_A: float
    exit_A: float
    entry_H: float
    exit_H: float
    gross_return: float
    net_return_50bp: float
    net_return_70bp: float
    max_adverse_return: float
    max_favorable_return: float
    exit_reason: str


def quote_weighted_mid(bid, bid_qty, ask, ask_qty):
    denom = bid_qty + ask_qty
    return np.where(denom > 0, (bid * ask_qty + ask * bid_qty) / denom, np.nan)


def prepare_sheet(df: pd.DataFrame, notional_mode: str) -> pd.DataFrame:
    df = df.copy()
    required = [
        "Time",
        "Buy1Px",
        "Buy1Qty",
        "Sell1Px",
        "Sell1Qty",
        "Buy1px",
        "buy1qty",
        "sell1px",
        "sell1qty",
        "Notional",
        "notional",
        "USDCNH",
        "USDHKD",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")

    df["Time"] = pd.to_datetime(df["Time"], errors="coerce")
    df = df.dropna(subset=["Time"]).sort_values("Time").reset_index(drop=True)
    df["var"] = df["USDCNH"] / df["USDHKD"]
    df["A"] = quote_weighted_mid(df["Buy1Px"], df["Buy1Qty"], df["Sell1Px"], df["Sell1Qty"])
    df["H"] = quote_weighted_mid(df["Buy1px"], df["buy1qty"], df["sell1px"], df["sell1qty"]) * df["var"]
    df["weekday"] = df["Time"].dt.weekday
    iso = df["Time"].dt.isocalendar()
    df["week"] = iso.week.astype(float)
    df["year"] = df["Time"].dt.year
    df["month"] = df["Time"].dt.month
    df["NotionalFeature"] = make_notional_feature(df, "Notional", notional_mode)
    df["notionalFeature"] = make_notional_feature(df, "notional", notional_mode)
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["A", "H"] + FEATURES)
    return df.reset_index(drop=True)


def make_notional_feature(df: pd.DataFrame, col: str, mode: str) -> pd.Series:
    mode = mode.lower()
    values = df[col].astype(float)
    if mode == "raw":
        return values
    if mode in {"cum", "cum-auto"}:
        dates = df["Time"].dt.date
        if mode == "cum":
            return values.groupby(dates).cumsum()
        cutoff = pd.Timestamp("2022-12-14").date()
        use_cumsum = df["Time"].dt.date < cutoff
        out = values.copy()
        out.loc[use_cumsum] = values.loc[use_cumsum].groupby(dates.loc[use_cumsum]).cumsum()
        return out

    if mode not in {"bar", "bar-auto"}:
        raise ValueError(f"unknown notional mode: {mode}")

    if mode == "bar":
        use_diff = pd.Series(True, index=df.index)
    else:
        cutoff = pd.Timestamp("2022-12-14").date()
        use_diff = df["Time"].dt.date >= cutoff

    out = values.copy()
    if use_diff.any():
        dates = df["Time"].dt.date
        diffed = values.groupby(dates).diff()
        # First regular row of a day should keep its cumulative value. Negative
        # diffs are treated as intraday resets/odd timestamp rows, so keep the
        # current value instead of creating an impossible negative bar notional.
        diffed = diffed.where(diffed.notna(), values)
        diffed = diffed.where(diffed >= 0, values)
        out.loc[use_diff] = diffed.loc[use_diff]
    return out


def rolling_raw_metric(df: pd.DataFrame) -> pd.Series:
    return df["A"] / df["H"]


def rolling_linear_residual(df: pd.DataFrame, train_window: int) -> pd.Series:
    vals = np.full(len(df), np.nan)
    a = df["A"].to_numpy(float)
    h = df["H"].to_numpy(float)
    for i in range(train_window, len(df)):
        x = a[i - train_window : i]
        y = h[i - train_window : i]
        denom = float(np.dot(x, x))
        if denom <= 0:
            continue
        beta = float(np.dot(x, y) / denom)
        vals[i] = beta * a[i] - h[i]
    return pd.Series(vals, index=df.index)


def rolling_rf_residual(df: pd.DataFrame, train_window: int, n_estimators: int) -> pd.Series:
    vals = np.full(len(df), np.nan)
    x = df[FEATURES]
    y = df["H"]
    for i in range(train_window, len(df)):
        model = RandomForestRegressor(
            n_estimators=n_estimators,
            random_state=42,
            n_jobs=-1,
            min_samples_leaf=3,
        )
        model.fit(x.iloc[i - train_window : i], y.iloc[i - train_window : i])
        pred = float(model.predict(x.iloc[[i]])[0])
        vals[i] = pred - float(y.iloc[i])
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


def backtest_metric(
    symbol: str,
    method: str,
    df: pd.DataFrame,
    metric: pd.Series,
    normalizer: pd.Series,
    entry_low: float = 0.05,
    exit_low: float = 0.75,
    entry_high: float = 0.95,
    exit_high: float = 0.25,
    lookback: int = 400,
) -> tuple[MethodResult, list[TradeResult]]:
    pct = rolling_percentile(metric, lookback)
    m = metric.to_numpy(float)
    norm = normalizer.to_numpy(float)
    p = pct.to_numpy(float)
    pos = np.zeros(len(m))
    cur = 0.0
    entries = []
    hold_lengths = []
    open_i = None
    open_direction = ""
    exit_events: list[tuple[int, int, str, str]] = []
    for i in range(len(m)):
        if not np.isfinite(p[i]):
            pos[i] = cur
            continue
        if cur == 0:
            if p[i] <= entry_low:
                cur = 1.0
                entries.append(i)
                open_i = i
                open_direction = "long_residual"
            elif p[i] >= entry_high:
                cur = -1.0
                entries.append(i)
                open_i = i
                open_direction = "short_residual"
        elif cur > 0 and p[i] >= exit_low:
            if open_i is not None:
                hold_lengths.append(i - open_i)
                exit_events.append((open_i, i, open_direction, "percentile_exit"))
            cur = 0.0
            open_i = None
            open_direction = ""
        elif cur < 0 and p[i] <= exit_high:
            if open_i is not None:
                hold_lengths.append(i - open_i)
                exit_events.append((open_i, i, open_direction, "percentile_exit"))
            cur = 0.0
            open_i = None
            open_direction = ""
        pos[i] = cur

    prev_pos = np.roll(pos, 1)
    prev_pos[0] = 0
    dm = np.diff(m, prepend=np.nan)
    # Position +1 means long residual, -1 means short residual. Normalize to an
    # approximate return unit so raw ratio, linear residual, and RF residual are comparable.
    gross_bar = prev_pos * dm / norm
    gross_bar[~np.isfinite(gross_bar)] = 0
    turnover = np.abs(pos - prev_pos)
    gross_curve = np.cumsum(gross_bar)

    def net_curve(round_trip_bp: float) -> np.ndarray:
        half_turn_cost = round_trip_bp / 2 / 10000
        return np.cumsum(gross_bar - turnover * half_turn_cost)

    def max_dd(curve: np.ndarray) -> float:
        peak = np.maximum.accumulate(curve)
        return float(np.min(curve - peak)) if len(curve) else 0.0

    curve50 = net_curve(50)
    curve70 = net_curve(70)
    if open_i is not None and open_i < len(m) - 1:
        hold_lengths.append(len(m) - 1 - open_i)
        exit_events.append((open_i, len(m) - 1, open_direction, "end_of_sample"))

    trades: list[TradeResult] = []
    times = df["Time"].reset_index(drop=True)
    a_vals = df["A"].to_numpy(float)
    h_vals = df["H"].to_numpy(float)
    for entry_i, exit_i, direction, reason in exit_events:
        if exit_i <= entry_i:
            continue
        trade_gross_path = np.cumsum(gross_bar[entry_i + 1 : exit_i + 1])
        gross = float(trade_gross_path[-1]) if len(trade_gross_path) else 0.0
        trades.append(
            TradeResult(
                symbol=symbol,
                method=method,
                direction=direction,
                entry_time=str(times.iloc[entry_i]),
                exit_time=str(times.iloc[exit_i]),
                entry_index=int(entry_i),
                exit_index=int(exit_i),
                holding_bars=int(exit_i - entry_i),
                entry_metric=float(m[entry_i]),
                exit_metric=float(m[exit_i]),
                entry_percentile=float(p[entry_i]),
                exit_percentile=float(p[exit_i]),
                entry_A=float(a_vals[entry_i]),
                exit_A=float(a_vals[exit_i]),
                entry_H=float(h_vals[entry_i]),
                exit_H=float(h_vals[exit_i]),
                gross_return=gross,
                net_return_50bp=gross - 50 / 10000,
                net_return_70bp=gross - 70 / 10000,
                max_adverse_return=float(np.min(trade_gross_path)) if len(trade_gross_path) else 0.0,
                max_favorable_return=float(np.max(trade_gross_path)) if len(trade_gross_path) else 0.0,
                exit_reason=reason,
            )
        )

    summary = MethodResult(
        symbol=symbol,
        method=method,
        rows=int(np.sum(np.isfinite(metric))),
        trades=len(entries),
        gross_return=float(gross_curve[-1]) if len(gross_curve) else 0.0,
        net_return_50bp=float(curve50[-1]) if len(curve50) else 0.0,
        net_return_70bp=float(curve70[-1]) if len(curve70) else 0.0,
        max_drawdown=max_dd(gross_curve),
        avg_hold_bars=float(np.mean(hold_lengths)) if hold_lengths else 0.0,
    )
    return summary, trades


def run(
    input_file: Path,
    train_window: int,
    lookback: int,
    max_sheets: int | None,
    estimators: list[int],
    only_sheets: list[str] | None = None,
    notional_mode: str = "raw",
    start_date: str | None = None,
    end_date: str | None = None,
    regular_hours_only: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    xl = pd.ExcelFile(input_file)
    results: list[MethodResult] = []
    trades: list[TradeResult] = []
    sheet_names = only_sheets if only_sheets else xl.sheet_names
    sheet_names = sheet_names[:max_sheets] if max_sheets else sheet_names
    for sheet in sheet_names:
        raw = pd.read_excel(input_file, sheet_name=sheet)
        try:
            df = prepare_sheet(raw, notional_mode)
        except Exception as exc:
            print(f"SKIP {sheet}: {exc}")
            continue
        if start_date:
            df = df[df["Time"] >= pd.Timestamp(start_date)].reset_index(drop=True)
        if end_date:
            df = df[df["Time"] < pd.Timestamp(end_date)].reset_index(drop=True)
        if regular_hours_only:
            hhmm = df["Time"].dt.strftime("%H:%M")
            morning = (hhmm >= "09:30") & (hhmm <= "11:30")
            afternoon = (hhmm >= "13:00") & (hhmm <= "15:00")
            df = df[morning | afternoon].reset_index(drop=True)
        if len(df) < train_window + lookback + 50:
            print(f"SKIP {sheet}: only {len(df)} usable rows")
            continue
        metrics = {
            "raw_ratio": rolling_raw_metric(df),
            f"linear_w{train_window}": rolling_linear_residual(df, train_window),
        }
        for n in estimators:
            metrics[f"rf{n}_w{train_window}"] = rolling_rf_residual(df, train_window, n)
        for method, metric in metrics.items():
            summary, method_trades = backtest_metric(
                symbol=sheet,
                method=method,
                df=df,
                metric=metric,
                normalizer=df["A"],
                lookback=lookback,
            )
            results.append(summary)
            trades.extend(method_trades)
        print(f"DONE {sheet}: {len(df)} rows")
    return pd.DataFrame([r.__dict__ for r in results]), pd.DataFrame([t.__dict__ for t in trades])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path(r"AH file\new15minbar1500-20230217.xlsx"))
    parser.add_argument("--train-window", type=int, default=200)
    parser.add_argument("--lookback", type=int, default=400)
    parser.add_argument("--max-sheets", type=int, default=None)
    parser.add_argument(
        "--estimators",
        type=str,
        default="5",
        help="Comma-separated RF n_estimators values. Use empty string to skip RF.",
    )
    parser.add_argument(
        "--sheets",
        type=str,
        default="",
        help="Comma-separated sheet names to run. Empty means all sheets.",
    )
    parser.add_argument(
        "--notional-mode",
        choices=["raw", "bar", "bar-auto", "cum", "cum-auto"],
        default="cum-auto",
        help="RF notional feature mode. raw preserves old script behavior; bar-auto converts dates from 2022-12-14 onward to bar values; cum-auto converts earlier bar values to cumulative values.",
    )
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument(
        "--include-off-hours",
        action="store_true",
        help="Include rows outside the A/H overlapping regular session. Default keeps 09:30-11:30 and 13:00-15:00 only.",
    )
    parser.add_argument("--out", type=Path, default=Path(r"AH file\ah_baseline_rf5_cum_overlap_summary.csv"))
    parser.add_argument("--trades-out", type=Path, default=None)
    args = parser.parse_args()
    estimators = [] if args.estimators.lower() in {"none", "skip", "no"} else [int(x) for x in args.estimators.split(",") if x.strip()]
    only_sheets = [x.strip() for x in args.sheets.split(",") if x.strip()] or None
    res, trades = run(
        args.input,
        args.train_window,
        args.lookback,
        args.max_sheets,
        estimators,
        only_sheets,
        args.notional_mode,
        args.start_date or None,
        args.end_date or None,
        not args.include_off_hours,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    res.to_csv(args.out, index=False, encoding="utf-8-sig")
    trades_out = args.trades_out or args.out.with_name(args.out.stem + "_trades.csv")
    trades_out.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(trades_out, index=False, encoding="utf-8-sig")
    summary = (
        res.groupby("method")
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
    print("\nSUMMARY")
    print(summary.to_string())
    print(f"\nWROTE {args.out}")
    print(f"WROTE {trades_out}")


if __name__ == "__main__":
    main()
