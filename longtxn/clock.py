"""Injectable clock. Production code reads now(); tests/bench advance it."""


class ManualClock:
    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self._t += float(seconds)
        return self._t

    def set(self, t: float) -> None:
        if t < self._t:
            raise ValueError("clock cannot go backwards")
        self._t = float(t)
