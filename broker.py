"""
Broker abstraction for BybitbotFVG.

Two implementations share the same interface so the rest of the bot does not
care whether it is trading for real or simulating:

  * LiveBroker  - sends real orders to Bybit (mainnet or demo account) via the
                  BybitClient. Requires valid API keys.

  * PaperBroker - a fully STANDALONE, built-in demo. It keeps its OWN
                  simulated wallet and positions inside the bot, marks them
                  against live public prices, and fills Take Profit / Stop
                  Loss locally. It needs NO Bybit account and NO API keys -
                  only public market data (klines/tickers), which the bot
                  already fetches without authentication.

All internal accounting is done in the settle coin (USDT, the unit of the
USDT-perp contracts). Balances are shown in the display currency (PHP) using
the configured USDT->PHP rate.

Interface used by the Bot:
    name()                      -> str
    validate()                  -> bool
    ensure_funds()              -> None
    get_balance()               -> float   (free settle-coin balance for sizing)
    get_equity()                -> float   (wallet + unrealised PnL)
    open_positions_map()        -> {symbol: position_dict}
    ensure_leverage(strategy)   -> None
    open_position(spec)         -> bool
    on_tick(price_map)          -> None     (paper: process TP/SL fills)
"""

import json
import os
import time

from bybit_client import BybitError, resolve_api


# ---------------------------------------------------------------------------
# Live broker
# ---------------------------------------------------------------------------
class LiveBroker:
    def __init__(self, client, config, logger):
        self.client = client
        self.cfg = config
        self.log = logger
        self.category = config["trade"]["category"]
        self.settle_coin = config["currency"]["settle_coin"]
        self.api = resolve_api(config)

    def name(self):
        return "LIVE (%s, env=%s)" % (self.client.host, self.api["env"])

    def validate(self):
        key = self.api["api_key"]
        sec = self.api["api_secret"]
        if (not key or key.startswith("YOUR_") or not sec
                or sec.startswith("YOUR_")):
            self.log.error(
                "LIVE mode (env=%s) needs API keys. Edit config.json -> api "
                "(%s_api_key / %s_api_secret), or run with --demo to use the "
                "built-in standalone demo." %
                (self.api["env"], self.api["env"], self.api["env"]))
            return False
        try:
            self.client.sync_time(force=True)
            wallet, avail = self.client.get_coin_balance(coin=self.settle_coin)
            self.log.info("API key validated. Wallet=%.2f %s | Available=%.2f"
                          % (wallet, self.settle_coin, avail))
            return True
        except BybitError as exc:
            host = self.client.host
            if exc.ret_code == 10003:
                self.log.error(
                    "API key rejected (10003: %s). The key does not belong to "
                    "this environment (%s). Bybit has mainnet, mainnet-demo, "
                    "testnet, testnet-demo - a key only works on the one it "
                    "was created in. Run `python3 check_api.py`." %
                    (exc.ret_msg, host))
            elif exc.ret_code == 10004:
                self.log.error("Signature error (10004): wrong api_secret or "
                               "clock skew. Run `python3 check_api.py`.")
            else:
                self.log.error("Validation failed: %s" % exc)
            return False

    def ensure_funds(self):
        df = self.cfg.get("demo_funds", {})
        if not (self.api["env"] == "demo" and df.get("auto_request")):
            return
        try:
            wallet, _ = self.client.get_coin_balance(
                coin=df.get("coin", "USDT"))
            if wallet < float(df.get("min_balance_threshold", 10000)):
                self.log.info("Demo balance low (%.2f); requesting funds..."
                              % wallet)
                self.client.request_demo_funds(
                    coin=df.get("coin", "USDT"),
                    amount=df.get("amount", "100000"))
        except BybitError as exc:
            self.log.warning("Could not top up demo funds: %s" % exc)

    def get_balance(self):
        wallet, avail = self.client.get_coin_balance(coin=self.settle_coin)
        return avail if avail > 0 else wallet

    def get_equity(self):
        wallet, _ = self.client.get_coin_balance(coin=self.settle_coin)
        return wallet

    def open_positions_map(self):
        try:
            positions = self.client.get_open_positions(
                self.category, settle_coin=self.settle_coin)
        except BybitError as exc:
            self.log.warning("positions fetch failed: %s" % exc)
            return {}
        return {p.get("symbol"): p for p in positions}

    def ensure_leverage(self, strat):
        if strat.leverage_set:
            return
        try:
            self.client.set_leverage(self.category, strat.symbol,
                                     strat.leverage_str())
            strat.leverage_set = True
            self.log.info("[%s] leverage set to %sx" %
                          (strat.symbol, strat.leverage_str()))
        except BybitError as exc:
            self.log.warning("[%s] could not set leverage: %s" %
                             (strat.symbol, exc))

    def open_position(self, spec):
        try:
            resp = self.client.place_market_order(
                self.category, spec["symbol"], spec["side"], spec["qty"],
                take_profit=spec["tp"], stop_loss=spec["sl"], position_idx=0)
            oid = resp.get("result", {}).get("orderId")
            self.log.info(
                "OPENED [%s] %s qty=%s @~%.6f TP=%s SL=%s lev=%sx chain=%d "
                "id=%s" %
                (spec["symbol"], spec["direction"].upper(), spec["qty"],
                 spec["entry"], spec["tp"], spec["sl"], spec["leverage"],
                 spec["chain"], oid))
            return True
        except BybitError as exc:
            self.log.error("[%s] order failed: %s" % (spec["symbol"], exc))
            return False

    def on_tick(self, price_map):
        # Live exchange handles TP/SL server-side; nothing to do locally.
        return


# ---------------------------------------------------------------------------
# Paper broker (standalone built-in demo)
# ---------------------------------------------------------------------------
class PaperBroker:
    def __init__(self, config, logger):
        self.cfg = config
        self.log = logger
        p = config.get("paper", {})
        self.settle_coin = config["currency"]["settle_coin"]
        self.display_ccy = config["currency"].get("display_currency", "PHP")
        self.fx = float(config["currency"].get("usdt_to_php_rate", 1.0)) or 1.0
        self.fee_pct = float(p.get("taker_fee_pct", 0.0))
        self.state_file = p.get("state_file", "paper_state.json")

        # Convert the configured starting balance into the settle coin (USDT).
        start = float(p.get("starting_balance", 100000))
        bal_ccy = str(p.get("balance_currency", self.settle_coin)).upper()
        if bal_ccy in (self.display_ccy.upper(), "PHP"):
            self.start_usdt = start / self.fx
            self._start_label = "%s %s (= %.2f %s)" % (
                self.display_ccy, format(start, ",.2f"),
                self.start_usdt, self.settle_coin)
        else:
            self.start_usdt = start
            self._start_label = "%.2f %s" % (start, self.settle_coin)

        # Internal state (all in settle coin / USDT).
        self.cash = self.start_usdt          # free balance
        self.realized = 0.0                  # cumulative realised PnL
        self.wins = 0
        self.losses = 0
        self.positions = {}                  # symbol -> position dict
        self.marks = {}                      # symbol -> last seen price
        self._seq = 0

        if not p.get("reset_on_start") and os.path.exists(self.state_file):
            self._load()
        else:
            self._save()

    # -- persistence ----------------------------------------------------- #
    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                st = json.load(fh)
            self.cash = float(st.get("cash", self.start_usdt))
            self.realized = float(st.get("realized", 0.0))
            self.wins = int(st.get("wins", 0))
            self.losses = int(st.get("losses", 0))
            self.positions = st.get("positions", {})
            self._seq = int(st.get("seq", 0))
            self.log.info("Loaded paper state: cash=%.2f %s, %d open position(s)"
                          % (self.cash, self.settle_coin, len(self.positions)))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Could not load paper state (%s); starting fresh."
                             % exc)
            self.cash = self.start_usdt

    def _save(self):
        st = {
            "cash": self.cash,
            "realized": self.realized,
            "wins": self.wins,
            "losses": self.losses,
            "positions": self.positions,
            "seq": self._seq,
            "updated": int(time.time()),
        }
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(st, fh, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("Could not save paper state: %s" % exc)

    # -- display helpers ------------------------------------------------- #
    def _php(self, usdt):
        return "%s %s" % (self.display_ccy, format(usdt * self.fx, ",.2f"))

    def _used_margin(self):
        return sum(float(p["margin"]) for p in self.positions.values())

    # -- interface ------------------------------------------------------- #
    def name(self):
        return "PAPER (standalone built-in demo)"

    def validate(self):
        self.log.info("Standalone demo wallet ready. Starting balance: %s"
                      % self._start_label)
        self.log.info("Current cash: %.2f %s (%s)" %
                      (self.cash, self.settle_coin, self._php(self.cash)))
        return True

    def ensure_funds(self):
        # If the simulated account is wiped out and nothing is open, refill it
        # so the demo keeps running.
        if not self.positions and self.cash <= 0:
            self.log.info("Paper wallet empty; refilling to starting balance.")
            self.cash = self.start_usdt
            self._save()

    def get_balance(self):
        # Free balance available to size a new position with.
        return max(0.0, self.cash)

    def get_equity(self):
        upnl = 0.0
        for sym, p in self.positions.items():
            mark = self.marks.get(sym, float(p["entry"]))
            upnl += self._position_pnl(p, mark)
        return self.cash + self._used_margin() + upnl

    def open_positions_map(self):
        out = {}
        for sym, p in self.positions.items():
            mark = self.marks.get(sym, float(p["entry"]))
            out[sym] = {
                "symbol": sym,
                "side": p["side"],
                "size": p["qty"],
                "avgPrice": p["entry"],
                "unrealisedPnl": self._position_pnl(p, mark),
                "takeProfit": p["tp"],
                "stopLoss": p["sl"],
            }
        return out

    def ensure_leverage(self, strat):
        strat.leverage_set = True  # nothing to do in the simulator

    def _position_pnl(self, p, price):
        qty = float(p["qty"])
        entry = float(p["entry"])
        if p["side"] == "Buy":
            return qty * (price - entry)
        return qty * (entry - price)

    def open_position(self, spec):
        symbol = spec["symbol"]
        if symbol in self.positions:
            return False
        qty = float(spec["qty"])
        entry = float(spec["entry"])
        lev = float(spec["leverage"]) or 1.0
        notional = qty * entry
        margin = notional / lev
        fee = notional * self.fee_pct / 100.0

        if margin + fee > self.cash:
            self.log.warning("[%s] not enough paper balance (need %.2f, have "
                             "%.2f); skipping." %
                             (symbol, margin + fee, self.cash))
            return False

        self.cash -= (margin + fee)
        self._seq += 1
        self.positions[symbol] = {
            "id": "paper-%d" % self._seq,
            "side": spec["side"],
            "qty": qty,
            "entry": entry,
            "tp": float(spec["tp"]),
            "sl": float(spec["sl"]),
            "margin": margin,
            "leverage": lev,
            "open_fee": fee,
            "open_time": int(time.time()),
        }
        self.marks[symbol] = entry
        self.log.info(
            "OPENED [%s] %s qty=%s @~%.6f TP=%s SL=%s lev=%sx chain=%d "
            "(margin=%s, fee=%.4f %s) | free=%s" %
            (symbol, spec["direction"].upper(), spec["qty"], entry,
             spec["tp"], spec["sl"], spec["leverage"], spec["chain"],
             self._php(margin), fee, self.settle_coin, self._php(self.cash)))
        self._save()
        return True

    def _close(self, symbol, exit_price, reason):
        p = self.positions.pop(symbol)
        pnl = self._position_pnl(p, exit_price)
        notional = float(p["qty"]) * exit_price
        exit_fee = notional * self.fee_pct / 100.0
        self.cash += float(p["margin"]) + pnl - exit_fee
        self.realized += pnl - exit_fee - float(p["open_fee"])
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        self.log.info(
            "CLOSED [%s] %s @ %.6f (%s) | PnL=%s | realised=%s | free=%s | "
            "equity=%s" %
            (symbol, p["side"], exit_price, reason, self._php(pnl),
             self._php(self.realized), self._php(self.cash),
             self._php(self.get_equity())))
        self.marks.pop(symbol, None)
        self._save()

    def on_tick(self, price_map):
        """Mark positions and fill TP/SL locally against live prices."""
        if not self.positions:
            return
        changed = False
        for symbol in list(self.positions.keys()):
            price = price_map.get(symbol)
            if price is None:
                continue
            self.marks[symbol] = price
            p = self.positions[symbol]
            if p["side"] == "Buy":
                if price >= p["tp"]:
                    self._close(symbol, p["tp"], "TP"); changed = True
                elif price <= p["sl"]:
                    self._close(symbol, p["sl"], "SL"); changed = True
            else:
                if price <= p["tp"]:
                    self._close(symbol, p["tp"], "TP"); changed = True
                elif price >= p["sl"]:
                    self._close(symbol, p["sl"], "SL"); changed = True
        if not changed:
            # Persist updated marks occasionally (cheap; keeps equity fresh).
            self._save()


# ---------------------------------------------------------------------------
def make_broker(client, config, logger):
    mode = str(config.get("mode", "paper")).lower()
    if mode == "live":
        return LiveBroker(client, config, logger)
    return PaperBroker(config, logger)
