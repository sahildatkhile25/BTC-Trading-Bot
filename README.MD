# BTC ATM Dual-Side Breakout Strategy
### Delta Exchange India · Python · TradingView WebSocket

An automated options trading bot that simultaneously watches the nearest ATM **Call** and **Put** on Delta Exchange India, fires on a breakout pattern, and manages the full trade lifecycle — entry, bracket order, and exit — with pure USDT sizing.

---

## How It Works

```
Startup
  └── Fetch live BTC spot price from Delta API
  └── Auto-select nearest ATM CALL + PUT (same expiry)
  └── Open two TradingView WebSocket feeds (one per leg)

Each leg independently watches for:

  🔴 Closed RED candle
       ↓
  🟢 Closed GREEN candle
       ↓
  📈 LTP breaks above green-candle HIGH  ← trigger

First leg to trigger:
  ✅ Market buy on that option
  📌 Bracket order placed (target +2%, stop = red-candle low)
  ❌ Other leg cancelled immediately

On trade exit (target or stop):
  🔄 Both legs re-arm and resume scanning
```

---

## Features

- **Auto ATM selection** — strike nearest to spot is picked at runtime, no manual input
- **Dual WebSocket feeds** — each leg has its own TradingView connection (`DELTA:C-BTC-60000-290626`)
- **Real-time candle display** — closed bars print permanently; live forming bar updates in-place on one line
- **Pure USDT sizing** — `lots = floor(CAPITAL_USDT / option_LTP)`, no BTC conversion
- **Bracket orders** — target and stop placed on exchange automatically in live mode
- **Paper mode by default** — zero risk until you flip `LIVE_TRADING=true`
- **Thread-safe** — shared position lock ensures only one trade can be active at any time
- **Auto-reconnect** — WebSocket feeds reconnect on disconnect

---

## Terminal Output

```
  [C-BTC-60000-290626] 09:14 IST 🔴 O=530 H=534 L=522 C=524 V=12  #1
  [C-BTC-60000-290626] 09:15 IST 🟢 O=524 H=531 L=521 C=529 V=18  #2
  [C-BTC-60000-290626] 09:15 IST 🔔 Signal → red_low=522  green_high=531  watching for breakout above 531
  [C-BTC-60000-290626] 09:16 IST 🟡 LIVE O=529 H=533 L=528 C=532 🔔 SIGNAL   ← updates in-place

════════════════════════════════════════════════════════════
  ✅ ENTRY [C-BTC-60000-290626]
     Lots : 1  @  532 USDT
     Target : 542.64 USDT  |  Stop : 522 USDT
     Capital used ≈ 532 USDT
     Max gain ≈ 10.64 USDT  | Max loss ≈ 10 USDT
════════════════════════════════════════════════════════════
```

---

## Installation

**Requirements:** Python 3.10+

```bash
pip install requests websocket-client
```

---

## Usage

### Paper Mode (default — no real orders)
```bash
python delta_btc_atm_dual_strategy.py
```

### Live Mode
```bash
DELTA_API_KEY=your_key \
DELTA_API_SECRET=your_secret \
LIVE_TRADING=true \
python delta_btc_atm_dual_strategy.py
```

### Specific Expiry
```bash
EXPIRY_DATE=29-06-2026 python delta_btc_atm_dual_strategy.py
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DELTA_API_KEY` | _(empty)_ | Delta Exchange API key (required for live) |
| `DELTA_API_SECRET` | _(empty)_ | Delta Exchange API secret (required for live) |
| `LIVE_TRADING` | `false` | Set `true` to send real orders |
| `DELTA_BASE_URL` | `https://api.india.delta.exchange` | Delta API base URL |
| `TV_CALL_SYMBOL` | _(auto)_ | TradingView symbol for CALL feed — auto-built as `DELTA:C-BTC-STRIKE-DDMMYY` |
| `TV_PUT_SYMBOL` | _(auto)_ | TradingView symbol for PUT feed — auto-built as `DELTA:P-BTC-STRIKE-DDMMYY` |
| `TV_TIMEFRAME` | `1` | Candle resolution in minutes |
| `TV_HIST_BARS` | `200` | History bars to load on connect |
| `UNDERLYING` | `BTC` | Delta underlying asset symbol |
| `EXPIRY_DATE` | _(nearest)_ | Expiry in `DD-MM-YYYY` format; blank = nearest live expiry |
| `CAPITAL_USDT` | `1000` | Capital per trade in USDT |
| `TARGET_PCT` | `0.02` | Target as fraction of entry price (0.02 = 2%) |
| `SIGNAL_VALID_MINUTES` | `1` | Minutes a signal stays active before expiring (0 = never expire) |

---

## Sizing Logic

```
lots = floor(CAPITAL_USDT / option_LTP_USDT)
```

Delta India options are quoted and settled in **USDT** directly. No BTC lot-size conversion is needed. Every price, P&L, and capital figure displayed is in USDT.

**Example:**
- Capital: 1000 USDT
- Option LTP: 526 USDT
- Lots bought: `floor(1000 / 526)` = **1 lot**

---

## Signal Logic

```
Pattern required (on the OPTION's own candle feed):

  Candle N-1 : RED  (close < open)
  Candle N   : GREEN (close > open)
  Candle N   must immediately follow Candle N-1 (consecutive 1-min bars)

Trigger:
  LTP of the option > Candle N high  →  ENTRY
  Stop  = Candle N-1 low  (round to tick size)
  Target = entry × (1 + TARGET_PCT)  (round to tick size)
```

Signals expire after `SIGNAL_VALID_MINUTES` minutes if not triggered (set to `0` to disable expiry).

---

## Architecture

```
run()
 ├── DeltaClient.resolve_atm_pair()   → OptionContract × 2
 ├── DualStrategy
 │    ├── Leg [CALL]  ←── TvFeed (DELTA:C-BTC-XXXXX-DDMMYY)
 │    └── Leg [PUT]   ←── TvFeed (DELTA:P-BTC-XXXXX-DDMMYY)
 │
 └── On trigger (first leg wins):
      ├── DeltaClient.market_buy()
      ├── DeltaClient.bracket()
      └── Other leg → cancel_signal()
```

**Key classes:**

| Class | Responsibility |
|---|---|
| `DeltaClient` | REST API — product lookup, order placement, bracket orders |
| `TvFeed` | TradingView WebSocket — streams candles and LTP per leg |
| `Leg` | Per-side signal detection, candle history, live display |
| `DualStrategy` | Shared position lock, trade coordination, exit management |

---

## Risk Warning

> This software is for **educational and research purposes only**.
> Options trading carries significant risk of loss. Always test in paper mode first.
> Past performance of any strategy is not indicative of future results.
> You are solely responsible for any trades placed using this software.

---

## License

MIT — free to use, modify, and distribute.