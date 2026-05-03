# AH Phase 2 Handoff: OOS Selection, Features, and Portfolio Construction

This file is the starting point for the second phase of the AH shares project.
Phase 1 rebuilt the old AH strategy stack and established an executable Python
baseline. Phase 2 should focus on selection discipline, out-of-sample testing,
ratio features, and portfolio construction.

## Project Root

```text
C:\ChatGPT_sandbox\AH shares
```

GitHub remote:

```text
https://github.com/skyishimmel919/AH-shares
```

## Phase 1 Status

The old AH strategy framework has been reproduced and documented. The current
baseline is:

```text
model: RF5
n_estimators: 5
random_state: 42
min_samples_leaf: 3
train_window: 200 bars
lookback: 400 residuals
entry/exit: 5% entry, 75% exit for long residual
            95% entry, 25% exit for short residual
bar size: 15 minutes
bars per day: 16
base notional: 100,000 CNY per leg
share rounding: 100 shares
cost scenarios: 30bp / 50bp / 70bp round trip
primary cost case: net50
```

Primary documentation:

```text
BASELINE_RF5_CUM.md
AH_DATA_PATHS.md
AH_CORP_ACTION_ADJUSTMENT.md
FX_3M_RATE_SOURCES.md
```

Core scripts:

```text
ah_build_15m_bars.py
ah_build_usdcnh_spot_bars.py
ah_merge_fx_ratio.py
ah_backtest_real_fill.py
ah_package_full_results.py
ah_analyze_short_blend.py
ah_diagnose_suspicious_pairs.py
```

## Data and Result Artifacts

Local lightweight full-result package:

```text
AH file\full_results_package_20250401_20260421
```

Important package files:

```text
universe_ranking.csv
universe_summary.csv
universe_side_summary.csv
universe_curve_metrics.csv
universe_monthly_pnl.csv
universe_comments.csv
universe_short_inventory_variants.csv
rankings\long_side_cross_fill_net50.csv
rankings\pure_short_side_cross_fill_net50.csv
rankings\short_with_a_inventory_cross_fill_net50.csv
rankings\short_with_ah_inventory_cross_fill_net50.csv
rankings\short_blend_50_50_cross_fill_net50.csv
```

Remote full package, including heavier parquet and per-pair chart artifacts:

```text
guangdong:/home/ken/temp/ah_shares_run/output/real_fill_full_100k_lot100_package_20250401_20260421
```

Remote full raw output:

```text
guangdong:/home/ken/temp/ah_shares_run/output/real_fill_full_100k_lot100_metrics_20250401_20260421
```

The local package is enough for ranking and summary work. For portfolio curve
construction, prefer the remote parquet inputs when available.

## Execution and Direction Definitions

Primary execution assumption:

```text
cross_fill + mid_mark
```

Entry and exit use executable bid/ask prices. In-position mark-to-market uses
adjusted quote-weighted mid for a more stable curve.

Research upper-bound execution:

```text
mid_fill
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

The most important strategy views for Phase 2 are:

```text
long_side cross_fill net50
pure_short_side cross_fill net50
short_blend_50_50 cross_fill net50
```

Do not rank by raw total PnL, because raw total includes a pure short-spread leg
that is not directly implementable without inventory.

## Short-Side Variants

Pure short side:

```text
sell A, buy H
```

This is the cleanest measure of whether the short-spread signal itself has
alpha. It is theoretical unless A-share borrow or inventory is available.

2b, A-inventory variant:

```text
Hold 100k A as inventory.
On short-spread entry, sell A and buy H.
Position changes from A to H.
```

2c, AH-spread-inventory variant:

```text
Hold 100k long A / short H spread.
On short-spread entry, flatten to zero.
On exit, restore long A / short H spread.
```

50/50 short blend:

```text
0.5 * 2b + 0.5 * 2c
inventory = 1.0A - 0.5H
```

The blend is useful, but should be gated by pure short-side quality. If pure
short side is not positive and stable, 2b/2c/blend gains may just be inventory
beta rather than spread alpha.

Suggested pure-short gates:

```text
light:
    pure_short_pnl > 0
    pure_short_sharpe > 0.5
    pure_short_trades >= 15

strict:
    pure_short_pnl > 30,000 CNY
    pure_short_sharpe > 1.0
    pure_short_max_pct_drawdown > -35%
    pure_short_trades >= 15
```

## Important Result Interpretation

Return denominator:

```text
1.0 return = 100,000 CNY PnL
```

Example:

```text
net_return_50bp = 0.41
net_pnl_cny_50bp = 41,000 CNY
```

Absolute drawdown:

```text
max_drawdown = drawdown of cumulative return units
```

Percentage drawdown:

```text
equity = 1 + cumulative_return
max_pct_drawdown = equity / rolling_peak_equity - 1
```

Use percentage drawdown and Calmar-like metrics for risk-normalized ranking.
Absolute drawdown alone can misclassify a strategy that first earns several
notional units and then gives back one notional unit.

## Current Universe Snapshot

Full run date range:

```text
2025-04-01 to 2026-04-21
```

Universe:

```text
requested pairs: 186
valid backtest pairs: 176
skipped pairs: 10
```

High-level observations:

```text
long_side cross_fill net50:
    positive PnL symbols: about 106 / 176
    clean-ish candidates: about 41

pure_short_side cross_fill net50:
    positive PnL symbols: about 120 / 176
    clean-ish candidates under strict filters: about 47

short_blend_50_50 cross_fill net50:
    positive PnL symbols: about 103 / 176
    clean-ish candidates: about 42
```

These are full-sample observations and must not be treated as production
selection evidence without out-of-sample validation.

## Phase 2 Ranking Policy

Do not rank candidates by raw PnL alone.

For strategy-result ranking, use a two-step process:

```text
1. Hard filters:
   net PnL > 0
   enough trades
   acceptable max_pct_drawdown
   monthly PnL not overly concentrated
   A/H notional ratio after rounding close to 1

2. Sort:
   primary: annualized_return / abs(max_pct_drawdown)
   secondary: net PnL
   cross-checks: Sharpe, Sortino, profit factor, win rate, holding days
```

Use separate rankings for:

```text
long_side cross_fill net50
pure_short_side cross_fill net50
short_blend_50_50 cross_fill net50
```

For short blend, require pure short side to pass at least the light pure-short
gate before treating blend performance as meaningful.

## Phase 2 Work Plan

### 1. First-Half Ranking, Second-Half OOS Validation

Initial split:

```text
ranking window: 2025-04-01 to 2025-10-31
OOS window:     2025-11-01 to 2026-04-21
```

For each strategy view:

```text
long_side cross_fill net50
pure_short_side cross_fill net50
short_blend_50_50 cross_fill net50
```

Rank using only ranking-window results, select top N, then evaluate only OOS
performance.

Recommended N values:

```text
10, 20, 30, 40
```

Outputs:

```text
selected_symbols_by_strategy_and_topN.csv
oos_portfolio_summary.csv
oos_portfolio_monthly_pnl.csv
oos_portfolio_curves.parquet
oos_portfolio_charts\
```

### 2. Rolling 3-Month Selection

Use a rolling selection framework:

```text
rank prior 3 months
trade next 3 months
rebalance every 3 months
```

This checks whether pair quality persists through time and reduces full-sample
hindsight bias.

### 3. Ratio Mean-Reversion Feature Table

Build pre-trade features using only each ranking window:

```text
ratio_level
ratio_percentile
ratio_volatility
AR(1) phi
half_life_bars
half_life_days
ADF p-value
Hurst exponent
variance ratio
mean crossing count
extreme-to-median reversion probability
trend slope
trend t-stat
trend R-squared
```

Then test whether these features explain future OOS PnL, Sharpe, and drawdown.

Key user hypothesis to test:

```text
Very high A/H premium may be bad for long-side trading because the A leg is
entered at a high absolute premium, high-premium names often have higher ratio
volatility, and their ratio may trend rather than mean-revert.
```

### 4. Feature-to-OOS Relationship

Create one row per pair per ranking window:

```text
symbol
window_start
window_end
strategy_view
past_ratio_level
past_ratio_percentile
half_life_bars
half_life_days
hurst
adf_pvalue
mean_cross_count
trend_slope
trend_r2
rank_window_pnl
rank_window_sharpe
rank_window_max_pct_dd
oos_pnl
oos_sharpe
oos_max_pct_dd
```

Analyze:

```text
feature quantile vs OOS return
feature quantile vs OOS Sharpe
feature quantile vs OOS max_pct_drawdown
rank-window performance vs OOS performance
```

### 5. Portfolio Construction

After OOS and rolling validation:

```text
build topN equal-weight portfolios
compare long-side-only, pure-short-theoretical, and short-blend portfolios
check monthly PnL concentration
check drawdown timing and max_pct_drawdown
check whether OOS topN remains robust across ranking metrics
```

Do not optimize RF parameters or entry/exit thresholds until selection
stability and OOS behavior are understood.

## Suggested New Thread Starter

Use this as the first message in a new Codex thread:

```text
继续 AH shares 项目第二阶段。

项目根目录：
C:\ChatGPT_sandbox\AH shares

请先读取：
BASELINE_RF5_CUM.md
AH_PHASE2_HANDOFF.md

第一阶段 baseline 已经落地：
- RF5, train_window=200, lookback=400
- 15min bar, 16 bars/day
- entry 5%, exit 75%
- cross_fill + mid_mark
- net50 round-trip cost
- 每腿 100k CNY notional, A/H 等值, round 100 shares
- 主要策略视角：long_side, pure_short_side, short_blend_50_50
- 全量结果在 AH file\full_results_package_20250401_20260421

第二阶段目标：
1. 做前半段排名、后半段 OOS 验证。
2. 做 3 个月滚动 topN 组合。
3. 建 ratio mean-reversion 特征表。
4. 检验这些特征和未来 OOS PnL / Sharpe / drawdown 的关系。
5. 最后再做组合级 PnL 和标的筛选。

请从实现第一半排名、第二半 OOS 验证开始。
```

