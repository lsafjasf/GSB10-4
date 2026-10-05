# -*- coding: utf-8 -*-
"""
集群健康状态传播库（仅标准库，Python 3.8+）。

设计要点
========
1. 每条上报 Update(node_id, seq, healthy) 带单调递增序号 seq。
2. 防重放：每个节点只保留按 seq 排序的最近 window 条观测（滑动窗口）。
   - seq 已在窗口内          -> DUPLICATE（重复，丢弃）
   - seq 大于历史最大        -> NEW
   - seq 较小但落入窗口范围  -> STALE_MERGED（迟到乱序，并入窗口参与收敛）
   - seq 小到不可能再进窗口  -> EXPIRED（过期，丢弃）
3. 派生状态是窗口的【纯函数】：窗口 = 已见序号中最大的 window 个，
   与到达顺序无关。因此同一批更新以任意顺序到达，最终状态必然一致。
4. 抖动抑制：窗口内状态翻转次数 >= flap_threshold 即进入 SUPPRESSED，
   路由侧按摘除处理（保守、安全）；抖动退出窗口且连续健康达到
   recover_threshold 后才恢复 UP。
5. 摘除/恢复对称：连续不健康 >= remove_threshold 摘除，
   连续健康 >= recover_threshold 恢复；构造时强制 recover >= remove，
   恢复不会比摘除更激进。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple


class Status(Enum):
    UP = "UP"                # 健康，可路由
    DOWN = "DOWN"            # 摘除
    SUPPRESSED = "SUPPRESSED"  # 抖动抑制，按摘除处理，区别于宕机


class Verdict(Enum):
    NEW = "NEW"                    # 全新序号
    DUPLICATE = "DUPLICATE"        # 窗口内重复序号
    STALE_MERGED = "STALE_MERGED"  # 迟到但仍在窗口内，已并入
    EXPIRED = "EXPIRED"            # 旧于窗口，已丢弃


@dataclass(frozen=True)
class Update:
    node_id: int
    seq: int          # 发送方单调递增序号（如版本号 / Lamport clock）
    healthy: bool


class _Node:
    __slots__ = ("max_seq", "seqs", "vals", "status")

    def __init__(self) -> None:
        self.max_seq: int = -1
        self.seqs: List[int] = []    # 升序，长度 <= window
        self.vals: List[bool] = []   # 与 seqs 平行的健康观测
        self.status: Status = Status.UP


def derive(vals: List[bool],
           remove_threshold: int,
           recover_threshold: int,
           flap_threshold: Optional[int]) -> Status:
    """窗口观测序列 -> 状态。纯函数：相同输入永远得到相同输出。

    vals 为按时间从旧到新的健康布尔序列（长度 <= window）。
    """
    n = len(vals)
    if n == 0:
        return Status.UP

    # 1) 抖动：统计窗口内翻转次数
    transitions = 0
    for i in range(1, n):
        if vals[i] != vals[i - 1]:
            transitions += 1
    if flap_threshold is not None and transitions >= flap_threshold:
        return Status.SUPPRESSED

    # 2) 对称的摘除 / 恢复迟滞（以窗口最旧观测为初值重放）
    up = vals[0]
    run_val = vals[0]
    run_len = 1
    for x in vals[1:]:
        if x == run_val:
            run_len += 1
        else:
            run_val, run_len = x, 1
        if not x and run_len >= remove_threshold:
            up = False
        elif x and run_len >= recover_threshold:
            up = True
    return Status.UP if up else Status.DOWN


class ClusterHealth:
    """集群健康状态机。线程不安全（自测单线程；生产可外加锁）。"""

    def __init__(self,
                 window: int = 16,
                 remove_threshold: int = 3,
                 recover_threshold: Optional[int] = None,
                 flap_threshold: Optional[int] = 6) -> None:
        if recover_threshold is None:
            recover_threshold = remove_threshold
        if window < 2:
            raise ValueError("window 必须 >= 2")
        if not 1 <= remove_threshold <= window:
            raise ValueError("remove_threshold 必须在 [1, window] 内")
        if not 1 <= recover_threshold <= window:
            raise ValueError("recover_threshold 必须在 [1, window] 内")
        # 恢复条件不得比摘除条件更激进
        if recover_threshold < remove_threshold:
            raise ValueError(
                "recover_threshold(%d) 不得小于 remove_threshold(%d)"
                % (recover_threshold, remove_threshold))
        if flap_threshold is not None and not 1 <= flap_threshold <= window - 1:
            raise ValueError("flap_threshold 必须在 [1, window-1] 内，或为 None")

        self.window = window
        self.remove_threshold = remove_threshold
        self.recover_threshold = recover_threshold
        self.flap_threshold = flap_threshold
        self._nodes: Dict[int, _Node] = {}

    def apply(self, update: Update) -> Tuple[Verdict, Status]:
        """吸收一条上报，返回 (判定, 该节点当前派生状态)。"""
        node = self._nodes.get(update.node_id)
        if node is None:
            node = self._nodes[update.node_id] = _Node()

        seq, val = update.seq, update.healthy
        pos = bisect.bisect_left(node.seqs, seq)

        if pos < len(node.seqs) and node.seqs[pos] == seq:
            # 重复序号：无论何时到达都不生效（即使健康位不同也忽略）
            return Verdict.DUPLICATE, node.status

        if seq > node.max_seq:
            verdict = Verdict.NEW
            node.max_seq = seq
            node.seqs.append(seq)
            node.vals.append(val)
        elif len(node.seqs) < self.window or seq > node.seqs[0]:
            # 迟到乱序但仍可影响“最大 window 条”集合：并入并重排。
            # 窗口是已见序号集合的纯函数，并入后所有顺序得到同一窗口。
            verdict = Verdict.STALE_MERGED
            node.seqs.insert(pos, seq)
            node.vals.insert(pos, val)
        else:
            # 已滑出窗口的旧序号，不可能再影响派生状态
            return Verdict.EXPIRED, node.status

        if len(node.seqs) > self.window:
            del node.seqs[0]
            del node.vals[0]

        node.status = derive(node.vals, self.remove_threshold,
                             self.recover_threshold, self.flap_threshold)
        return verdict, node.status

    def status(self, node_id: int) -> Status:
        node = self._nodes.get(node_id)
        return node.status if node is not None else Status.UP

    def healthy(self, node_id: int) -> bool:
        """路由侧判定：只有 UP 可路由；SUPPRESSED 按摘除处理。"""
        return self.status(node_id) is Status.UP

    def snapshot(self) -> Dict[int, Status]:
        return {nid: node.status for nid, node in self._nodes.items()}
