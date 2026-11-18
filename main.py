"""Patient trend rider for agent-fun/bars-replay/v1 (make.fun btc-race-24h-20261118-1444).

One asset (CBBTC/USDC), long only, one position at a time, full-size entries.
The replay fills an order decided on bar k at bar k+1's open and charges about
26 bps per full-size round trip (4 bps fee + 2 bps modelled slippage + pool
impact), so the bot only trades slow, confirmed trends and never chases
minute-scale noise: every fast signal I tested lost to the 1-bar latency.

Entry, when flat and at least SLOW closes have been seen:
  - EMA(FAST) sits above EMA(SLOW) by more than GAP_BPS (a developed trend,
    not a fresh cross), and
  - the 60-bar momentum is above MOM_ENTER_BPS.
  A buy spends all cash. Once equity is LOCK_PROFIT_USDC above the start
  while flat, the profit lock latches and re-entry needs momentum above
  MOM_LOCKED_BPS: banked profit is only risked on exceptional moves.

Exit, the whole position, on the first of:
  - a trailing stop: the close falls TRAIL_BPS below the highest close
    since entry,
  - the trend flip: EMA(FAST) back under EMA(SLOW),
  - momentum exhaustion: the 60-bar momentum falls under MOM_EXIT_BPS.
After any exit the bot waits COOLDOWN_BARS bars before it may enter again.

Determinism: no clock, no environment, no randomness; the EMAs run in
float64 on + - * / alone, every threshold comparison is exact integer
arithmetic on the bar closes, and gaps (a batch with no bar for the
symbol) advance nothing. Two runs on the same bars send the same orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

FAST = 30
SLOW = 120
MOM_WIN = 60
GAP_BPS = 15
MOM_ENTER_BPS = 20
MOM_LOCKED_BPS = 80
MOM_EXIT_BPS = -20
TRAIL_BPS = 80
COOLDOWN_BARS = 60
LOCK_PROFIT_UNITS = 5_000_000  # $5.00 in micro-USDC
MAX_SLIPPAGE_BPS = 100


class Ema:
    """An EMA seeded as the mean of its first span of closes, then 2/(span+1)
    of the way to each new close, exactly like the kit's baseline bots."""

    def __init__(self, span):
        self.span = span
        self.warm = []
        self.value = None

    def step(self, close):
        if self.value is not None:
            self.value += (2.0 / (self.span + 1)) * (close - self.value)
            return
        self.warm.append(close)
        if len(self.warm) == self.span:
            self.value = sum(self.warm) / self.span


class Bot:
    def __init__(self, hello):
        self.symbol = hello["symbols"][0]["symbol"]
        self.start_cash = int(hello["capital"]["quote_units"])
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.fast = Ema(FAST)
        self.slow = Ema(SLOW)
        self.closes = []  # int closes, one per bar seen for the symbol
        self.peak = 0  # highest close since the current entry was decided
        self.cool_until = -1  # first batch seq allowed to enter again
        self.locked = False  # profit lock, latches once equity is ahead

    def on_bar(self, close):
        self.closes.append(close)
        self.fast.step(float(close))
        self.slow.step(float(close))

    def momentum_bps_num(self, close):
        """(close - close MOM_WIN bars ago) * 10000, the numerator of the
        60-bar momentum in bps; None until enough bars have been seen."""
        if len(self.closes) <= MOM_WIN:
            return None
        prev = self.closes[-1 - MOM_WIN]
        return (close - prev) * 10_000, prev

    def decide(self, state, batch):
        bars = [b for b in batch["bars"] if b["symbol"] == self.symbol]
        if not bars:
            return []  # a gap minute: no new bar, no new decision
        close = int(bars[-1]["c"])
        self.on_bar(close)
        if state.warming_up:
            return []

        held = state.positions.get(self.symbol)
        if held is not None and held.qty > 0:
            if close > self.peak:
                self.peak = close
            if self._should_exit(state, close):
                self.cool_until = state.seq + COOLDOWN_BARS
                return [{
                    "side": "sell",
                    "symbol": self.symbol,
                    "qty": held.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
            return []

        # Flat: latch the profit lock, then look for an entry.
        if state.equity >= self.start_cash + LOCK_PROFIT_UNITS:
            self.locked = True
        if state.seq < self.cool_until:
            return []
        if self.fast.value is None or self.slow.value is None:
            return []
        gap_bps = (self.fast.value - self.slow.value) * 10_000 / self.slow.value
        if gap_bps <= GAP_BPS:
            return []
        mom = self.momentum_bps_num(close)
        if mom is None:
            return []
        mom_num, prev = mom
        threshold = MOM_LOCKED_BPS if self.locked else MOM_ENTER_BPS
        if mom_num <= threshold * prev:
            return []
        size = min(state.cash, state.weight_room(self.symbol))
        if size < self.min_notional:
            return []
        self.peak = close
        return [{
            "side": "buy",
            "symbol": self.symbol,
            "notional_quote_units": size,
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }]

    def _should_exit(self, state, close):
        # Trailing stop, exact integer arithmetic.
        if close * 10_000 < self.peak * (10_000 - TRAIL_BPS):
            return True
        # Trend flip.
        if self.fast.value is not None and self.slow.value is not None:
            if self.fast.value < self.slow.value:
                return True
        # Momentum exhaustion.
        mom = self.momentum_bps_num(close)
        if mom is not None:
            mom_num, prev = mom
            if mom_num < MOM_EXIT_BPS * prev:
                return True
        return False


_BOT = None


def on_hello(state):
    global _BOT
    _BOT = Bot(state.hello)


def on_batch(state, batch):
    return _BOT.decide(state, batch)


makefun_bars.run(on_batch, on_hello=on_hello)
