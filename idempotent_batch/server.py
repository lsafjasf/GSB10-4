"""带幂等键的模拟服务端（测试替身）。

关键承诺，等价于真实系统中“服务端支持幂等键”的契约：

1. 提交分两步：先生成副作用（effect），再把 (idem_key -> result) 写入提交日志；
   写入日志后调用才可能返回成功。
2. 同一个 idem_key 的重复提交：命中日志则**只回放结果，不再执行副作用**；
   带不同 payload 回放会被拒绝（422），暴露调用方误用。
3. 504/-3/-4 这类错误可以在“副作用已产生、但响应丢失”时返回，用于模拟
   “调用方结果未知、实际已提交”的场景。
4. lookup（对账查询）在服务端入口处失败时表现为传输错误，但服务端本身没动；
   durable_lookup 直接读提交日志，代表“事后从持久层/对账通道核实”，只可能
   返回三种确定事实：已提交成功 / 已提交永久失败 / 不存在。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import BatchError

# 脚本动作（由工作负载按 item 类型预置，服务端按尝试次数执行）。
COMMIT = "COMMIT"                 # 执行副作用并提交成功
PERMANENT = 422                   # 提交一条“永久失败”（确定性业务拒绝）
PRE_503 = 503                     # 受理前失败，无副作用
PRE_429 = 429
AMBIG_TIMEOUT = -3                # 发送后/响应丢失：副作用已产生与否按位置决定
POST_504 = 504                    # 副作用已提交但响应丢失（结果未知）
POST_500 = 500
LOOKUP_TRANSIENT = 503            # 对账接口入口失败（-1/503，服务端无动作）
LOOKUP_GATEWAY = -1


@dataclass
class Committed:
    key: str
    payload: object
    ok: bool
    code: int                       # 200 成功；否则为永久失败码


@dataclass
class _State:
    commits: dict[str, Committed] = field(default_factory=dict)
    effects: list[str] = field(default_factory=list)   # 真实副作用流水
    submit_calls: int = 0
    lookup_calls: int = 0
    durable_calls: int = 0
    replayed: int = 0


class FakeServer:
    def __init__(self, delay: float = 0.0):
        self._lock = threading.Lock()
        self._state = _State()
        self._scripts: dict[str, list] = {}
        self._lookup_scripts: dict[str, list] = {}
        self._delay = delay

    def register(self, key: str, submit_script: list, lookup_script: Optional[list] = None):
        self._scripts[key] = list(submit_script)
        if lookup_script is not None:
            self._lookup_scripts[key] = list(lookup_script)

    # ---- 提交 ----
    def submit(self, key: str, payload: object, attempt: int) -> Committed:
        """attempt 从 0 开始，代表“针对该幂等键的第几次提交调用”（含重放）。"""
        with self._lock:
            self._state.submit_calls += 1
            prior = self._state.commits.get(key)
            if prior is not None:
                if prior.payload != payload:
                    raise BatchError(422, "idempotency key reused with different payload")
                self._state.replayed += 1
                return prior

            action = self._scripts.get(key, [COMMIT])
            step = action[min(attempt, len(action) - 1)]

        # 锁外模拟耗时，保持并发性。
        if self._delay:
            _sleep(self._delay)

        with self._lock:
            # 双重检查：模拟“等待期间另一线程已用同键提交”。
            prior = self._state.commits.get(key)
            if prior is not None:
                self._state.replayed += 1
                return prior

            if step == COMMIT:
                self._state.effects.append(key)
                rec = Committed(key, payload, True, 200)
                self._state.commits[key] = rec
                return rec
            if isinstance(step, int) and step in (422, 400, 403, 404):
                rec = Committed(key, payload, False, step)
                self._state.commits[key] = rec   # 永久失败同样落日志，可回放
                raise BatchError(step, "permanent business rejection")
            if step in (PRE_503, PRE_429, 502, -1, -2):
                raise BatchError(step, "pre-commit transient failure")
            if step == AMBIG_TIMEOUT:
                # 语义上“响应丢失”：本实现把它放在提交前（请求可能没执行），
                # 由调用方对账确认；POST_* 才是“副作用已产生”的未知。
                raise BatchError(step, "response timed out (unknown outcome)")
            if step in (POST_504, POST_500):
                # 副作用已经产生并提交，只是响应没回去 => 必须对账出成功。
                self._state.effects.append(key)
                rec = Committed(key, payload, True, 200)
                self._state.commits[key] = rec
                raise BatchError(step, "response lost after commit")
            raise BatchError(500, f"unknown script action {step!r}")

    # ---- 对账 ----
    def lookup(self, key: str, lookup_attempt: int):
        """在线对账。可能返回 Committed / None（确定未提交）/ 抛传输错误。"""
        with self._lock:
            self._state.lookup_calls += 1
            script = self._lookup_scripts.get(key)
            if script:
                step = script[min(lookup_attempt, len(script) - 1)]
                if step in (LOOKUP_TRANSIENT, LOOKUP_GATEWAY, 502, -2):
                    raise BatchError(step, "lookup transport failure")
                if step == "OK":
                    return self._state.commits.get(key)
            return self._state.commits.get(key)

    def durable_lookup(self, key: str):
        """持久层核实：只返回确定事实，绝不抛“结果未知”。"""
        with self._lock:
            self._state.durable_calls += 1
            rec = self._state.commits.get(key)
            if rec is None:
                return None
            return ("COMMITTED_SUCCESS" if rec.ok else "COMMITTED_FAILURE", rec)

    # ---- 观测 ----
    def effect_count(self) -> int:
        with self._lock:
            return len(self._state.effects)

    def effects_snapshot(self) -> list[str]:
        with self._lock:
            return sorted(self._state.effects)

    def stats_snapshot(self) -> dict:
        with self._lock:
            return {"submit_calls": self._state.submit_calls,
                    "lookup_calls": self._state.lookup_calls,
                    "durable_calls": self._state.durable_calls,
                    "replayed": self._state.replayed,
                    "committed": len(self._state.commits),
                    "effects": len(self._state.effects)}


def _sleep(seconds: float):
    import time
    time.sleep(seconds)


# --------------------------------------------------------------------------
# 工作负载：为每个条目生成确定性的提交/对账脚本
# --------------------------------------------------------------------------
class Workload:
    """条目类型 -> (提交脚本, 对账脚本, 预期结果)。

    预期结果表示“无限重试 + 强制对账后”该条目的确定结局。
    """

    KINDS = {
        # 全部成功
        "success":            ([COMMIT], None, "SUCCESS"),
        # 第 1 次 503（无副作用），第 2 次成功
        "transient_then_ok":  ([503, COMMIT], None, "SUCCESS"),
        "rate_then_ok":       ([429, 429, COMMIT], None, "SUCCESS"),
        # 永久失败
        "permanent_422":      ([422], None, "FAILURE_PERMANENT"),
        "permanent_403":      ([403], None, "FAILURE_PERMANENT"),
        # 重试中再次失败：连续两次超时（提交前未知），第 3 次才成功
        "timeout_twice_ok":   ([-3, -3, COMMIT], [503, "OK"], "SUCCESS_AFTER_RECONCILE"),
        # 超时后实际已经提交（副作用已产生、响应丢失），对账确认成功
        "post_504_committed": ([504], ["OK"], "SUCCESS_AFTER_RECONCILE"),
        "post_500_committed": ([500], [503, "OK"], "SUCCESS_AFTER_RECONCILE"),
        # 在线对账持续故障，走持久层核实：实际已提交
        "durable_reconciled": ([504], [503, 503, 503], "SUCCESS_AFTER_RECONCILE"),
        # 重试预算耗尽：每次都是受理前 503
        "exhausted_503":      ([503], None, "FAILURE_RETRY_EXHAUSTED"),
        # 每次都是提交前超时（结果未知），预算耗尽后由持久层确认“确实没提交”
        "exhausted_timeout":  ([-3], [503], "FAILURE_RETRY_EXHAUSTED"),
        # 先可重试错误，随后永久失败
        "transient_then_perm":([503, 422], None, "FAILURE_PERMANENT"),
    }

    def __init__(self, server: FakeServer):
        self.server = server
        self.specs: list[tuple[str, str, str]] = []   # (key, kind, expected)

    def add(self, key: str, kind: str):
        submit_script, lookup_script, expected = self.KINDS[kind]
        self.server.register(key, submit_script, lookup_script)
        self.specs.append((key, kind, expected))

    @staticmethod
    def seeded_mix(server: FakeServer, n: int, seed: int = 275):
        """生成 n 条确定性混合负载。类型比例固定，可复现。"""
        wl = Workload(server)
        # 权重（%）：大多数一次成功，少量各种失败/对账情形
        weights = [
            ("success", 70),
            ("transient_then_ok", 8),
            ("rate_then_ok", 4),
            ("permanent_422", 5),
            ("permanent_403", 2),
            ("timeout_twice_ok", 3),
            ("post_504_committed", 3),
            ("post_500_committed", 1),
            ("durable_reconciled", 1),
            ("exhausted_503", 2),
            ("exhausted_timeout", 0),          # 压力测试中不放“预算耗尽”，见 bench
            ("transient_then_perm", 1),
        ]
        rng = _Lcg(seed)
        kinds = []
        for kind, pct in weights:
            kinds.extend([kind] * (n * pct // 100))
        while len(kinds) < n:
            kinds.append("success")
        kinds = kinds[:n]
        # 确定性洗牌
        for i in range(n - 1, 0, -1):
            j = rng.next() % (i + 1)
            kinds[i], kinds[j] = kinds[j], kinds[i]
        for i, kind in enumerate(kinds):
            wl.add(f"item-{i:08d}", kind)
        return wl


class _Lcg:
    """确定性线性同余 RNG，避免跨版本 random 差异。"""
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next(self) -> int:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state
