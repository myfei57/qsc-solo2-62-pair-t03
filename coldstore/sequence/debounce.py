"""Delayed confirmation of feedback signals, with chatter detection."""

from __future__ import annotations


class Debouncer:
    """Trust a raw signal only after it holds steady for ``delay`` seconds.

    Contact bounce on a limit switch is normal and is absorbed here: the
    debounced value only flips once the raw input has been stable for the
    whole delay. Too many edges inside the sliding window means the feedback
    loop itself is faulty, which the caller escalates instead of releasing
    the interlock to manual judgement by default.
    """

    def __init__(self, delay: float, chatter_edges: int, chatter_window: float) -> None:
        self.delay = delay
        self.chatter_edges = chatter_edges
        self.chatter_window = chatter_window
        self.stable: bool | None = None
        self._raw: bool | None = None
        self._raw_since = 0.0
        self._edges: list[float] = []

    def update(self, raw: bool, now: float) -> bool:
        """Fold one raw sample in; return True when chatter exceeds the limit."""

        if self._raw is None:
            self._raw = raw
            self._raw_since = now
        elif raw != self._raw:
            self._raw = raw
            self._raw_since = now
            self._edges.append(now)
            self._edges = [at for at in self._edges if now - at <= self.chatter_window]
            if len(self._edges) > self.chatter_edges:
                return True
        self.refresh(now)
        return False

    def refresh(self, now: float) -> None:
        """Re-evaluate the stable value, as an IO scan would every cycle."""

        if self._raw is None:
            return
        if self.stable is None:
            if now - self._raw_since >= self.delay:
                self.stable = self._raw
        elif self._raw != self.stable and now - self._raw_since >= self.delay:
            self.stable = self._raw

    @property
    def edges(self) -> int:
        """Edges currently inside the sliding window."""

        return len(self._edges)
