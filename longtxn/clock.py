"""可注入的逻辑时钟：所有时间都由测试/调用方推进，库本身不读墙钟。"""


class ManualClock:
    def __init__(self, start: float = 0.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        if dt < 0:
            raise ValueError("clock cannot go backwards")
        self._t += dt
        return self._t
