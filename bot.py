#!/usr/bin/env python3
"""
BybitbotFVG - a pure-Python Fair Value Gap auto-trading bot for Bybit.

Strategy is based on the LuxAlgo "Fair Value Gap [LuxAlgo]" indicator
(CC BY-NC-SA 4.0). See LICENSE for full attribution.

Runs on Termux and Linux with a standard CPython install. No pip, no
third-party packages, no `requests` - only the Python standard library.

Usage:
    python3 bot.py                 # uses ./config.json
    python3 bot.py my_config.json  # custom config path

Edit config.json to set your API keys, timeframe, symbol, stop loss /
take profit (by ROI), position sizing and currency display.
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
        self.kline_limit = int(eng.get("kline_limit", 200))

        api = self.cfg["api"]
        self.client = BybitClient(
            api_key=api["api_key"],
            api_secret=api["api_secret"],
            demo=api.get("demo", True),
            testnet=api.get("testnet", False),
            recv_window=api.get("recv_window", 20000),
            logger=self.log,
        )

        self.strategy = FVGStrategy(self.client, self.cfg, self.log)

        cur = self.cfg["currency"]
        self.display_ccy = cur.get("display_currency", "PHP")
        self.fx = float(cur.get("usdt_to_php_rate", 58.0))
        self.settle_coin = cur.get("settle_coin", "USDT")

        self._running = True

    # ------------------------------------------------------------------ #
    def to_display(self, usdt_amount):
        """Convert a settle-coin (USDT) amount to the display currency."""
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
        # A signed call proves the keys are valid and the clock is in sync.
        try:
            self.client.sync_time(force=True)
            wallet, avail = self.client.get_coin_balance(coin=self.settle_coin)
            self.log.info(
                "API key validated. Wallet=%s %.2f (%s %.2f) | "
                "Available=%s %.2f" %
                (self.settle_coin, wallet, self.display_ccy,
                 self.to_display(wallet), self.settle_coin, avail))
            return True
        except BybitError as exc:
            if exc.ret_code in (10003, 10004, 10005, 33004):
                self.log.error(
                    "API credentials rejected (retCode %s: %s). Check that "
                    "the key is a DEMO key, not expired, and has trade "
                    "permission." % (exc.ret_code, exc.ret_msg))
            else:
                self.log.error("Validation call failed: %s" % exc)
            return False

    def ensure_demo_funds(self):
        df = self.cfg.get("demo_funds", {})
        if not (self.cfg["api"].get("demo", True) and df.get("auto_request")):
            return
        try:
            wallet, _ = self.client.get_coin_balance(coin=df.get("coin",
                                                                 "USDT"))
            if wallet < float(df.get("min_balance_threshold", 10000)):
                self.log.info("Demo balance low (%.2f); requesting funds..."
                              % wallet)
                self.client.request_demo_funds(
                    coin=df.get("coin", "USDT"),
                    amount=df.get("amount", "100000"))
                self.log.info("Demo funds requested.")
        except BybitError as exc:
            self.log.warning("Could not top up demo funds: %s" % exc)

    def setup_leverage(self):
        try:
            self.client.set_leverage(
                self.strategy.category, self.strategy.symbol,
                self.cfg["trade"]["leverage"])
            self.log.info("Leverage set to %sx for %s" %
                          (self.cfg["trade"]["leverage"],
                           self.strategy.symbol))
        except BybitError as exc:
            self.log.warning("Could not set leverage: %s" % exc)

    # ------------------------------------------------------------------ #
    def report_status(self):
        try:
            positions = self.client.get_open_positions(
                self.strategy.category, symbol=self.strategy.symbol)
        except BybitError as exc:
            self.log.warning("status: positions fetch failed: %s" % exc)
            return
        if not positions:
            self.log.info("No open positions.")
            return
        for p in positions:
            size = p.get("size")
            side = p.get("side")
            entry = p.get("avgPrice")
            pnl = float(p.get("unrealisedPnl", 0) or 0)
            self.log.info(
                "Position: %s %s @ %s | uPnL=%s %.2f (%s %.2f) | "
                "TP=%s SL=%s" %
                (side, size, entry, self.settle_coin, pnl, self.display_ccy,
                 self.to_display(pnl), p.get("takeProfit"),
                 p.get("stopLoss")))

    # ------------------------------------------------------------------ #
    def run_once(self):
        # 1) refresh closed candles & detect / chain FVGs
        candles = self.client.get_kline(
            self.strategy.category, self.strategy.symbol,
            self.strategy.timeframe, limit=self.kline_limit)
        if len(candles) < 4:
            self.log.warning("Not enough candles yet.")
            return
        # The last kline is the in-progress candle; drop it.
        closed = candles[:-1]
        self.strategy.update_fvgs(closed)

        # 2) check retrace-to-mid entry against the live price
        last_price = self.client.get_last_price(
            self.strategy.category, self.strategy.symbol)
        if last_price is None:
            last_price = closed[-1]["close"]

        if self.strategy.pending and not self.strategy.pending["triggered"]:
            p = self.strategy.pending
            self.log.debug(
                "Armed %s mid=%.4f chain=%d | price=%.4f" %
                (p["direction"], p["mid"], p["chain"], last_price))
            self.strategy.try_enter(last_price)

    def run(self):
        self.log.info("=" * 60)
        self.log.info("BybitbotFVG starting | host=%s | symbol=%s | tf=%s" %
                      (self.client.host, self.strategy.symbol,
                       self.strategy.timeframe))
        self.log.info("SL=%s%% ROI  TP=%s%% ROI  size=%s%% of wallet  lev=%sx"
                      % (self.strategy.sl_roi, self.strategy.tp_roi,
                         self.strategy.position_size_pct,
                         self.strategy.leverage))
        self.log.info("Display currency: %s (1 %s = %.4f %s)" %
                      (self.display_ccy, self.settle_coin, self.fx,
                       self.display_ccy))
        self.log.info("=" * 60)

        if not self.validate_credentials():
            self.log.error("Exiting due to credential error.")
            return

        self.ensure_demo_funds()
        try:
            self.strategy.load_instrument()
        except BybitError as exc:
            self.log.error("Could not load instrument info: %s" % exc)
            return
        self.setup_leverage()

        status_every = max(1, int(60 / self.poll))
        tick = 0
        while self._running:
            try:
                self.run_once()
                if tick % status_every == 0:
                    self.report_status()
                    self.ensure_demo_funds()
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
