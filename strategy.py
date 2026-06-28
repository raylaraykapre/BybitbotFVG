"""
FVG trading strategy engine.

Rules (as requested):
  * Detect Fair Value Gaps using the LuxAlgo logic (see fvg.py).
  * When an FVG forms (on a *closed* candle of the selected timeframe),
    arm a setup. A bullish FVG arms a LONG, a bearish FVG arms a SHORT.
  * Enter only when price retraces back to the MID of the identified FVG.
  * Chaining: if another FVG forms right after the previous one (on the
    immediately following candle), move the pending entry to the mid of the
    newer FVG. This continues for up to `max_fvg_chain` (default 3) FVGs.
  * Position size = `position_size_pct` (default 85%) of wallet balance.
  * Stop loss / take profit follow Bybit's "% by ROI":
        price_move = roi_pct / 100 / leverage
        long : tp = entry*(1+move_tp), sl = entry*(1-move_sl)
        short: tp = entry*(1-move_tp), sl = entry*(1+move_sl)
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from bybit_client import BybitError
from fvg import detect_fvg_at, auto_threshold


INTERVAL_MS = {
    "1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
    "30": 1_800_000, "60": 3_600_000, "120": 7_200_000, "240": 14_400_000,
    "360": 21_600_000, "720": 43_200_000,
    "D": 86_400_000, "W": 604_800_000, "M": 2_592_000_000,
}


class FVGStrategy:
    def __init__(self, client, config, logger):
        self.client = client
        self.cfg = config
        self.log = logger

        t = config["trade"]
        self.category = t["category"]
        self.symbol = t["symbol"]
        self.timeframe = str(t["timeframe"])
        self.leverage = float(t["leverage"])
        self.position_size_pct = float(t["position_size_pct"])
        self.max_open = int(t["max_open_positions"])
        self.max_chain = int(t["max_fvg_chain"])
        self.threshold_pct = float(t.get("fvg_threshold_pct", 0.0))
        self.auto_thresh = bool(t.get("auto_threshold", False))

        r = config["risk"]
        self.sl_roi = float(r["stop_loss_roi_pct"])
        self.tp_roi = float(r["take_profit_roi_pct"])

        self.settle_coin = config["currency"]["settle_coin"]

        # instrument precision
        self.qty_step = Decimal("0.001")
        self.min_qty = Decimal("0.001")
        self.tick_size = Decimal("0.1")

        # state
        self.pending = None          # active armed setup or None
        self.last_closed_start = None
        self.interval_ms = INTERVAL_MS.get(self.timeframe, 300_000)

    # ------------------------------------------------------------------ #
    # instrument / precision
    # ------------------------------------------------------------------ #
    def load_instrument(self):
        info = self.client.get_instrument_info(self.category, self.symbol)
        lot = info.get("lotSizeFilter", {})
        price = info.get("priceFilter", {})
        if lot.get("qtyStep"):
            self.qty_step = Decimal(str(lot["qtyStep"]))
        if lot.get("minOrderQty"):
            self.min_qty = Decimal(str(lot["minOrderQty"]))
        if price.get("tickSize"):
            self.tick_size = Decimal(str(price["tickSize"]))
        self.log.info(
            "Instrument %s loaded: qtyStep=%s minQty=%s tickSize=%s" %
            (self.symbol, self.qty_step, self.min_qty, self.tick_size))

    def round_qty(self, qty):
        q = Decimal(str(qty))
        stepped = (q / self.qty_step).to_integral_value(ROUND_DOWN) \
            * self.qty_step
        return stepped

    def round_price(self, price):
        p = Decimal(str(price))
        stepped = (p / self.tick_size).quantize(Decimal("1"),
                                                rounding=ROUND_HALF_UP) \
            * self.tick_size
        return stepped

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #
    def get_balance(self):
        wallet, avail = self.client.get_coin_balance(
            coin=self.settle_coin, account_type="UNIFIED")
        # Use available balance for sizing; fall back to wallet balance.
        return avail if avail > 0 else wallet

    def open_position_count(self):
        try:
            pos = self.client.get_open_positions(
                self.category, symbol=self.symbol)
            return len(pos)
        except BybitError as exc:
            self.log.warning("could not fetch positions: %s" % exc)
            return 0

    # ------------------------------------------------------------------ #
    # sizing & risk
    # ------------------------------------------------------------------ #
    def compute_qty(self, balance, price):
        margin = balance * self.position_size_pct / 100.0
        notional = margin * self.leverage
        qty_raw = notional / price
        qty = self.round_qty(qty_raw)
        if qty < self.min_qty:
            return Decimal("0")
        return qty

    def compute_tp_sl(self, entry, direction):
        entry = float(entry)
        move_tp = (self.tp_roi / 100.0) / self.leverage
        move_sl = (self.sl_roi / 100.0) / self.leverage
        if direction == "long":
            tp = entry * (1 + move_tp)
            sl = entry * (1 - move_sl)
        else:
            tp = entry * (1 - move_tp)
            sl = entry * (1 + move_sl)
        return self.round_price(tp), self.round_price(sl)

    # ------------------------------------------------------------------ #
    # FVG detection + chaining
    # ------------------------------------------------------------------ #
    def _threshold(self, closed_candles):
        if self.auto_thresh:
            return auto_threshold(closed_candles)
        return self.threshold_pct / 100.0

    def update_fvgs(self, closed_candles):
        """Detect a freshly-completed FVG and arm / chain a setup.

        Only runs when a *new* candle has just closed. Returns the FVG that
        was registered this call, or None.
        """
        if len(closed_candles) < 3:
            return None

        newest = closed_candles[-1]
        if self.last_closed_start == newest["start"]:
            return None  # no new closed candle since last check
        first_run = self.last_closed_start is None
        self.last_closed_start = newest["start"]
        if first_run:
            # On the very first poll, don't fire on historical gaps; just
            # establish the baseline so we only trade gaps formed live.
            self.log.info("Baseline set at candle start=%s" % newest["start"])
            return None

        threshold = self._threshold(closed_candles)
        idx = len(closed_candles) - 1
        fvg = detect_fvg_at(closed_candles, idx, threshold=threshold)

        # Mitigation check: drop a pending setup whose gap was fully filled
        # by this newly closed candle before we ever got an entry.
        self._check_mitigation(newest)

        if fvg is None:
            return None

        if self.pending is not None and not self.pending["triggered"]:
            gap = fvg.start_time - self.pending["last_time"]
            consecutive = 0 < gap <= int(self.interval_ms * 1.5)
            if consecutive and self.pending["chain"] < self.max_chain:
                self.pending.update({
                    "direction": fvg.direction,
                    "mid": fvg.mid,
                    "top": fvg.top,
                    "bottom": fvg.bottom,
                    "last_time": fvg.start_time,
                    "chain": self.pending["chain"] + 1,
                })
                self.log.info(
                    "Chained FVG #%d (%s) -> new entry mid=%.4f" %
                    (self.pending["chain"], fvg.direction, fvg.mid))
                return fvg

        # start a fresh setup
        self.pending = {
            "direction": fvg.direction,
            "mid": fvg.mid,
            "top": fvg.top,
            "bottom": fvg.bottom,
            "last_time": fvg.start_time,
            "chain": 1,
            "triggered": False,
        }
        self.log.info(
            "Armed %s FVG: top=%.4f bottom=%.4f mid=%.4f (await retrace)" %
            (fvg.direction, fvg.top, fvg.bottom, fvg.mid))
        return fvg

    def _check_mitigation(self, candle):
        if self.pending is None or self.pending["triggered"]:
            return
        close = candle["close"]
        p = self.pending
        if p["direction"] == "long" and close < p["bottom"]:
            self.log.info("Pending LONG FVG mitigated (close<%0.4f); dropped"
                          % p["bottom"])
            self.pending = None
        elif p["direction"] == "short" and close > p["top"]:
            self.log.info("Pending SHORT FVG mitigated (close>%0.4f); dropped"
                          % p["top"])
            self.pending = None

    # ------------------------------------------------------------------ #
    # entry
    # ------------------------------------------------------------------ #
    def retrace_reached(self, last_price):
        if self.pending is None or self.pending["triggered"]:
            return False
        mid = self.pending["mid"]
        if self.pending["direction"] == "long":
            # bullish gap sits below price; wait for pullback down to mid
            return last_price <= mid
        else:
            # bearish gap sits above price; wait for pullback up to mid
            return last_price >= mid

    def try_enter(self, last_price):
        """If a pending setup's retrace is hit, open the position."""
        if not self.retrace_reached(last_price):
            return None

        if self.open_position_count() >= self.max_open:
            self.log.info("Max open positions reached; skipping entry.")
            return None

        direction = self.pending["direction"]
        balance = self.get_balance()
        if balance <= 0:
            self.log.warning("Balance is zero; cannot size position.")
            return None

        entry = last_price
        qty = self.compute_qty(balance, entry)
        if qty <= 0:
            self.log.warning("Computed qty below minimum; skipping entry.")
            return None

        tp, sl = self.compute_tp_sl(Decimal(str(entry)), direction)
        side = "Buy" if direction == "long" else "Sell"

        order = {
            "side": side,
            "direction": direction,
            "qty": qty,
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "chain": self.pending["chain"],
        }

        if self.cfg["engine"].get("dry_run"):
            self.log.info("[DRY RUN] would open %s qty=%s entry=%.4f "
                          "tp=%s sl=%s" % (side, qty, entry, tp, sl))
        else:
            resp = self.client.place_market_order(
                self.category, self.symbol, side, qty,
                take_profit=tp, stop_loss=sl, position_idx=0)
            order_id = resp.get("result", {}).get("orderId")
            order["order_id"] = order_id
            self.log.info(
                "OPENED %s %s qty=%s @~%.4f TP=%s SL=%s (chain=%d) id=%s" %
                (direction.upper(), self.symbol, qty, entry, tp, sl,
                 self.pending["chain"], order_id))

        self.pending["triggered"] = True
        self.pending = None
        return order
