#!/usr/bin/env python3
"""
BybitbotFVG - a pure-Python Fair Value Gap auto-trading bot for Bybit.

Strategy is based on the LuxAlgo "Fair Value Gap [LuxAlgo]" indicator
(CC BY-NC-SA 4.0). See LICENSE for full attribution.

Runs on Termux and Linux with a standard CPython install. No pip, no
third-party packages, no `requests` - only the Python standard library.

It scans ALL USDT perpetual pairs (configurable), detects Fair Value Gaps
on the chosen timeframe, and enters on a retrace to the FVG mid. Leverage is
set per pair as a PERCENT of that pair's maximum leverage.

Usage:
    python3 bot.py                 # uses ./config.json
    python3 bot.py my_config.json  # custom config path
"""

import json
import logging
import os
import signal
import sys
import time

from bybit_client import BybitClient, BybitError
from strategy import FVGStrategy


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def setup_logger(log_file):
    logger = logging.getLogger("BybitbotFVG")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


class Bot:
    def __init__(self, config_path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        eng = self.cfg["engine"]
        self.log = setup_logger(eng.get("log_file"))
        self.poll = float(eng.get("poll_seconds", 5))
        self.kline_limit = int(eng.get("kline_limit", 60))
        self.scan_batch = int(eng.get("scan_batch", 30))
        self.dry_run = bool(eng.get("dry_run", False))

        api = self.cfg["api"]
        self.client = BybitClient(
            api_key=api["api_key"],
            api_secret=api["api_secret"],
            demo=api.get("demo", True),
            testnet=api.get("testnet", False),
            recv_window=api.get("recv_window", 20000),
            logger=self.log,
        )

        t = self.cfg["trade"]
        self.category = t["category"]
        self.timeframe = str(t["timeframe"])
        self.quote_coin = t.get("quote_coin", "USDT")
        self.symbols_cfg = t.get("symbols", "ALL")
        self.max_symbols = int(t.get("max_symbols", 0))
        self.max_open = int(t["max_open_positions"])

        cur = self.cfg["currency"]
        self.display_ccy = cur.get("display_currency", "PHP")
        self.fx = float(cur.get("usdt_to_php_rate", 58.0))
        self.settle_coin = cur.get("settle_coin", "USDT")

        self.strategies = {}     # symbol -> FVGStrategy
        self.scan_order = []     # list of symbols for round-robin
        self.scan_idx = 0

        self._running = True

    # ------------------------------------------------------------------ #
    def to_display(self, usdt_amount):
        return usdt_amount * self.fx

    def fmt_money(self, usdt_amount):
        return "%s %s" % (self.display_ccy,
                          format(self.to_display(usdt_amount), ",.2f"))

    # ------------------------------------------------------------------ #
    def validate_credentials(self):
        api = self.cfg["api"]
        if (not api["api_key"] or api["api_key"].startswith("YOUR_")
                or not api["api_secret"]
                or api["api_secret"].startswith("YOUR_")):
            self.log.error(
                "API key/secret not configured. Edit config.json -> api. "
                "Create keys inside the Bybit DEMO trading account.")
            return False
        try:
            self.client.sync_time(force=True)
            wallet, avail = self.client.get_coin_balance(coin=self.settle_coin)
            self.log.info(
                "API key validated. Wallet=%s %.2f (%s) | Available=%s %.2f" %
                (self.settle_coin, wallet, self.fmt_money(wallet),
                 self.settle_coin, avail))
            return True
        except BybitError as exc:
            host = self.client.host
            if exc.ret_code == 10003:
                self.log.error(
                    "API key rejected (retCode 10003: %s). This almost always "
                    "means the key does NOT belong to the environment the bot "
                    "is calling (%s). Bybit has 4 separate envs: mainnet, "
                    "mainnet-demo, testnet, testnet-demo - a key only works on "
                    "the one it was created in." % (exc.ret_msg, host))
                self.log.error(
                    "Fix: create the key from INSIDE Demo Trading (separate "
                    "account/user ID) and keep api.demo=true. Run "
                    "`python3 check_api.py` to see which env your key matches.")
            elif exc.ret_code == 10004:
                self.log.error(
                    "Signature error (retCode 10004): wrong api_secret or a "
                    "large device clock skew. Re-copy the secret; the bot "
                    "auto-syncs time. Run `python3 check_api.py` to diagnose.")
            elif exc.ret_code in (10005, 33004):
                self.log.error(
                    "API key problem (retCode %s: %s): key may lack trade "
                    "permission or be expired." %
                    (exc.ret_code, exc.ret_msg))
            else:
                self.log.error("Validation call failed: %s" % exc)
            return False

    def ensure_demo_funds(self):
        df = self.cfg.get("demo_funds", {})
        if not (self.cfg["api"].get("demo", True) and df.get("auto_request")):
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
                self.log.info("Demo funds requested.")
        except BybitError as exc:
            self.log.warning("Could not top up demo funds: %s" % exc)

    # ------------------------------------------------------------------ #
    def discover_symbols(self):
        """Build per-symbol strategies for every (or selected) USDT perp."""
        if isinstance(self.symbols_cfg, list) and self.symbols_cfg:
            wanted = set(self.symbols_cfg)
        else:
            wanted = None  # ALL

        self.log.info("Discovering %s perpetual pairs (quote=%s)..." %
                      ("selected" if wanted else "ALL", self.quote_coin))
        instruments = self.client.get_all_instruments(
            self.category, status="Trading", quote_coin=self.quote_coin,
            contract_type="LinearPerpetual")

        count = 0
        for info in instruments:
            symbol = info.get("symbol")
            if not symbol:
                continue
            if wanted is not None and symbol not in wanted:
                continue
            strat = FVGStrategy(symbol, self.cfg, self.log)
            strat.set_instrument(info)
            self.strategies[symbol] = strat
            count += 1
            if self.max_symbols and count >= self.max_symbols:
                break

        self.scan_order = list(self.strategies.keys())
        self.log.info("Tracking %d symbols. Example leverage: %s" %
                      (len(self.scan_order), self._leverage_sample()))
        if not self.scan_order:
            self.log.error("No symbols matched. Check trade.symbols / "
                           "quote_coin in config.json.")

    def _leverage_sample(self):
        out = []
        for sym in self.scan_order[:4]:
            s = self.strategies[sym]
            out.append("%s=%sx(max %s)" %
                       (sym, s.leverage_str(),
                        format(s.max_leverage.normalize(), "f")))
        return ", ".join(out) if out else "n/a"

    # ------------------------------------------------------------------ #
    def open_positions_map(self):
        """Return {symbol: position_dict} for all open positions (1 call)."""
        try:
            positions = self.client.get_open_positions(
                self.category, settle_coin=self.settle_coin)
        except BybitError as exc:
            self.log.warning("positions fetch failed: %s" % exc)
            return {}
        return {p.get("symbol"): p for p in positions}

    def get_balance(self):
        wallet, avail = self.client.get_coin_balance(coin=self.settle_coin)
        return avail if avail > 0 else wallet

    def ensure_leverage(self, strat):
        if strat.leverage_set:
            return
        try:
            self.client.set_leverage(self.category, strat.symbol,
                                     strat.leverage_str())
            strat.leverage_set = True
            self.log.info("[%s] leverage set to %sx (max %s)" %
                          (strat.symbol, strat.leverage_str(),
                           format(strat.max_leverage.normalize(), "f")))
        except BybitError as exc:
            self.log.warning("[%s] could not set leverage: %s" %
                             (strat.symbol, exc))

    # ------------------------------------------------------------------ #
    def scan_klines_batch(self):
        """Refresh klines + detect FVGs for the next batch of symbols."""
        if not self.scan_order:
            return
        n = len(self.scan_order)
        batch = min(self.scan_batch, n)
        for _ in range(batch):
            symbol = self.scan_order[self.scan_idx % n]
            self.scan_idx = (self.scan_idx + 1) % n
            strat = self.strategies.get(symbol)
            if strat is None:
                continue
            try:
                candles = self.client.get_kline(
                    self.category, symbol, self.timeframe,
                    limit=self.kline_limit)
                if len(candles) < 4:
                    continue
                closed = candles[:-1]  # drop in-progress candle
                strat.update_fvgs(closed)
            except BybitError as exc:
                self.log.debug("[%s] kline error: %s" % (symbol, exc))

    def check_entries(self, price_map, open_map):
        """Trigger entries where price retraced to the FVG mid."""
        open_count = len(open_map)
        balance = None
        for symbol, strat in self.strategies.items():
            if not strat.has_pending():
                continue
            if symbol in open_map:
                continue  # already have a position on this symbol
            price = price_map.get(symbol)
            if price is None:
                continue
            if not strat.retrace_reached(price):
                continue
            if open_count >= self.max_open:
                self.log.debug("Max open positions (%d) reached; holding %s"
                               % (self.max_open, symbol))
                continue

            if balance is None:
                try:
                    balance = self.get_balance()
                except BybitError as exc:
                    self.log.warning("balance fetch failed: %s" % exc)
                    return
            if balance <= 0:
                self.log.warning("Balance is zero; cannot open positions.")
                return

            spec = strat.prepare_entry(balance, price)
            if spec is None:
                self.log.debug("[%s] qty below minimum; skip" % symbol)
                strat.mark_entered()
                continue

            if self.dry_run:
                self.log.info(
                    "[DRY RUN][%s] would %s qty=%s @~%.6f TP=%s SL=%s "
                    "lev=%sx chain=%d" %
                    (symbol, spec["side"], spec["qty"], spec["entry"],
                     spec["tp"], spec["sl"], spec["leverage"], spec["chain"]))
                strat.mark_entered()
                open_count += 1
                continue

            self.ensure_leverage(strat)
            try:
                resp = self.client.place_market_order(
                    self.category, symbol, spec["side"], spec["qty"],
                    take_profit=spec["tp"], stop_loss=spec["sl"],
                    position_idx=0)
                oid = resp.get("result", {}).get("orderId")
                self.log.info(
                    "OPENED [%s] %s qty=%s @~%.6f TP=%s SL=%s lev=%sx "
                    "chain=%d id=%s" %
                    (symbol, spec["direction"].upper(), spec["qty"],
                     spec["entry"], spec["tp"], spec["sl"], spec["leverage"],
                     spec["chain"], oid))
                strat.mark_entered()
                open_count += 1
                balance = None  # force refresh next entry
            except BybitError as exc:
                self.log.error("[%s] order failed: %s" % (symbol, exc))
                strat.mark_entered()

    # ------------------------------------------------------------------ #
    def report_status(self, open_map):
        if not open_map:
            self.log.info("No open positions. Tracking %d symbols." %
                          len(self.scan_order))
            return
        for symbol, p in open_map.items():
            pnl = float(p.get("unrealisedPnl", 0) or 0)
            self.log.info(
                "Position [%s]: %s %s @ %s | uPnL=%s | TP=%s SL=%s" %
                (symbol, p.get("side"), p.get("size"), p.get("avgPrice"),
                 self.fmt_money(pnl), p.get("takeProfit"), p.get("stopLoss")))

    def price_map(self):
        out = {}
        try:
            for t in self.client.get_all_tickers(self.category):
                lp = t.get("lastPrice")
                if lp:
                    out[t.get("symbol")] = float(lp)
        except BybitError as exc:
            self.log.warning("tickers fetch failed: %s" % exc)
        return out

    # ------------------------------------------------------------------ #
    def run_once(self, report=False):
        prices = self.price_map()
        open_map = self.open_positions_map()
        self.scan_klines_batch()
        self.check_entries(prices, open_map)
        if report:
            self.report_status(open_map)
            self.ensure_demo_funds()

    def run(self):
        self.log.info("=" * 60)
        self.log.info("BybitbotFVG starting | host=%s | tf=%s" %
                      (self.client.host, self.timeframe))
        self.log.info("SL=%s%% ROI  TP=%s%% ROI  size=%s%% of wallet  "
                      "leverage=%s%% of each pair's max" %
                      (self.cfg["risk"]["stop_loss_roi_pct"],
                       self.cfg["risk"]["take_profit_roi_pct"],
                       self.cfg["trade"]["position_size_pct"],
                       self.cfg["trade"]["leverage_pct"]))
        self.log.info("Max open positions: %d | Display currency: %s "
                      "(1 %s = %.4f %s)" %
                      (self.max_open, self.display_ccy, self.settle_coin,
                       self.fx, self.display_ccy))
        self.log.info("=" * 60)

        if not self.validate_credentials():
            self.log.error("Exiting due to credential error.")
            return

        self.ensure_demo_funds()
        try:
            self.discover_symbols()
        except BybitError as exc:
            self.log.error("Could not discover symbols: %s" % exc)
            return
        if not self.scan_order:
            return

        # Roughly one full kline cycle = (symbols/scan_batch)*poll seconds.
        cycle = max(1, len(self.scan_order) / max(1, self.scan_batch)) * self.poll
        self.log.info("Scanning ~%d symbols/tick; full scan every ~%.0fs."
                      % (min(self.scan_batch, len(self.scan_order)), cycle))

        status_every = max(1, int(60 / self.poll))
        tick = 0
        while self._running:
            try:
                self.run_once(report=(tick % status_every == 0))
            except BybitError as exc:
                self.log.error("API error: %s" % exc)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("Unexpected error: %s" % exc)
            tick += 1
            time.sleep(self.poll)

        self.log.info("Bot stopped.")

    def stop(self, *_):
        self.log.info("Shutdown signal received; stopping...")
        self._running = False


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    if not os.path.exists(config_path):
        print("Config file not found: %s" % config_path)
        sys.exit(1)
    bot = Bot(config_path)
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    bot.run()


if __name__ == "__main__":
    main()
