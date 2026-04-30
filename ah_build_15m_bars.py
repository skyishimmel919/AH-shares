from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import h5py
import hdf5plugin  # noqa: F401
import numpy as np
import pandas as pd
from numba import types
from numba.typed import Dict


HK_BOOKBUILDER_ROOT = Path("/home/ken/temp/forAlex/newHK_v6_alex_use_sent_addSanity")
if HK_BOOKBUILDER_ROOT.exists():
    sys.path.insert(0, str(HK_BOOKBUILDER_ROOT))

from hkex_loader_cash import load_h5_tables  # type: ignore  # noqa: E402
from testing.hkex_book.book_builder_cash import BookBuildingCashJit, feed_by_guide  # type: ignore  # noqa: E402


PRICE_SCALE_HK = 1000.0
REGULAR_HK_TRADE_TYPES = (0, 100)
DEFAULT_HKEX_ROOTS = (
    Path("/mnt/nas2/onboard1/raw_hdf5_b/hkex"),
    Path("/mnt/nas8/onboard1/raw_hdf5_b/hkex"),
)


@dataclass(frozen=True)
class Pair:
    name: str
    a_code: str
    h_code5: str
    h_symbol: str


def ns_to_ts(ns: np.ndarray) -> pd.DatetimeIndex:
    return pd.to_datetime(ns.astype("int64"), unit="ns").tz_localize("UTC").tz_convert("Asia/Shanghai").tz_localize(None)


def parse_pairs(path: Path) -> list[Pair]:
    if path.suffix.lower() == ".csv":
        df0 = pd.read_csv(path, dtype=object)
        df = pd.DataFrame(
            {
                "name": df0["name"],
                "a_code": df0["a_code"],
                "h_code": df0["h_code"],
            }
        )
        rows = df.itertuples(index=False)
        return [
            Pair(
                name="" if pd.isna(r.name) else str(r.name).strip(),
                a_code=str(r.a_code).split(".")[0].strip().zfill(6),
                h_code5=str(r.h_code).split(".")[0].strip().zfill(5),
                h_symbol=str(int(str(r.h_code).split(".")[0].strip())),
            )
            for r in rows
            if not pd.isna(r.a_code) and not pd.isna(r.h_code)
        ]

    df = pd.read_excel(path, sheet_name=0, dtype=object)
    pairs: list[Pair] = []
    for _, row in df.iloc[1:].iterrows():
        name = "" if pd.isna(row.iloc[1]) else str(row.iloc[1]).strip()
        a_raw = row.iloc[3]
        h_raw = row.iloc[9]
        if pd.isna(a_raw) or pd.isna(h_raw):
            continue
        a_code = str(a_raw).split(".")[0].strip().zfill(6)
        h_code5 = str(h_raw).split(".")[0].strip().zfill(5)
        if not (a_code.isdigit() and h_code5.isdigit()):
            continue
        pairs.append(Pair(name=name, a_code=a_code, h_code5=h_code5, h_symbol=str(int(h_code5))))
    return pairs


def a_exchange(a_code: str) -> str:
    if a_code.startswith("6"):
        return "sse"
    if a_code.startswith(("0", "3")):
        return "sze"
    raise ValueError(f"unknown A-share exchange for {a_code}")


def make_bar_grid(date: str, minutes: int) -> pd.DataFrame:
    d = pd.Timestamp(date)
    starts = []
    for start, end in [("09:30", "11:30"), ("13:00", "15:00")]:
        rng = pd.date_range(f"{d:%Y-%m-%d} {start}", f"{d:%Y-%m-%d} {end}", freq=f"{minutes}min", inclusive="left")
        starts.extend(rng)
    out = pd.DataFrame({"bar_start": starts})
    out["bar_end"] = out["bar_start"] + pd.Timedelta(minutes=minutes)
    return out


def qwmid(bid: pd.Series, bid_qty: pd.Series, ask: pd.Series, ask_qty: pd.Series) -> pd.Series:
    denom = bid_qty + ask_qty
    return ((bid * ask_qty + ask * bid_qty) / denom).where(denom > 0)


def build_a_bars(h5: h5py.File, pair: Pair, grid: pd.DataFrame) -> pd.DataFrame | None:
    if pair.a_code not in h5 or "L1" not in h5[pair.a_code]:
        return None
    arr = h5[pair.a_code]["L1"][:]
    if arr.size == 0:
        return None
    df = pd.DataFrame(
        {
            "ts": ns_to_ts(arr["local_ts"]),
            "bid": arr["BidPrice1"].astype(float),
            "bid_qty": arr["BidVolume1"].astype(float),
            "ask": arr["OfferPrice1"].astype(float),
            "ask_qty": arr["OfferVolume1"].astype(float),
            "cum_notional": arr["TotalTradeValue"].astype(float),
        }
    )
    df = df[(df["bid"] > 0) & (df["ask"] > 0) & (df["bid_qty"] > 0) & (df["ask_qty"] > 0)].sort_values("ts")
    if df.empty:
        return None

    end_lookup = pd.merge_asof(
        grid[["bar_end"]].sort_values("bar_end"),
        df.rename(columns={"ts": "bar_end"}).sort_values("bar_end"),
        on="bar_end",
        direction="backward",
        tolerance=pd.Timedelta(minutes=20),
    )
    start_lookup = pd.merge_asof(
        grid[["bar_start"]].sort_values("bar_start"),
        df[["ts", "cum_notional"]].rename(columns={"ts": "bar_start", "cum_notional": "start_cum_notional"}).sort_values("bar_start"),
        on="bar_start",
        direction="backward",
        tolerance=pd.Timedelta(minutes=20),
    )
    out = grid[["bar_start", "bar_end"]].copy()
    out["Buy1Px"] = end_lookup["bid"].to_numpy()
    out["Buy1Qty"] = end_lookup["bid_qty"].to_numpy()
    out["Sell1Px"] = end_lookup["ask"].to_numpy()
    out["Sell1Qty"] = end_lookup["ask_qty"].to_numpy()
    out["a_cum_notional"] = end_lookup["cum_notional"].to_numpy()
    out["a_start_cum_notional"] = start_lookup["start_cum_notional"].to_numpy()
    out["a_start_cum_notional"] = out["a_start_cum_notional"].fillna(0.0)
    out["a_bar_notional"] = (out["a_cum_notional"] - out["a_start_cum_notional"]).clip(lower=0)
    out["a_mid"] = qwmid(out["Buy1Px"], out["Buy1Qty"], out["Sell1Px"], out["Sell1Qty"])
    return out


def best_from_map(book_map) -> tuple[float, float]:
    arr = book_map.collect_all()
    if arr.shape[0] == 0:
        return np.nan, np.nan
    idx = int(np.argsort(arr["key"])[0])
    return float(arr["px"][idx]) / PRICE_SCALE_HK, float(arr["val1"][idx])


def empty_member_vars():
    member_vars = Dict.empty(key_type=types.unicode_type, value_type=types.int64)
    member_vars["out_market_open"] = 0
    member_vars["more_updates"] = 0
    member_vars["out_order_count"] = 0
    member_vars["out_trade_count"] = 0
    member_vars["out_order_by_order"] = 0
    member_vars["finalize"] = 0
    return member_vars


def snapshot_book(book: BookBuildingCashJit) -> tuple[float, float, float, float]:
    bid, bid_qty = best_from_map(book.bid_map)
    ask, ask_qty = best_from_map(book.ask_map)
    return bid, bid_qty, ask, ask_qty


def build_h_bbo(h5_path: Path, pair: Pair, grid: pd.DataFrame) -> pd.DataFrame | None:
    try:
        add_arr, del_arr, mod_arr, trd_arr, guide = load_h5_tables(str(h5_path), pair.h_symbol, max_updates=None, debug=False)
    except Exception:
        return None
    if len(guide) == 0:
        return None

    ends_ns = (pd.to_datetime(grid["bar_end"]).astype("int64") - pd.Timestamp("1970-01-01").value).to_numpy(dtype="int64")
    book = BookBuildingCashJit(capacity_bid=5_000_000, capacity_ask=5_000_000)
    member_vars = empty_member_vars()
    rows: list[tuple[float, float, float, float]] = []
    bar_i = 0
    bound = np.where(np.diff(guide["localTs"]) != 0)[0] + 1
    bound = np.concatenate(([0], bound, [len(guide)]))
    for s, e in zip(bound[:-1], bound[1:]):
        ts_ns = int(guide[s]["localTs"])
        while bar_i < len(ends_ns) and ts_ns >= int(ends_ns[bar_i]):
            rows.append(snapshot_book(book))
            bar_i += 1
        feed_by_guide(book, guide[s:e], add_arr, del_arr, mod_arr, trd_arr)
        book.finalize(int(guide[e - 1]["localTs"]), False, member_vars)
    while bar_i < len(ends_ns):
        rows.append(snapshot_book(book))
        bar_i += 1

    out = grid[["bar_start", "bar_end"]].copy()
    vals = np.array(rows, dtype=float)
    out["Buy1px"] = vals[:, 0]
    out["buy1qty"] = vals[:, 1]
    out["sell1px"] = vals[:, 2]
    out["sell1qty"] = vals[:, 3]
    out["h_mid_hkd"] = qwmid(out["Buy1px"], out["buy1qty"], out["sell1px"], out["sell1qty"])
    return out


def build_h_trade_bars(h5_path: Path, pair: Pair, grid: pd.DataFrame) -> pd.DataFrame | None:
    with h5py.File(h5_path, "r") as h5:
        if pair.h_symbol not in h5 or "Trade" not in h5[pair.h_symbol]:
            return None
        arr = h5[pair.h_symbol]["Trade"][:]
    if arr.size == 0:
        return None
    mask = np.isin(arr["TrdType"], REGULAR_HK_TRADE_TYPES)
    arr = arr[mask]
    if arr.size == 0:
        return None
    ts = ns_to_ts(arr["TradeTime"] if "TradeTime" in arr.dtype.names else arr["local_ts"])
    df = pd.DataFrame(
        {
            "ts": ts,
            "h_trade_qty": arr["Quantity"].astype(float),
            "h_bar_notional": arr["Price"].astype(float) * arr["Quantity"].astype(float) / PRICE_SCALE_HK,
        }
    )
    pieces = []
    for _, g in grid.iterrows():
        m = (df["ts"] >= g["bar_start"]) & (df["ts"] < g["bar_end"])
        pieces.append((df.loc[m, "h_trade_qty"].sum(), df.loc[m, "h_bar_notional"].sum()))
    out = grid[["bar_start", "bar_end"]].copy()
    vals = np.array(pieces, dtype=float)
    out["h_trade_qty"] = vals[:, 0]
    out["h_bar_notional"] = vals[:, 1]
    out["h_cum_notional"] = out["h_bar_notional"].cumsum()
    return out


def load_usdcnh_bars(date: str, grid: pd.DataFrame, fx_root: Path) -> pd.DataFrame:
    out = grid[["bar_start", "bar_end"]].copy()
    out["USDCNH"] = np.nan
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
            "bar_end": ns_to_ts(arr["ts_recv"]),
            "bid": arr["bid_px_00"].astype(float),
            "ask": arr["ask_px_00"].astype(float),
            "bid_qty": arr["bid_sz_00"].astype(float),
            "ask_qty": arr["ask_sz_00"].astype(float),
        }
    )
    df = df[(df["bid"] > 0) & (df["ask"] > 0)].sort_values("bar_end")
    if df.empty:
        return out
    df["USDCNH"] = qwmid(df["bid"], df["bid_qty"], df["ask"], df["ask_qty"])
    matched = pd.merge_asof(
        out[["bar_end"]].sort_values("bar_end"),
        df[["bar_end", "USDCNH"]].rename(columns={"bar_end": "quote_ts"}).sort_values("quote_ts"),
        left_on="bar_end",
        right_on="quote_ts",
        direction="backward",
        tolerance=pd.Timedelta(minutes=2),
    )
    out["USDCNH"] = matched["USDCNH"].to_numpy()
    out["usdcnh_quote_age_sec"] = (matched["bar_end"] - matched["quote_ts"]).dt.total_seconds().to_numpy()
    return out


def load_usdhkd_previous_day(date: str, usdhkd_df: pd.DataFrame | None) -> float:
    if usdhkd_df is None or usdhkd_df.empty:
        return np.nan
    d = int(date)
    hist = usdhkd_df[usdhkd_df["date"].astype(int) < d].sort_values("date")
    if hist.empty:
        return np.nan
    return float(hist.iloc[-1]["USDHKD"])


def build_pair_day(
    pair: Pair,
    date: str,
    data_root: Path,
    h_path: Path,
    minutes: int,
) -> pd.DataFrame | None:
    day_dir = data_root / date
    ex = a_exchange(pair.a_code)
    a_path = day_dir / f"{ex}.{date}.h5"
    if not a_path.exists() or not h_path.exists():
        return None
    grid = make_bar_grid(date, minutes)
    with h5py.File(a_path, "r") as ah5:
        a_bars = build_a_bars(ah5, pair, grid)
    if a_bars is None:
        return None
    h_bbo = build_h_bbo(h_path, pair, grid)
    if h_bbo is None:
        return None
    h_trd = build_h_trade_bars(h_path, pair, grid)
    if h_trd is None:
        return None
    out = a_bars.merge(h_bbo, on=["bar_start", "bar_end"], how="inner").merge(h_trd, on=["bar_start", "bar_end"], how="inner")
    out.insert(0, "date", date)
    out.insert(1, "name", pair.name)
    out.insert(2, "a_code", pair.a_code)
    out.insert(3, "h_code", pair.h_code5)
    out["Time"] = out["bar_start"]
    out["Notional"] = out["a_cum_notional"]
    out["notional"] = out["h_cum_notional"]
    required = ["a_mid", "h_mid_hkd", "a_bar_notional", "h_bar_notional"]
    if len(out) != len(grid) or out[required].isna().any().any():
        return None
    return out


def iter_dates(data_root: Path, start: str, end: str) -> list[str]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    out = []
    for d in sorted(data_root.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        ts = pd.Timestamp(d.name)
        if start_ts <= ts <= end_ts:
            out.append(d.name)
    return out


def parse_roots(raw: str) -> list[Path]:
    roots = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            roots.append(Path(item))
    return roots


def resolve_h_omdc_path(date: str, data_root: Path, hkex_roots: list[Path]) -> Path | None:
    candidate_dirs = [data_root / date]
    candidate_dirs.extend(root / date for root in hkex_roots)
    names = [
        f"hkex.omdc.{date}.b.gre.h5",
        f"hkex_omdc.{date}.b.gre.h5",
        f"hkex.omdc.{date}.b.h5",
        f"hkex_omdc.{date}.b.h5",
        f"hkex.omdc.{date}.gre.h5",
        f"hkex_omdc.{date}.gre.h5",
        f"hkex.omdc.{date}.h5",
        f"hkex_omdc.{date}.h5",
    ]
    for directory in candidate_dirs:
        for name in names:
            path = directory / name
            if path.exists():
                return path
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path("/mnt/nas2/onboard1/raw_hdf5_b/sse"))
    parser.add_argument(
        "--hkex-roots",
        default=",".join(str(p) for p in DEFAULT_HKEX_ROOTS),
        help="Comma-separated HKEX fallback roots with YYYYMMDD subdirs.",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--start", default="20250401")
    parser.add_argument("--end", default="20260421")
    parser.add_argument("--bar-minutes", type=int, default=15)
    parser.add_argument("--max-pairs", type=int, default=0)
    parser.add_argument("--max-dates", type=int, default=0)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    hkex_roots = parse_roots(args.hkex_roots)
    pairs = parse_pairs(args.pairs)
    if args.max_pairs:
        pairs = pairs[: args.max_pairs]
    dates = iter_dates(args.data_root, args.start, args.end)
    if args.max_dates:
        dates = dates[: args.max_dates]

    manifest_rows = []
    print(f"pairs={len(pairs)} dates={len(dates)} out={args.out_dir}", flush=True)
    for date in dates:
        t0 = time.time()
        day_dir = args.data_root / date
        h_path = resolve_h_omdc_path(date, args.data_root, hkex_roots)
        required = [day_dir / f"sse.{date}.h5", day_dir / f"sze.{date}.h5"]
        if not all(p.exists() for p in required) or h_path is None:
            print(f"SKIP_DATE {date} missing_required_file", flush=True)
            continue
        out_path = args.out_dir / f"ah_base_15m_bars_{date}.parquet"
        if out_path.exists():
            print(f"SKIP_DATE {date} existing {out_path}", flush=True)
            continue
        day_frames = []
        ok = 0
        for i, pair in enumerate(pairs, 1):
            try:
                bars = build_pair_day(pair, date, args.data_root, h_path, args.bar_minutes)
            except Exception as exc:
                print(f"PAIR_ERR {date} {pair.a_code}/{pair.h_code5} {type(exc).__name__}: {exc}", flush=True)
                bars = None
            if bars is not None:
                day_frames.append(bars)
                ok += 1
            if i % 20 == 0:
                print(f"PROGRESS {date} pair {i}/{len(pairs)} ok={ok}", flush=True)
        if day_frames:
            day_df = pd.concat(day_frames, ignore_index=True)
            day_df.to_parquet(out_path, index=False)
            rows = len(day_df)
        else:
            rows = 0
        manifest_rows.append(
            {
                "date": date,
                "pairs_ok": ok,
                "rows": rows,
                "seconds": round(time.time() - t0, 3),
            }
        )
        pd.DataFrame(manifest_rows).to_csv(args.out_dir / "manifest.csv", index=False)
        print(f"DONE_DATE {date} pairs_ok={ok} rows={rows} seconds={time.time() - t0:.1f}", flush=True)


if __name__ == "__main__":
    main()
