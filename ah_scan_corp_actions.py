from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd


EVENT_COLUMNS_CN = {
    "effective_date": "生效日期",
    "index_code": "指数代码",
    "index_name": "指数简称",
    "security_code": "成份券代码",
    "security_name": "成份券简称",
    "exchange": "交易所",
    "event_type": "事件类型",
    "bonus_share_rate": "送股比例",
    "rights_rate": "配、供股比例",
    "consolidation_rate": "合股比例",
    "split_rate": "拆股比例",
    "cash_dividend": "分红比例",
    "dividend_currency": "分红货币",
    "current_calc_shares": "当前计算用股本",
    "effective_calc_shares": "拟生效计算用股本",
    "rights_price": "配、供股价",
}


def read_csv_any(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "gb18030", "big5", "latin1"):
        try:
            return pd.read_csv(path, dtype=str, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path, dtype=str, encoding="latin1")


def parse_pairs(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df["a_code"] = df["a_code"].astype(str).str.split(".").str[0].str.zfill(6)
    df["h_code"] = df["h_code"].astype(str).str.split(".").str[0].str.zfill(5)
    return df[["name", "a_code", "h_code"]].drop_duplicates()


def list_ca_files(root: Path, start: str, end: str) -> list[Path]:
    files = []
    for day_dir in sorted(root.iterdir()):
        name = day_dir.name
        if not name.endswith("Pre"):
            continue
        date = name[:8]
        if not (start <= date <= end):
            continue
        files.extend(sorted(day_dir.glob("*ca*.csv")))
    return files


def scan_huabao_ca(root: Path, pairs: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    a_set = set(pairs["a_code"])
    rows = []
    for path in list_ca_files(root, start, end):
        try:
            df = read_csv_any(path)
        except Exception as exc:
            rows.append({"source_file": str(path), "source_error": f"{type(exc).__name__}: {exc}"})
            continue
        if "成份券代码" not in df.columns or "事件类型" not in df.columns:
            continue
        df["成份券代码"] = df["成份券代码"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        sub = df[df["成份券代码"].isin(a_set)].copy()
        if sub.empty:
            continue
        for out_col, cn_col in EVENT_COLUMNS_CN.items():
            if cn_col not in sub.columns:
                sub[cn_col] = np.nan
        sub = sub[[cn for cn in EVENT_COLUMNS_CN.values()]].rename(columns={cn: out for out, cn in EVENT_COLUMNS_CN.items()})
        sub["source"] = "huabao_samba_ca"
        sub["source_file"] = str(path)
        sub["source_date"] = path.parent.name[:8]
        rows.append(sub)
    if not rows:
        return pd.DataFrame()
    out = pd.concat([r for r in rows if isinstance(r, pd.DataFrame)], ignore_index=True)
    out = out.merge(pairs, left_on="security_code", right_on="a_code", how="left")
    numeric_cols = [
        "bonus_share_rate",
        "rights_rate",
        "consolidation_rate",
        "split_rate",
        "cash_dividend",
        "current_calc_shares",
        "effective_calc_shares",
        "rights_price",
    ]
    for col in numeric_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.drop_duplicates(
        subset=[
            "effective_date",
            "a_code",
            "h_code",
            "event_type",
            "bonus_share_rate",
            "rights_rate",
            "split_rate",
            "cash_dividend",
            "source_file",
        ]
    )
    return out


def date_to_int_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace("-", "", regex=False), errors="coerce").astype("Int64")


def load_pair_daily_ratio(merged_dir: Path) -> pd.DataFrame:
    frames = []
    cols = ["date", "a_code", "h_code", "a_mid", "h_mid_hkd", "h_mid_cny", "ah_ratio", "a_bar_notional", "h_bar_notional"]
    for path in sorted(merged_dir.glob("ah_merged_fx_ratio_15m_*.parquet")):
        frames.append(pd.read_parquet(path, columns=cols))
    if not frames:
        raise SystemExit(f"no merged files found in {merged_dir}")
    df = pd.concat(frames, ignore_index=True)
    daily = (
        df.groupby(["date", "a_code", "h_code"])
        .agg(
            bars=("ah_ratio", "size"),
            a_mid_mean=("a_mid", "mean"),
            h_hkd_mean=("h_mid_hkd", "mean"),
            h_cny_mean=("h_mid_cny", "mean"),
            ratio_mean=("ah_ratio", "mean"),
            a_notional=("a_bar_notional", "sum"),
            h_notional=("h_bar_notional", "sum"),
        )
        .reset_index()
        .sort_values(["a_code", "h_code", "date"])
    )
    return daily


def validate_events(events: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    daily = daily.copy()
    daily["date_int"] = pd.to_numeric(daily["date"], errors="coerce").astype("Int64")
    rows = []
    for _, ev in events.iterrows():
        eff = int(str(ev["effective_date"]).replace("-", ""))
        g = daily[(daily["a_code"] == ev["a_code"]) & (daily["h_code"] == ev["h_code"])].sort_values("date_int")
        prev = g[g["date_int"] < eff].tail(1)
        post = g[g["date_int"] >= eff].head(1)
        row = ev.to_dict()
        for prefix, part in [("prev", prev), ("post", post)]:
            if part.empty:
                continue
            r = part.iloc[0]
            for col in ["date", "a_mid_mean", "h_hkd_mean", "h_cny_mean", "ratio_mean", "a_notional", "h_notional"]:
                row[f"{prefix}_{col}"] = r[col]
        if not prev.empty and not post.empty:
            p = prev.iloc[0]
            q = post.iloc[0]
            row["a_price_jump"] = q["a_mid_mean"] / p["a_mid_mean"] if p["a_mid_mean"] else np.nan
            row["h_hkd_price_jump"] = q["h_hkd_mean"] / p["h_hkd_mean"] if p["h_hkd_mean"] else np.nan
            row["ratio_jump"] = q["ratio_mean"] / p["ratio_mean"] if p["ratio_mean"] else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def detect_price_jumps(daily: pd.DataFrame) -> pd.DataFrame:
    out = daily.sort_values(["a_code", "h_code", "date"]).copy()
    for col in ["a_mid_mean", "h_hkd_mean", "ratio_mean"]:
        out[f"prev_{col}"] = out.groupby(["a_code", "h_code"])[col].shift(1)
        out[f"{col}_jump"] = out[col] / out[f"prev_{col}"]
    ratios = [1 / 5, 1 / 4, 1 / 3, 1 / 2, 2, 3, 4, 5]
    masks = []
    for col in ["a_mid_mean_jump", "h_hkd_mean_jump", "ratio_mean_jump"]:
        near = pd.Series(False, index=out.index)
        for r in ratios:
            near |= out[col].between(r * 0.88, r * 1.12)
        near |= out[col].ge(1.8) | out[col].le(0.56)
        masks.append(near)
    flagged = out[masks[0] | masks[1] | masks[2]].copy()
    return flagged


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--huabao-root", type=Path, default=Path("/mnt/nas2/onboard1/ref_data/huabao_samba/HQ"))
    parser.add_argument("--merged-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--start", default="20250401")
    parser.add_argument("--end", default="20260421")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    pairs = parse_pairs(args.pairs)
    events = scan_huabao_ca(args.huabao_root, pairs, args.start, args.end)
    daily = load_pair_daily_ratio(args.merged_dir)
    validated = validate_events(events, daily) if not events.empty else events
    jumps = detect_price_jumps(daily)
    if not events.empty:
        events.to_csv(args.out_dir / "corp_action_events_huabao_raw.csv", index=False)
        validated.to_csv(args.out_dir / "corp_action_events_huabao_validated.csv", index=False)
    daily.to_csv(args.out_dir / "pair_daily_price_ratio_summary.csv", index=False)
    jumps.to_csv(args.out_dir / "price_jump_detector_events.csv", index=False)

    print("huabao_events", len(events))
    if not events.empty:
        print(events["event_type"].value_counts(dropna=False).to_string())
        print("pairs_with_events", events[["a_code", "h_code"]].drop_duplicates().shape[0])
        print("sample_validated")
        print(validated.sort_values(["effective_date", "a_code"]).head(20).to_string(index=False))
    print("price_jump_events", len(jumps))
    print(jumps.sort_values("ratio_mean_jump", ascending=False).head(20).to_string(index=False))
    print("out_dir", args.out_dir)


if __name__ == "__main__":
    main()
