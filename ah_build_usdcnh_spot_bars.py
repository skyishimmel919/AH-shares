from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import hdf5plugin  # noqa: F401
import numpy as np
import pandas as pd


DEFAULT_OMDD_ROOTS = (
    Path("/mnt/nas2/onboard1/raw_hdf5_b/sse"),
    Path("/mnt/nas2/onboard1/raw_hdf5_b/hkex"),
    Path("/mnt/nas8/onboard1/raw_hdf5_b/sse"),
    Path("/mnt/nas8/onboard1/raw_hdf5_b/hkex"),
)


def ns_to_ts(ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.to_datetime(ns.astype("int64"), unit="ns").tz_localize("UTC").tz_convert("Asia/Shanghai").tz_localize(None)


def make_bar_grid(date: str, minutes: int) -> pd.DataFrame:
    d = pd.Timestamp(date)
    starts = []
    for start, end in [("09:30", "11:30"), ("13:00", "15:00")]:
        starts.extend(pd.date_range(f"{d:%Y-%m-%d} {start}", f"{d:%Y-%m-%d} {end}", freq=f"{minutes}min", inclusive="left"))
    out = pd.DataFrame({"bar_start": starts})
    out["bar_end"] = out["bar_start"] + pd.Timedelta(minutes=minutes)
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


def parse_roots(raw: str) -> list[Path]:
    roots = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            roots.append(Path(item))
    return roots


def resolve_omdd_path(date: str, omdd_roots: list[Path]) -> Path | None:
    names = [
        f"hkex.omdd.{date}.h5",
        f"hkex_omdd.{date}.h5",
        f"hkex.omdd.{date}.b.gre.h5",
        f"hkex_omdd.{date}.b.gre.h5",
        f"hkex.omdd.{date}.b.h5",
        f"hkex_omdd.{date}.b.h5",
        f"hkex.omdd.{date}.gre.h5",
        f"hkex_omdd.{date}.gre.h5",
    ]
    for root in omdd_roots:
        day_dir = root / date
        for name in names:
            path = day_dir / name
            if path.exists():
                return path
    return None


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
        trade_rows = 0
        qty = 0
        if order_book_id in h5 and "Trade" in h5[order_book_id]:
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


def build_day(
    date: str,
    omdd_roots: list[Path],
    rates_df: pd.DataFrame,
    rate_col: str,
    bar_minutes: int,
    max_age_minutes: int,
    day_count: int,
) -> tuple[pd.DataFrame, dict[str, object]]:
    grid = make_bar_grid(date, bar_minutes)
    out = grid.copy()
    out["USDCNH_FUT"] = np.nan
    out["USDCNH_SPOT_EST"] = np.nan
    out["USDCNH"] = np.nan
    out["usdcnh_source"] = "omdd_trade"
    out["usdcnh_rate_col"] = rate_col
    out["USD_3M_RATE"] = np.nan
    out["CNH_3M_RATE"] = np.nan
    out["CNY_SHIBOR_3M_RATE"] = np.nan
    out["rate_used"] = np.nan
    out["usdcnh_contract"] = ""
    out["usdcnh_order_book_id"] = ""
    out["usdcnh_expiry"] = np.nan
    out["usdcnh_days_to_expiry"] = np.nan
    out["usdcnh_last_trade_ts"] = pd.NaT
    out["usdcnh_last_trade_age_sec"] = np.nan

    manifest = {"date": date, "status": "init", "rows": 0, "rate_col": rate_col}
    omdd_path = resolve_omdd_path(date, omdd_roots)
    if omdd_path is None:
        manifest["status"] = "missing_omdd"
        return out, manifest
    manifest["omdd_path"] = str(omdd_path)

    rate_row = load_rate_row(date, rates_df, rate_col)
    if rate_row is None or not np.isfinite(float(rate_row["USD_3M_RATE"])) or not np.isfinite(float(rate_row[rate_col])):
        manifest["status"] = "missing_rate"
        return out, manifest
    usd_rate = float(rate_row["USD_3M_RATE"])
    cnh_rate_for_adjustment = float(rate_row[rate_col])

    with h5py.File(omdd_path, "r") as h5:
        if "SeriesDefinitionBase" not in h5:
            manifest["status"] = "missing_series_definition"
            return out, manifest
        contract = select_omdd_main_cus_contract(h5)
        if contract is None:
            manifest["status"] = "missing_contract"
            return out, manifest
        order_book_id = str(contract["order_book_id"])
        if contract["qty"] <= 0 or order_book_id not in h5 or "Trade" not in h5[order_book_id]:
            manifest.update({"status": "missing_trade", "contract": contract["symbol"], "order_book_id": order_book_id})
            return out, manifest
        trades = h5[order_book_id]["Trade"][:]

    trade_df = pd.DataFrame(
        {
            "trade_ts": ns_to_ts(trades["TradeTime"]),
            "USDCNH_FUT": trades["Price"].astype(float) / 10000.0,
        }
    )
    trade_df = trade_df[trade_df["USDCNH_FUT"] > 0].sort_values("trade_ts")
    if trade_df.empty:
        manifest["status"] = "empty_trade"
        return out, manifest

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
    spot_est = matched["USDCNH_FUT"] * (1.0 + usd_rate * t) / (1.0 + cnh_rate_for_adjustment * t)

    out["USDCNH_FUT"] = matched["USDCNH_FUT"].to_numpy()
    out["USDCNH_SPOT_EST"] = spot_est.to_numpy()
    out["USDCNH"] = out["USDCNH_SPOT_EST"]
    out["USD_3M_RATE"] = usd_rate
    for col in ["CNH_3M_RATE", "CNY_SHIBOR_3M_RATE"]:
        if col in rate_row.index and pd.notna(rate_row[col]):
            out[col] = float(rate_row[col])
    out["rate_used"] = cnh_rate_for_adjustment
    out["usdcnh_contract"] = str(contract["symbol"])
    out["usdcnh_order_book_id"] = order_book_id
    out["usdcnh_expiry"] = int(contract["expiry"])
    out["usdcnh_days_to_expiry"] = days_to_expiry
    out["usdcnh_last_trade_ts"] = matched["trade_ts"].to_numpy()
    out["usdcnh_last_trade_age_sec"] = (matched["bar_end"] - matched["trade_ts"]).dt.total_seconds().to_numpy()

    max_age = float(out["usdcnh_last_trade_age_sec"].max()) if not out["usdcnh_last_trade_age_sec"].isna().all() else np.nan
    manifest.update(
        {
            "status": "ok" if not out["USDCNH_SPOT_EST"].isna().any() else "stale_or_missing_trade",
            "rows": int(out["USDCNH_SPOT_EST"].notna().sum()),
            "contract": contract["symbol"],
            "order_book_id": order_book_id,
            "expiry": int(contract["expiry"]),
            "days_to_expiry": days_to_expiry,
            "trade_qty": int(contract["qty"]),
            "trade_rows": int(contract["trade_rows"]),
            "max_age_sec": max_age,
            "usd_rate": usd_rate,
            "rate_used": cnh_rate_for_adjustment,
        }
    )
    return out, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--omdd-root",
        type=Path,
        default=None,
        help="Single OMDD root kept for backward compatibility; prefer --omdd-roots.",
    )
    parser.add_argument(
        "--omdd-roots",
        default=",".join(str(p) for p in DEFAULT_OMDD_ROOTS),
        help="Comma-separated roots with YYYYMMDD subdirs for OMDD fallback lookup.",
    )
    parser.add_argument("--rates-csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--start", default="20250401")
    parser.add_argument("--end", default="20260421")
    parser.add_argument("--rate-col", default="CNY_SHIBOR_3M_RATE")
    parser.add_argument("--bar-minutes", type=int, default=15)
    parser.add_argument("--max-age-minutes", type=int, default=10)
    parser.add_argument("--day-count", type=int, default=365)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    omdd_roots = [args.omdd_root] if args.omdd_root is not None else parse_roots(args.omdd_roots)
    rates = pd.read_csv(args.rates_csv, dtype={"date": str})
    required = {"date", "USD_3M_RATE", args.rate_col}
    missing = required - set(rates.columns)
    if missing:
        raise SystemExit(f"rates csv missing columns: {sorted(missing)}")

    manifest_rows = []
    dates = pd.date_range(pd.Timestamp(args.start), pd.Timestamp(args.end), freq="D").strftime("%Y%m%d")
    for date in dates:
        out_path = args.out_dir / f"usdcnh_spot_15m_{date}.parquet"
        if out_path.exists():
            continue
        df, manifest = build_day(date, omdd_roots, rates, args.rate_col, args.bar_minutes, args.max_age_minutes, args.day_count)
        if manifest["status"] == "ok":
            df.to_parquet(out_path, index=False)
        manifest_rows.append(manifest)
        pd.DataFrame(manifest_rows).to_csv(args.out_dir / "manifest.csv", index=False)
        print(
            f"{date} {manifest['status']} rows={manifest.get('rows', 0)} "
            f"contract={manifest.get('contract', '')} max_age={manifest.get('max_age_sec', np.nan)}",
            flush=True,
        )
    if manifest_rows:
        pd.DataFrame(manifest_rows).to_csv(args.out_dir / "manifest.csv", index=False)


if __name__ == "__main__":
    main()
