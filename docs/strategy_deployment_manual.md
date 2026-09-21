# LongVol v0.6.1 US Panic-Dislocation Rebound Strategy

> **Superseded by v0.7.** This file is retained as a historical release record. Current executable rules, conditional Monte Carlo, dual risk budgets, and point-in-time news workflow are documented in [`strategy_manual_v0.7.md`](strategy_manual_v0.7.md).

## Production Deployment, Rules, and Research Manual

Application version: 0.6.1  
Strategy-rule version: 0.6.0  
Date: 2026-09-10  
Scope: After-close research plus guarded intraday Moomoo execution  
Execution boundary: paper by default; live requires three independent approvals

> LongVol v0.6 removes the screenshot, image-upload, and OCR pipeline. Moomoo Stock Screening V2 supplies the equity universe; Option Screening reduces the surface; the Moomoo trading API supplies USD funds, positions, orders, and guarded order mutations. During cold start, a same-day Moomoo IV percentile replaces only the local-IV warm-up gate and sizing uses fixed risk without Kelly. The system switches to validated Kelly only after local data and approved out-of-sample evidence pass every integrity check.

Runtime dependencies are pinned to OpenD `10.10.7008`, `moomoo-api==10.10.7008`, and `protobuf==5.29.5`. OpenD 10.7 can accept connections and serve the legacy option-chain endpoint, but it does not support the Option Screening V2 protocol used here and returns `NN_ProtoRet_SvrFailed`. Moomoo 10.10.7008 screening parsers also still access `FieldDescriptor.label`, which protobuf 7.x no longer exposes. `healthcheck` verifies both the OpenD and Python dependency versions instead of treating socket connectivity as full readiness.

# 1. Purpose and boundaries

LongVol uses long calls to express directional convexity after a panic dislocation. It is not a delta-neutral volatility-arbitrage strategy. A correct directional thesis can still lose because of IV crush, theta, bid/ask spread, or slippage.

The system addresses candidate omission, rule drift, incomplete journaling, and discretionary movement of stops. It is responsible for:

- automated coarse screening, tracking, and candidate expiry;
- market-data synchronization, timestamps, and data-quality checks;
- panic, compression, volume-profile, short-pressure, and fundamental gates;
- option-surface checks, contract selection, conservative scenario pricing, and an automatic sizing state machine;
- a layered exit state machine for actual positions;
- a next-session intraday execution-quality and safety layer;
- paper/live account isolation, idempotent DAY limit orders, cancellation, reconciliation, and terminal-fill materialization;
- audit records for configuration, features, signals, accounts, broker orders, fills, position lifecycle, and failures.

[PAGE BREAK]

It deliberately does not:

- infer a Kelly probability from a language model or an in-sample point estimate;
- convert HKD to USD or count foreign-currency balances as US-option bankroll;
- guarantee the execution price of a stop;
- recreate a historical option backtest from today's chain;
- infer dealer net-gamma direction from ordinary open interest;
- allow a language model to determine a trade or position size;
- behave as a high-frequency system or replace a broker-side emergency control.

[PAGE BREAK]

## 1.1 Three meanings of “production”

- Engineering production: repeatable runs, locking, fail-closed behavior, auditability, broker reconciliation, and recoverable backups. v0.6 targets this level.
- Strategy validation: the same rules retain positive net expectancy out of sample after realistic costs. This still requires historical research.
- Automated execution: v0.6 contains a deliberately narrow order layer. It still requires paper acceptance, operational monitoring, an external kill switch, and a tested manual broker fallback before REAL mode.

# 2. Architecture and daily flow

Every daily stage shares one `as_of` date:

1. `account-sync` resolves one explicit US account, reads USD funds, broker positions, current orders, and 30 days of order history, reconciles known LongVol orders, and materializes terminal fills.
2. `get_stock_screen` filters on price, market capitalization, five-day decline, and volume ratio, and retrieves the minimum required fields.
3. Local code calculates 20-day average dollar volume and merges `data/tracked.csv`. Non-position candidates expire after 30 days; OPEN holdings do not.
4. OpenD preflights historical K-line quota before any file write, then synchronizes daily bars, up to 90 calendar days of five-minute bars, short data, Option Screening, and Option Underlying Rank.
5. Option Screening retrieves `OI ≥ 10` and `daily volume ≥ 10` subsets and deduplicates them by option code. Normal same-day runs refresh tradable calls with Market Snapshot; a pre-open catch-up refreshes the full retained union and keeps only contracts timestamped on `as_of`. The vendor IV percentile is stored separately and never backfills local near-ATM IV history.
6. OpenAI fundamental research may use only information published on or before `as_of`, returning strict JSON and source URLs.
7. `scan` applies data, panic, stabilization, geometry, short, fundamental, option, automatic sizing, and portfolio gates using broker-synchronized USD capital.
8. `exits` applies deterministic priorities to every OPEN position. The next session, `intraday-monitor` checks execution conditions and risk exits without changing the thesis.
9. SQLite and CSV preserve signals, order intents, remote states, terminal fills, and post-fill positions. A submitted order is never assumed to be a fill.

There is no screenshot fallback. If the Screener capability or permission is unavailable, the run fails visibly instead of silently switching to unverified data.

## 2.1 Decision time versus execution time

The after-close engine decides whether a setup deserves capital. The intraday layer only decides whether the frozen setup is still executable. This separation prevents spread noise or a single intraday tick from rewriting the research rule while still addressing the practical fact that option spreads vary materially through the session.

Entries are eligible only in the next-session window, 10:00–15:30 New York time by default. Quotes require a source timestamp no older than 20 seconds. A missing, stale, one-sided, or excessively wide market blocks entry. Underlying stop breaks, excessive upside chasing, an invalid live option payoff scenario, or signal expiry cancel the entry.

Hard exits—expiry risk, underlying hard stop, and time limit—are not vetoed merely because the option spread is wide. Price-sensitive premium stops and target exits wait for a usable two-sided market. If no positive bid exists, the system alerts rather than recording a fictitious exit.

# 3. Why sequential gates, not a weighted score

The project does not yet have enough point-in-time observations to estimate stable feature weights. A total score would let unrelated strengths offset veto conditions: an extreme panic score could hide an untradeable spread, or technical strength could hide a permanent valuation break.

The gate sequence makes each assumption falsifiable:

1. establish a price-and-flow dislocation;
2. require independent volatility or option-stress evidence;
3. wait for selling-pressure compression and reversal;
4. verify overhead supply, support, and target room;
5. reject short-pressure, fundamental, option-price, Kelly, and portfolio failures.

After a sufficiently large point-in-time sample exists, a probability model may rank candidates that already pass the hard gates. Freshness, thesis break, liquidity, and risk caps should remain vetoes.

# 4. Automated coarse screening

## 4.1 Stock Screening V2

The coarse screen favors recall over precision. Defaults are:

| Condition | Default | Purpose |
|---|---:|---|
| Market | US equities | Stable market and data semantics |
| Price | ≥ USD 5 | Reduce microstructure noise in very low-priced stocks |
| Market cap | ≥ USD 1 billion | Reduce financing, delisting, and manipulation risk |
| Five-day change | ≤ -8% | Capture an acute decline |
| Volume ratio | ≥ 2.0 | Capture abnormal flow |
| 20-day average dollar volume | ≥ USD 20 million | Preserve underlying liquidity |
| Maximum results | 200 | Bound downstream cost |

Stock Screening expresses the first five conditions and retrieves price and 20-day average volume. Local code calculates average dollar volume. The exact 60-day peak-to-current drawdown is computed from daily bars rather than a similar vendor factor. Equity-level IV/HV is not redundantly retrieved here; volatility features come from the option surface.

Stock Screening V2 is paginated. The official limit is ten calls per 30 seconds for the same endpoint. The adapter rate-limits each endpoint independently so stock and option screening do not unnecessarily block unrelated quote calls.

## 4.2 Tracked universe

`data/tracked.csv` is a rolling research universe, not a permanent watchlist. A non-position symbol expires when `last_seen` is older than `tracked_max_age_days=30`. A symbol with a real OPEN position stays protected until a real `CLOSE/EXIT` fill updates the position file.

[PAGE BREAK]

## 4.3 Minimum-sufficient Option Screening union

An OI-only screen can miss new protection flow; a volume-only screen can omit the stable surface. Each underlying therefore uses two server-side queries:

- surface subset: `open_interest ≥ option_surface_min_open_interest`;
- flow subset: `volume ≥ option_flow_min_volume`.

Rows are deduplicated by option code. The union is capped at 4,000 contracts by default and an oversized response fails instead of truncating silently. The API returns at most 200 rows per page, and the adapter honors its ten-calls-per-30-seconds limit.

Option Screening exposes the current surface and does not timestamp individual rows. In a normal same-day run, local DTE, delta, OI, and volume filters identify possible calls; only those calls receive a Market Snapshot refresh for executable bid/ask, IV, Greeks, and lot size. In a prior-session catch-up before 09:00 New York time, every retained contract is refreshed and only contracts whose Market Snapshot `update_time` date exactly equals `as_of` survive. This strict path is a bounded catch-up for the latest completed session, not historical replay.

[PAGE BREAK]

# 5. Panic and dislocation assumptions

## 5.1 Panic is more than a large decline

A normal decline may reflect a permanent change in earnings, funding, or the valuation anchor. The strategy seeks a temporary dislocation created by concentrated selling or protection demand. Price-and-flow evidence is necessary, plus one independent confirmation.

The default price-flow gate requires:

- 60-day drawdown ≤ -20%;
- five-day drawdown ≤ -8%;
- recent shock-day volume z-score ≥ 1.5;
- at least one of: shock return ≤ -5%, gap ≤ -2%, range/ATR ≥ 1.5, or a forced-liquidation proxy.

The forced-liquidation proxy combines abnormal volume, wide range, a close near the low, and a material decline. Daily bars cannot identify margin calls, redemptions, or seller identity, so the feature is explicitly labeled as a proxy.

## 5.2 Why Yang–Zhang remains a feature

Yang–Zhang uses open, high, low, and close to separate overnight gaps, open-to-close variance, and the Rogers–Satchell intraday range term. Because panic often contains both an overnight gap and a wide intraday range, it carries more information than close-to-close realized volatility.

It is neither downside volatility nor an entry signal. The system uses `short-window YZ / prior-window YZ ≥ 1.25` plus a downside-return share of at least 60% as only one independent confirmation. Research must run estimator ablations; if Yang–Zhang adds no stable out-of-sample value, it should become a logged diagnostic instead of a gate.

## 5.3 Option-stress evidence

The option-panic features are:

- a near-ATM IV jump versus the prior observation;
- 25-delta put IV above call IV;
- near-term ATM IV above far-term ATM IV;
- an abnormal put/call volume ratio and put-volume share.

At least two signals are required. Volume does not reveal aggressor direction, so put volume is not described as confirmed put buying. Earnings and other dated events can also create a local term-structure inversion and must be interpreted by the fundamental stage.

## 5.4 Combined panic gate

`price_flow=true` and at least one of the following must hold:

- Yang–Zhang plus downside confirmation;
- option-panic confirmation.

Failure produces `NO_TRADE`. Success makes the symbol eligible for `WATCH` or a later `BUY_CANDIDATE`.

# 6. Entry timing: compression and reversal

The strategy does not buy during an expanding selloff. Compression can become true only after `panic_confirmed=true` and is anchored to the most recent shock day on which abnormal volume and a price/range shock occurred on the same daily bar. By default, at least two complete sessions must elapse after that shock.

Selling-pressure exhaustion requires:

- mean volume over up to five post-shock sessions divided by shock-day volume at or below 0.85;
- either 10-day ATR divided by the preceding 20-day mean true range at or below 0.85, or 10-day realized volatility divided by the preceding 20-day realized volatility at or below 0.85;
- post-shock volume decay is mandatory and at least one of ATR/RV must pass. The ordinary five-day/20-day `volume_ratio` remains a diagnostic and is not a substitute for event-anchored decay.

The latest bar must then also satisfy:

- close above the prior close;
- close location at or above 0.65;
- the latest low does not break the lows of the prior two observations.

This is a transparent, fail-closed exhaustion proxy, not a bottom forecast. A high-volume negative return may itself contain the liquidity-shock signal, so volume need not fall below its pre-event normal level; it must decay after the shock.

The literature supports short-term reversal after liquidity shocks and separating fundamental news from non-fundamental price pressure, but it does not establish 0.85 or two sessions as universally optimal. Nagel (2012) interprets short-term reversal returns as compensation for liquidity provision; Campbell, Grossman, and Wang (1993) link high-volume declines to higher expected returns; Da, Liu, and Schaumburg (2014) find stronger reversal in returns unexplained by fundamental cash-flow news; Cox and Peterson (1994) warn that short-run reversals after extreme declines may reflect liquidity and bid-ask bounce while longer-run performance remains poor. The configured values are conservative priors that still require walk-forward/OOS ablation:

- https://doi.org/10.1093/rfs/hhs066
- https://doi.org/10.3386/w4193
- https://doi.org/10.1287/mnsc.2013.1766
- https://doi.org/10.1111/j.1540-6261.1994.tb04428.x

[PAGE BREAK]

# 7. Volume profile, price vacuum, and target

Production entry requires at least 100 five-minute bars from the panic leg. Each small bar's volume is distributed uniformly across its covered price rows. The engine calculates:

- point of control;
- 70% value area;
- local high-volume nodes;
- volume share and density in the 10% price band above spot;
- ATR distance to profile support, resistance, and target.

The default price-vacuum gate requires overhead share ≤ 25%, overhead density ≤ 75% of the profile average, target room ≥ 1 ATR, and profile support within 0–3 ATR. A daily-bar profile is diagnostic only and cannot pass the production gate.

The frozen target is the nearest reachable upper HVN, value-area low, POC, or material gamma concentration. If none exists, the fallback is 2 ATR. A target must provide at least 1 ATR of room.

# 8. Correct semantics for short and gamma data

## 8.1 Short data

FINRA daily short volume is reported transaction flow, not unclosed short interest. LongVol records separately:

- the change between the two latest published short-interest observations;
- daily short-volume ratio relative to its recent or 20-day baseline.

At least one series must weaken and neither may materially worsen. Short interest is valid for at most 21 days and daily flow for at most four days by default. Stale data cannot pass the gate.

## 8.2 Gamma concentration proxy

An ordinary chain provides strike, gamma, OI, and multiplier but not position direction or dealer counterparty. The system computes by strike:

`abs(gamma) × OI × multiplier × spot² × 1%`

A strike is material only when it represents at least 5% of absolute proxy exposure in the screened union. It may be a target or obstacle, never confirmed support, and it does not satisfy the support gate. Because the input is the Option Screening union, the quality label is `screened_union_dealer_side_unknown`.

# 9. Fundamental research

OpenAI is restricted to non-numeric classification: catalyst, possible valuation-regime break, and thesis status (`intact`, `uncertain`, or `broken`). The request requires:

- information published on or before `as_of` only;
- a date and direct URL for each material fact;
- preference for issuer, SEC, regulator, or court sources;
- `needs_review=true` when evidence is insufficient, conflicting, or of unclear timing;
- confidence capped at 0.49 when no valid source URL is returned.

Research is fresh for seven days by default and must have confidence ≥ 0.65 with no review flag. `catalyst_present` is logged but is not a hard gate. `valuation_regime_break=true` or `thesis_status=broken` is a veto. API storage is disabled for these requests.

# 10. Option surface and contract selection

## 10.1 Surface gates

The screened surface must satisfy:

- 25-delta put-call skew ≤ 20 volatility points;
- far-minus-near ATM IV between -10 and +15 volatility points;
- IV-regime percentile ≤ 85%: cold start requires an exact-`as_of` Moomoo vendor percentile, while validated mode requires a locally computed percentile from at least 60 prior near-ATM observations;
- near-ATM IV / Yang–Zhang volatility ≤ 2.25.

These conditions reduce the chance of buying vega while panic pricing remains extreme. Vendor percentile and local history remain separate data series; neither fills missing values in the other. The OI/volume union is a deliberate speed/coverage trade-off. Daily logs preserve contract count and thresholds so later research can measure truncation risk.

## 10.2 Tradable contract gate

The default instrument is a call:

| Condition | Default |
|---|---:|
| DTE | 45–150 days |
| Absolute delta | 0.25–0.45 |
| Open interest | ≥ 100 |
| Daily volume | ≥ 10 |
| Spread / mid | ≤ 10% |
| Ask / spot | ≤ 15% |
| Quote time | Same date as `as_of` |

Ranking favors a narrower spread, delta near 0.40, and DTE near 90. The restored 0.25–0.45 band expresses the original convex, lower-premium intent; it is not widened further because very low-delta calls become increasingly dependent on a large, fast move. Zero bid, ask below bid, invalid IV/Greeks, or a stale quote is a veto.

## 10.3 Conservative option scenarios

Position sizing requires option-level target and stop prices. The default Black–Scholes target scenario uses the frozen spot target and a 15% IV crush. The stop scenario uses the frozen spot stop and a 5% IV increase. A conservative execution haircut is applied to the target value.

These values are sizing scenarios, not executable quotes. A validated point-in-time surface model should override them and carry its own version identifier.

# 11. Automatic sizing, Kelly, currency, and small-account limits

`config/sizing.json` accepts only `mode=AUTO`, preventing an operator from forcing Kelly past the qualification checks. The mode is resolved independently for each symbol and date:

| Mode | IV gate | Sizing | Signal |
|---|---|---|---|
| `COLD_START_FIXED_RISK` | exact-date Moomoo IV percentile ≤ 85% | fixed stop risk; no probability | `PILOT_CANDIDATE` |
| `VALIDATED_KELLY` | at least 60 prior local near-ATM IV observations; percentile ≤ 85% | quarter Kelly using an approved lower-bound probability | `BUY_CANDIDATE` |

Cold-start per-contract loss is `(P-S)×m+2c`. Quantity is capped by 2.5% stop risk, 5% premium, and one contract. The entire cold-start portfolio is also capped at 2.5% frozen risk, 5% premium, and one OPEN position. This is not a substitute estimate for Kelly: it neither requires nor reads `p`, and it does not report a fictitious edge.

In validated mode, per-contract net loss and gain are `loss=(P-S)×m+2c` and `gain=(T-P)×m-2c`. With approved lower-bound win probability p and payoff ratio b=gain/loss, `full Kelly=p-(1-p)/b`. The engine applies quarter Kelly, capped at 3% stop risk, 8% premium, and two contracts. Portfolio caps are 9% frozen risk, 20% premium, three OPEN positions, and one per underlying.

Automatic transition does not mean automatic research approval. Validated Kelly requires all of the following: at least 60 local prior IV observations; a probability in (0,1); `approved=true`; `out_of_sample=true`; exact strategy-version match; a real study file with a matching SHA-256; a non-future sample end; at least 60 out-of-sample observation days; and at least 30 completed trades. Any failure returns the symbol to cold start and logs the blocker and mode transition. The application never generates the probability, approves the report, or uses vendor data to fill the local series.

The bankroll is not total multi-currency account equity. `account-sync` asks Moomoo for USD funds and defaults to `usd_net_cash_power`, then applies `strategy_equity_fraction` and a USD 2,500 dedicated-capital ceiling. Some US `STOCK_AND_OPTION` paper accounts return `us_cash` but omit per-currency `cashInfoList.netCashPower`, which the SDK represents as `usd_net_cash_power=N/A`. Only in `SIMULATE`, when the default field is unavailable, the system uses the explicit USD `us_cash` value as a conservative, non-margin bankroll source and records the resolved field and fallback reason. `REAL` remains fail-closed. HKD is not converted or counted. With USD 2,500, cold-start budgets are about USD 125 premium and USD 62.50 frozen stop risk. A whole contract that breaches either limit remains `WATCH`.

These percentages are risk-governance ceilings, not empirically optimal allocations. Long-option stops are not guaranteed, so full premium remains the economic tail loss even when planned stop risk is smaller.

[PAGE BREAK]

# 12. Signal states and complete exit rules

## 12.1 Signal states

- `NO_TRADE`: panic is absent or the valuation/thesis is broken.
- `WATCH`: panic exists, but stabilization, geometry, short, fundamental, option, sizing, or portfolio readiness is incomplete.
- `PILOT_CANDIDATE`: every hard gate passes in cold-start fixed-risk mode. It is still not a fill.
- `BUY_CANDIDATE`: every hard gate passes in validated-Kelly mode. It is still not a fill.

## 12.2 Immutable entry fields

A real `OPEN` must preserve entry spot and option price, entry ATR, hard stop, premium stop, target spot and option price, target source, entry IV and percentile, risk per contract, multiplier, expiry, maximum holding days, and the full entry feature snapshot.

The recorder rejects duplicate OPEN positions, a fill inconsistent with the frozen entry, missing stop/target/IV, understated initial risk, invalid dates, or a duplicate broker order ID. `REDUCE` cannot take a position to zero; `CLOSE/EXIT` must close all remaining contracts.

## 12.3 Exit priority

| Priority | Trigger | Default action |
|---:|---|---|
| 1 | EXPIRY_RISK: DTE ≤ 5 | EXIT |
| 2 | THESIS_BREAK | EXIT |
| 3 | UNDERLYING_HARD_STOP: daily low reaches the frozen stop | EXIT |
| 4 | OPTION_PREMIUM_STOP: executable bid reaches the frozen premium stop | EXIT |
| 5 | PROFIT_GIVEBACK: reached 1R, then returned to 0R | EXIT |
| 6 | TIME_STOP: frozen holding limit, default 30 days | EXIT |
| 7 | DTE_EXIT: DTE ≤ 21 | EXIT |
| 8 | IV_CRUSH: IV falls ≥ 25% before target R | EXIT |
| 9 | SHORT_PRESSURE_REACCELERATION while spot is below entry | EXIT |
| 10 | STRUCTURE_TARGET_REACHED | REDUCE; EXIT for one contract |
| 11 | OPTION_TARGET_REACHED or ≥ 1.5R | REDUCE; EXIT for one contract |

The underlying stop uses frozen entry ATR and cannot widen with current volatility. A long position is valued at executable bid, not midpoint. A one-contract reduction exits fully; multi-contract `REDUCE` proposes half, subject to the actual fill. Missing option bid cannot hide an expiry, thesis, underlying, or time-based exit; the system keeps `EXIT` and records `missing_inputs`. A stop trigger is not an execution guarantee and can suffer gap or liquidity slippage.

## 12.4 Broker order safety

Only `NORMAL` DAY limit orders are permitted. Every intent has a deterministic client order ID stored in Moomoo's `remark`; local and remote checks prevent duplicate submission. An entry is revalidated against a fresh option quote, synchronized USD capital, buying power, live spread, live ask drift, frozen option stop, and underlying no-chase/stop limits immediately before submission. The broker layer independently verifies signal status against sizing mode and re-enforces the cold-start one-contract, 5% premium, and 2.5% stop-risk caps.

The default `SIMULATE` environment requires a US `STOCK_AND_OPTION` paper account. `REAL` additionally requires `allow_live_orders=true`, `MOOMOO_TRADE_PASSWORD_MD5`, and the exact per-command confirmation `I_UNDERSTAND_LIVE_ORDERS`. Place/cancel calls are never automatically retried because a network timeout does not prove that the broker rejected the first mutation.

The monitor cancels a known active entry order when quotes disappear or become stale, the signal expires, or the underlying invalidates the setup. Terminal full or cancelled-part fills are reconciled from order state and materialized once into the local ledger. Moomoo paper trading does not provide deal, fee, or cash-flow APIs; paper fees are therefore configured estimates and the order's dealt quantity/average price are the fill source.

An active hard-risk exit that is no longer marketable is repriced to the latest positive bid. If an active target order has reserved sellable quantity when a hard exit appears, the target is cancelled first and the risk exit waits for the next poll, preventing two competing sell orders. Target exits are not chased through a widening spread.

# 13. Installation and operation

## 13.1 Prerequisites

- Python 3.11 or later;
- a running, logged-in local Moomoo OpenD;
- US equity and OPRA OpenAPI quote permissions;
- a US Moomoo trading account; paper mode requires `STOCK_AND_OPTION`;
- an OpenAI API key;
- a correctly synchronized system clock.

## 13.2 Local installation

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[openai,moomoo]'

cp .env.example .env
set -a; source .env; set +a

longvol healthcheck --data-dir data --state-dir state --opend --broker --research \
  --trading-config config/trading.json
PYTHONPATH=src python -m unittest discover -s tests -v
```

Direct dependencies are pinned to `openai==3.8.0` and `moomoo-api==10.10.7008`. Any upgrade requires retesting Screener requests, field mappings, pagination, rate limiting, and the complete regression suite.

## 13.3 Daily command

Run after the US close:

```bash
python scripts/run_daily.py \
  --config config/strategy.json \
  --sizing config/sizing.json \
  --trading-config config/trading.json
```

The default wrapper uses the current New York date and refuses a same-day close run before 17:00 New York time or on a weekend. `--allow-nonstandard-time` is reserved for an explicitly reviewed same-day exception such as an exchange half-day.

To catch up the most recently completed session, start before 09:00 New York time and supply the date explicitly:

```bash
python scripts/run_daily.py --as-of 2026-09-09 \
  --config config/strategy.json \
  --sizing config/sizing.json \
  --trading-config config/trading.json
```

Catch-up mode accepts only a recent weekday candidate within four calendar days. The exact daily bar and dated Moomoo regime data must prove the requested session. Stock Screening V2 is used only for candidate discovery. Every retained Option Screening contract is refreshed through Market Snapshot, and only rows whose New York `update_time` date equals `as_of` enter the option surface. Missing timestamps or zero verified contracts fail closed. Sizing uses the real current pre-open USD account snapshot; it never invents a historical balance. After 09:00 New York time, the system rejects a prior-session full-option catch-up.

Direct CLI:

```bash
longvol daily --as-of 2026-09-08 --start 2025-08-04 \
  --config config/strategy.json --sizing config/sizing.json \
  --trading-config config/trading.json
```

## 13.4 Component-level troubleshooting

```bash
longvol account-sync --trading-config config/trading.json \
  --output state/broker/account.json --local-positions data/positions.csv

longvol screener --as-of 2026-09-08 \
  --config config/strategy.json \
  --output data/candidate_inbox/2026-09-08.csv

longvol sync --symbols AAPL,MSFT --start 2025-08-04 --end 2026-09-08 \
  --kind all --data-dir data --config config/strategy.json

longvol research --symbol AAPL --packet research_packets/AAPL.txt \
  --as-of 2026-09-08 --output state/fundamentals/AAPL.json \
  --fundamentals-out data/fundamentals.csv

longvol scan --candidates data/candidate_inbox/2026-09-08.csv \
  --bars-dir data/bars --intraday-dir data/intraday --options-dir data/options \
  --option-history data/option_history.csv --option-regime data/option_regime.csv \
  --short data/short.csv --fundamentals data/fundamentals.csv \
  --sizing config/sizing.json \
  --account-state state/broker/account.json \
  --positions data/positions.csv --config config/strategy.json \
  --output state/signals/2026-09-08.csv

longvol exits --positions data/positions.csv \
  --signals state/signals/2026-09-08.csv --bars-dir data/bars \
  --options-dir data/options --as-of 2026-09-08 \
  --config config/strategy.json --output state/exits/2026-09-08.csv

# Dry-run one order; add --submit only after reviewing the output.
longvol place-order --intent data/order_intent_example.json \
  --trading-config config/trading.json

# One intraday dry-run; --submit --loop enables the paper monitor.
longvol intraday-monitor --signals state/signals/2026-09-08.csv \
  --positions data/positions.csv --trading-config config/trading.json \
  --output state/intraday/latest.json
```

## 13.5 Docker

Docker is optional. It pins the Python/SDK environment, isolates dependencies, and improves reproducibility; it does not create a trading advantage. A local virtual environment is simpler for initial OpenD troubleshooting. OpenD remains on the host and the container reaches it through `host.docker.internal`:

```bash
mkdir -p data state research_packets
docker compose build
docker compose run --rm longvol
```

On Linux, verify host resolution and set `LOCAL_UID/LOCAL_GID` so mounted files retain the intended owner. Inject secrets through environment variables; never bake them into the image.

[PAGE BREAK]

# 14. Trading log and research data

`state/trading_log.sqlite3` uses WAL mode, foreign keys, and a busy timeout. Principal tables are:

- `runs`: run type, source, model, status, config JSON/hash, metadata, and errors;
- `candidate_snapshots`: daily status, gates, full metrics, reasons, and selected contract;
- `feature_snapshots`: normalized daily research features;
- `sizing_mode_decisions`: per-symbol mode, qualification blockers, and evidence snapshot;
- `trades`: real fills, fees, order IDs, and post-event position state;
- `exit_decisions`: every trigger, missing input, and proposal without an assumed fill;
- `account_snapshots`: USD funds, selected equity field, account/environment, and risk flags;
- `broker_orders`: intent, remote status, dealt quantity/average, and fill-materialization state;
- `events`: data, API, and command failures.

Primary files are `state/signals/YYYY-MM-DD.csv`, `state/exits/YYYY-MM-DD.csv`, `data/option_history.csv`, `data/option_regime.csv`, `data/positions.csv`, and `data/tracked.csv`. Vendor regime and locally derived IV history are never merged.

Known terminal Moomoo fills are materialized automatically by `account-sync` and the submitted intraday monitor. `record-trade` remains the recovery/manual-fill path:

```bash
longvol record-trade --trade-json data/trade_open_example.json \
  --positions-out data/positions.csv --log-db state/trading_log.sqlite3
```

Export and back up research data:

```bash
longvol export-log --log-db state/trading_log.sqlite3 \
  --table all --output-dir research_exports/2026-09-08
sqlite3 state/trading_log.sqlite3 '.backup state/backups/trading_log_2026-09-08.sqlite3'
```

[PAGE BREAK]

# 15. Failure modes and monitoring

These failures block a new entry decision, while independently established safety exits remain visible:

- OpenD unreachable, missing permissions, or Screener failure;
- insufficient historical K-line quota for newly requested symbols;
- latest daily bar not equal to `as_of`;
- prior-session catch-up started at or after 09:00 New York time;
- option Market Snapshot missing or not timestamped on `as_of`;
- oversized option union, pagination error, or empty snapshot;
- option quote timestamp not equal to `as_of`;
- missing, stale, or excessive Moomoo vendor IV percentile while cold-start sizing is active;
- insufficient five-minute profile;
- stale short or fundamental data;
- invalid OpenAI structured output, source, or cutoff handling;
- SQLite or inter-process lock failure.
- ambiguous account selection, non-USD bankroll, local/broker position mismatch, or insufficient buying power;
- missing quote source time, entry spread above 12%, excessive ask drift, or a broken frozen stop;
- order mutation with unknown network outcome, reconciliation failure, or an unmaterializable terminal fill.

Monitor run success, candidate count, gate-failure distribution, sizing mode and transitions, Kelly blockers, API latency/retries, option-union size, quote age, spread distribution, cancel rate, order status age, fill slippage, local/broker position mismatches, unmaterialized fills, log growth, backup restoration, and differences between proposals and actual fills.

# 16. Validation path and parameter governance

## 16.1 Minimum credible data

A credible backtest requires point-in-time option bid/ask/IV/Greeks/OI, delisted equities, corporate actions, the actual trading calendar, publication timestamps for fundamental evidence, entry at ask, exit at bid, fees, and slippage. Current Option Screening supports forward paper tracking; it does not reconstruct a historical chain.

## 16.2 Research sequence

1. Freeze v0.6. Paper cold start may immediately collect real signals, features, and fills, but those records must never be presented as earlier history.
2. Audit data failure and selection bias before inspecting returns.
3. Run ablations for price-flow, Yang–Zhang, option panic, compression, profile, short, and fundamentals.
4. Use time-series walk-forward validation and event/sector subgroups.
5. Estimate a conservative lower confidence bound for win probability; never use the training point estimate in Kelly.
6. Change a threshold only when statistical, economic, and execution evidence agree, and increment the strategy version.

Do not repeatedly tune the same small sample, remove failed trades, or use news published after the historical decision. Config hashes and dated sources are designed to expose that research contamination.

## 16.3 Cold start and validation clocks

- Price data: first synchronization must return at least 80 daily bars and the panic-leg profile needs at least 100 five-minute bars. These may be ordinary, genuine Moomoo historical bars.
- IV cold start: no 60-day wait is required. The IV gate uses Moomoo Option Underlying Rank for the exact `as_of` date. A missing or stale value produces `WATCH`; it is not imputed.
- Local IV warm-up: every scan extracts and appends that day's near-ATM IV to `option_history.csv`. Validated mode reads at least 60 observations strictly before the current date—roughly 12 trading weeks.
- Strategy/Kelly validation: 60 IV dates do not establish a probability. The switch also requires at least 60 out-of-sample observation days, 30 completed trades, an approved lower-bound probability, a real report hash, and exact version match. Rare setups may take materially longer.

Paper operation can therefore start immediately after data-quality checks pass. Early live operation, if separately authorized after paper fault testing, remains constrained to `PILOT_CANDIDATE` fixed-risk sizing. “Automatic” means recognizing already approved evidence, never self-approving it.

[PAGE BREAK]

# 17. Deployment acceptance checklist

- Stock Screening V2, Option Screening, and market snapshot pass a smoke test with the actual OpenD account permissions.
- Pagination, rate limiting, retries, oversized results, and empty responses fail closed.
- No screenshot, image-upload, or OCR command or code path remains.
- Latest daily bar and option timestamp match `as_of`.
- Compilation, unit tests, scan smoke test, and exit smoke test pass.
- OPEN frozen fields are complete; duplicate orders and illegal position transitions are rejected.
- An underlying hard stop still returns `EXIT` when the current option bid is missing.
- SIMULATE account selection, USD funds, DAY limit submission, invalid-entry cancellation, partial terminal fill, restart idempotency, and local/broker reconciliation pass fault-injection tests.
- `REAL` remains disabled until an operator separately enables it; a missing password or confirmation phrase blocks every mutation.
- SQLite backup and restore are tested.
- A cold-start signal can only be `PILOT_CANDIDATE`, and the broker independently enforces one contract, 5% premium, and 2.5% stop risk.
- Vendor IV and local near-ATM IV are persisted separately, with no mutual backfill.
- Before Kelly, local IV count, probability, approval, out-of-sample flag, report SHA-256, version, sample end, observation days, and completed trades all pass.
- Every sizing selection and transition is recoverable from SQLite.
- Paper operational acceptance completes before REAL mode is considered; insufficient strategy evidence keeps REAL in cold-start limits.

[PAGE BREAK]

# 18. Primary references

- [Moomoo Stock Screening V2](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-stock-screen.html)
- [Moomoo Option Screening](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-screen.html)
- [Moomoo Option Underlying Rank](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-underlying-rank.html)
- [Moomoo Option Underlying Historical Statistics](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-underlying-his-statistic.html)
- [Moomoo Option Chain](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-option-chain.html)
- [Moomoo Market Snapshot](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-market-snapshot.html)
- [Moomoo Historical K-line](https://openapi.moomoo.com/moomoo-api-doc/en/quote/request-history-kline.html)
- [Moomoo Daily Short Volume](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-daily-short-volume.html)
- [Moomoo Permissions](https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html)
- [TradingView Volume Profile concepts](https://www.tradingview.com/support/solutions/43000502040-volume-profile-indicators-basic-concepts/)
- [FINRA Short Sale Volume User Guide](https://www.finra.org/sites/default/files/2020-12/short-sale-volume-user-guide.pdf)
- [Yang and Zhang, “Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices”](https://doi.org/10.1086/209650)
- [OCC Characteristics and Risks of Standardized Options](https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document)
- [OpenAI API Production Best Practices](https://developers.openai.com/api/docs/guides/production-best-practices)
- [Moomoo Historical K-line Quota](https://openapi.moomoo.com/moomoo-api-doc/en/quote/get-history-kl-quota.html)
- [Moomoo Paper Trading](https://openapi.moomoo.com/moomoo-api-doc/en/qa/trade.html)
- [Moomoo Account List](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-acc-list.html)
- [Moomoo Funds](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-funds.html)
- [Moomoo Place Order](https://openapi.moomoo.com/moomoo-api-doc/en/trade/place-order.html)
- [Moomoo Order List](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-order-list.html)
- [Moomoo Modify or Cancel Order](https://openapi.moomoo.com/moomoo-api-doc/en/trade/modify-order.html)
- [Moomoo Historical Orders](https://openapi.moomoo.com/moomoo-api-doc/en/trade/get-history-order-list.html)

[PAGE BREAK]

# 19. Conclusion

v0.6.1 encodes two requirements at once: begin collecting real paper data quickly, while never inventing history or a Kelly probability. Vendor IV supports an auditable cold start, local IV accumulates separately, fixed-risk caps protect early capital, and complete approved evidence unlocks Kelly automatically. Pre-open catch-up still requires source timestamps for every option row used; current screening values are never relabelled as historical data. The next priority is leakage-free signals, spreads, cancellations, fills, and outcomes—not looser gates.
