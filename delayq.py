"""
delayq.py — 持久化延迟消息队列（仅 Python 3 标准库）。

设计要点
========
1. 投递次序
   每条计划在 schedule 时得到一个单调递增的入队序号 seq。
   最小堆元素为 (due_ms, seq, msg_id)，因此到期次序严格按
       (到期时间, 入队序号)
   排序：先按到期时间；同一时刻到期的按入队先后投递，确定且不随机。

2. 持久化与重启恢复
   所有状态变化以 JSON Lines 追加写入 WAL（write-ahead log，文件 queue.log）：
       {"op":"S","id":..,"at":..,"seq":..,"p":..}  计划
       {"op":"D","ids":[..]}                        一批投递
       {"op":"C","id":..,"st":"ok"}                 取消（未到期）
       {"op":"C","id":..,"st":"delivered"}          取消（已投递）
   打开队列时重放 WAL：S 建堆，D 标记已投递，C 标记已取消。
   堆用 list 一次性 heapify，恢复复杂度 O(n)。
   seq 持久化在每条 S 记录里，重启后继续递增，不依赖内存状态。

   持久化等级：
   - 每条 S/C/批量 D 记录后 flush（Python 层缓冲落盘，防进程崩溃/kill -9）；
   - fsync=True 时还调用 os.fsync（防整机断电，默认关，压测可关）。
   进程崩溃语义：D 记录与回调之间若进程被杀，重启后该消息可能再投递一次
   （at-least-once）；正常重启不重复（已提交的 D 记录会被重放）。

3. 取消语义（幂等、确定）
   cancel(id) 返回四种确定状态：
       OK         消息存在且未到期投递，已取消，不再投递
       DELIVERED  消息已经投递（取消晚了），属于"已投递的取消"的确定处理：
                  不会回滚投递，返回 DELIVERED 并在 WAL 记一条 C/st=delivered
       CANCELED   消息此前已被取消（重复取消）
       UNKNOWN    没有这个 id（从未计划，或投递后无取消记录）
   规则：只有状态为 pending 的取消才真正生效；状态只沿
       pending -> canceled / pending -> delivered 单向迁移。

4. 到期扫描复杂度
   due(now_ms) 只反复弹出堆顶 while heap[0].due <= now：
       时间 O(k * log n)，k=本次到期条数，与未到期消息数量无关；
   不会遍历全部计划，所以堆积大量未到期消息不会使扫描变慢。
   取消是惰性删除：cancel 只改状态表，堆元素留在堆里，到期弹出时跳过，
   每条惰性元素至多被处理一次（摊还 O(log n)）。
   未使用任何随机数，顺序完全确定。

时间由调用方注入（due_ms / now_ms 均为调用方给定的整数毫秒），
本模块不调用 time.time() 等任何时钟，便于确定性自测。
"""

from __future__ import annotations

import heapq
import json
import os
from dataclasses import dataclass
from typing import Iterator, Optional

# cancel 的确定返回状态
OK = "ok"
DELIVERED = "delivered"
CANCELED = "canceled"
UNKNOWN = "unknown"

_PENDING = 0
_DELIVERED = 1
_CANCELED = 2


@dataclass(frozen=True)
class Delivery:
    id: str
    payload: str
    at_ms: int
    seq: int


class DelayQueue:
    def __init__(self, path: str, fsync: bool = False):
        self.path = path
        self._fsync = fsync
        # 堆：(due_ms, seq, msg_id)
        self._heap: list[tuple[int, int, str]] = []
        # msg_id -> [due_ms, seq, payload, state]
        self._meta: dict[str, list] = {}
        self._next_seq = 0
        self._fh = open(path, "a+", encoding="utf-8", buffering=1)
        self._fh.seek(0)
        scheduled = 0
        delivered = 0
        canceled = 0
        for line in self._fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            op = rec["op"]
            if op == "S":
                self._meta[rec["id"]] = [rec["at"], rec["seq"], rec["p"], _PENDING]
                self._heap.append((rec["at"], rec["seq"], rec["id"]))
                if rec["seq"] >= self._next_seq:
                    self._next_seq = rec["seq"] + 1
                scheduled += 1
            elif op == "D":
                for mid in rec["ids"]:
                    if mid in self._meta:
                        self._meta[mid][3] = _DELIVERED
                        delivered += 1
            elif op == "C":
                if rec["id"] in self._meta:
                    if rec["st"] == OK:
                        self._meta[rec["id"]][3] = _CANCELED
                    else:  # delivered：投递记录已先于取消记录落盘
                        self._meta[rec["id"]][3] = _DELIVERED
                    canceled += 1
        heapq.heapify(self._heap)
        self.recovered = {
            "scheduled": scheduled,
            "delivered_records": delivered,
            "cancel_records": canceled,
        }

    # ---- 持久化 ----
    def _write(self, rec: dict) -> None:
        self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._fh.flush()
        if self._fsync:
            os.fsync(self._fh.fileno())

    # ---- API ----
    def schedule(self, msg_id: str, at_ms: int, payload: str) -> int:
        """登记一条延迟消息。id 重复会抛 ValueError（调度不允许复用 id）。"""
        if not isinstance(msg_id, str) or not msg_id:
            raise ValueError("msg_id must be a non-empty string")
        if msg_id in self._meta:
            raise ValueError(f"duplicate msg_id: {msg_id}")
        seq = self._next_seq
        self._next_seq += 1
        self._meta[msg_id] = [at_ms, seq, payload, _PENDING]
        heapq.heappush(self._heap, (at_ms, seq, msg_id))
        self._write({"op": "S", "id": msg_id, "at": at_ms, "seq": seq, "p": payload})
        return seq

    def cancel(self, msg_id: str) -> str:
        """取消消息，返回 OK / DELIVERED / CANCELED / UNKNOWN，幂等。"""
        entry = self._meta.get(msg_id)
        if entry is None:
            return UNKNOWN
        state = entry[3]
        if state == _CANCELED:
            return CANCELED
        if state == _DELIVERED:
            # 已投递的取消：不回滚、不重投，确定返回 DELIVERED（记录便于审计）
            self._write({"op": "C", "id": msg_id, "st": DELIVERED})
            return DELIVERED
        entry[3] = _CANCELED
        self._write({"op": "C", "id": msg_id, "st": OK})
        return OK

    def due(self, now_ms: int) -> list[Delivery]:
        """取出并提交 now_ms 时刻全部到期消息，按 (at_ms, seq) 确定排序。"""
        out: list[Delivery] = []
        while self._heap and self._heap[0][0] <= now_ms:
            at_ms, seq, msg_id = heapq.heappop(self._heap)
            entry = self._meta.get(msg_id)
            if entry is None or entry[3] != _PENDING:
                continue  # 已取消/已投递的惰性元素
            entry[3] = _DELIVERED
            out.append(Delivery(msg_id, entry[2], at_ms, seq))
        if out:
            # 一批投递只写一条 D 记录（先落盘再交给调用方）
            self._write({"op": "D", "ids": [d.id for d in out]})
        return out

    def peek_due_ms(self) -> Optional[int]:
        """下一条有效到期时间（跳过惰性元素），供调度线程 sleep 用。"""
        while self._heap:
            at_ms, _seq, msg_id = self._heap[0]
            entry = self._meta.get(msg_id)
            if entry is not None and entry[3] == _PENDING:
                return at_ms
            heapq.heappop(self._heap)
        return None

    def stats(self) -> dict:
        pending = sum(1 for e in self._meta.values() if e[3] == _PENDING)
        delivered = sum(1 for e in self._meta.values() if e[3] == _DELIVERED)
        canceled = sum(1 for e in self._meta.values() if e[3] == _CANCELED)
        return {
            "pending": pending,
            "delivered": delivered,
            "canceled": canceled,
            "heap_entries": len(self._heap),
            "next_seq": self._next_seq,
        }

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()

    def __enter__(self) -> "DelayQueue":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
