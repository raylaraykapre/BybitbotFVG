"""
FVG trading strategy engine (per-symbol).

One FVGStrategy instance is created for each traded symbol. It holds that
symbol's precision filters, its detection state, and the pending setup.
Account-level concerns (balance, global position count, order placement) are
handled by the Bot, which drives many strategies at once.

Rules (as requested):
  * Detect Fair Value Gaps using the LuxAlgo logic (see fvg.py).
  * When an FVG forms (on a *closed* candle of the selected timeframe),
    arm a setup. A bullish FVG arms a LONG, a bearish FVG arms a SHORT.
  * Enter only when price retraces back to the MID of the identified FVG.
  * Chaining: if another FVG forms right after the previous one (on the
    immediately following candle), move the pending entry to the mid of the
    newer FVG. Continues for up to `max_fvg_chain` (default 3) FVGs.
  * Position size = `position_size_pct` (default 85%) of wallet balance.
  * Leverage is a PERCENT of each pair's MAX leverage:
        actual_leverage = pair_max_leverage * leverage_pct / 100
    e.g. a pair with 100x max at 75% -> 75x; a 12x-max pair at 50% -> 6x.
  * Stop loss / take profit follow Bybit's "% by ROI":
        price_move = roi_pct / 100 / actual_leverage
        long : tp = entry*(1+move_tp), sl = entry*(1-move_sl)
        short: tp = entry*(1-move_tp), sl = entry*(1+move_sl)
"""

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from fvg import detect_fvg_at, auto_threshold


INTERVAL_MS = {
    "1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
    "30": 1_800_000, "60": 3_600_000, "120": 7_200_000, "240": 14_400_000,
    "360": 21_600_000, "720": 43_200_000,
    "D": 86_400_000, "W": 604_800_000, "M": 2_592_000_000,
}


def _crossed_to_mid(direction, mid, top, bottom, prev, price):
    """True when price crosses to the FVG mid from the origin side.

    A bearish ("short") FVG sits above price -> wait for a rise up to the mid.
    A bullish ("long") FVG sits below price -> wait for a fall down to the mid.
    Requires a real crossing (using the previous price) and keeps the trigger
    inside the gap zone.
    """
    if prev is None:
        return False
    if direction == "short":
        return prev < mid <= price <= top
    return prev > mid >= price >= bottom


class FVGStrategy:
    def __init__(self, symbol, config, logger):
        self.symbol = symbol
        self.cfg = config
        self.log = logger

        t = config["trade"]
        self.category = t["category"]
        self.timeframe = str(t["timeframe"])
        self.leverage_pct = float(t["leverage_pct"])
        self.position_size_pct = float(t["position_size_pct"])
        self.max_chain = int(t["max_fvg_chain"])
        self.threshold_pct = float(t.get("fvg_threshold_pct", 0.0))
        self.auto_thresh = bool(t.get("auto_threshold", False))

        r = config["risk"]
        self.sl_roi = float(r["stop_loss_roi_pct"])
        self.tp_roi = float(r["take_profit_roi_pct"])

        # Close an open position when an OPPOSITE (reversal) FVG forms and
        # price retraces to its mid.
        self.exit_on_opp_fvg = bool(t.get("exit_on_opposite_fvg", True))

        # instrument precision / leverage (set via set_instrument)
        self.qty_step = Decimal("0.001")
        self.min_qty = Decimal("0.001")
        self.tick_size = Decimal("0.1")
        self.max_leverage = Decimal("10")
        self.min_leverage = Decimal("1")
        self.lev_step = Decimal("0.01")
        self.actual_leverage = Decimal("10")
        self.leverage_set = False

        # state
        self.pending = None
        self.last_closed_start = None
        self.last_price = None
        self.exit_pending = None        # opposite-FVG exit setup
        self.exit_last_price = None
        self.interval_ms = INTERVAL_MS.get(self.timeframe, 300_000)

    # ------------------------------------------------------------------ #
    # instrument / precision / leverage
    # ------------------------------------------------------------------ #
    def set_instrument(self, info):
        lot = info.get("lotSizeFilter", {})
        price = info.get("priceFilter", {})
        lev = info.get("leverageFilter", {})
        if lot.get("qtyStep"):
            self.qty_step = Decimal(str(lot["qtyStep"]))
        if lot.get("minOrderQty"):
            self.min_qty = Decimal(str(lot["minOrderQty"]))
        if price.get("tickSize"):
            self.tick_size = Decimal(str(price["tickSize"]))
        if lev.get("maxLeverage"):
            self.max_leverage = Decimal(str(lev["maxLeverage"]))
        if lev.get("minLeverage"):
            self.min_leverage = Decimal(str(lev["minLeverage"]))
        if lev.get("leverageStep") and Decimal(str(lev["leverageStep"])) > 0:
            self.lev_step = Decimal(str(lev["leverageStep"]))
        self.actual_leverage = self._compute_leverage()

    def _compute_leverage(self):
        """actual_leverage = max_leverage * leverage_pct/100, floored to step
        and clamped to [min_leverage, max_leverage]."""
        target = self.max_leverage * Decimal(str(self.leverage_pct)) / Decimal("100")
        if self.lev_step > 0:
            stepped = (target / self.lev_step).to_integral_value(ROUND_DOWN) \
                * self.lev_step
        else:
            stepped = target
        if stepped < self.min_leverage:
            stepped = self.min_leverage
        if stepped > self.max_leverage:
            stepped = self.max_leverage
        return stepped

    @property
    def leverage(self):
        return float(self.actual_leverage)

    def leverage_str(self):
        return format(self.actual_leverage.normalize(), "f")

    def round_qty(self, qty):
        q = Decimal(str(qty))
        return (q / self.qty_step).to_integral_value(ROUND_DOWN) * self.qty_step

    def round_price(self, price):
        p = Decimal(str(price))
        return (p / self.tick_size).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP) * self.tick_size

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

    def update_fvgs(self, closed_candles, position_side=None):
        """Detect a freshly-completed FVG and route it.

        With no open position (position_side=None) it arms/chains the ENTRY
        setup. With an open position it arms/chains an EXIT setup from an
        opposite (reversal) FVG, so the position is closed when price retraces
        to that new FVG's mid. Only fires on a newly closed candle.
        """
        if len(closed_candles) < 3:
            return None

        newest = closed_candles[-1]
        if self.last_closed_start == newest["start"]:
            return None
        first_run = self.last_closed_start is None
        self.last_closed_start = newest["start"]
        if first_run:
            # Establish baseline so we only trade gaps formed live.
            return None

        threshold = self._threshold(closed_candles)
        idx = len(closed_candles) - 1
        fvg = detect_fvg_at(closed_candles, idx, threshold=threshold)

        if position_side is not None:
            # In a position: manage the opposite-FVG exit only.
            self._update_exit(fvg, position_side)
            return fvg

        # Flat: any leftover exit setup no longer applies.
        if self.exit_pending is not None:
            self.clear_exit()

        # Drop a pending entry whose gap was fully filled before entry.
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
                self.log.info("[%s] Chained FVG #%d. Entry mid %.6f." %
                              (self.symbol, self.pending["chain"], fvg.mid))
                return fvg

        self.pending = {
            "direction": fvg.direction,
            "mid": fvg.mid,
            "top": fvg.top,
            "bottom": fvg.bottom,
            "last_time": fvg.start_time,
            "chain": 1,
            "triggered": False,
        }
        self.log.info("[%s] Armed %s FVG. Entry mid %.6f." %
                      (self.symbol, fvg.direction, fvg.mid))
        return fvg

    def _update_exit(self, fvg, position_side):
        """Arm/chain an exit from an FVG opposite to the open position."""
        if not self.exit_on_opp_fvg or fvg is None:
            return
        want = "short" if position_side == "Buy" else "long"
        if fvg.direction != want:
            return
        if self.exit_pending is not None:
            gap = fvg.start_time - self.exit_pending["last_time"]
            consecutive = 0 < gap <= int(self.interval_ms * 1.5)
            if consecutive and self.exit_pending["chain"] < self.max_chain:
                self.exit_pending.update({
                    "mid": fvg.mid, "top": fvg.top, "bottom": fvg.bottom,
                    "last_time": fvg.start_time,
                    "chain": self.exit_pending["chain"] + 1,
                })
                self.log.info("[%s] Chained exit FVG #%d. Close at mid %.6f." %
                              (self.symbol, self.exit_pending["chain"],
                               fvg.mid))
                return
        self.exit_pending = {
            "direction": want, "mid": fvg.mid, "top": fvg.top,
            "bottom": fvg.bottom, "last_time": fvg.start_time, "chain": 1,
        }
        self.exit_last_price = None
        self.log.info("[%s] Reversal FVG. Close at mid %.6f on retrace." %
                      (self.symbol, fvg.mid))

    def _check_mitigation(self, candle):
        if self.pending is None or self.pending["triggered"]:
            return
        close = candle["close"]
        p = self.pending
        if p["direction"] == "long" and close < p["bottom"]:
            self.log.info("[%s] Long setup invalidated." % self.symbol)
            self.pending = None
        elif p["direction"] == "short" and close > p["top"]:
            self.log.info("[%s] Short setup invalidated." % self.symbol)
            self.pending = None

    # ------------------------------------------------------------------ #
    # entry preparation (placement is done by the Bot)
    # ------------------------------------------------------------------ #
    def has_pending(self):
        return self.pending is not None and not self.pending["triggered"]

    def retrace_reached(self, last_price):
        """True when price retraces to the entry FVG mid from the origin side."""
        if not self.has_pending():
            self.last_price = last_price
            return False
        p = self.pending
        prev = self.last_price
        self.last_price = last_price
        return _crossed_to_mid(p["direction"], p["mid"], p["top"],
                               p["bottom"], prev, last_price)

    # ------------------------------------------------------------------ #
    # exit on opposite (reversal) FVG
    # ------------------------------------------------------------------ #
    def has_exit(self):
        return self.exit_pending is not None

    def exit_reached(self, last_price):
        """True when price retraces to the exit (reversal) FVG mid."""
        if self.exit_pending is None:
            self.exit_last_price = last_price
            return False
        p = self.exit_pending
        prev = self.exit_last_price
        self.exit_last_price = last_price
        return _crossed_to_mid(p["direction"], p["mid"], p["top"],
                               p["bottom"], prev, last_price)

    def clear_exit(self):
        self.exit_pending = None
        self.exit_last_price = None

    def prepare_entry(self, balance, price):
        """Return an order spec dict if a position can be sized, else None.
        Does NOT place the order."""
        direction = self.pending["direction"]
        qty = self.compute_qty(balance, price)
        if qty <= 0:
            return None
        tp, sl = self.compute_tp_sl(Decimal(str(price)), direction)
        return {
            "symbol": self.symbol,
            "direction": direction,
            "side": "Buy" if direction == "long" else "Sell",
            "qty": qty,
            "entry": price,
            "tp": tp,
            "sl": sl,
            "chain": self.pending["chain"],
            "leverage": self.leverage_str(),
        }

    def mark_entered(self):
        if self.pending is not None:
            self.pending["triggered"] = True
        self.pending = None
