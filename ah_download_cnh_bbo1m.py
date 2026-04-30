from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import databento as db
import h5py
import numpy as np
import pandas as pd


MONTH_CODES = "FGHJKMNQUVXZ"
CNH_PATTERN = re.compile(rf"^CNH[{MONTH_CODES}]\d{{1,2}}$")


def load_key(key_source: Path) -> str:
    text = key_source.read_text(errors="ignore")
    match = re.search(r"db\.Historical\(['\"]([^'\"]+)['\"]\)", text)
    if not match:
        match = re.search(r"api_key\s*=\s*['\"]([^'\"]+)['\"]", text)
    if not match:
        raise RuntimeError(f"Could not find Databento key in {key_source}")
    return match.group(1)


def frame_to_records(df: pd.DataFrame) -> np.ndarray:
    out = np.empty(
        len(df),
        dtype=np.dtype(
            [
                ("ts_recv", "<i8"),
                ("ts_event", "<i8"),
                ("bid_px_00", "<f8"),
                ("ask_px_00", "<f8"),
                ("bid_sz_00", "<f8"),
                ("ask_sz_00", "<f8"),
                ("symbol", "S16"),
            ]
        ),
    )
    out["ts_recv"] = pd.to_datetime(df["ts_recv"], utc=True).astype("int64").to_numpy()
    out["ts_event"] = pd.to_datetime(df["ts_event"], utc=True).astype("int64").to_numpy()
    out["bid_px_00"] = df["bid_px_00"].astype(float).to_numpy()
    out["ask_px_00"] = df["ask_px_00"].astype(float).to_numpy()
    out["bid_sz_00"] = df["bid_sz_00"].astype(float).to_numpy()
    out["ask_sz_00"] = df["ask_sz_00"].astype(float).to_numpy()
    out["symbol"] = df["symbol"].astype(str).to_numpy(dtype="S16")
    return out


def resolve_daily_main(client: db.Historical, start: str, end_excl: str) -> dict[str, str]:
    data = client.timeseries.get_range(
        dataset="GLBX.MDP3",
        schema="ohlcv-1d",
        symbols="CNH.FUT",
        stype_in="parent",
        start=start,
        end=end_excl,
    )
    df = data.to_df().reset_index()
    if df.empty:
        return {}
    df = df[df["symbol"].astype(str).str.match(CNH_PATTERN)].copy()
    df["date"] = pd.to_datetime(df["ts_event"], utc=True).dt.strftime("%Y%m%d")
    main = {}
    for day, grp in df.groupby("date"):
        top = grp.sort_values(["volume", "symbol"], ascending=[False, True]).iloc[0]
        main[str(day)] = str(top["symbol"])
    return main


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2026-04-22", help="exclusive end date")
    parser.add_argument("--raw-root", type=Path, default=Path("/mnt/nas2/ken_databento_fx_cnh_bbo1m_raw"))
    parser.add_argument("--h5-root", type=Path, default=Path("/mnt/nas2/ken_databento_fx_cnh_bbo1m_h5"))
    parser.add_argument("--key-source", type=Path, default=Path("/home/ken/tmp/download_clj6_20260305.py"))
    args = parser.parse_args()

    args.raw_root.mkdir(parents=True, exist_ok=True)
    args.h5_root.mkdir(parents=True, exist_ok=True)
    client = db.Historical(load_key(args.key_source))

    raw_path = args.raw_root / f"GLBX.MDP3_CNH.FUT_{args.start}_{args.end}_bbo-1m.dbn.zst"
    if not raw_path.exists() or raw_path.stat().st_size == 0:
        print(f"DOWNLOAD {raw_path}", flush=True)
        client.timeseries.get_range(
            dataset="GLBX.MDP3",
            schema="bbo-1m",
            symbols="CNH.FUT",
            stype_in="parent",
            start=args.start,
            end=args.end,
            path=str(raw_path),
        )
    else:
        print(f"REUSE {raw_path}", flush=True)

    print("RESOLVE daily main", flush=True)
    main_symbols = resolve_daily_main(client, args.start, args.end)
    manifest = pd.DataFrame([{"date": d, "symbol": s} for d, s in sorted(main_symbols.items())])
    manifest.to_csv(args.h5_root / f"manifest_cnh_bbo1m_{args.start}_{args.end}.csv", index=False)

    print("LOAD raw DBN", flush=True)
    df = db.DBNStore.from_file(raw_path).to_df().reset_index()
    df = df[df["symbol"].astype(str).str.match(CNH_PATTERN)].copy()
    df["date"] = pd.to_datetime(df["ts_recv"], utc=True).dt.strftime("%Y%m%d")
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df[(df["bid_px_00"] > 0) & (df["ask_px_00"] > 0)].copy()
    print(f"ROWS usable={len(df)}", flush=True)

    summary_rows = []
    for day, grp in df.groupby("date"):
        symbol = main_symbols.get(str(day))
        if not symbol:
            continue
        use = grp[grp["symbol"].astype(str) == symbol].sort_values("ts_recv")
        out_path = args.h5_root / f"cme.{day}.h5"
        with h5py.File(out_path, "w") as h5:
            h5.attrs["source"] = "databento"
            h5.attrs["dataset"] = "GLBX.MDP3"
            h5.attrs["schema"] = "bbo-1m"
            h5.attrs["day"] = day
            h5.attrs["continuous_mode"] = "daily_main_by_volume"
            grp_h5 = h5.create_group(symbol)
            grp_h5.attrs["root_symbol"] = "CNH"
            grp_h5.attrs["actual_symbol"] = symbol
            rec = frame_to_records(use)
            ds = grp_h5.create_dataset("bbo-1m", data=rec, chunks=(min(len(rec), 100_000),) if len(rec) else None)
            ds.attrs["timezone_for_ah_alignment"] = "Asia/Shanghai via UTC timestamp conversion"
        summary_rows.append({"date": day, "symbol": symbol, "rows": len(use), "out": str(out_path)})
        print(f"WROTE {day} {symbol} rows={len(use)}", flush=True)
    pd.DataFrame(summary_rows).to_csv(args.h5_root / f"summary_cnh_bbo1m_{args.start}_{args.end}.csv", index=False)


if __name__ == "__main__":
    main()
