# BybitbotFVG

A **pure-Python** Fair Value Gap (FVG) auto-trading bot for **Bybit**, built
to run on **Termux** (Android) and **Linux**. It trades the
[LuxAlgo "Fair Value Gap"](https://www.tradingview.com/) strategy on Bybit's
USDT perpetual contracts. It ships with a **standalone built-in demo** so you
can run it immediately with **no Bybit account and no API keys**.

> No `pip`, no `requests`, no third-party packages. It uses **only the Python
> standard library** (`urllib`, `hmac`, `hashlib`, `json`). If you have
> Python 3, you can run it.

The FVG detection logic is ported from the **"Fair Value Gap [LuxAlgo]"**
Pine Script indicator (© LuxAlgo, CC BY-NC-SA 4.0). See [LICENSE](LICENSE)
for full credit and license terms.

---

## Modes: demo vs live

Set `"mode"` in `config.json` (or pass `--demo` / `--live`):

| Mode | What it does | Needs API keys? |
|------|--------------|-----------------|
| **`demo`** (default) | **Built-in demo.** The bot keeps its *own* simulated wallet and positions inside the program, reads Bybit's live charts, and fills Take Profit / Stop Loss locally. Nothing is sent to any exchange account. | **No** |
| `live` | Sends real orders to Bybit (real money) using your API keys. | Yes |

The built-in demo:
- Starts with a configurable wallet (`demo.starting_balance`, in PHP by
  default) and **persists** to `demo_state.json`, so balance and open
  positions survive restarts.
- Uses **only public market data** (klines + tickers) — these need no
  authentication, so you can demo-trade without ever creating a key.
- Simulates taker fees (`demo.taker_fee_pct`) and tracks realised PnL,
  wins and losses.
- Set `demo.reset_on_start: true` to wipe it back to the starting balance.

---

## What the bot does

1. **Scans every USDT perpetual pair** on Bybit (configurable; can be limited
   to a chosen list).
2. Reads closed candles of your chosen timeframe and detects Fair Value Gaps
   using the exact LuxAlgo rule:
   - **Bullish FVG** → arms a **LONG**.
   - **Bearish FVG** → arms a **SHORT**.
3. Waits for price to **retrace back to the mid** of the identified FVG, then
   opens a position in that direction.
4. **FVG chaining:** if a new FVG forms *right after* the previous one (on the
   immediately following candle), the pending entry is **reconstructed to the
   mid of the newer FVG**. This continues for up to **3** consecutive FVGs
   (`max_fvg_chain`).
5. Sizes every position at **85%** of the **whole wallet balance**
   (`position_size_pct`, editable up or down anytime).
6. Sets leverage **per pair** as a **percent of that pair's maximum leverage**
   (`leverage_pct`). For example, at `leverage_pct = 75`:
   - a pair whose max leverage is **100x** → **75x**
   - a pair whose max leverage is **12x** → **9x** (75% of 12)
   - a pair whose max leverage is **50x** → **37.5x** (rounded to the pair's
     leverage step)
7. Sets **Stop Loss** and **Take Profit** by **ROI** exactly like Bybit's TP/SL
   menu:
   - Stop Loss at **30% ROI**
   - Take Profit at **350% ROI**
   - (ROI → price uses that pair's actual leverage:
     `price_move = ROI% / 100 / leverage`.)
8. Reports balances and PnL in **Philippine Peso (PHP)**.

> **Leverage % examples (your request):** if a pair's max is **100x**, `75`
> means **75x**; if a pair's max is **12x**, `50` means **6x**. The bot reads
> each pair's max leverage from Bybit and applies your percentage to it,
> snapping to the pair's allowed leverage step.

> **Position sizing:** every entry uses `position_size_pct` of the full wallet
> balance as margin (notional = margin × leverage). The default is **85%**;
> change it in `config.json` at any time.

---

## Important notes about "currency in PHP"

Bybit crypto **perpetual contracts settle in USDT** — there is no native PHP
wallet for derivatives. The bot computes sizing/PnL internally in USDT (the
contract unit) and **displays everything in PHP** using a configurable rate
(`currency.usdt_to_php_rate`). In the built-in demo you can even set the
starting balance directly in PHP (`demo.balance_currency: "PHP"`).

---

## Install

You only need Python 3 (3.8+). 

### Termux (Android)
```bash
pkg update && pkg install python git -y
git clone https://github.com/raylaraykapre/BybitbotFVG.git
cd BybitbotFVG
```

### Linux
```bash
sudo apt install python3 git -y     # or your distro's package manager
git clone https://github.com/raylaraykapre/BybitbotFVG.git
cd BybitbotFVG
```

There is nothing to `pip install`.

---

## API keys: do I need them?

| You want to... | Run | API keys? |
|----------------|-----|-----------|
| Practice risk-free inside the bot | `python3 bot.py --demo` | **None** |
| Trade real money on Bybit | `python3 bot.py --live` | Yes — `api.api_key` / `api.api_secret` |

The **demo is fully built into the bot**: it keeps its own simulated wallet,
reads Bybit's **live public charts**, and fills TP/SL locally. It needs **no
API keys and no Bybit account**. Only **LIVE** mode needs keys.

**Creating a LIVE key:** on your Bybit account, create an API key with
**Unified Trading + trade** permission, then put it in `config.json` under
`live_api`. Run `python3 check_api.py` to verify it.

---

## Configure (`config.json`)

```jsonc
{
  "mode": "demo",                           // "demo" = built-in (no keys), "live" = real orders

  "demo": {
    "starting_balance": 100000,             // simulated wallet (in balance_currency)
    "balance_currency": "PHP",              // "PHP" or "USDT"
    "taker_fee_pct": 0.055,                 // simulated taker fee per fill
    "state_file": "demo_state.json",        // persists wallet + positions
    "reset_on_start": false                 // true = wipe back to starting_balance
  },

  "live_api": {                              // ONLY used for --live (real money)
    "api_key": "",                          // leave empty for demo
    "api_secret": "",
    "recv_window": 20000                    // keeps requests valid (anti clock-skew)
  },

  "trade": {
    "category": "linear",                   // USDT perpetuals
    "symbols": "ALL",                       // "ALL" pairs, or ["BTCUSDT","ETHUSDT"]
    "quote_coin": "USDT",
    "timeframe": "5",                       // 1,3,5,15,30,60,120,240,360,720,D,W,M
    "leverage_pct": 75,                     // % of EACH pair's MAX leverage
    "position_size_pct": 85,                // 85% of wallet per position (editable)
    "max_open_positions": 1,                // current open positions allowed (global)
    "max_fvg_chain": 3,                     // chain up to 3 consecutive FVGs
    "fvg_threshold_pct": 0.0,
    "auto_threshold": false,
    "max_symbols": 0                        // 0 = no limit; else cap pairs scanned
  },

  "risk": {
    "stop_loss_roi_pct": 30,                // SL at 30% ROI
    "take_profit_roi_pct": 350              // TP at 350% ROI
  },

  "currency": {
    "display_currency": "PHP",
    "settle_coin": "USDT",
    "usdt_to_php_rate": 58.0
  },

  "engine": {
    "poll_seconds": 60,                     // scan + check entries every 60s
    "kline_limit": 60,
    "scan_batch": 1000,                     // symbols scanned per tick (all pairs)
    "log_file": "bot.log",
    "dry_run": false
  }
}
```

The **demo needs no keys** — `live_api` is left empty and is only read when you
run `--live`. Editable: **mode**, **stop loss / take profit (ROI)**, **open
positions** (`max_open_positions`), **timeframe**, **leverage %**
(`leverage_pct`), and **position size** (`position_size_pct`, default 85%).

### How scanning all pairs works (rate-friendly)
- Live prices come from **one** `tickers` call per tick (covers all symbols).
- Open positions come from **one** call per tick.
- Klines are scanned every `poll_seconds` (default **60s**). With
  `scan_batch: 1000` the whole USDT-perp universe is scanned each tick;
  lower `scan_batch` if you ever hit rate limits.
- Leverage is set **lazily** the first time the bot trades a given pair, so it
  doesn't fire hundreds of calls on startup.

---

## Run

```bash
python3 bot.py
```

When you start it in a terminal, the bot **asks you to choose the mode**:

```
 1) DEMO  - built-in paper trading (no API keys, simulated wallet) - live charts
 2) LIVE  - REAL orders on Bybit using your API keys
```

You can also force the mode from the command line (and skip the prompt):

```bash
python3 bot.py --demo      # built-in standalone demo (no keys, no real orders)
python3 bot.py --live      # real orders on Bybit (uses your API keys)
python3 bot.py --live --yes  # live + skip the real-money confirmation
python3 bot.py my.json --demo
```

Both modes connect to **live Bybit charts/prices**. The only difference is
where orders go: DEMO fills them in the bot's own simulated wallet; LIVE sends
them to Bybit. The mode you pass on the command line overrides the `"mode"`
value in `config.json`; with no flag and no terminal (e.g. `nohup`), the
`config.json` value is used.

> **Safety:** LIVE mode trades **real money**, so the bot makes you type
> `YES` to confirm, and refuses to start unattended (e.g. under `nohup`)
> unless you pass `--yes`.

Use a custom config file:
```bash
python3 bot.py my_config.json --demo
```

Run it persistently in Termux/Linux (survives terminal close):
```bash
nohup python3 bot.py --demo > run.out 2>&1 &
```

Stop it with `Ctrl+C` (or `kill <pid>`); it shuts down gracefully.

**Tip:** set `"dry_run": true` in `config.json` to watch it detect FVGs and
announce the trades it *would* place, without filling them in either mode.

### Resetting the demo wallet
The demo wallet is **saved to `demo_state.json` and reloaded on every restart**
(so your balance and positions survive restarts). That means editing
`starting_balance` does **nothing** until you reset. To apply a new balance:
```bash
python3 bot.py --reset        # wipes the demo wallet back to starting_balance
```
(Or set `"reset_on_start": true` in `config.json`, or delete `demo_state.json`.)

---

## "Make sure the API will not be invalid"

The client guards against the most common cause of "invalid API" errors —
clock skew between your device and Bybit:

- It syncs with Bybit server time (`/v5/market/time`) and applies the offset
  to every signed request.
- It uses a generous `recv_window` (20s by default).
- On `retCode 10002/10004` (timestamp / signature) it **re-syncs and retries**
  automatically, and it backs off on rate limits.

If your key/secret are wrong, expired, or lack trade permission, the bot says
so clearly on startup and exits instead of spamming the API.

---

## Troubleshooting

### `retCode 10003: API key is invalid` (LIVE mode)
Your LIVE key was not accepted. Common causes:
- The key/secret was mis-copied, deleted, or expired.
- It was created in the wrong Bybit environment (it must be a normal mainnet
  Unified Trading key).
- An IP whitelist on the key excludes your device.

Run the diagnostic to see where your key is valid:
```bash
python3 check_api.py
```
Create a LIVE key on your Bybit account with **Unified Trading + trade**
permission and put it in `config.json` under `api`.

> Reminder: DEMO mode needs no key — `python3 bot.py --demo`.

### `retCode 10004: error sign`
Wrong `api_secret`, or a very large device clock skew. Re-copy the secret.
The bot already auto-syncs time with Bybit, so clock skew is rarely the cause.

### `retCode 10005 / 33004`
The key lacks trade permission, or it has expired. Recreate it with
Unified Trading **trade** permission.

### IP restriction
If you set an IP whitelist when creating the key, your phone/VPS IP must be
included, or every signed call will fail.

---

## Files

| File              | Purpose                                                  |
|-------------------|----------------------------------------------------------|
| `bot.py`          | Main loop: poll, detect, enter, report (paper or live).  |
| `broker.py`       | PaperBroker (standalone demo) + LiveBroker (real orders).|
| `bybit_client.py` | Pure-stdlib Bybit V5 REST client (signing, retries).     |
| `fvg.py`          | LuxAlgo Fair Value Gap detection, ported to Python.      |
| `strategy.py`     | Retrace-to-mid entries, chaining, ROI SL/TP, sizing.     |
| `config.json`     | All user-editable settings.                              |
| `check_api.py`    | Diagnose which Bybit environment your API key belongs to.|
| `demo_state.json` | Auto-saved demo wallet/positions (created at runtime).   |
| `LICENSE`         | CC BY-NC-SA 4.0 + credit to LuxAlgo.                      |

---

## Disclaimer

This bot can lose money. Crypto derivatives are risky. Test on **Demo** first.
The authors and LuxAlgo are not liable for any losses. See [LICENSE](LICENSE).
