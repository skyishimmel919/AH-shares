# AH RF5 Cumulative-Notional Baseline

This is the current production-safe baseline for the AH relative-value backtest.

## Source Files

- Input workbook: `AH file/new15minbar1500-20230217.xlsx`
- Backtest script: `ah_backtest_compare.py`
- Baseline summary output: `AH file/ah_baseline_rf5_cum_overlap_summary.csv`
- Baseline trade log output: `AH file/ah_baseline_rf5_cum_overlap_summary_trades.csv`

## Baseline Command

Run from `C:\ChatGPT_sandbox\AH shares`:

```powershell
& '.venv\Scripts\python.exe' ah_backtest_compare.py --out 'AH file\ah_baseline_rf5_cum_overlap_summary.csv'
```

The script default settings are intentionally the baseline settings. To reproduce the current baseline, do not override `--estimators`, `--notional-mode`, `--train-window`, `--lookback`, or `--include-off-hours`.

## Data Definitions

Each workbook sheet is one AH pair. The raw input columns used by the baseline are:

- A-share best bid/ask: `Buy1Px`, `Buy1Qty`, `Sell1Px`, `Sell1Qty`
- H-share best bid/ask: `Buy1px`, `buy1qty`, `sell1px`, `sell1qty`
- A-share notional: `Notional`
- H-share notional: `notional`
- FX: `USDCNH`, `USDHKD`

The model computes:

```text
FX = USDCNH / USDHKD

A = (Buy1Px * Sell1Qty + Sell1Px * Buy1Qty) / (Sell1Qty + Buy1Qty)

H = ((Buy1px * sell1qty + sell1px * buy1qty) / (buy1qty + sell1qty)) * FX
```

`A` and `H` are quote-weighted mid prices. `H` is converted into CNH/CNY terms using `USDCNH / USDHKD`.

## Notional Definition

The baseline uses cumulative notional as the RF liquidity feature.

The historical file has a data-definition break:

- Before `2022-12-14`, `Notional` / `notional` behaves like single-bar notional.
- From `2022-12-14` onward, normal-session `Notional` / `notional` behaves like intraday cumulative notional.

Baseline mode is `cum-auto`:

```text
date < 2022-12-14:
    cumulative_notional = same-day cumulative sum of raw bar notional

date >= 2022-12-14:
    cumulative_notional = raw Notional / notional
```

This was chosen after an RF10 A/B test split by the 2022-12-14 break. Cumulative notional was slightly better than single-bar notional in both periods, but the edge was small; keep the bar-vs-cumulative switch available for future sanity checks.

## RF Model

Default RF setting:

```text
n_estimators = 5
random_state = 42
min_samples_leaf = 3
```

RF input features:

```text
A
NotionalFeature
notionalFeature
weekday
week
year
month
```

RF target:

```text
H
```

RF residual:

```text
rf_residual = RF_predicted_H - actual_H
```

`RF5` is the default because full-pool tests showed RF5/RF10/RF30 were close, RF50 did not improve results, and RF5 had the best total net result in the initial test set.

## Window Definitions

Default windows:

```text
train_window = 200
lookback = 400
```

`train_window` controls model fitting:

```text
At bar i, train RF using bars i-200 through i-1.
Predict H at bar i.
Compute residual at bar i.
```

`lookback` controls signal percentile:

```text
At bar i, compare current residual with the previous 400 residuals.
Use that percentile to decide entry/exit.
```

## Trading-Hour Filter

The baseline only allows bars inside the A/H overlapping regular session:

```text
09:30-11:30
13:00-15:00
```

Rows such as `09:15`, `03:45`, and `05:30` are excluded by default. This is important because older raw workbooks include some non-executable or abnormal timestamps, and earlier exploratory results that included them were slightly inflated.

Use `--include-off-hours` only for legacy replication checks.

## Signal Rules

Default percentile rules:

```text
Long residual entry:  percentile <= 0.05
Long residual exit:   percentile >= 0.75

Short residual entry: percentile >= 0.95
Short residual exit:  percentile <= 0.25
```

Direction definitions:

```text
long_residual / buy pair:
    buy A, sell H
    betting A/H ratio moves up

short_residual / sell pair:
    sell A, buy H
    betting A/H ratio moves down
```

The current baseline uses single-bar confirmation:

```text
One qualifying bar is enough to enter or exit.
```

This matches the observed old spreadsheet formula style, which used current-row percentile conditions and did not show a consecutive-bar confirmation rule.

## Execution Assumption

The baseline uses the same bar's quote-weighted mid-derived residual for signal and return calculation. It is a clean research baseline, not yet a conservative execution simulator.

Important limitation:

```text
The baseline does not yet use direction-specific executable bid/ask fill prices.
```

The old Excel workbook contains bid/ask-side formulas for buy/sell residuals, so a stricter next version should add an execution mode:

```text
mid_mode:
    quote-weighted mid for signal and PnL

executable_mode:
    use direction-specific A/H bid/ask prices
```

Current Excel inspection:

```text
new15minbar1500-20230217.xlsx:
    Contains only raw 15-minute A/H BBO, notional, FX columns.

2021 *_output.xlsx files:
    Contain raw BBO plus derived A/H/pred columns, but no clear execution PnL
    formula columns.

ParameterOptimizationReports.xlsm:
    Contains parameter/result summaries such as LongRtn, ShortRtn, LongEntry,
    LongExit, ShortEntry, ShortExit, but not the underlying directional
    fill-price formula.

backtestingsheet3000(3).xlsm:
    This is the inspected workbook that contains actual backtest formulas.
    The key sheet is `MainPage`.
```

In `backtestingsheet3000(3).xlsm` / `MainPage`, row 4 defines:

```text
D = A bid
E = A ask
J = H bid
K = H ask
W = A baseline price from C/open
AA = H baseline price from I/open, FX-adjusted when enabled
```

The signal residuals have explicit buy/sell executable sides:

```text
BG BuyRes  = A ask * beta + alpha - H bid * FX
BH SellRes = A bid * beta + alpha - H ask * FX

BI BuyPRank  = percentile(BuyRes over lookback residual history)
BJ SellPRank = percentile(SellRes over lookback residual history)
```

Representative formulas from row 5:

```text
BG5 = IF(OR(F5=0,L5=0),BG6,E5*AE5+AF5-J5*IF($AH$1="FX_ADJ=YES",O5,1))
BH5 = IF(OR(F5=0,L5=0),BH6,D5*AE5+AF5-K5*IF($AH$1="FX_ADJ=YES",O5,1))
```

The A/H mark/fill price columns also use direction-specific bid/ask when
entering or valuing an open directional position:

```text
BK = A_MM_Px
BL = B_MM_Px
```

Simplified from `BK5` / `BL5`:

```text
If flat and BuyPRank < lower entry:
    A price = A ask
    H price = H bid

If flat and SellPRank > upper entry:
    A price = A bid
    H price = H ask

If already long A / short H:
    A price = A bid
    H price = H ask

If already short A / long H:
    A price = A ask
    H price = H bid
```

There are also branches where the formula uses the baseline `W/I` price on
certain exit conditions, so the old Excel is not a pure full-crossing model.
But it clearly did use bid/ask-sided residuals and directional bid/ask prices
for the main executable-price logic.

More precise old-Excel execution logic:

The workbook is sorted with newer bars above older bars, so row `r+1` is the
previous state and row `r` is the current bar. In the examples below, `BZ6` is
the previous A-share position and `BZ5` is the current A-share position.

```text
BI = BuyPRank
BJ = SellPRank
BZ = A_Pos
CA = H_Pos
CB = Nav
BK = A_MM_Px
BL = B_MM_Px
W  = A baseline price
I  = H baseline price
D/E = A bid/ask
J/K = H bid/ask
```

When flat on the previous bar:

```text
If BuyPRank < LowerEd_A:
    enter long A / short H
    BK = A ask
    BL = H bid

If SellPRank > UpperEnter_A:
    enter short A / long H
    BK = A bid
    BL = H ask

Otherwise:
    stay flat
    BK = A baseline price
    BL = H baseline price
```

When already long A / short H:

```text
Position exit rule:
    exit when SellPRank > LowerExit_A

Price rule:
    if SellPRank < LowerExit_A:
        BK = A baseline price
        BL = H baseline price
    else:
        BK = A bid
        BL = H ask
```

So while the long-A trade is still far from the exit threshold, the template
marks with baseline prices. Once it reaches the exit side, it uses executable
bid/ask prices for the exit.

When already short A / long H:

```text
Position exit rule:
    exit when BuyPRank < UpperExit_A

Price rule:
    if BuyPRank > UpperExit_A:
        BK = A baseline price
        BL = H baseline price
    else:
        BK = A ask
        BL = H bid
```

So while the short-A trade is still far from the exit threshold, the template
marks with baseline prices. Once it reaches the exit side, it uses executable
bid/ask prices for the exit.

NAV formula:

```text
CB5 =
    BZ6 * (BK5 - BK6)
  + CA6 * (BL5 - BL6) * FX
  + CB6
  + optional FX hedge term
```

Round-trip trading cost is applied at the exit condition, not as a separate
entry cost:

```text
If previous position was short A and BuyPRank < UpperExit_A:
    multiply NAV by (1 + RT_Cost * LevRatio)

If previous position was long A and SellPRank > LowerExit_A:
    multiply NAV by (1 + RT_Cost * LevRatio)
```

In `backtestingsheet3000(3).xlsm`, `RT_Cost` points to `CA2`, whose displayed
value is `-0.002`.

Position sizing in the old Excel:

```text
BZ = A_Pos
CA = H_Pos
CB = Nav
W  = A baseline price
AE = regression beta / hedge ratio
LW = alternative hedge ratio when BY3 = 1
```

For the main directional strategy columns:

```text
A position when entering long A:
    BZ = CB * LevRatio / W

A position when entering short A:
    BZ = -CB * LevRatio / W

H position:
    CA = -BZ / hedge_ratio

hedge_ratio = IF(BY3=1, LW, AE)
```

So the old Excel does not simply force A-notional and H-notional to be equal.
It sizes the A leg from NAV and then sizes the H leg from the regression hedge
ratio. The resulting H CNY notional equals:

```text
abs(CA) * H_price_cny = abs(BZ) / hedge_ratio * H_price_cny
```

This is equal to the A notional only when:

```text
hedge_ratio ~= H_price_cny / A_price_cny
```

The current first Python real-fill smoke test used a simpler equal-CNY-notional
pair-return convention:

```text
A leg CNY notional = H leg CNY notional
```

For a 100k CNY base notional, the smoke-test notional check confirmed
`A_notional_cny / H_notional_cny = 1.0` for every generated trade before lot-size
rounding. This is a deliberate simplification, not yet the old Excel beta-hedged
position sizing.

Current Python real-fill sizing baseline:

```text
base_notional_cny = 100,000 per leg
A_qty = round(base_notional_cny / A_fill_price / 100) * 100
H_qty = round(base_notional_cny / H_fill_price_cny / 100) * 100
```

Both legs are rounded to 100 shares for the research backtest. This matches the
A-share board-lot constraint and is an acceptable first approximation for H
shares. A production execution system should replace this with the actual H-share
lot size from reference data.

The Python baseline deliberately does not use the old Excel `AE` / `LW` hedge
ratio for position sizing. RF is used for signal residual generation, and the
trade size is now one-to-one CNY notional by construction.

Smoke-test output using this rule:

```text
AH file/real_fill_smoke_100k_lot100_20250401_20260421/
```

Three-pair notional-ratio check after 100-share rounding:

```text
002594/01211 mean A/H notional ratio: ~0.997, range ~0.91 to 1.12
601318/02318 mean A/H notional ratio: ~1.000, range ~0.95 to 1.05
688981/00981 mean A/H notional ratio: ~0.995-0.996, range ~0.92 to 1.08
```

The remaining deviation is from the deliberate 100-share rounding, not from a
hedge-ratio model.

Side-specific reporting:

The real-fill backtest now reports `long_residual`, `short_residual`, and total
statistics separately. This is required because A-share shorting is usually not
directly executable. Short-spread results are therefore reported in three ways:

```text
short_theoretical:
    Only the short-residual trades, theoretical sell-A/buy-H pair PnL.

short_with_a_inventory:
    Hold a 100k CNY A-share inventory and add short-residual trading PnL.
    This approximates the see-saw mode:
        normal state = long A
        short-spread state = long H

short_with_ah_inventory:
    Hold a 100k CNY long A / short H spread inventory and add short-residual
    trading PnL. This approximates:
        normal state = long A / short H
        short-spread signal state = flat
```

Risk and holding-period reporting:

The real-fill output includes trade-level, curve-level, and monthly metrics:

```text
real_fill_smoke_summary.csv
    Total per-symbol/per-fill-mode metrics, including annualized return,
    annualized vol, Sharpe, Sortino, Calmar, profit factor, win/loss stats,
    max holding bars/days, and exposure time.

real_fill_smoke_side_summary.csv
    The same trade-level statistics split by long_residual and short_residual.

real_fill_smoke_curve_metrics.csv
    Curve-level metrics for total, long side, short side, and the short-side
    inventory variants.

real_fill_smoke_monthly_pnl.csv
    Monthly return and CNY PnL by curve.
```

Holding days are converted from bars using:

```text
1 trading day = 16 bars
holding_days = holding_bars / 16
```

Annualized curve metrics use the same 16-bar day convention and 252 trading
days per year:

```text
bars_per_year = 16 * 252
```

Cached-value check on `backtestingsheet3000(3).xlsm`:

The formula looks beta-hedged, but the cached workbook values show it was almost
equal-notional in practice for the sampled `MainPage` strategy:

```text
Position rows checked: 1,615
mean A/H CNY notional ratio:   1.0013
median A/H CNY notional ratio: 1.0043
5%-95% range:                  0.9644 to 1.0286
90% of rows:                   within about 3% of 1.0
99.5% of rows:                 within about 5% of 1.0
```

Output artifact:

```text
AH file/data/old_excel_mainpage_position_notional_check.csv
```

So the user's memory is correct for this inspected workbook: although the
formula uses `AE` / `LW` as a hedge ratio, the realized A and H CNY notionals
were very close to one-to-one.

RF script check:

```text
AH file/rf_version2.ipynb
AH file/rf_version2_backup.ipynb
```

The RF notebook computes:

```python
rf = RandomForestRegressor(n_estimators=10)
rf.fit(train_features, train_labels["H"])
predictions = rf.predict(test_features)
errors = predictions - test_labels["H"]
b.iloc[length:, 26] = resultrf
```

The written `pred` column is therefore the RF residual, not a hedge ratio. The
RF script does not output beta, hedge ratio, or position sizing. If the Excel
backtest used RF residuals for signal triggering, its hedge ratio still came
from the Excel linear regression beta (`AE`) or alternate `LW` branch.

Interpretation:

The old Excel should be understood as:

```text
cross_fill + mid_mark
```

It is not a separate hybrid-fill concept. Entry and exit use executable
bid/ask-side prices, while bars between entry and exit are marked with the
baseline/mid price so the mark-to-market curve is less noisy. The mid mark
affects interim drawdown and curve shape, but final trade PnL is driven mainly
by entry and exit fill prices.

The older confirmed research price is quote-weighted mid:

```text
A_mid = (Buy1Px * Sell1Qty + Sell1Px * Buy1Qty) / (Sell1Qty + Buy1Qty)
H_mid = (Buy1px * sell1qty + sell1px * buy1qty) / (buy1qty + sell1qty)
```

Execution modes for the next backtest should be treated as explicit scenario
assumptions:

```text
mid_fill:
    Entry and exit both use quote-weighted mid.

cross_fill:
    Entry and exit both use executable bid/ask sides. In-position
    mark-to-market uses quote-weighted mid / adjusted mid for a stable curve.

join_h_then_join_a_fill:
    Practical execution model used historically: quote the less liquid H-share
    leg first by joining H BBO; keep monitoring whether the ratio still qualifies
    while waiting. If H fills, immediately quote the A-share hedge by joining A
    BBO until filled. Since A-share liquidity is usually much stronger, the A leg
    is expected to complete quickly without crossing. This should normally sit
    between mid_fill and cross_bidask_fill, and likely closer to mid_fill than to
    full crossing.

Transaction cost is a round-trip cost and should be applied when the position
exits. Standard cost scenarios:

30bp round trip
50bp round trip
70bp round trip
```

## Current Baseline Result

For `new15minbar1500-20230217.xlsx`, with the baseline command above:

```text
RF5:
symbols = 17
trades = 518
gross_return = 11.409032
net_return_50bp = 8.836532
net_return_70bp = 7.807532
median_symbol_net_50bp = 0.535965

rolling linear:
trades = 96
gross_return = 1.919055
net_return_50bp = 1.461555
net_return_70bp = 1.278555

raw ratio:
trades = 66
gross_return = -0.047516
net_return_50bp = -0.345016
net_return_70bp = -0.464016
```

## Legacy Replication Check

The current script can exactly reproduce the earlier exploratory RF10/raw-notional/include-off-hours run:

```powershell
& '.venv\Scripts\python.exe' ah_backtest_compare.py --estimators 10 --notional-mode raw --include-off-hours --out 'AH file\ah_recheck_rf10_raw_include_offhours_summary.csv'
```

That recheck matched the earlier `ah_method_compare_results_rf10_rf30_all.csv` to floating-point precision:

```text
max_abs_net50_diff = 1.66e-15
trade_count_diff = 0 for every symbol
```

This means later differences are intentional definition changes, not code drift.

## Next Work

Recommended next steps:

1. Run the full AH universe with the current `mid_fill` and `cross_fill` modes.
2. Add optional `confirmation_bars = 2`.
3. Add `next_bar_mid` execution mode for a more conservative no-lookahead fill assumption.
4. Use the trade log to analyze concentration, MAE/MFE, holding time, and per-symbol stability.
5. Add out-of-sample splits before doing broader parameter sweeps.
