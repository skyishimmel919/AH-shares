# Official 3M Rate Inputs for USDCNH Future-to-Spot Conversion

This file defines the daily 3-month rates used to convert HKEX USD/CNH futures
prices into estimated spot `USDCNH` for AH ratio calculation.

## Formula

For `USDCNH` quoted as CNH per 1 USD:

```text
T = days_to_expiry / 365
USDCNH_SPOT_EST = USDCNH_FUT * (1 + USD_3M_RATE * T) / (1 + CNH_3M_RATE * T)
```

All rate columns are stored as annualized decimals, not percentages.

## Production CSV

Canonical local file:

```text
AH file/fx_3m_rates_official.csv
```

Schema:

```text
date
USD_3M_RATE
CNH_3M_RATE
CNY_SHIBOR_3M_RATE
usd_raw_percent
cnh_raw_percent
cny_shibor_raw_percent
usd_source
cnh_source
cny_shibor_source
usd_source_url
cnh_source_url
cny_shibor_source_url
```

Example:

```text
date,USD_3M_RATE,CNH_3M_RATE,usd_raw_percent,cnh_raw_percent
20260421,0.0366868,0.0157121,3.66868,1.57121
```

## USD 3M Source

Default official public source:

```text
New York Fed Markets Data API
Endpoint: https://markets.newyorkfed.org/api/rates/secured/sofrai/search.json
Field: average90day
Stored as: USD_3M_RATE = average90day / 100
Source label: NYFED_SOFRAI_90D_AVG
```

Why this source:

- It is published by the Federal Reserve Bank of New York.
- The 90-day SOFR Average is an official daily reference-rate series.
- The API supports date-range backfill.

Limitation:

- `average90day` is a compounded historical SOFR average, not a forward-looking
  3-month term rate.
- For pure futures fair-value work, a licensed forward-looking 3M Term SOFR
  source such as CME Term SOFR is theoretically better. Use this NY Fed series
  as the free official baseline unless a licensed term-rate feed is added.

## CNH 3M Source

Default official public source:

```text
Treasury Markets Association benchmark page
URL: https://benchmark.tma.org.hk/benchmark/history/cnh-hk-interbank-offered-rate
Row: 3M
Stored as: CNH_3M_RATE = 3M / 100
Source label: TMA_CNH_HIBOR_3M
```

Why this source:

- TMA is the benchmark administrator for CNH HIBOR.
- CNH HIBOR is the offshore RMB interbank offered-rate benchmark.
- The fixing covers tenors including 3 months.

Limitation:

- The public TMA history page currently exposes only the latest displayed
  fixings, not a full historical bulk API.
- Therefore the local CSV must be updated regularly to build and preserve a
  daily archive.

## Update Command

Run from:

```text
C:\ChatGPT_sandbox\AH shares
```

Command:

```powershell
python .\ah_update_3m_rates.py --out "AH file\fx_3m_rates_official.csv"
```

For a USD backfill window:

```powershell
python .\ah_update_3m_rates.py --start 2026-04-01 --end 2026-04-29 --out "AH file\fx_3m_rates_official.csv"
```

The updater:

1. Fetches NY Fed SOFRAI 90-day average for the requested date range.
2. Fetches the latest TMA CNH HIBOR 3M fixings from the benchmark page.
3. Merges by date.
4. Appends/deduplicates into the canonical CSV.

## Use in AH Merge

`ah_merge_fx_ratio.py` expects the canonical rate CSV when running:

```powershell
python .\ah_merge_fx_ratio.py `
  --usdcnh-source omdd_trade `
  --rates-csv "AH file\fx_3m_rates_official.csv" `
  --usdhkd-csv "AH file\fx_usdhkd_frankfurter_20250320_20260421.csv" `
  --base-dir <base_bar_dir> `
  --out-dir <ratio_out_dir>
```

The merge uses the latest available rate row at or before the trade date.

## CNY 3M Official Fallback / Sensitivity Source

China official onshore RMB source:

```text
CFETS / National Interbank Funding Center Shibor history API
Endpoint: https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis
Query: lang=en&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD
Field: 3M
Stored as: CNY_SHIBOR_3M_RATE = 3M / 100
Source label: CFETS_SHIBOR_3M
```

Status:

- This is an official onshore CNY rate, not offshore CNH.
- Use it for history coverage, diagnostics, and sensitivity runs when CNH HIBOR
  history is missing.
- Do not silently substitute it for `CNH_3M_RATE` in the baseline FX merge. If
  used, label the run explicitly as `CNY_SHIBOR_3M` fallback.
- Historical backfill has been run from 2007-01-04 through 2026-04-29 into
  `AH file/fx_3m_rates_official.csv`.

Recent CNH-vs-CNY comparison from currently available overlapping TMA dates:

```text
date      CNH HIBOR 3M   CNY Shibor 3M   CNH-CNY
20260421  1.57121%       1.44200%        +12.921 bp
20260422  1.57303%       1.43750%        +13.553 bp
20260423  1.57515%       1.43450%        +14.065 bp
20260424  1.58788%       1.42750%        +16.038 bp
20260427  1.58970%       1.42900%        +16.070 bp
```

On these dates, CNH HIBOR 3M is directionally close to but consistently above
CNY Shibor 3M by about 13-16 bp. For a roughly 55-day CNH futures expiry, this
rate-input difference moves the future-to-spot adjustment by only a few basis
points, much smaller than the 30-60 bp raw futures roll gap. It is therefore a
reasonable fallback for sensitivity analysis, but not the preferred baseline
when actual CNH HIBOR is available.

Official-source evidence:

- CFETS states that its official website is designated by the PBOC to publish
  interbank market information, including RMB interest-rate benchmarks.
- CFETS states that the Shibor official website is the PBOC-designated platform
  for Shibor and LPR release.
- The Shibor implementation rules say National Interbank Funding Center is
  authorized as the publisher, Shibor covers O/N, 1W, 2W, 1M, 3M, 6M, 9M, and
  1Y, and the rates are annual percent on Act/360.
