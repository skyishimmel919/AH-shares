from __future__ import annotations

import argparse
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests


NYFED_SOFRAI_URL = "https://markets.newyorkfed.org/api/rates/secured/sofrai/search.json"
TMA_CNH_HIBOR_URL = "https://benchmark.tma.org.hk/benchmark/history/cnh-hk-interbank-offered-rate"
CFETS_SHIBOR_HIS_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"
OUTPUT_COLUMNS = [
    "date",
    "USD_3M_RATE",
    "CNH_3M_RATE",
    "CNY_SHIBOR_3M_RATE",
    "usd_raw_percent",
    "cnh_raw_percent",
    "cny_shibor_raw_percent",
    "usd_source",
    "cnh_source",
    "cny_shibor_source",
    "usd_source_url",
    "cnh_source_url",
    "cny_shibor_source_url",
]


def parse_yyyymmdd(value: str) -> date:
    return pd.Timestamp(value).date()


def fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def fetch_usd_sofr_90d(start: date, end: date) -> pd.DataFrame:
    params = {"startDate": start.isoformat(), "endDate": end.isoformat(), "type": "rate"}
    response = requests.get(NYFED_SOFRAI_URL, params=params, timeout=30)
    response.raise_for_status()
    records = response.json().get("refRates", [])
    rows = []
    for row in records:
        if row.get("type") != "SOFRAI":
            continue
        raw = row.get("average90day")
        if raw is None:
            continue
        rows.append(
            {
                "date": pd.Timestamp(row["effectiveDate"]).strftime("%Y%m%d"),
                "USD_3M_RATE": float(raw) / 100.0,
                "usd_raw_percent": float(raw),
                "usd_source": "NYFED_SOFRAI_90D_AVG",
                "usd_source_url": NYFED_SOFRAI_URL,
            }
        )
    return pd.DataFrame(rows).sort_values("date")


def fetch_cnh_hibor_3m_latest() -> pd.DataFrame:
    response = requests.get(TMA_CNH_HIBOR_URL, timeout=30)
    response.raise_for_status()
    html = response.text

    header_match = re.search(r"<thead><tr><th>Date</th>(.*?)</tr></thead>", html, flags=re.S)
    row_match = re.search(r"<tr><td>3M</td>(.*?)</tr>", html, flags=re.S)
    if not header_match or not row_match:
        raise RuntimeError("Could not locate TMA CNH HIBOR date header and 3M row")

    dates = re.findall(r"<th>(\d{2}/\d{2}/\d{4})</th>", header_match.group(1))
    values = re.findall(r"<td>([-+]?\d+(?:\.\d+)?)</td>", row_match.group(1))
    if len(dates) != len(values):
        raise RuntimeError(f"TMA date/value count mismatch: {len(dates)} dates vs {len(values)} values")

    rows = []
    for d, value in zip(dates, values):
        raw = float(value)
        rows.append(
            {
                "date": pd.to_datetime(d, dayfirst=True).strftime("%Y%m%d"),
                "CNH_3M_RATE": raw / 100.0,
                "cnh_raw_percent": raw,
                "cnh_source": "TMA_CNH_HIBOR_3M",
                "cnh_source_url": TMA_CNH_HIBOR_URL,
            }
        )
    return pd.DataFrame(rows).sort_values("date")


def fetch_cny_shibor_3m(start: date, end: date) -> pd.DataFrame:
    rows = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=180), end)
        params = {"lang": "en", "startDate": cursor.isoformat(), "endDate": chunk_end.isoformat()}
        response = requests.get(CFETS_SHIBOR_HIS_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        message = payload.get("data", {}).get("messageEn")
        if message:
            raise RuntimeError(f"CFETS Shibor API message for {cursor} to {chunk_end}: {message}")
        for row in payload.get("records", []):
            raw = row.get("3M")
            raw_date = row.get("showDateCN")
            if not raw or not raw_date:
                continue
            raw_value = float(raw)
            rows.append(
                {
                    "date": pd.Timestamp(raw_date).strftime("%Y%m%d"),
                    "CNY_SHIBOR_3M_RATE": raw_value / 100.0,
                    "cny_shibor_raw_percent": raw_value,
                    "cny_shibor_source": "CFETS_SHIBOR_3M",
                    "cny_shibor_source_url": CFETS_SHIBOR_HIS_URL,
                }
            )
        cursor = chunk_end + timedelta(days=1)
    if not rows:
        return pd.DataFrame(columns=["date", "CNY_SHIBOR_3M_RATE", "cny_shibor_raw_percent", "cny_shibor_source", "cny_shibor_source_url"])
    return pd.DataFrame(rows).sort_values("date").drop_duplicates("date", keep="last")


def merge_rates(usd: pd.DataFrame, cnh: pd.DataFrame, cny_shibor: pd.DataFrame) -> pd.DataFrame:
    merged = pd.merge(usd, cnh, on="date", how="outer")
    merged = pd.merge(merged, cny_shibor, on="date", how="outer").sort_values("date")
    for col in OUTPUT_COLUMNS:
        if col not in merged:
            merged[col] = pd.NA
    return merged[OUTPUT_COLUMNS]


def update_file(out_path: Path, new_rows: pd.DataFrame) -> pd.DataFrame:
    if out_path.exists():
        old = pd.read_csv(out_path, dtype={"date": str})
        combined = pd.concat([old, new_rows], ignore_index=True)
    else:
        combined = new_rows
    combined["date"] = combined["date"].astype(str)
    combined = combined.sort_values("date")
    for col in OUTPUT_COLUMNS:
        if col not in combined:
            combined[col] = pd.NA
    rows = []
    for day, grp in combined.groupby("date", sort=True):
        row = {"date": day}
        for col in OUTPUT_COLUMNS:
            if col == "date":
                continue
            valid = grp[col].dropna()
            row[col] = valid.iloc[-1] if not valid.empty else pd.NA
        rows.append(row)
    combined = pd.DataFrame(rows)[OUTPUT_COLUMNS]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out_path, index=False)
    return combined


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("AH file/fx_3m_rates_official.csv"))
    parser.add_argument("--start", default=None, help="YYYY-MM-DD or YYYYMMDD. Defaults to 14 days before today.")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD or YYYYMMDD. Defaults to today.")
    args = parser.parse_args()

    end = parse_yyyymmdd(args.end) if args.end else date.today()
    start = parse_yyyymmdd(args.start) if args.start else end - timedelta(days=14)

    usd = fetch_usd_sofr_90d(start, end)
    cnh = fetch_cnh_hibor_3m_latest()
    cny_shibor = fetch_cny_shibor_3m(start, end)
    new_rows = merge_rates(usd, cnh, cny_shibor)
    complete = update_file(args.out, new_rows)

    latest = complete.dropna(subset=["USD_3M_RATE", "CNH_3M_RATE"]).tail(5)
    print(
        f"WROTE {args.out} rows={len(complete)} "
        f"complete_cnh_rows={complete.dropna(subset=['USD_3M_RATE','CNH_3M_RATE']).shape[0]} "
        f"cny_shibor_rows={complete.dropna(subset=['CNY_SHIBOR_3M_RATE']).shape[0]}"
    )
    if not latest.empty:
        cols = ["date", "USD_3M_RATE", "CNH_3M_RATE", "CNY_SHIBOR_3M_RATE", "usd_source", "cnh_source", "cny_shibor_source"]
        print(latest[cols].to_string(index=False))
    if cnh["date"].min() > fmt_date(start):
        print("NOTE TMA public history page currently exposes only its latest displayed fixings; run this updater daily to build the local archive.")


if __name__ == "__main__":
    main()
