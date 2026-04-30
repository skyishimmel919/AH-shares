# AH Corporate-Action Adjustment

This note defines how the AH project should handle splits, bonus shares,
rights issues, and cash dividends before computing AH ratios and RF residuals.

## Problem

The raw intraday H5 prices are not adjusted for corporate actions. If one leg
adjusts before the other, the raw AH ratio can jump mechanically even when the
economic relative value did not change.

Observed example:

- Pair: `002594.SZ / 01211.HK`
- H-share adjusted around `2025-06-10`
- A-share adjusted around `2025-07-29`
- Raw ratio was mechanically distorted between the two dates.

Therefore, the ratio layer should use corporate-action-adjusted A and H prices.

## Preferred Inputs

Use daily adjustment factors when available:

- A-share: Tushare `adj_factor`, cross-check with Huabao corporate-action files
- H-share: Tushare `hk_daily_adj.adj_factor`, cross-check with HKEX entitlement
  announcements or other corporate-action feeds

Dividend and corporate-action details remain useful for explanation and
cross-validation, but the calculation layer should prefer adjustment factors.

Current source plan:

```text
A-share primary:
    Tushare adj_factor

A-share validation:
    Tushare qfq/hfq adjusted daily prices, Huabao corporate-action files

H-share preferred if permission/frequency allows:
    Tushare hk_adjfactor or hk_daily_adj queried by trade_date for all HK names

H-share practical fallback:
    Yahoo Finance daily chart adjusted close, queried programmatically by ticker
    such as 1211.HK, then infer project-consistent factors by matching the
    Yahoo adjusted close to this project's own H5 raw daily close.

Manual browser/download fallback:
    Only use Yahoo Finance web downloads if the programmatic chart endpoint
    becomes unavailable.
```

Tushare notes:

- `hk_adjfactor` supports `trade_date=YYYYMMDD` and returns all HK names for
  that date, with `cum_adjfactor` and `close_price`.
- `hk_daily_adj` also supports `trade_date=YYYYMMDD`, with adjusted daily
  fields and `adj_factor`.
- The current account may be frequency limited on `hk_daily_adj`, so any puller
  must cache results and run slowly by date instead of repeatedly querying
  individual tickers.

Yahoo notes:

- The Yahoo chart endpoint returns daily `close`, `adjclose`, dividends, and
  splits for HK tickers like `1211.HK`.
- Use Yahoo as an adjusted-close source, not as the raw-price source. The raw
  intraday close remains this project's H5 close.
- Yahoo HK symbols use four digits before `.HK`, e.g. `0038.HK`, `0386.HK`,
  `1211.HK`.

Current Yahoo download artifact:

```text
Script:
    ah_download_yahoo_hk_adj.py

Input universe:
    AH file/data/ah_pair_universe_from_AH_stock_compare_20260430.csv

Output directory:
    AH file/data/yahoo_hk_daily_adj_20250401_20260421/

Main files:
    manifest.csv
    yahoo_hk_daily_adjclose_20250401_20260421.csv
    yahoo_hk_events_20250401_20260421.csv
    per_symbol/*_daily.csv
    per_symbol/*_events.csv
    raw_json/*.json
```

The 2026-04-30 AH workbook contained 190 unique valid H-share codes. Yahoo
download status for the project test window `2025-04-01` through `2026-04-21`:

```text
Yahoo responses: 188 ok, 2 error
Tickers with daily rows in the test window: 183
Combined daily rows: 42,468
Corporate-action event rows: 203
```

No daily rows in the test window:

```text
00501 / 0501.HK
00638 / 0638.HK
00699 / 0699.HK
01187 / 1187.HK
02493 / 2493.HK
02615 / 2615.HK
03296 / 3296.HK
```

Spot checks showed several of these begin after the backtest end date
`2026-04-21`, for example `3296.HK` has Yahoo rows starting `2026-04-23` and
`0501.HK`, `0638.HK`, `0699.HK`, `2493.HK` had rows on `2026-04-29`.

## Factor From Adjusted Price

If a source provides adjusted daily close but not an explicit factor, infer a
project-consistent factor by matching the adjusted close to our own H5 raw
daily close.

For each symbol and trade date:

```text
external_adjusted_close = adjusted close from vendor
h5_raw_close            = last valid raw H5 mid/close for the same session

implied_adj_factor_raw_scale = external_adjusted_close / h5_raw_close
```

This produces an implied factor on the vendor's adjusted-price scale.

For AH ratio work, normalize the factor to a common anchor date so only relative
changes matter:

```text
normalized_adj_factor[t] =
    implied_adj_factor_raw_scale[t] / implied_adj_factor_raw_scale[anchor_date]
```

Then:

```text
adjusted_intraday_price[t, bar] =
    raw_intraday_price[t, bar] * normalized_adj_factor[t]
```

The anchor date should normally be the latest available trade date in the test
window. This makes the adjusted historical series comparable to current-price
terms.

## Factor From Explicit Adjustment Factor

If the source provides an explicit daily adjustment factor, use the same
normalization rule:

```text
normalized_adj_factor[t] =
    vendor_adj_factor[t] / vendor_adj_factor[anchor_date]

adjusted_intraday_price[t, bar] =
    raw_intraday_price[t, bar] * normalized_adj_factor[t]
```

This is equivalent to back-adjusting every historical intraday bar into the
anchor-date share-count/dividend basis.

## AH Ratio

Use adjusted prices before FX conversion:

```text
a_mid_adj_cny = a_mid_raw_cny * a_norm_adj_factor
h_mid_adj_hkd = h_mid_raw_hkd * h_norm_adj_factor
h_mid_adj_cny = h_mid_adj_hkd * USDCNH_SPOT_EST / USDHKD

ah_ratio_adj = a_mid_adj_cny / h_mid_adj_cny
```

The old raw ratio should still be retained for diagnostics:

```text
ah_ratio_raw = a_mid_raw_cny / (h_mid_raw_hkd * USDCNH_SPOT_EST / USDHKD)
```

## Validation

Every adjustment-factor source must pass two checks before backtesting:

1. Event-list validation

   Known corporate-action dates from Huabao/Tushare/HKEX should produce visible
   factor changes on or near the expected ex-date.

2. Price-jump coverage

   Large overnight raw-price jumps in either A or H should be covered by a
   factor jump. If a large raw jump has no factor change, flag the symbol/date
   before running strategy results.

For inferred factors from adjusted closes, also check:

```text
external_adjusted_close ~= h5_raw_close * implied_adj_factor_raw_scale
```

The residual should be near zero except for normal close-definition differences.

## Backtest Rule

Backtests should default to adjusted ratio/residual once the factor table is
available. Raw ratio runs are useful only as diagnostics or legacy comparison.

## Current Adjusted-Ratio Smoke Test

Current scripts:

```text
A-share factor downloader:
    ah_download_tushare_a_adj.py

Adjusted-ratio chart builder:
    ah_plot_adjusted_ratio_samples.py
```

Current A-share factor artifact:

```text
AH file/data/tushare_a_adj_factor_20250401_20260421.csv
AH file/data/tushare_a_adj_factor_20250401_20260421_manifest.csv
```

The first A-share factor pull covered all 190 A codes in the AH workbook.

Current adjusted-ratio chart output:

```text
Remote:
    ~/temp/ah_shares_run/output/adjusted_ratio_charts_20250401_20260421/

Local:
    AH file/adjusted_ratio_charts_20250401_20260421/
```

Sample chart files:

```text
adjusted_ratio_002594_01211.png
adjusted_ratio_601318_02318.png
adjusted_ratio_601899_02899.png
adjusted_ratio_601628_02628.png
adjusted_ratio_688981_00981.png
adjusted_ratio_pair_summary.csv
h_implied_yahoo_factor_daily.csv
```

BYD validation:

```text
Pair: 002594 / 01211
Raw ratio min/max:      0.933790 / 3.027412
Adjusted ratio min/max: 0.901048 / 1.221388
```

The large raw-ratio jump between the H-share and A-share corporate-action dates
is removed after applying A Tushare factors and H Yahoo-implied factors.
