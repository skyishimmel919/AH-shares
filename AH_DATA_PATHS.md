# AH Market Data Path Notes

This project uses the Guangdong NAS market-data archive through WSL/SSH.
The current normalized A-share/HKEX bundle path is:

```text
guangdong:/mnt/nas2/onboard1/raw_hdf5_b/sse/YYYYMMDD/
```

The directory name is `sse`, but the date folder can contain Shanghai A-share,
Shenzhen A-share, HKEX cash, and some futures H5 files for the same trading day.

Example:

```text
/mnt/nas2/onboard1/raw_hdf5_b/sse/20260421/
```

Older 2025 HKEX files can also live in HKEX-specific roots:

```text
/mnt/nas2/onboard1/raw_hdf5_b/hkex/YYYYMMDD/
/mnt/nas8/onboard1/raw_hdf5_b/hkex/YYYYMMDD/
```

## File Naming

A-share files:

```text
sse.YYYYMMDD.h5      Shanghai Stock Exchange
sse.YYYYMMDD.b.h5
sze.YYYYMMDD.h5      Shenzhen Stock Exchange
sze.YYYYMMDD.b.h5
```

HKEX cash files:

```text
hkex.omdc.YYYYMMDD.b.gre.h5
hkex.omdc.YYYYMMDD.b.h5
hkex.omdc.YYYYMMDD.gre.h5
hkex.omdc.YYYYMMDD.h5
```

Older HKEX-root files use an underscore between `hkex` and `omdc/omdd`:

```text
hkex_omdc.YYYYMMDD.b.gre.h5
hkex_omdc.YYYYMMDD.b.h5
hkex_omdc.YYYYMMDD.gre.h5
hkex_omdc.YYYYMMDD.h5
```

For AH H-share quotes, use the OMDC cash-product file first:

```text
hkex.omdc.YYYYMMDD.b.gre.h5
hkex_omdc.YYYYMMDD.b.gre.h5
```

OMDD files are also present for derivatives/order data, but H-share cash BBO
should come from OMDC unless later validation shows otherwise.

Availability note checked on `20260429`:

```text
2025-04-01 to 2025-06-30:
  /mnt/nas2/onboard1/raw_hdf5_b/hkex/YYYYMMDD/hkex_omdc.YYYYMMDD*.h5
  /mnt/nas8/onboard1/raw_hdf5_b/hkex/YYYYMMDD/hkex_omdc.YYYYMMDD*.h5
  Both roots have 65 OMDC+OMDD trading days in this date range.
  Some holiday/non-full-trading dates still have placeholder H5 files with only
  `ETC` and no cash symbol groups; skip those dates under the AH-both-open rule.

2025-07-01 onward:
  /mnt/nas2/onboard1/raw_hdf5_b/sse/YYYYMMDD/hkex.omdc.YYYYMMDD*.h5
  /mnt/nas8/onboard1/raw_hdf5_b/sse/YYYYMMDD/hkex.omdc.YYYYMMDD*.h5
```

So for the current AH one-year build, H-share cash OMDC is available from
`20250401`; use the HKEX-specific root and underscore filename for dates before
`20250701`, then use the normalized `sse` date folder and dot filename from
`20250701` onward.

## Symbol Rules

A-share exchange routing:

```text
6xxxxx -> sse.YYYYMMDD*.h5
0xxxxx -> sze.YYYYMMDD*.h5
3xxxxx -> sze.YYYYMMDD*.h5
```

HKEX symbols in the AH list are five-digit strings such as `00388` or `01528`.
Inside OMDC H5, the top-level ticker group drops leading zeroes:

```text
00388 -> /388
01528 -> /1528
06821 -> /6821
```

## A-Share Datasets

For both `sse` and `sze`, each stock appears as a top-level group keyed by the
six-digit A-share code.

Observed examples on `20260421`:

```text
/601828/L1
/601828/snapshots
/601828/orders_v2
/601828/trades_v2

/002821/L1
/002821/SZE_Snapshot_300111
/002821/orders_v2
/002821/trades_v2
```

The simplest bar source is `L1`. It is a roughly 3-second snapshot stream with:

```text
TimeStamp
BidPrice1
BidVolume1
OfferPrice1
OfferVolume1
LastPrice
TotalTradeVolume
TotalTradeValue
TradingPhase
```

`TotalTradeVolume` and `TotalTradeValue` are cumulative intraday values. For
single-bar volume/notional, use the first difference within each trading day.

## HKEX OMDC Datasets

Each HKEX cash symbol is a top-level group keyed by the integer ticker.

Observed datasets:

```text
/1528/AggregateOrderBookUpdate
/1528/TradeTicker
/1528/Trade
/1528/Statistics
/1528/NominalPrice
```

For executable BBO reconstruction, use the existing HK cash order-by-order
bookbuilding implementation:

```text
guangdong:~/temp/forAlex/newHK_v6_alex_use_sent_addSanity/
  extract_hk_cash_bookbuilding_numba_v58_vectorize_cash_hk_for_alex.py
  hkex_loader_cash.py
  testing/hkex_book/book_builder_cash.py
```

The working path is:

1. `hkex_loader_cash.load_h5_tables(...)` loads `AddOrder`, `DeleteOrder`,
   `ModifyOrder`, and `Trade` for one cash symbol.
2. It builds a chronological `guide` sorted by `localTs` and event kind.
3. `feed_by_guide(...)` feeds those events into `BookBuildingCashJit`.
4. The book builder maintains `bid_map`, `ask_map`, `order_id_map`, and
   `key_id_map`.
5. Best bid/ask comes from the first row of the sorted bid/ask maps.

Do not treat `AggregateOrderBookUpdate` as a direct BBO snapshot. It is useful
for reference and validation, but the cash book builder reconstructs the book
from order-by-order OMDC messages.

Useful fields:

```text
AggregateOrderBookUpdate:
  record_ts, SecurityCode, Price, AggregateQuantity, Side, PriceLevel,
  UpdateAction, NumberOfOrders

Statistics:
  record_ts, SecurityCode, SharesTraded, Turnover, HighPrice, LowPrice,
  LastPrice, VWAP

TradeTicker:
  record_ts, SecurityCode, Price, AggregateQuantity, TradeTime
```

`AggregateOrderBookUpdate` can be used to reconstruct BBO by maintaining price
levels per side. Observed side convention:

```text
Side = 0  bid
Side = 1  ask
```

HKEX prices are stored as integers. In the sampled OMDC files, divide by `1000`
to get HKD price:

```text
01528: Price 1320   -> 1.320 HKD
06821: Price 95500  -> 95.500 HKD
00388: Price 411600 -> 411.600 HKD
```

`Statistics.Turnover` and `Statistics.SharesTraded` are cumulative intraday
values. Divide `Turnover` by the same price scale when comparing against HKD
notional. Use first differences for single-bar turnover.

## HK Cash Bookbuilding Smoke Test

Sample tested on `20260421`, symbol `01528` as OMDC group `/1528`, using:

```text
/mnt/nas2/onboard1/raw_hdf5_b/sse/20260421/hkex.omdc.20260421.b.gre.h5
```

Loaded OBO event counts:

```text
AddOrder:    2676
DeleteOrder: 2553
ModifyOrder: 205
Trade:       192
Guide:       5626
```

Opening BBO samples produced by `BookBuildingCashJit`:

```text
time_hkt        bid_px  bid_qty  ask_px  ask_qty
09:30:02.094    1.29    5000     1.31    19600
09:30:34.160    1.29    5000     1.31    19600
09:31:09.046    1.29    5000     1.31    19600
09:33:39.477    1.29    4400     1.31    19600
```

Sanity checks run against `20260421`:

```text
01528:
  crossed/locked BBO rows: 0
  non-positive top quantity rows: 0
  AggregateOrderBookUpdate level-1 price alignment: bid 97.56%, ask 95.81%, both 93.37%
  Statistics turnover/shares are monotonic
  final Statistics turnover: 2,469,804 HKD
  summed Trade price*qty:   2,469,280 HKD
  raw Trade rows: 192
  inferred trade-diff rows from book builder: 137
  raw Trade total shares: 1,900,200
  inferred absolute trade shares: 1,899,800
  difference: 400 shares from two 09:20 auction trades before continuous trading
  after excluding pre-09:30 auction trades, inferred trade quantity/notional matches raw Trade

02359:
  crossed/locked BBO rows: 0
  non-positive top quantity rows: 0
  AggregateOrderBookUpdate level-1 price alignment: bid 89.46%, ask 90.30%, both 87.27%
  Statistics turnover is monotonic

00388:
  crossed/locked BBO rows: 0
  non-positive top quantity rows: 0
  AggregateOrderBookUpdate level-1 price alignment: bid 88.37%, ask 88.20%, both 82.53%
  Statistics turnover is monotonic
```

The `AggregateOrderBookUpdate` comparison is only a reference check because the
book builder uses order-by-order messages as the source of truth. Mismatches are
expected around update timing and feed semantics, but the high alignment plus no
crossed/locked top book makes the OBO builder usable for the first AH bar build.

Batch trade-list sanity check on the AH H-share universe from the
`AH price-list workbook`, using `20260421` OMDC:

```text
Output files:
  AH file/hk_trade_match_20260421.csv
  AH file/hk_raw_trade_types_20260421.csv

Input H-share symbols: 190
Successfully replayed: 185
Missing in OMDC file: 4
  1187, 2493, 2615, 3296
Replay/load error: 1
  2402: loader saw no localTs field
```

For continuous trading sanity, compare only HKT `09:30:00 <= t < 16:00:00`.
The first pass compared OBO-inferred trades against raw `Trade` rows with
`TrdType in (0, 100)`, matching the current cash builder's `VALID_TRD_TYPES`.

```text
Regular-trade total qty/notional exact matches: 103 / 185
Regular-trade non-exact symbols: 82 / 185

Total raw regular qty:      2,779,354,150
Total inferred abs qty:     2,782,348,400
Difference:                    2,994,250

Total raw regular notional: 56,120,990,977 HKD
Total inferred notional:    56,164,730,112 HKD
Difference:                    43,739,135 HKD
```

Comparing against all `Trade` rows regardless of `TrdType` does not solve the
gap; inferred trades are below all-trade totals because many active names have
`TrdType 101/102` rows that are not fully represented by the current book
builder output.

The result is good enough for a first AH BBO/bar builder, but not yet a complete
proof that the cash bookbuilder exactly reproduces every exchange trade type.
Further inspection showed that many apparent per-trade mismatches are grouping
and timestamp-bucketing differences. Raw `Trade` can split a single matching wave
into many rows with the same `TradeTime` and price, while the OBO builder can
emit one larger inferred trade for that wave. `TradeTicker` is more aggregated
than `Trade`, but it is still a reference feed rather than the current notional
source.

The current AH bar definition is therefore:

```text
HK BBO:      reconstructed from OMDC order-by-order book builder
HK notional: raw OMDC Trade rows where TrdType in (0, 100)
             and HKT 09:30:00 <= time < 16:00:00
Exclude:     TrdType 101/102/4/22 and other non-regular trade types
```

This definition intentionally matches the regular executable book notional. It
does not attempt to reproduce full `Statistics.Turnover`, which also includes
nonregular trade types.

For later HK bookbuilding work, the remaining validation gap is to debug the
symbols where total inferred regular trade and raw `Trade` 0/100 are not exact.
For the first AH bar/backtest path, use raw `Trade` 0/100 for HK notional and the
bookbuilder only for BBO.

## AH 5-Minute Bar Smoke Test

First end-to-end A/H bar build was run on `20260421` for two high-H-turnover
pairs from the AH workbook:

```text
CATL   A 300750   H 03750
SMIC   A 688981   H 00981
```

Output:

```text
AH file/ah_bar_smoke_20260421_top2_5min.csv
```

Bar definition:

```text
Bar size:        5 minutes
Trading window: AH overlap only
                 09:30-11:30 and 13:00-15:00
A BBO:           A-share L1 BidPrice1/OfferPrice1
A notional:      A-share L1 TotalTradeValue first difference
H BBO:           OMDC OBO bookbuilder best bid/ask
H notional:      raw OMDC Trade where TrdType in (0, 100)
FX:              not included in this smoke file; H prices remain HKD
```

Smoke result:

```text
Rows: 96 total, 48 bars per pair

300750 / 03750:
  A notional sum: 24,756,620,000 A-side raw currency
  H notional sum:  1,618,405,000 HKD
  H trade qty:        2,208,700 shares

688981 / 00981:
  A notional sum:  3,913,451,000 A-side raw currency
  H notional sum:  1,795,663,000 HKD
  H trade qty:       29,931,000 shares
```

This smoke test confirms that the required ingredients for new AH bar creation
are available from NAS H5:

```text
A-side BBO
A-side notional
H-side reconstructed BBO
H-side regular executable notional
```

The next production step is to add FX conversion and run the same bar builder
across a larger liquid AH universe before feeding it into the RF5/cum-auto
baseline backtest.

Important architecture rule:

```text
Layer 1: build and store A/H base bars independently.
         A-side prices stay in CNY.
         H-side prices stay in HKD.
         A/H base bars do not depend on any FX source.

Layer 2: merge A/H base bars with FX.
         Convert the selected CNH futures price to estimated spot USDCNH first.
         Only this layer computes H CNY price and AH ratio.
```

This separation is intentional. If the CNH source, futures roll logic, or
future-to-spot conversion changes later, rebuild only the FX merge/ratio layer;
do not rebuild the A/H base bars.

## FX Source

Use Databento CME CNH 1-minute BBO for `USDCNH`:

```text
Raw:
  /mnt/nas2/ken_databento_fx_cnh_bbo1m_raw/

Daily H5:
  /mnt/nas2/ken_databento_fx_cnh_bbo1m_h5/cme.YYYYMMDD.h5

Dataset:
  /CNH*/bbo-1m
```

The FX conversion should use the quote-weighted BBO mid from:

```text
bid_px_00
ask_px_00
bid_sz_00
ask_sz_00
```

Align by `ts_recv` converted from UTC to `Asia/Shanghai`. For each AH 15-minute
bar, use the latest CNH BBO row at or before the bar end. The bar builder uses a
freshness limit and skips the date if CNH BBO is missing or stale.

Do not download or rely on `mbp-10` for AH FX conversion. It is much larger than
needed. `ohlcv-1m` and `trades` are cheap but trade-sparse, so they can produce
stale last-trade values during AH trading hours.

### OMDD USD/CNH Mapping

OMDD also contains HKEX USD/CNH derivatives. The mapping is not by the visible
symbol such as `CUSM6`; the actual H5 group key is `SeriesDefinitionBase.OrderBookID`.

For a NAS daily file:

```text
/mnt/nas2/onboard1/raw_hdf5_b/sse/YYYYMMDD/hkex.omdd.YYYYMMDD.h5
```

Read:

```text
/SeriesDefinitionBase/SeriesDefinitionBase
/SeriesDefinitionExtended/SeriesDefinitionExtended
/CommodityDefinition/CommodityDefinition
/ClassDefinition/ClassDefinition
```

Mapping rule:

```text
SeriesDefinitionBase.Symbol starts with CUS or MCS
PutOrCall == 0
CommodityDefinition.CommodityName == USD/CNH
Use SeriesDefinitionBase.OrderBookID as the H5 group key
```

2026-04-21 examples:

```text
CUSK6  OrderBookID=197621  expiry=20260518
CUSM6  OrderBookID=721909  expiry=20260615
CUSN6  OrderBookID=1115125 expiry=20260713
CUSU6  OrderBookID=328693  expiry=20260914
CUSZ6  OrderBookID=132085  expiry=20261214
MCSK6  OrderBookID=201889  expiry=20260518
MCSM6  OrderBookID=529569  expiry=20260615
```

On 2026-04-21, the highest-volume full-size USD/CNH futures contract was:

```text
CUSM6 / OrderBookID=721909 / Trade rows=7,363 / Quantity=18,039 / VWAP=6.788044
```

The raw OMDD price field uses 4 decimals for CUS/MCS, so divide by `10000`.
For example raw price `67888` means `6.7888`.

Existing HK derivatives bookbuilding helpers:

```text
/home/ken/temp/forAlex/newHK_v6_alex_use_sent_addSanity/hkex_loader_deri.py
/home/ken/temp/forTesting/newHK_v6_alex_use_sent_addSanity/build_hk_fut_bbo.py
```

The loader expects the `OrderBookID` string as its `symbol` argument. It reads
the group directly and returns `AddOrder`, `DeleteOrder`, `Trade`, and a sorted
guide.

For AH FX conversion, do not rebuild an OMDD order book for USD/CNH. Use the
selected main CUS contract's trade stream directly:

```text
/OrderBookID/Trade
USDCNH_FUT = Trade.Price / 10000
timestamp = Trade.TradeTime
```

For each AH bar, take the latest trade at or before the bar end. The FX merge
should keep the last-trade age and apply a freshness threshold so a stale FX
print does not silently flow into ratio calculation.

Do not use the raw CNH futures price directly as the AH conversion FX. Futures
contracts can jump on roll dates because different expiries include different
interest-rate carry. Before computing the AH ratio, convert the selected main
contract's futures price into an estimated spot price:

```text
T = days_to_expiry / 365
USDCNH_SPOT_EST = USDCNH_FUT * (1 + USD_3M_RATE * T) / (1 + CNH_3M_RATE * T)
```

This is the simple covered-interest-parity adjustment for a USD/CNH quote
priced as CNH per USD. Keep both fields in the merged output:

```text
USDCNH_FUT
USDCNH_SPOT_EST
usdcnh_contract
usdcnh_order_book_id
usdcnh_expiry
usdcnh_last_trade_age_sec
USD_3M_RATE
CNH_3M_RATE
```

AH conversion should use `USDCNH_SPOT_EST / USDHKD`, not `USDCNH_FUT / USDHKD`.

Maintain a daily official 3-month rate input with at least:

```text
date,USD_3M_RATE,CNH_3M_RATE
20260421,0.0430,0.0210
```

Rates should be annualized decimals. Use the latest available rate at or before
the trading date. The exact official source still needs to be pinned down before
this becomes the production FX baseline.

Official 3-month-rate source documentation and updater:

```text
FX_3M_RATE_SOURCES.md
ah_update_3m_rates.py
AH file/fx_3m_rates_official.csv
```

Current baseline sources:

```text
USD_3M_RATE: New York Fed SOFR 90-day Average, API field average90day / 100
Default CNH futures carry input: CNY_SHIBOR_3M_RATE from CFETS Shibor 3M / 100
Reference only when available: CNH_3M_RATE from TMA CNH HIBOR 3M fixing / 100
```

The TMA public benchmark page exposes only the latest displayed CNH fixing
dates, so the updater should be run regularly to preserve a local daily archive.

The updater also fetches official onshore CNY Shibor 3M history from CFETS:

```text
Endpoint:
  https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis

Field:
  3M / 100 -> CNY_SHIBOR_3M_RATE

Coverage:
  The public endpoint supports historical date-range queries, but only up to
  one year per request. The updater chunks long requests automatically.
```

This project uses `CNY_SHIBOR_3M_RATE` as the default carry input because it has
stable official history and is easy to maintain. `CNH_3M_RATE` remains in the
rate file for comparison and sensitivity checks when available.

Generated bar-level `USDCNH_SPOT_EST` history:

```text
Script:
  ah_build_usdcnh_spot_bars.py

Remote output:
  ~/temp/ah_shares_run/output/usdcnh_spot_15m_20250401_20260421/

Files:
  usdcnh_spot_15m_YYYYMMDD.parquet
  manifest.csv

Rate column:
  CNY_SHIBOR_3M_RATE

Formula:
  USDCNH_SPOT_EST = USDCNH_FUT * (1 + USD_3M_RATE * T) / (1 + CNY_SHIBOR_3M_RATE * T)
```

Current generation result:

```text
Requested date range: 2025-04-01 to 2026-04-21
OMDD standard files available from: 2025-07-01
OK FX spot days: 193
Parquet files: 193
Missing OMDD days: 183
Stale or incomplete trade days: 6
```

Example 2026-04-21:

```text
Contract: CUSM6 / OrderBookID=721909 / Expiry=2026-06-15
Days to expiry: 55
USD_3M_RATE: 0.0366868
CNY_SHIBOR_3M_RATE: 0.01442
09:45 USDCNH_FUT=6.7891, USDCNH_SPOT_EST=6.811830
Max last-trade age: 21.71 seconds
```

Roll-adjustment sanity test:

```text
Script:
  ah_test_usdcnh_roll_adjustment.py

Remote output:
  ~/temp/ah_shares_run/output/usdcnh_roll_adjustment_test_20250401_20260421/

Files:
  usdcnh_cus_contract_daily_volume.csv
  usdcnh_cus_daily_main.csv
  usdcnh_roll_adjustment_comparison.csv
  usdcnh_roll_adjustment_summary.csv
```

The first sanity run used constant rates `USD_3M_RATE=0.043` and
`CNH_3M_RATE=0.021`, only to test whether the formula removes futures-roll
carry gaps. On the available standard OMDD daily files from 2025-07-01 through
2026-04-21, the old/new main-contract comparison showed:

```text
Roll switches tested: 28
Compared bar pairs:   4,373
Mean raw futures old/new absolute gap:       42.03 bp
Mean spot-adjusted old/new absolute gap:      4.23 bp
Mean absolute-gap reduction after adjustment: 37.81 bp
```

Representative roll windows:

```text
2025-12-12 CUSZ5 -> CUSH6: raw abs gap 53.74 bp, spot-adjusted 0.95 bp
2025-12-15 CUSH6 -> CUSF6: raw abs gap 31.98 bp, spot-adjusted 1.45 bp
2026-03-13 CUSH6 -> CUSM6: raw abs gap 55.30 bp, spot-adjusted 1.68 bp
2026-03-16 CUSM6 -> CUSJ6: raw abs gap 39.36 bp, spot-adjusted 1.77 bp
```

This strongly supports using future-to-spot conversion before AH ratio
calculation. Re-run the same script with the official daily 3-month rates before
locking final production numbers.

2026-04-21 `CUSM6 / 721909` smoke test:

```text
First trade: 2026-04-21 08:31:19.920 Asia/Shanghai
Last trade:  2026-04-21 16:05:00.550 Asia/Shanghai
AH 15-minute bar-end max last-trade age from 09:45 to 15:00: 3.21 minutes
Bars older than 10 minutes: 0
```

The existing derivatives bookbuilder was still useful for proving the mapping,
but it is not required for the CNH input.

For `USDHKD`, use previous-day daily reference rates. Current local file:

```text
AH file/fx_usdhkd_frankfurter_20250320_20260421.csv
```

## H5 Reading Note

Remote Python reads require `hdf5plugin` before opening compressed datasets:

```python
import hdf5plugin
import h5py
```

Without `hdf5plugin`, `h5py` can list structure but fails when reading rows.

## Corporate-Action Adjustment

The raw A/H H5 bars are not enough for final AH ratio work because splits,
bonus shares, rights issues, and dividends can create mechanical ratio jumps.
The project rule is documented in:

```text
AH_CORP_ACTION_ADJUSTMENT.md
```

In short, the ratio layer should retain raw prices for diagnostics but compute
the strategy ratio from adjusted intraday prices:

```text
a_mid_adj_cny = a_mid_raw_cny * a_norm_adj_factor
h_mid_adj_hkd = h_mid_raw_hkd * h_norm_adj_factor
h_mid_adj_cny = h_mid_adj_hkd * USDCNH_SPOT_EST / USDHKD
ah_ratio_adj  = a_mid_adj_cny / h_mid_adj_cny
```

If an external data source provides adjusted close but not an explicit factor,
infer a project-consistent daily factor by matching that adjusted close to this
project's own H5 raw daily close, then normalize the factor to the test-window
anchor date.
