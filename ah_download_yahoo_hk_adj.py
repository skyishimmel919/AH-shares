from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import pandas as pd
import requests


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def yyyymmdd_to_epoch(date_s: str) -> int:
    d = dt.datetime.strptime(date_s, "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def h_code_to_yahoo_symbol(h_code: str) -> str:
    # Yahoo Finance uses 4-digit HK tickers, e.g. 0038.HK, 0386.HK, 1211.HK.
    return f"{int(str(h_code).strip()):04d}.HK"


def fetch_chart(symbol: str, start_date: str, end_date: str, timeout: int = 30) -> dict:
    # Yahoo's period2 is exclusive. Add one day so end_date is included.
    end_dt = dt.datetime.strptime(end_date, "%Y%m%d") + dt.timedelta(days=1)
    params = {
        "period1": yyyymmdd_to_epoch(start_date),
        "period2": int(end_dt.replace(tzinfo=dt.timezone.utc).timestamp()),
        "interval": "1d",
        "events": "history|div|split",
        "includeAdjustedClose": "true",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(YAHOO_CHART_URL.format(symbol=symbol), params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    err = data.get("chart", {}).get("error")
    if err:
        raise RuntimeError(err)
    result = data.get("chart", {}).get("result") or []
    if not result:
        raise RuntimeError("empty Yahoo chart result")
    return result[0]


def parse_daily(result: dict, h_code: str, yahoo_symbol: str) -> pd.DataFrame:
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    adj = (result.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    for i, ts in enumerate(timestamps):
        date = pd.Timestamp(ts, unit="s", tz="UTC").tz_convert("Asia/Hong_Kong").strftime("%Y%m%d")
        row = {
            "h_code": str(h_code).zfill(5),
            "yahoo_symbol": yahoo_symbol,
            "trade_date": date,
            "open": value_at(quote.get("open"), i),
            "high": value_at(quote.get("high"), i),
            "low": value_at(quote.get("low"), i),
            "close": value_at(quote.get("close"), i),
            "volume": value_at(quote.get("volume"), i),
            "adjclose": value_at(adj, i),
        }
        if row["close"] and row["adjclose"]:
            row["yahoo_adjclose_factor"] = row["adjclose"] / row["close"]
        else:
            row["yahoo_adjclose_factor"] = None
        rows.append(row)
    return pd.DataFrame(rows)


def value_at(values, i: int):
    if values is None or i >= len(values):
        return None
    return values[i]


def parse_events(result: dict, h_code: str, yahoo_symbol: str) -> pd.DataFrame:
    rows = []
    events = result.get("events") or {}
    for kind, payload in events.items():
        if not isinstance(payload, dict):
            continue
        for _, item in payload.items():
            ts = item.get("date")
            date = pd.Timestamp(ts, unit="s", tz="UTC").tz_convert("Asia/Hong_Kong").strftime("%Y%m%d") if ts else None
            row = {
                "h_code": str(h_code).zfill(5),
                "yahoo_symbol": yahoo_symbol,
                "event_type": kind,
                "event_date": date,
                "amount": item.get("amount"),
                "numerator": item.get("numerator"),
                "denominator": item.get("denominator"),
                "splitRatio": item.get("splitRatio"),
                "raw_json": json.dumps(item, ensure_ascii=True, sort_keys=True),
            }
            rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--start-date", default="20250401")
    parser.add_argument("--end-date", default="20260421")
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    per_symbol_dir = args.out_dir / "per_symbol"
    raw_dir = args.out_dir / "raw_json"
    per_symbol_dir.mkdir(exist_ok=True)
    raw_dir.mkdir(exist_ok=True)

    universe = pd.read_csv(args.universe, dtype={"a_code": str, "h_code": str})
    tickers = sorted(universe["h_code"].astype(str).str.zfill(5).unique())

    daily_frames = []
    event_frames = []
    manifest_rows = []

    for idx, h_code in enumerate(tickers, start=1):
        yahoo_symbol = h_code_to_yahoo_symbol(h_code)
        status = "ok"
        message = ""
        result = None
        for attempt in range(1, args.max_retries + 1):
            try:
                result = fetch_chart(yahoo_symbol, args.start_date, args.end_date)
                break
            except Exception as exc:
                status = "error"
                message = f"{type(exc).__name__}: {exc}"
                if attempt < args.max_retries:
                    time.sleep(args.sleep * attempt * 5)
        if result is not None:
            (raw_dir / f"{h_code}_{yahoo_symbol}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            daily = parse_daily(result, h_code, yahoo_symbol)
            events = parse_events(result, h_code, yahoo_symbol)
            daily.to_csv(per_symbol_dir / f"{h_code}_{yahoo_symbol}_daily.csv", index=False)
            if not events.empty:
                events.to_csv(per_symbol_dir / f"{h_code}_{yahoo_symbol}_events.csv", index=False)
            daily_frames.append(daily)
            if not events.empty:
                event_frames.append(events)
            manifest_rows.append(
                {
                    "h_code": h_code,
                    "yahoo_symbol": yahoo_symbol,
                    "status": "ok",
                    "rows": len(daily),
                    "first_date": daily["trade_date"].min() if not daily.empty else None,
                    "last_date": daily["trade_date"].max() if not daily.empty else None,
                    "events": len(events),
                    "message": "",
                }
            )
        else:
            manifest_rows.append(
                {
                    "h_code": h_code,
                    "yahoo_symbol": yahoo_symbol,
                    "status": status,
                    "rows": 0,
                    "first_date": None,
                    "last_date": None,
                    "events": 0,
                    "message": message,
                }
            )
        print(f"{idx:03d}/{len(tickers)} {h_code} {yahoo_symbol} {manifest_rows[-1]['status']} rows={manifest_rows[-1]['rows']} events={manifest_rows[-1]['events']}")
        time.sleep(args.sleep)

    manifest = pd.DataFrame(manifest_rows)
    manifest.to_csv(args.out_dir / "manifest.csv", index=False)

    if daily_frames:
        all_daily = pd.concat(daily_frames, ignore_index=True).sort_values(["h_code", "trade_date"])
        all_daily.to_csv(args.out_dir / f"yahoo_hk_daily_adjclose_{args.start_date}_{args.end_date}.csv", index=False)
    if event_frames:
        all_events = pd.concat(event_frames, ignore_index=True).sort_values(["h_code", "event_date", "event_type"])
        all_events.to_csv(args.out_dir / f"yahoo_hk_events_{args.start_date}_{args.end_date}.csv", index=False)

    print("summary")
    print(manifest["status"].value_counts(dropna=False).to_string())
    print(f"out_dir={args.out_dir}")


if __name__ == "__main__":
    main()
