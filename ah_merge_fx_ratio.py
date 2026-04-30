from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import hdf5plugin  # noqa: F401
import numpy as np
import pandas as pd


def ns_to_ts(ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.to_datetime(ns.astype("int64"), unit="ns").tz_localize("UTC").tz_convert("Asia/Shanghai").tz_localize(None)


def qwmid(bid: pd.Series, bid_qty: pd.Series, ask: pd.Series, ask_qty: pd.Series) -> pd.Series:
    denom = bid_qty + ask_qty
    return ((bid * ask_qty + ask * bid_qty) / denom).where(denom > 0)


def make_bar_grid(date: str, minutes: int) -> pd.DataFrame:
    d = pd.Timestamp(date)
    starts = []
    for start, end in [("09:30", "11:30"), ("13:00", "15:00")]:
        starts.extend(pd.date_range(f"{d:%Y-%m-%d} {start}", f"{d:%Y-%m-%d} {end}", freq=f"{minutes}min", inclusive="left"))
    out = pd.DataFrame({"bar_start": starts})
    out["bar_end"] = out["bar_start"] + pd.Timedelta(minutes=minutes)
    return out


def load_usdcnh_bars(date: str, grid: pd.DataFrame, fx_root: Path, max_age_minutes: int) -> pd.DataFrame:
    out = grid[["bar_start", "bar_end"]].copy()
    out["USDCNH"] = np.nan
    out["USDCNH_FUT"] = np.nan
    out["USDCNH_SPOT_EST"] = np.nan
    out["usdcnh_source"] = "databento_bbo"
    out["usdcnh_quote_age_sec"] = np.nan
    fx_path = fx_root / f"cme.{date}.h5"
    if not fx_path.exists():
        return out
    with h5py.File(fx_path, "r") as h5:
        symbols = sorted([k for k in h5.keys() if k.startswith("CNH") and "bbo-1m" in h5[k]])
        if not symbols:
            return out
        arr = h5[symbols[0]]["bbo-1m"][:]
    if arr.size == 0:
        return out
    df = pd.DataFrame(
        {
            "quote_ts": ns_to_ts(arr["ts_recv"]),
            "bid": arr["bid_px_00"].astype(float),
            "ask": arr["ask_px_00"].astype(float),
            "bid_qty": arr["bid_sz_00"].astype(float),
            "ask_qty": arr["ask_sz_00"].astype(float),
        }
    )
    df = df[(df["bid"] > 0) & (df["ask"] > 0)].sort_values("quote_ts")
    if df.empty:
        return out
    df["USDCNH"] = qwmid(df["bid"], df["bid_qty"], df["ask"], df["ask_qty"])
    matched = pd.merge_asof(
        out[["bar_end"]].sort_values("bar_end"),
        df[["quote_ts", "USDCNH"]].sort_values("quote_ts"),
        left_on="bar_end",
        right_on="quote_ts",
        direction="backward",
        tolerance=pd.Timedelta(minutes=max_age_minutes),
    )
    out["USDCNH"] = matched["USDCNH"].to_numpy()
    out["USDCNH_FUT"] = out["USDCNH"]
    out["USDCNH_SPOT_EST"] = out["USDCNH"]
    out["usdcnh_quote_age_sec"] = (matched["bar_end"] - matched["quote_ts"]).dt.total_seconds().to_numpy()
    return out


def decode_h5_scalar(value: object) -> str:
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("ascii", errors="ignore").strip("\x00").strip()
    if hasattr(value, "item"):
        value = value.item()
    return str(value)


def load_rate_row(date: str, rates_df: pd.DataFrame, rate_col: str) -> pd.Series | None:
    d = int(date)
    hist = rates_df[rates_df["date"].astype(int) <= d].sort_values("date")
    hist = hist[hist["USD_3M_RATE"].notna() & hist[rate_col].notna()]
    if hist.empty:
        return None
    return hist.iloc[-1]


def select_omdd_main_cus_contract(h5: h5py.File) -> dict[str, object] | None:
    base = h5["SeriesDefinitionBase"]["SeriesDefinitionBase"][:]
    candidates = []
    for row in base:
        symbol = decode_h5_scalar(row["Symbol"])
        order_book_id = decode_h5_scalar(row["OrderBookID"])
        if not symbol.startswith("CUS"):
            continue
        if "/" in symbol:
            continue
        if int(row["PutOrCall"]) != 0:
            continue
        if order_book_id not in h5 or "Trade" not in h5[order_book_id]:
            trade_rows = 0
            qty = 0
        else:
            trades = h5[order_book_id]["Trade"]
            trade_rows = len(trades)
            qty = int(trades["Quantity"][:].astype(np.int64).sum()) if trade_rows else 0
        candidates.append(
            {
                "symbol": symbol,
                "order_book_id": order_book_id,
                "expiry": int(row["ExpirationDate"]),
                "trade_rows": trade_rows,
                "qty": qty,
            }
        )
    if not candidates:
        return None
    candidates.sort(key=lambda x: (int(x["qty"]), int(x["trade_rows"]), x["symbol"]), reverse=True)
    return candidates[0]


def load_usdcnh_omdd_trade_bars(
    date: str,
    grid: pd.DataFrame,
    omdd_root: Path,
    rates_df: pd.DataFrame,
    rate_col: str,
    max_age_minutes: int,
    day_count: int,
) -> pd.DataFrame:
    out = grid[["bar_start", "bar_end"]].copy()
    out["USDCNH"] = np.nan
    out["USDCNH_FUT"] = np.nan
    out["USDCNH_SPOT_EST"] = np.nan
    out["usdcnh_source"] = "omdd_trade"
    out["usdcnh_rate_col"] = rate_col
    out["usdcnh_contract"] = ""
    out["usdcnh_order_book_id"] = ""
    out["usdcnh_expiry"] = np.nan
    out["usdcnh_days_to_expiry"] = np.nan
    out["USD_3M_RATE"] = np.nan
    out["CNH_3M_RATE"] = np.nan
    out["CNY_SHIBOR_3M_RATE"] = np.nan
    out["rate_used"] = np.nan
    out["usdcnh_quote_age_sec"] = np.nan

    omdd_path = omdd_root / date / f"hkex.omdd.{date}.h5"
    if not omdd_path.exists():
        return out
    rate_row = load_rate_row(date, rates_df, rate_col)
    if rate_row is None:
        return out
    usd_rate = float(rate_row["USD_3M_RATE"])
    rate_used = float(rate_row[rate_col])
    if not np.isfinite(usd_rate) or not np.isfinite(rate_used):
        return out

    with h5py.File(omdd_path, "r") as h5:
        if "SeriesDefinitionBase" not in h5:
            return out
        contract = select_omdd_main_cus_contract(h5)
        if contract is None:
            return out
        order_book_id = str(contract["order_book_id"])
        if contract["qty"] <= 0 or order_book_id not in h5 or "Trade" not in h5[order_book_id]:
            return out
        trades = h5[order_book_id]["Trade"][:]

    if trades.size == 0:
        return out
    trade_df = pd.DataFrame(
        {
            "trade_ts": ns_to_ts(trades["TradeTime"]),
            "USDCNH_FUT": trades["Price"].astype(float) / 10000.0,
        }
    )
    trade_df = trade_df[(trade_df["USDCNH_FUT"] > 0)].sort_values("trade_ts")
    if trade_df.empty:
        return out

    matched = pd.merge_asof(
        out[["bar_end"]].sort_values("bar_end"),
        trade_df[["trade_ts", "USDCNH_FUT"]].sort_values("trade_ts"),
        left_on="bar_end",
        right_on="trade_ts",
        direction="backward",
        tolerance=pd.Timedelta(minutes=max_age_minutes),
    )
    trade_date = pd.Timestamp(date)
    expiry_date = pd.Timestamp(str(contract["expiry"]))
    days_to_expiry = max((expiry_date - trade_date).days, 0)
    t = days_to_expiry / float(day_count)
    spot_est = matched["USDCNH_FUT"] * (1.0 + usd_rate * t) / (1.0 + rate_used * t)

    out["USDCNH_FUT"] = matched["USDCNH_FUT"].to_numpy()
    out["USDCNH_SPOT_EST"] = spot_est.to_numpy()
    out["USDCNH"] = out["USDCNH_SPOT_EST"]
    out["usdcnh_contract"] = str(contract["symbol"])
    out["usdcnh_order_book_id"] = order_book_id
    out["usdcnh_expiry"] = int(contract["expiry"])
    out["usdcnh_days_to_expiry"] = days_to_expiry
    out["USD_3M_RATE"] = usd_rate
    if "CNH_3M_RATE" in rate_row.index and pd.notna(rate_row["CNH_3M_RATE"]):
        out["CNH_3M_RATE"] = float(rate_row["CNH_3M_RATE"])
    if "CNY_SHIBOR_3M_RATE" in rate_row.index and pd.notna(rate_row["CNY_SHIBOR_3M_RATE"]):
        out["CNY_SHIBOR_3M_RATE"] = float(rate_row["CNY_SHIBOR_3M_RATE"])
    out["rate_used"] = rate_used
    out["usdcnh_quote_age_sec"] = (matched["bar_end"] - matched["trade_ts"]).dt.total_seconds().to_numpy()
    return out


def load_usdhkd_previous_day(date: str, usdhkd_df: pd.DataFrame) -> float:
    d = int(date)
    hist = usdhkd_df[usdhkd_df["date"].astype(int) < d].sort_values("date")
    if hist.empty:
        return np.nan
    return float(hist.iloc[-1]["USDHKD"])


def parse_dirs(raw: str | None) -> list[Path]:
    if raw is None:
        return []
    out = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            out.append(Path(item))
    return out


def collect_base_paths(base_dirs: list[Path]) -> list[Path]:
    by_date: dict[str, Path] = {}
    for base_dir in base_dirs:
        for path in sorted(base_dir.glob("ah_base_15m_bars_*.parquet")):
            date = path.stem.split("_")[-1]
            by_date[date] = path
    return [by_date[d] for d in sorted(by_date)]


def load_prebuilt_usdcnh_spot_bars(date: str, grid: pd.DataFrame, spot_dirs: list[Path]) -> pd.DataFrame:
    out = grid[["bar_start", "bar_end"]].copy()
    for spot_dir in spot_dirs:
        path = spot_dir / f"usdcnh_spot_15m_{date}.parquet"
        if not path.exists():
            continue
        fx = pd.read_parquet(path)
        return out.merge(fx, on=["bar_start", "bar_end"], how="left")
    out["USDCNH"] = np.nan
    out["USDCNH_FUT"] = np.nan
    out["USDCNH_SPOT_EST"] = np.nan
    out["usdcnh_source"] = "prebuilt_missing"
    out["usdcnh_last_trade_age_sec"] = np.nan
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--base-dirs", help="Comma-separated base bar dirs. Overrides --base-dir when provided.")
    parser.add_argument("--usdcnh-source", choices=["databento_bbo", "omdd_trade"], default="omdd_trade")
    parser.add_argument("--usdcnh-spot-dirs", help="Comma-separated prebuilt USDCNH spot bar dirs. When set, no raw FX H5 is read.")
    parser.add_argument("--fx-root", type=Path, default=Path("/mnt/nas2/ken_databento_fx_cnh_bbo1m_h5"))
    parser.add_argument("--omdd-root", type=Path, default=Path("/mnt/nas2/onboard1/raw_hdf5_b/sse"))
    parser.add_argument("--usdhkd-csv", type=Path, required=True)
    parser.add_argument("--rates-csv", type=Path)
    parser.add_argument("--rate-col", default="CNY_SHIBOR_3M_RATE")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--bar-minutes", type=int, default=15)
    parser.add_argument("--max-age-minutes", type=int, default=10)
    parser.add_argument("--day-count", type=int, default=365)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    usdhkd = pd.read_csv(args.usdhkd_csv)
    spot_dirs = parse_dirs(args.usdcnh_spot_dirs)
    rates = None
    if args.usdcnh_source == "omdd_trade" and not spot_dirs:
        if args.rates_csv is None:
            raise SystemExit("--rates-csv is required when --usdcnh-source omdd_trade")
        rates = pd.read_csv(args.rates_csv)
        required_cols = {"date", "USD_3M_RATE", args.rate_col}
        missing = required_cols - set(rates.columns)
        if missing:
            raise SystemExit(f"--rates-csv missing columns: {sorted(missing)}")
    manifest_rows = []
    base_dirs = parse_dirs(args.base_dirs) if args.base_dirs else [args.base_dir]
    for base_path in collect_base_paths(base_dirs):
        date = base_path.stem.split("_")[-1]
        out_path = args.out_dir / f"ah_merged_fx_ratio_15m_{date}.parquet"
        if out_path.exists():
            print(f"SKIP {date} existing", flush=True)
            continue
        grid = make_bar_grid(date, args.bar_minutes)
        if spot_dirs:
            fx = load_prebuilt_usdcnh_spot_bars(date, grid, spot_dirs)
        elif args.usdcnh_source == "databento_bbo":
            fx = load_usdcnh_bars(date, grid, args.fx_root, args.max_age_minutes)
        else:
            assert rates is not None
            fx = load_usdcnh_omdd_trade_bars(date, grid, args.omdd_root, rates, args.rate_col, args.max_age_minutes, args.day_count)
        usdhkd_value = load_usdhkd_previous_day(date, usdhkd)
        age_col = "usdcnh_last_trade_age_sec" if "usdcnh_last_trade_age_sec" in fx.columns else "usdcnh_quote_age_sec"
        max_age = float(fx[age_col].max()) if age_col in fx.columns and not fx[age_col].isna().all() else np.nan
        if fx["USDCNH"].isna().any() or not np.isfinite(usdhkd_value):
            print(f"SKIP {date} missing_fx max_usdcnh_age_sec={max_age} usdhkd={usdhkd_value}", flush=True)
            manifest_rows.append({"date": date, "status": "missing_fx", "rows": 0, "usdcnh_source": args.usdcnh_source, "max_usdcnh_age_sec": max_age, "usdhkd_prev_day": usdhkd_value})
            continue
        base = pd.read_parquet(base_path)
        merged = base.merge(fx, on=["bar_start", "bar_end"], how="inner")
        merged["USDHKD"] = usdhkd_value
        merged["fx_hkd_to_cny"] = merged["USDCNH"] / merged["USDHKD"]
        merged["h_mid_cny"] = merged["h_mid_hkd"] * merged["fx_hkd_to_cny"]
        merged["ah_ratio"] = merged["a_mid"] / merged["h_mid_cny"]
        merged["ah_premium"] = merged["ah_ratio"] - 1.0
        merged["A"] = merged["a_mid"]
        merged["H"] = merged["h_mid_cny"]
        merged["H_HKD"] = merged["h_mid_hkd"]
        ts = pd.to_datetime(merged["bar_end"])
        merged["weekday"] = ts.dt.weekday
        merged["week"] = ts.dt.isocalendar().week.astype(int)
        merged["year"] = ts.dt.year
        merged["month"] = ts.dt.month
        merged["bar_index"] = merged.groupby(["date", "a_code", "h_code"]).cumcount()
        merged.to_parquet(out_path, index=False)
        manifest_rows.append(
            {
                "date": date,
                "status": "ok",
                "rows": len(merged),
                "pairs": merged[["a_code", "h_code"]].drop_duplicates().shape[0],
                "usdcnh_source": args.usdcnh_source,
                "usdcnh_contract": fx["usdcnh_contract"].iloc[0] if "usdcnh_contract" in fx else "",
                "usdcnh_order_book_id": fx["usdcnh_order_book_id"].iloc[0] if "usdcnh_order_book_id" in fx else "",
                "usdcnh_expiry": fx["usdcnh_expiry"].iloc[0] if "usdcnh_expiry" in fx else np.nan,
                "usdcnh_days_to_expiry": fx["usdcnh_days_to_expiry"].iloc[0] if "usdcnh_days_to_expiry" in fx else np.nan,
                "max_usdcnh_age_sec": max_age,
                "usdhkd_prev_day": usdhkd_value,
            }
        )
        pd.DataFrame(manifest_rows).to_csv(args.out_dir / "manifest.csv", index=False)
        print(f"DONE {date} rows={len(merged)} max_usdcnh_age_sec={max_age}", flush=True)
    if manifest_rows:
        pd.DataFrame(manifest_rows).to_csv(args.out_dir / "manifest.csv", index=False)


if __name__ == "__main__":
    main()
