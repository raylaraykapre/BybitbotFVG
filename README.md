# BybitbotFVG

A **pure-Python** Fair Value Gap (FVG) auto-trading bot for **Bybit**, built
to run on **Termux** (Android) and **Linux**. It trades the
[LuxAlgo "Fair Value Gap"](https://www.tradingview.com/) strategy on Bybit's
USDT perpetual contracts using Bybit's **Demo Trading** environment by
default.

> No `pip`, no `requests`, no third-party packages. It uses **only the Python
> standard library** (`urllib`, `hmac`, `hashlib`, `json`). If you have
> Python 3, you can run it.

The FVG detection logic is ported from the **"Fair Value Gap [LuxAlgo]"**
Pine Script indicator (© LuxAlgo, CC BY-NC-SA 4.0). See [LICENSE](LICENSE)
for full credit and license terms.

---

## What the bot does

1. **Scans every USDT perpetual pair** on Bybit (configurable; can be limited
   to a chosen list).
2. Reads closed candles of your chosen timeframe and detects Fair Value Gaps
   using the exact LuxAlgo rule:
   - **Bullish FVG** → arms a **LONG**.
   - **Bearish FVG** → arms a **SHORT**.
3. Waits for price to **retrace back to the mid** of the identified FVG, then
   opens a market position in that direction.
4. **FVG chaining:** if a new FVG forms *right after* the previous one (on the
   immediately following candle), the pending entry is **reconstructed to the
   mid of the newer FVG**. This continues for up to **3** consecutive FVGs
   (`max_fvg_chain`).
5. Sizes every position at **85%** of wallet balance (`position_size_pct`).
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

---

## Important notes about "currency in PHP"

Bybit crypto **perpetual contracts settle in USDT** — there is no native PHP
wallet for derivatives. So the bot trades the USDT-settled contract and
**displays/accounts all balances and PnL in PHP** using a configurable
exchange rate (`currency.usdt_to_php_rate`). Update that rate to match the
current USDT→PHP rate whenever you like.

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

## Get your Bybit DEMO API keys

1. Log in to Bybit and open **Demo Trading** (the simulated account).
2. Inside the Demo Trading account, create an **API key** with **read +
   trade** (Unified Trading / Contract) permissions.
3. Copy the key and secret into `config.json`.

> Demo keys ONLY work against `https://api-demo.bybit.com`, which is what the
> bot uses when `api.demo = true`. Do not create the key from Testnet.

---

## Configure (`config.json`)

```jsonc
{
  "api": {
    "api_key": "YOUR_DEMO_API_KEY",      // <-- edit
    "api_secret": "YOUR_DEMO_API_SECRET",// <-- edit
    "demo": true,                         // true = api-demo.bybit.com
    "recv_window": 20000                  // keeps requests valid (anti clock-skew)
  },
  "trade": {
    "category": "linear",                 // USDT perpetuals
    "symbols": "ALL",                     // "ALL" pairs, or ["BTCUSDT","ETHUSDT"]
    "quote_coin": "USDT",                 // only scan pairs quoted in this coin
    "timeframe": "5",                     // 1,3,5,15,30,60,120,240,360,720,D,W,M
    "leverage_pct": 75,                   // % of EACH pair's MAX leverage
    "position_size_pct": 85,              // 85% of wallet per position
    "max_open_positions": 1,              // current open positions allowed (global)
    "max_fvg_chain": 3,                   // chain up to 3 consecutive FVGs
    "fvg_threshold_pct": 0.0,             // min gap size %
    "auto_threshold": false,              // LuxAlgo "Auto" threshold
    "max_symbols": 0                      // 0 = no limit; else cap pairs scanned
  },
  "risk": {
    "stop_loss_roi_pct": 30,              // SL at 30% ROI
    "take_profit_roi_pct": 350            // TP at 350% ROI
  },
  "currency": {
    "display_currency": "PHP",
    "settle_coin": "USDT",
    "usdt_to_php_rate": 58.0              // update to current rate
  },
  "demo_funds": {
    "auto_request": true,                 // auto top-up demo wallet
    "coin": "USDT",
    "amount": "100000",
    "min_balance_threshold": 10000
  },
  "engine": {
    "poll_seconds": 5,                    // how often to check prices/entries
    "kline_limit": 60,                    // candles fetched per symbol
    "scan_batch": 30,                     // symbols whose klines refresh per tick
    "log_file": "bot.log",
    "dry_run": false                      // true = simulate, place no orders
  }
}
```

Everything you asked to be editable lives here: **API key/secret**, **stop
loss & take profit (by ROI)**, **current open positions** (`max_open_positions`),
the **timeframe to trade from**, and the **leverage percentage**
(`leverage_pct`). Set `symbols` to `"ALL"` to scan every USDT perpetual, or a
list to restrict it.

### How scanning all pairs works (rate-friendly)
- Live prices come from **one** `tickers` call per tick (covers all symbols).
- Open positions come from **one** call per tick.
- Klines are refreshed **round-robin**, `scan_batch` symbols per tick, so the
  whole universe is rescanned every `~(symbols / scan_batch) * poll_seconds`.
  With ~500 pairs, `scan_batch=30`, `poll=5s`, that's a full pass roughly
  every 80s — well inside a 5-minute candle. Increase `scan_batch` to scan
  faster, lower it if you hit rate limits.
- Leverage is set **lazily** the first time the bot trades a given pair, so it
  doesn't fire hundreds of calls on startup.

---

## Run

```bash
python3 bot.py
```

Use a custom config file:
```bash
python3 bot.py my_config.json
```

Run it persistently in Termux/Linux (survives terminal close):
```bash
nohup python3 bot.py > run.out 2>&1 &
```

Stop it with `Ctrl+C` (or `kill <pid>`); it shuts down gracefully.

**Tip:** set `"dry_run": true` first to watch it detect FVGs and announce the
trades it *would* place, without sending real (demo) orders.

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

### `retCode 10003: API key is invalid`
Bybit has **four independent environments** and an API key only works on the
one it was created in:

| Environment    | Host                          | config.json |
|----------------|-------------------------------|-------------|
| mainnet        | `api.bybit.com`               | `demo=false` (real money) |
| **mainnet-demo** | `api-demo.bybit.com`        | `demo=true` (bot default) |
| testnet        | `api-testnet.bybit.com`       | `testnet=true` |

`10003` means your key does **not** belong to the environment the bot is
calling. The usual cause: you created a normal key on the mainnet API page
instead of from **inside Demo Trading**.

Run the built-in diagnostic to see exactly where your key is valid:
```bash
python3 check_api.py
```

**To create a proper DEMO key:**
1. Log in to your normal Bybit (mainnet) account.
2. Switch to **Demo Trading** — it is a *separate account with its own user ID*.
3. While **in** Demo Trading, open the **API** menu (hover your avatar → API)
   and create a key with **Read + Trade (Unified Trading)** permission.
4. Put that key/secret into `config.json` with `api.demo = true`.

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
| `bot.py`          | Main loop: validate keys, poll, detect, enter, report.   |
| `bybit_client.py` | Pure-stdlib Bybit V5 REST client (signing, retries).     |
| `fvg.py`          | LuxAlgo Fair Value Gap detection, ported to Python.      |
| `strategy.py`     | Retrace-to-mid entries, chaining, ROI SL/TP, sizing.     |
| `config.json`     | All user-editable settings.                              |
| `check_api.py`    | Diagnose which Bybit environment your API key belongs to.|
| `LICENSE`         | CC BY-NC-SA 4.0 + credit to LuxAlgo.                      |

---

## Disclaimer

This bot can lose money. Crypto derivatives are risky. Test on **Demo** first.
The authors and LuxAlgo are not liable for any losses. See [LICENSE](LICENSE).
