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

Two modes (config.json -> "mode"):
  * "demo" - a built-in demo. The bot keeps its own simulated wallet/positions
             and fills TP/SL locally against Bybit's live public charts. No
             Bybit account or API keys required.
  * "live" - sends real orders to Bybit and needs valid API keys.

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

from bybit_client import BybitClient, BybitError, resolve_api
from broker import make_broker
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
    def __init__(self, config_path, mode_override=None, reset=False):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        eng = self.cfg["engine"]
        self.log = setup_logger(eng.get("log_file"))
        self.poll = float(eng.get("poll_seconds", 5))
        self.kline_limit = int(eng.get("kline_limit", 60))
        self.scan_batch = int(eng.get("scan_batch", 30))
        self.dry_run = bool(eng.get("dry_run", False))

        # Resolve the trading mode: CLI / selector override wins over config.
        if mode_override:
            self.cfg["mode"] = mode_override
        self.mode = str(self.cfg.get("mode", "demo")).lower()

        # --reset forces the built-in demo wallet back to starting_balance.
        if reset:
            section = self.cfg.get("demo")
            if section is None:
                section = self.cfg.setdefault("paper", {})
            section["reset_on_start"] = True

        # Resolve the Bybit API credentials for LIVE trading (none for demo).
        self.api = resolve_api(self.cfg)
        # The client is used for PUBLIC market data (klines/tickers/instruments)
        # in every mode. In demo mode it never makes authenticated calls.
        self.client = BybitClient(
            api_key=self.api["api_key"],
            api_secret=self.api["api_secret"],
            demo=self.api["demo"],
            testnet=self.api["testnet"],
            recv_window=self.api["recv_window"],
            logger=self.log,
        )

        self.broker = make_broker(self.client, self.cfg, self.log)

        t = self.cfg["trade"]
        self.category = t["category"]
        self.timeframe = str(t["timeframe"])
        self.quote_coin = t.get("quote_coin", "USDT")
        self.symbols_cfg = t.get("symbols", "ALL")
        self.max_symbols = int(t.get("max_symbols", 0))
        self.max_open = int(t["max_open_positions"])
        self.position_size_pct = float(t["position_size_pct"])

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
    def discover_symbols(self):
        """Build per-symbol strategies for every (or selected) USDT perp."""
        if isinstance(self.symbols_cfg, list) and self.symbols_cfg:
            wanted = set(self.symbols_cfg)
        else:
            wanted = None  # ALL

        self.log.info("Loading %s pairs." %
                      ("selected" if wanted else "all USDT"))
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
        self.log.info("Tracking %d symbols." % len(self.scan_order))
        if not self.scan_order:
            self.log.error("No symbols matched config.")

    # ------------------------------------------------------------------ #
    def scan_klines_batch(self):
        """Refresh klines + detect FVGs for the next batch of symbols."""
        if not self.scan_order:
            return
        n = len(self.scan_order)
        batch = min(self.scan_batch, n)
        for _ in range(batch):
            if not self._running:
                return
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
                self.log.debug("[%s] kline error." % symbol)

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
                self.log.debug("Max open positions reached.")
                continue

            if balance is None:
                try:
                    balance = self.broker.get_balance()
                except BybitError as exc:
                    self.log.warning("Balance fetch failed.")
                    return
            if balance <= 0:
                self.log.warning("Balance is zero.")
                return

            # Size = position_size_pct (85%) of the whole wallet balance.
            spec = strat.prepare_entry(balance, price)
            if spec is None:
                self.log.debug("[%s] Qty below minimum." % symbol)
                strat.mark_entered()
                continue

            if self.dry_run:
                self.log.info("[DRY][%s] Would %s %s @ %.6f. TP %s. SL %s." %
                              (symbol, spec["side"], spec["qty"],
                               spec["entry"], spec["tp"], spec["sl"]))
                strat.mark_entered()
                open_count += 1
                continue

            self.broker.ensure_leverage(strat)
            if self.broker.open_position(spec):
                strat.mark_entered()
                open_count += 1
                balance = None  # force refresh for the next entry
            else:
                strat.mark_entered()

    # ------------------------------------------------------------------ #
    def report_status(self, open_map, prices):
        armed = [(sym, s) for sym, s in self.strategies.items()
                 if s.has_pending()]
        try:
            equity = self.broker.get_equity()
            balance = self.broker.get_balance()
            self.log.info("Equity %s. Free %s. Open %d. Waiting %d." %
                          (self.fmt_money(equity), self.fmt_money(balance),
                           len(open_map), len(armed)))
        except BybitError as exc:
            self.log.warning("Wallet fetch failed.")
        for symbol, p in open_map.items():
            pnl = float(p.get("unrealisedPnl", 0) or 0)
            self.log.info("%s %s %s @ %s. PnL %s." %
                          (symbol, p.get("side"), p.get("size"),
                           p.get("avgPrice"), self.fmt_money(pnl)))
        # Show a few setups that are armed and waiting for the retrace.
        for symbol, s in armed[:3]:
            px = prices.get(symbol)
            px_txt = ("%.6f" % px) if px is not None else "?"
            self.log.info("Waiting: %s %s. Entry %.6f. Now %s." %
                          (symbol, s.pending["direction"], s.pending["mid"],
                           px_txt))

    def price_map(self):
        out = {}
        try:
            for t in self.client.get_all_tickers(self.category):
                lp = t.get("lastPrice")
                if lp:
                    out[t.get("symbol")] = float(lp)
        except BybitError as exc:
            self.log.warning("Price fetch failed.")
        return out

    # ------------------------------------------------------------------ #
    def run_once(self, report=False):
        prices = self.price_map()
        self.broker.on_tick(prices)          # paper: fill TP/SL locally
        open_map = self.broker.open_positions_map()
        self.scan_klines_batch()
        self.check_entries(prices, open_map)
        if report:
            # Re-fetch so a position opened this tick shows immediately.
            self.report_status(self.broker.open_positions_map(), prices)
            self.broker.ensure_funds()

    def run(self):
        self.log.info("BybitbotFVG started.")
        self.log.info("Mode: %s. Timeframe: %s." %
                      (self.mode.upper(), self.timeframe))
        self.log.info("Risk: SL %s%% ROI, TP %s%% ROI." %
                      (self.cfg["risk"]["stop_loss_roi_pct"],
                       self.cfg["risk"]["take_profit_roi_pct"]))
        self.log.info("Size: %s%% of wallet. Leverage: %s%% of pair max." %
                      (self.position_size_pct,
                       self.cfg["trade"]["leverage_pct"]))
        self.log.info("Max open: %d. Currency: %s." %
                      (self.max_open, self.display_ccy))
        if self.mode != "live":
            self.log.info("DEMO: trades are simulated inside the bot, NOT on "
                          "Bybit. Watch OPENED/CLOSED logs and the state file.")

        if not self.broker.validate():
            self.log.error("Startup failed.")
            return

        self.broker.ensure_funds()
        try:
            self.discover_symbols()
        except BybitError as exc:
            self.log.error("Symbol load failed: %s" % exc)
            return
        if not self.scan_order:
            return

        self.log.info("Scanning every %ds." % int(self.poll))

        status_every = max(1, int(60 / self.poll))
        tick = 0
        while self._running:
            try:
                self.run_once(report=(tick % status_every == 0))
            except BybitError as exc:
                self.log.error("API error: %s" % exc)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("Error: %s" % exc)
            tick += 1
            self._sleep(self.poll)

        self.log.info("Bot stopped.")

    def _sleep(self, seconds):
        """Sleep that wakes up immediately when stop() is called."""
        end = time.monotonic() + seconds
        while self._running:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))

    def stop(self, *_):
        if not self._running:
            # Second Ctrl+C: quit now, no waiting.
            print("\nForce quit.")
            os._exit(0)
        print("\nStopping (press Ctrl+C again to force quit).")
        self._running = False


USAGE = """\
BybitbotFVG - Fair Value Gap auto-trading bot for Bybit

Usage:
  python3 bot.py [config.json] [--demo | --live] [--yes]

Mode options (choose how it trades; both use LIVE Bybit charts/prices):
  --demo, --paper   Built-in demo: simulated wallet, no API keys, no real
                    orders. (Same as config "mode": "demo".)
  --live            Real orders on Bybit using your API keys.
                    (Same as config "mode": "live".)

If no mode flag is given and you run in a terminal, the bot asks you to pick.
Otherwise it uses the "mode" set in config.json.

Other:
  --reset           Reset the built-in demo wallet to starting_balance.
  --yes             Skip the real-money confirmation prompt in live mode.
  -h, --help        Show this help and exit.
"""


def parse_args(argv):
    config_path = "config.json"
    mode_override = None
    assume_yes = False
    reset = False
    for a in argv[1:]:
        al = a.lower()
        if al in ("--demo", "--paper", "-d", "demo", "paper"):
            mode_override = "demo"
        elif al in ("--live", "-l", "live"):
            mode_override = "live"
        elif al in ("--yes", "-y"):
            assume_yes = True
        elif al in ("--reset", "-r"):
            reset = True
        elif al in ("-h", "--help", "help"):
            print(USAGE)
            sys.exit(0)
        elif not a.startswith("-"):
            config_path = a
        else:
            print("Unknown option: %s\n" % a)
            print(USAGE)
            sys.exit(1)
    return config_path, mode_override, assume_yes, reset


def select_mode_interactive(default_mode):
    """Ask the user to pick demo vs live (only when run in a terminal)."""
    default_mode = (default_mode or "demo").lower()
    default_choice = "2" if default_mode == "live" else "1"
    print("Choose mode:")
    print("  1) DEMO - built-in, no API keys, live Bybit charts.")
    print("  2) LIVE - real orders on Bybit, needs API keys.")
    try:
        raw = input("Select [1=DEMO, 2=LIVE] (default %s): "
                    % default_choice).strip()
    except EOFError:
        raw = ""
    if raw == "":
        raw = default_choice
    if raw in ("2", "live", "l", "L"):
        return "live"
    return "demo"


def confirm_live(config, assume_yes):
    """Require an explicit confirmation before trading real money."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to start LIVE trading non-interactively. "
              "Use --yes to confirm, or --demo for the built-in demo.")
        return False
    print("WARNING: LIVE mode places REAL orders on Bybit with real money.")
    try:
        ans = input("Type YES to confirm: ").strip()
    except EOFError:
        ans = ""
    return ans == "YES"


def main():
    config_path, mode_override, assume_yes, reset = parse_args(sys.argv)
    if not os.path.exists(config_path):
        print("Config file not found: %s" % config_path)
        sys.exit(1)

    cfg_preview = load_config(config_path)
    default_mode = str(cfg_preview.get("mode", "demo")).lower()

    # Decide the mode: explicit flag > interactive prompt > config default.
    if mode_override is None and sys.stdin.isatty():
        mode_override = select_mode_interactive(default_mode)
    resolved_mode = (mode_override or default_mode).lower()

    if resolved_mode == "live" and not confirm_live(cfg_preview, assume_yes):
        print("Live trading not confirmed. Exiting.")
        sys.exit(0)

    bot = Bot(config_path, mode_override=resolved_mode, reset=reset)
    signal.signal(signal.SIGINT, bot.stop)
    signal.signal(signal.SIGTERM, bot.stop)
    bot.run()


if __name__ == "__main__":
    main()
