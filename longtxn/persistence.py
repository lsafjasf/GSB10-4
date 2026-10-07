"""仅用标准库实现的 JSONL 状态日志，用于崩溃后重建。

记录三类事实：
- begin   : 事务开始及其快照点(start_seq)、开始时刻(start_time)
- version : 已提交的版本（提交时写，崩溃恢复后据此重建版本链）
- terminal: commit / abort / degrade / force_abort 终态

恢复规则：只有 begin、没有 terminal 的事务是"崩溃前未决事务"，
重建时按 force_abort 处理（其缓冲写从未进入版本链，无需回滚版本）。
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple


class StateLog:
    def __init__(self, path: str):
        self.path = path
        self._fp = open(path, "a", buffering=1, encoding="utf-8")

    def _rec(self, rec: Dict[str, Any]) -> None:
        self._fp.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def log_begin(self, txn_id: int, start_seq: int, start_time: float) -> None:
        self._rec({"op": "begin", "txn": txn_id,
                   "start_seq": start_seq, "start_time": start_time})

    def log_version(self, txn_id: int, commit_seq: int, key: str,
                    value: Any) -> None:
        self._rec({"op": "version", "txn": txn_id, "seq": commit_seq,
                   "key": key, "value": value})

    def log_terminal(self, txn_id: int, action: str,
                     commit_seq: Optional[int] = None) -> None:
        rec: Dict[str, Any] = {"op": "terminal", "txn": txn_id,
                               "action": action}
        if commit_seq is not None:
            rec["commit_seq"] = commit_seq
        self._rec(rec)

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()


def replay(path: str) -> Tuple[Dict[int, Dict[str, Any]], int,
                               List[Dict[str, Any]], List[int]]:
    """回放日志，返回:
    active  : 重建后的活跃事务 {txn_id: {start_seq,start_time,state}}
              未决事务 state='recovered_aborted'，不放入活跃集合
    commit_seq: 日志中出现过的最大提交序号
    versions: 已提交版本记录 [{key,commit_seq,txn_id,value}]
    orphaned: 被恢复流程判定为未决并强制中止的事务 id
    """
    begun: Dict[int, Dict[str, Any]] = {}
    terminal: Dict[int, str] = {}
    commit_seq = 0
    versions: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            op = rec["op"]
            txn = rec["txn"]
            if op == "begin":
                begun[txn] = {"start_seq": rec["start_seq"],
                              "start_time": rec["start_time"],
                              "state": "active"}
            elif op == "version":
                versions.append({"key": rec["key"],
                                 "commit_seq": rec["seq"],
                                 "txn_id": txn, "value": rec["value"]})
            elif op == "terminal":
                terminal[txn] = rec["action"]
                if "commit_seq" in rec:
                    commit_seq = max(commit_seq, rec["commit_seq"])
    orphaned: List[int] = []
    active: Dict[int, Dict[str, Any]] = {}
    for txn, meta in begun.items():
        action = terminal.get(txn)
        if action is None:
            meta["state"] = "recovered_aborted"
            orphaned.append(txn)
        elif action == "degrade":
            meta["state"] = "degraded"
        elif action in ("commit", "abort", "force_abort"):
            meta["state"] = action
        else:
            meta["state"] = action
        if meta["state"] == "active":
            active[txn] = meta
    return active, commit_seq, versions, orphaned
