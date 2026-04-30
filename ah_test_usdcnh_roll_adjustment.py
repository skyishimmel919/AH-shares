from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import hdf5plugin  # noqa: F401
import numpy as np
import pandas as pd


def decode_h5_scalar(value: object) -> str:
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("ascii", errors="ignore").strip("\x00").strip()
    if hasattr(value, "item"):
        value = value.item()
    return str(value)


def ns_to_ts(ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.to_datetime(ns.astype("int64"), unit="ns").tz_localize("UTC").tz_convert("Asia/Shanghai").tz_localize(None)


def make_bar_ends(date: str, minutes: int) -> pd.DataFrame:
    d = pd.Timestamp(date)
    starts = []
    for start, end in [("09:30", "11:30"), ("13:00", "15:00")]:
        starts.extend(pd.date_range(f"{d:%Y-%m-%d} {start}", f"{d:%Y-%m-%d} {end}", freq=f"{minutes}min", inclusive="left"))
    out = pd.DataFrame({"bar_start": starts})
    out["bar_end"] = out["bar_start"] + pd.Timedelta(minutes=minutes)
    return out[["bar_end"]]


def load_rate(date: str, rates_df: pd.DataFrame | None, usd_rate: float, cnh_rate: float) -> tuple[float, float]:
    if rates_df is None:
        return usd_rate, cnh_rate
    hist = rates_df[rates_df["date"].astype(int) <= int(date)].sort_values("date")
    if hist.empty:
        return np.nan, np.nan
    row = hist.iloc[-1]
    return float(row["USD_3M_RATE"]), float(row["CNH_3M_RATE"])


def scan_daily_cus(h5_path: Path) -> list[dict[str, object]]:
    date = h5_path.parent.name
    rows: list[dict[str, object]] = []
    with h5py.File(h5_path, "r") as h5:
        if "SeriesDefinitionBase" not in h5:
            return rows
        base = h5["SeriesDefinitionBase"]["SeriesDefinitionBase"][:]
        for row in base:
            symbol = decode_h5_scalar(row["Symbol"])
            order_book_id = decode_h5_scalar(row["OrderBookID"])
            if not symbol.startswith("CUS"):
                continue
            if "/" in symbol:
                continue
            if int(row["PutOrCall"]) != 0:
                continue
            trade_rows = 0
            qty = 0
            vwap = np.nan
            first_trade = pd.NaT
            last_trade = pd.NaT
            if order_book_id in h5 and "Trade" in h5[order_book_id]:
                trades = h5[order_book_id]["Trade"][:]
                trade_rows = len(trades)
                if trade_rows:
                    q = trades["Quantity"].astype(np.int64)
                    qty = int(q.sum())
                    vwap = float((trades["Price"].astype(np.float64) * q).sum() / qty / 10000.0) if qty else np.nan
                    ts = ns_to_ts(trades["TradeTime"])
                    first_trade = ts.min()
                    last_trade = ts.max()
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "order_book_id": order_book_id,
                    "expiry": int(row["ExpirationDate"]),
                    "trade_rows": trade_rows,
                    "qty": qty,
                    "vwap": vwap,
                    "first_trade": first_trade,
                    "last_trade": last_trade,
                }
            )
    return rows


def load_contract_last_prices(
    h5_path: Path,
    order_book_id: str,
    bar_ends: pd.DataFrame,
    max_age_minutes: int,
) -> pd.DataFrame:
    out = bar_ends.copy()
    out["price"] = np.nan
    out["age_sec"] = np.nan
    with h5py.File(h5_path, "r") as h5:
        if order_book_id not in h5 or "Trade" not in h5[order_book_id]:
            return out
        trades = h5[order_book_id]["Trade"][:]
    if len(trades) == 0:
        return out
    trade_df = pd.DataFrame(
        {
            "trade_ts": ns_to_ts(trades["TradeTime"]),
            "price": trades["Price"].astype(float) / 10000.0,
        }
    ).sort_values("trade_ts")
    matched = pd.merge_asof(
        out[["bar_end"]].sort_values("bar_end"),
        trade_df[["trade_ts", "price"]].sort_values("trade_ts"),
        left_on="bar_end",
        right_on="trade_ts",
        direction="backward",
        tolerance=pd.Timedelta(minutes=max_age_minutes),
    )
    out["price"] = matched["price"].to_numpy()
    out["age_sec"] = (matched["bar_end"] - matched["trade_ts"]).dt.total_seconds().to_numpy()
    return out


def spot_estimate(fut_price: pd.Series, date: str, expiry: int, usd_rate: float, cnh_rate: float, day_count: int) -> pd.Series:
    trade_date = pd.Timestamp(date)
    expiry_date = pd.Timestamp(str(expiry))
    days_to_expiry = max((expiry_date - trade_date).days, 0)
    t = days_to_expiry / float(day_count)
    return fut_price * (1.0 + usd_rate * t) / (1.0 + cnh_rate * t)


def evaluate_roll_windows(
    main_df: pd.DataFrame,
    all_contracts: pd.DataFrame,
    data_root: Path,
    rates_df: pd.DataFrame | None,
    usd_rate: float,
    cnh_rate: float,
    bar_minutes: int,
    max_age_minutes: int,
    day_count: int,
    window_days: int,
) -> pd.DataFrame:
    main_df = main_df.sort_values("date").reset_index(drop=True)
    roll_idxs = [i for i in range(1, len(main_df)) if main_df.loc[i, "symbol"] != main_df.loc[i - 1, "symbol"]]
    rows: list[dict[str, object]] = []
    for idx in roll_idxs:
        old = main_df.loc[idx - 1]
        new = main_df.loc[idx]
        left = max(idx - window_days, 0)
        right = min(idx + window_days, len(main_df) - 1)
        for j in range(left, right + 1):
            date = str(main_df.loc[j, "date"])
            h5_path = data_root / date / f"hkex.omdd.{date}.h5"
            if not h5_path.exists():
                continue
            usd, cnh = load_rate(date, rates_df, usd_rate, cnh_rate)
            if not np.isfinite(usd) or not np.isfinite(cnh):
                continue
            bar_ends = make_bar_ends(date, bar_minutes)
            old_px = load_contract_last_prices(h5_path, str(old["order_book_id"]), bar_ends, max_age_minutes)
            new_px = load_contract_last_prices(h5_path, str(new["order_book_id"]), bar_ends, max_age_minutes)
            cmp = old_px.rename(columns={"price": "old_fut", "age_sec": "old_age_sec"}).merge(
                new_px.rename(columns={"price": "new_fut", "age_sec": "new_age_sec"}), on="bar_end", how="inner"
            )
            cmp = cmp.dropna(subset=["old_fut", "new_fut"])
            if cmp.empty:
                continue
            cmp["old_spot"] = spot_estimate(cmp["old_fut"], date, int(old["expiry"]), usd, cnh, day_count)
            cmp["new_spot"] = spot_estimate(cmp["new_fut"], date, int(new["expiry"]), usd, cnh, day_count)
            cmp["raw_gap_bp"] = (cmp["new_fut"] / cmp["old_fut"] - 1.0) * 10000.0
            cmp["spot_gap_bp"] = (cmp["new_spot"] / cmp["old_spot"] - 1.0) * 10000.0
            rows.append(
                {
                    "roll_date": str(new["date"]),
                    "date": date,
                    "old_symbol": old["symbol"],
                    "old_order_book_id": old["order_book_id"],
                    "old_expiry": int(old["expiry"]),
                    "new_symbol": new["symbol"],
                    "new_order_book_id": new["order_book_id"],
                    "new_expiry": int(new["expiry"]),
                    "bars_compared": len(cmp),
                    "usd_3m_rate": usd,
                    "cnh_3m_rate": cnh,
                    "raw_gap_bp_mean": float(cmp["raw_gap_bp"].mean()),
                    "raw_gap_bp_abs_mean": float(cmp["raw_gap_bp"].abs().mean()),
                    "raw_gap_bp_median": float(cmp["raw_gap_bp"].median()),
                    "spot_gap_bp_mean": float(cmp["spot_gap_bp"].mean()),
                    "spot_gap_bp_abs_mean": float(cmp["spot_gap_bp"].abs().mean()),
                    "spot_gap_bp_median": float(cmp["spot_gap_bp"].median()),
                    "improvement_bp_abs_mean": float(cmp["raw_gap_bp"].abs().mean() - cmp["spot_gap_bp"].abs().mean()),
                    "old_max_age_sec": float(cmp["old_age_sec"].max()),
                    "new_max_age_sec": float(cmp["new_age_sec"].max()),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=Path("/mnt/nas2/onboard1/raw_hdf5_b/sse"))
    parser.add_argument("--start", default="20250401")
    parser.add_argument("--end", default="20260421")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--rates-csv", type=Path)
    parser.add_argument("--usd-rate", type=float, default=0.043)
    parser.add_argument("--cnh-rate", type=float, default=0.021)
    parser.add_argument("--bar-minutes", type=int, default=15)
    parser.add_argument("--max-age-minutes", type=int, default=10)
    parser.add_argument("--day-count", type=int, default=365)
    parser.add_argument("--window-days", type=int, default=5)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rates_df = pd.read_csv(args.rates_csv) if args.rates_csv else None
    h5_paths = []
    for day_dir in sorted(p for p in args.data_root.iterdir() if p.is_dir()):
        p = day_dir / f"hkex.omdd.{day_dir.name}.h5"
        if not p.exists():
            continue
        date = p.parent.name
        if args.start <= date <= args.end:
            h5_paths.append(p)
    all_rows: list[dict[str, object]] = []
    for i, h5_path in enumerate(h5_paths, start=1):
        date = h5_path.parent.name
        try:
            rows = scan_daily_cus(h5_path)
            all_rows.extend(rows)
            best = max(rows, key=lambda r: (int(r["qty"]), int(r["trade_rows"]), str(r["symbol"]))) if rows else None
            if best:
                print(f"SCAN {i}/{len(h5_paths)} {date} main={best['symbol']} oid={best['order_book_id']} qty={best['qty']}", flush=True)
            else:
                print(f"SCAN {i}/{len(h5_paths)} {date} no_cus", flush=True)
        except Exception as exc:
            print(f"ERROR {date} {type(exc).__name__}: {exc}", flush=True)
    all_contracts = pd.DataFrame(all_rows)
    all_contracts.to_csv(args.out_dir / "usdcnh_cus_contract_daily_volume.csv", index=False)
    if all_contracts.empty:
        raise SystemExit("No CUS contracts found")
    main_df = (
        all_contracts.sort_values(["date", "qty", "trade_rows", "symbol"], ascending=[True, False, False, False])
        .groupby("date", as_index=False)
        .head(1)
        .sort_values("date")
        .reset_index(drop=True)
    )
    main_df.to_csv(args.out_dir / "usdcnh_cus_daily_main.csv", index=False)
    roll_df = evaluate_roll_windows(
        main_df,
        all_contracts,
        args.data_root,
        rates_df,
        args.usd_rate,
        args.cnh_rate,
        args.bar_minutes,
        args.max_age_minutes,
        args.day_count,
        args.window_days,
    )
    roll_df.to_csv(args.out_dir / "usdcnh_roll_adjustment_comparison.csv", index=False)
    if not roll_df.empty:
        summary = (
            roll_df.groupby(["roll_date", "old_symbol", "new_symbol"], as_index=False)
            .agg(
                dates=("date", "nunique"),
                bars=("bars_compared", "sum"),
                raw_gap_bp_abs_mean=("raw_gap_bp_abs_mean", "mean"),
                spot_gap_bp_abs_mean=("spot_gap_bp_abs_mean", "mean"),
                improvement_bp_abs_mean=("improvement_bp_abs_mean", "mean"),
            )
            .sort_values("roll_date")
        )
        summary.to_csv(args.out_dir / "usdcnh_roll_adjustment_summary.csv", index=False)
        print(summary.to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
