"""多阶段任务的部分提交库（仅 Python 标准库）。

可见性规则（对外契约）
----------------------
V1. 阶段内写入在「阶段提交」之前仅对任务自身可见（read-your-writes）：
    任务通过 Transaction.read 能读到本任务已写但未提交的值；
    任何任务外读者（Store.read / Store.snapshot）都看不到。
V2. 一个阶段提交（commit）后，该阶段的全部写入原子地对所有外部读者可见。
    这就是「部分提交」语义：任务未整体完成，已提交阶段的中间结果对外可见。
V3. 任务在阶段 k 失败时，阶段 0..k-1 的已提交效果会被回滚（撤销）；
    回滚完成后，外部读者看到的等价于任务从未执行过的状态。
V4. 失败点所在的未提交阶段（阶段 k）的写入永不对任务外可见，直接丢弃。
V5. 回滚是幂等的：同一阶段的成功撤销只执行一次；重复 rollback()
    不会重复执行已完成的撤销，因此对存储不产生额外副作用。
    回滚中某阶段补偿失败时，该阶段标记为 FAILED，其余阶段继续回滚；
    后续 rollback() 只重试 FAILED 阶段，不重做 DONE 阶段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

MISSING = object()  # 哨兵：键在提交前不存在


class Store:
    """支持阶段级提交的 KV 存储。committed 区的数据对外可见。"""

    def __init__(self) -> None:
        self._data: Dict[Any, Any] = {}
        self.write_count = 0   # 对 committed 区的写次数（用于验证幂等无副作用）
        self.commit_count = 0  # 阶段提交次数

    # ---- 外部读者接口：只能看到 committed 区 ----
    def read(self, key: Any, default: Any = None) -> Any:
        return self._data.get(key, default)

    def snapshot(self) -> Dict[Any, Any]:
        return dict(self._data)

    # ---- 内部写接口 ----
    def _apply(self, key: Any, value: Any) -> None:
        self._data[key] = value
        self.write_count += 1

    def _delete(self, key: Any) -> None:
        self._data.pop(key, None)
        self.write_count += 1

    def _commit(self, writes: Dict[Any, Any]) -> List[Tuple[Any, Any]]:
        """原子提交一个阶段的写入，返回撤销日志 [(key, 旧值或MISSING)]。"""
        entries = [(k, self._data.get(k, MISSING)) for k in writes]
        for k, v in writes.items():
            if v is MISSING:
                self._delete(k)
            else:
                self._apply(k, v)
        self.commit_count += 1
        return entries


class Transaction:
    """任务私有的写缓冲。阶段提交前，写入只在这里（规则 V1）。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.writes: Dict[Any, Any] = {}

    def write(self, key: Any, value: Any) -> None:
        self.writes[key] = value

    def read(self, key: Any, default: Any = None) -> Any:
        """read-your-writes：先读本任务缓冲，再读 committed 区。"""
        if key in self.writes:
            value = self.writes[key]
            return default if value is MISSING else value
        return self._store.read(key, default)

    def delete(self, key: Any) -> None:
        """在任务内删除一个键（提交前仅任务内可见，回滚时恢复旧值）。"""
        self.writes[key] = MISSING


class Stage:
    """一个阶段：apply 执行业务写入，compensate 可选自定义补偿逻辑。

    apply(tx): 通过 tx.write 产生写入；抛异常即本阶段失败。
    compensate(store, undo_entries): 自定义补偿；缺省时按撤销日志恢复旧值。
    """

    def __init__(
        self,
        name: str,
        apply: Callable[[Transaction], None],
        compensate: Optional[Callable[[Store, List[Tuple[Any, Any]]], None]] = None,
    ) -> None:
        self.name = name
        self.apply = apply
        self.compensate = compensate


class UndoState(Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass
class StageUndo:
    """一个已提交阶段的撤销记录。"""

    stage_name: str
    entries: List[Tuple[Any, Any]]
    compensate: Optional[Callable[[Store, List[Tuple[Any, Any]]], None]] = None
    state: UndoState = UndoState.PENDING
    attempts: int = 0          # 补偿实际执行次数（幂等性的直接证据）
    error: Optional[str] = None

    def rollback(self, store: Store) -> None:
        if self.state is UndoState.DONE:
            return  # 幂等关键：已完成的撤销绝不重复执行（规则 V5）
        self.attempts += 1
        try:
            if self.compensate is not None:
                self.compensate(store, self.entries)
            else:
                for key, old in reversed(self.entries):
                    if old is MISSING:
                        store._delete(key)
                    else:
                        store._apply(key, old)
            self.state = UndoState.DONE
            self.error = None
        except Exception as exc:  # 回滚中再次失败：标记 FAILED，不中断其他阶段
            self.state = UndoState.FAILED
            self.error = repr(exc)


@dataclass
class StageRollbackReport:
    stage: str
    state: str
    attempts: int
    error: Optional[str]


@dataclass
class RollbackResult:
    rollback_calls: int                     # rollback() 累计调用次数
    per_stage: List[StageRollbackReport]    # 每个失败点上的回退结果
    fully_rolled_back: bool


@dataclass
class TaskResult:
    status: str                 # "committed" | "rolled_back" | "rollback_incomplete"
    failed_stage: Optional[int] = None
    error: Optional[str] = None
    rollback: Optional[RollbackResult] = None


class Task:
    """多阶段任务：逐阶段执行并部分提交，失败时按逆序回滚。"""

    def __init__(self, store: Store, stages: List[Stage], name: str = "") -> None:
        self.store = store
        self.stages = stages
        self.name = name
        self._undo_log: List[StageUndo] = []
        self._rollback_calls = 0
        self.failed_stage: Optional[int] = None
        self.error: Optional[str] = None

    def run(self) -> TaskResult:
        for i, stage in enumerate(self.stages):
            tx = Transaction(self.store)
            try:
                stage.apply(tx)
            except Exception as exc:
                # 规则 V4：未提交的阶段写入直接丢弃，外部不可见
                self.failed_stage = i
                self.error = f"stage[{i}] {stage.name}: {exc!r}"
                rb = self.rollback()
                status = "rolled_back" if rb.fully_rolled_back else "rollback_incomplete"
                return TaskResult(status=status, failed_stage=i,
                                  error=self.error, rollback=rb)
            entries = self.store._commit(tx.writes)  # 规则 V2：部分提交点
            self._undo_log.append(StageUndo(stage.name, entries, stage.compensate))
        return TaskResult(status="committed")

    def rollback(self) -> RollbackResult:
        """幂等回滚：DONE 阶段跳过，仅 PENDING/FAILED 阶段会被（重）尝试。"""
        self._rollback_calls += 1
        for undo in reversed(self._undo_log):
            undo.rollback(self.store)
        report = [
            StageRollbackReport(u.stage_name, u.state.value, u.attempts, u.error)
            for u in reversed(self._undo_log)
        ]
        return RollbackResult(
            rollback_calls=self._rollback_calls,
            per_stage=report,
            fully_rolled_back=all(u.state is UndoState.DONE for u in self._undo_log),
        )
