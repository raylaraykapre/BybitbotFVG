"""
Fair Value Gap (FVG) detection.

Ported from the LuxAlgo "Fair Value Gap [LuxAlgo]" Pine Script indicator
(CC BY-NC-SA 4.0, see LICENSE). The detection rule is identical to the
`detect()` function of the original script:

    bull_fvg = low > high[2]  and close[1] > high[2]
               and (low - high[2]) / high[2] > threshold
    bear_fvg = high < low[2]  and close[1] < low[2]
               and (low[2] - high) / high   > threshold

Indexing note: in Pine, the *current* bar is index 0, [1] is the previous
bar, and [2] is two bars back. Here we work on a list of closed candles
ordered oldest -> newest, and evaluate the 3-bar window ending at index i:
    c0 = candles[i]      (the "current" bar in Pine terms)
    c1 = candles[i - 1]  ([1])
    c2 = candles[i - 2]  ([2])
"""


class FVG:
    """A detected Fair Value Gap.

    Attributes
    ----------
    top, bottom : float
        Upper / lower price boundary of the gap.
    is_bull : bool
        True for a bullish FVG (gap up), False for bearish (gap down).
    index : int
        Index of the candle (c0) that completed the gap.
    start_time : int
        Start time (ms) of c0.
    """

    __slots__ = ("top", "bottom", "is_bull", "index", "start_time")

    def __init__(self, top, bottom, is_bull, index, start_time):
        self.top = float(top)
        self.bottom = float(bottom)
        self.is_bull = bool(is_bull)
        self.index = int(index)
        self.start_time = int(start_time)

    @property
    def mid(self):
        return (self.top + self.bottom) / 2.0

    @property
    def size(self):
        return self.top - self.bottom

    @property
    def direction(self):
        return "long" if self.is_bull else "short"

    def to_dict(self):
        return {
            "direction": self.direction,
            "top": self.top,
            "bottom": self.bottom,
            "mid": self.mid,
            "index": self.index,
            "start_time": self.start_time,
        }

    def __repr__(self):
        return ("FVG(%s top=%.4f bottom=%.4f mid=%.4f idx=%d)" %
                (self.direction, self.top, self.bottom, self.mid, self.index))


def detect_fvg_at(candles, i, threshold=0.0):
    """Detect an FVG completing at candle index i. Returns an FVG or None."""
    if i < 2:
        return None
    c0 = candles[i]
    c1 = candles[i - 1]
    c2 = candles[i - 2]

    # Bullish FVG: gap between high two bars ago and current low.
    if (c0["low"] > c2["high"] and c1["close"] > c2["high"]):
        if c2["high"] > 0 and (c0["low"] - c2["high"]) / c2["high"] > threshold:
            return FVG(top=c0["low"], bottom=c2["high"], is_bull=True,
                       index=i, start_time=c0["start"])

    # Bearish FVG: gap between low two bars ago and current high.
    if (c0["high"] < c2["low"] and c1["close"] < c2["low"]):
        if c0["high"] > 0 and (c2["low"] - c0["high"]) / c0["high"] > threshold:
            return FVG(top=c2["low"], bottom=c0["high"], is_bull=False,
                       index=i, start_time=c0["start"])

    return None


def detect_all_fvgs(candles, threshold=0.0):
    """Return all FVGs in a candle list, oldest -> newest."""
    out = []
    for i in range(2, len(candles)):
        fvg = detect_fvg_at(candles, i, threshold=threshold)
        if fvg is not None:
            out.append(fvg)
    return out


def auto_threshold(candles):
    """Replicate the indicator's 'Auto' threshold.

    threshold = cumulative((high - low) / low) / bar_count
    i.e. the running average of each bar's range relative to its low.
    """
    if not candles:
        return 0.0
    total = 0.0
    count = 0
    for c in candles:
        if c["low"] > 0:
            total += (c["high"] - c["low"]) / c["low"]
            count += 1
    return (total / count) if count else 0.0
