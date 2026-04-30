from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd
import requests


TUSHARE_URL = "https://api.tushare.pro"


def a_code_to_ts_code(a_code: str) -> str:
    code = str(a_code).strip().zfill(6)
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    raise ValueError(f"cannot infer A-share exchange for {code}")


def tushare_call(api_name: str, token: str, params: dict, fields: str = "") -> pd.DataFrame:
    payload = {"api_name": api_name, "token": token, "params": params, "fields": fields}
    r = requests.post(TUSHARE_URL, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"{api_name} failed: code={data.get('code')} msg={data.get('msg')}")
    body = data.get("data") or {}
    return pd.DataFrame(body.get("items") or [], columns=body.get("fields") or [])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start-date", default="20250401")
    parser.add_argument("--end-date", default="20260421")
    parser.add_argument("--sleep", type=float, default=0.12)
    parser.add_argument("--token-env", default="TUSHARE_TOKEN")
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        raise SystemExit(f"missing environment variable {args.token_env}")

    universe = pd.read_csv(args.universe, dtype={"a_code": str})
    a_codes = sorted(universe["a_code"].astype(str).str.zfill(6).unique())

    frames = []
    manifest = []
    for idx, a_code in enumerate(a_codes, start=1):
        ts_code = a_code_to_ts_code(a_code)
        status = "ok"
        message = ""
        rows = 0
        try:
            df = tushare_call(
                "adj_factor",
                token,
                {"ts_code": ts_code, "start_date": args.start_date, "end_date": args.end_date},
                fields="ts_code,trade_date,adj_factor",
            )
            rows = len(df)
            if rows:
                df["a_code"] = a_code
                frames.append(df)
        except Exception as exc:
            status = "error"
            message = f"{type(exc).__name__}: {exc}"
        manifest.append({"a_code": a_code, "ts_code": ts_code, "status": status, "rows": rows, "message": message})
        print(f"{idx:03d}/{len(a_codes)} {a_code} {ts_code} {status} rows={rows}")
        time.sleep(args.sleep)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if frames:
        out = pd.concat(frames, ignore_index=True).sort_values(["a_code", "trade_date"])
        out.to_csv(args.out, index=False)
    pd.DataFrame(manifest).to_csv(args.out.with_name(args.out.stem + "_manifest.csv"), index=False)

    print("summary")
    print(pd.DataFrame(manifest)["status"].value_counts(dropna=False).to_string())
    print(f"out={args.out}")


if __name__ == "__main__":
    main()

