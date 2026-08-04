"""Shared live-poll scheduling, factored out of the dashboard backend so the
web dashboard, the terminal `monitor`, and `record` all schedule reads the same
way instead of each rolling their own loop.

The scheduler keeps a few non-answering or slow ids from dragging a live view:

* **fast lane** — trusted ids (verified/experimental/discovered) read every
  cycle with a tight per-status timeout;
* **slow lane** — a candidate/unconfirmed id may not answer at all, so at most
  one is polled per cycle and no more often than ``slow_interval``;
* **back-off** — an id that keeps missing is retried only occasionally, so it
  never stalls the good params;
* **cache** — the last good value rides through a transient miss (with its age),
  so a dropped frame doesn't blank the display.

``record`` wants faithful sampling instead (every param every cycle, a miss is a
real gap, no stale value passed off as a sample), so it uses ``live=False``:
the fast/slow/back-off/cache behaviour is skipped and misses are reported as-is.

The caller supplies ``read_one(param, timeout) -> value`` (raising on a miss) and
renders the returned ``PollResult`` list however it likes — web rows, a terminal
table, a CSV row.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, perf_counter

# Trusted ids answer in ~20 ms, so a tight timeout keeps a miss cheap; a
# candidate (unconfirmed) id may not answer at all, so it goes in the slow lane.
FAST_STATUS = {"verified-against-vida", "verified", "experimental", "discovered"}
SLOW_INTERVAL = 3.0   # seconds: how rarely a candidate/unknown id is polled live


def read_timeout(status: str) -> float:
    if status in ("verified-against-vida", "verified"):
        return 0.10
    if status in ("experimental", "discovered"):
        return 0.15
    return 0.25


@dataclass
class PollResult:
    """One parameter's outcome for a cycle. ``value`` is the raw read value (the
    caller formats it); ``fresh`` means it was read this cycle (vs served from
    cache); ``age`` is seconds since the value was actually read (0 when fresh)."""
    param: object
    value: object = None
    fresh: bool = False
    ok: bool = False
    age: float = 0.0
    error: str = ""


@dataclass
class PollStats:
    cycle_ms: float = 0.0
    rate: float | None = None
    selected: int = 0
    reads: int = 0
    timeouts: int = 0

    def as_dict(self) -> dict:
        return {"cycle_ms": self.cycle_ms, "rate": self.rate,
                "selected": self.selected, "reads": self.reads,
                "timeouts": self.timeouts}


class PollScheduler:
    """Schedules reads across cycles. Create once and call :meth:`cycle` per tick
    with the parameters to poll; it decides which to actually read, serves the
    rest from cache (live mode), and records timing in :attr:`stats`."""

    def __init__(self, read_one, *, live: bool = True,
                 slow_interval: float = SLOW_INTERVAL) -> None:
        self._read_one = read_one       # (param, timeout) -> value, raises on miss
        self._live = live
        self._slow_interval = slow_interval
        self._miss: dict = {}           # key -> consecutive misses (back-off)
        self._last: dict = {}           # key -> {"value": raw, "t": monotonic}
        self._slow_next: dict = {}      # key -> next monotonic time a slow id may poll
        self._poll = 0
        self.stats = PollStats()

    def cached(self, param) -> PollResult:
        """The last good value for a key not read this cycle, aged; or a miss."""
        last = self._last.get(param.key)
        if last is not None:
            return PollResult(param, value=last["value"], ok=True,
                              age=round(monotonic() - last["t"], 2))
        return PollResult(param, ok=False, error="no data yet")

    def cycle(self, params) -> list:
        self._poll += 1
        now = monotonic()
        started = perf_counter()
        results: list = []
        reads = timeouts = 0
        slow_done = False   # at most one candidate/slow read per cycle (live mode)
        for p in params:
            if self._live:
                slow = p.status not in FAST_STATUS
                misses = self._miss.get(p.key, 0)
                # Back off an id that keeps missing: retry only occasionally.
                backed_off = misses >= 3 and self._poll % 12 != 0
                slow_due = (slow and not slow_done
                            and now >= self._slow_next.get(p.key, 0.0))
                if backed_off or (slow and not slow_due):
                    results.append(self.cached(p))
                    continue
                if slow:
                    slow_done = True
                    self._slow_next[p.key] = now + self._slow_interval
            try:
                value = self._read_one(p, read_timeout(p.status))
            except Exception:  # noqa: BLE001 — any read failure is a miss
                self._miss[p.key] = self._miss.get(p.key, 0) + 1
                timeouts += 1
                results.append(self.cached(p) if self._live
                               else PollResult(p, ok=False, error="no answer"))
                continue
            self._miss[p.key] = 0
            self._last[p.key] = {"value": value, "t": monotonic()}
            reads += 1
            results.append(PollResult(p, value=value, fresh=True, ok=True))
        cycle_ms = (perf_counter() - started) * 1000
        self.stats = PollStats(cycle_ms=round(cycle_ms, 1),
                               rate=round(1000 / cycle_ms, 1) if cycle_ms > 1 else None,
                               selected=len(params), reads=reads, timeouts=timeouts)
        return results
