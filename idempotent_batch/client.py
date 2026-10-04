"""批量提交客户端：逐项重试 + 对账，保证结果可判定。

状态机（每个条目独立）：

    submit ──成功──> SUCCESS
    submit ──RETRYABLE──> 退避后重试（预算内）
    submit ──NON_RETRYABLE──> FAILURE_PERMANENT（终态）
    submit ──AMBIGUOUS──> 对账循环：
        lookup 命中已提交成功 ──> SUCCESS_AFTER_RECONCILE
        lookup 命中已提交永久失败 ──> FAILURE_PERMANENT
        lookup 确认未提交 ──> 带同一幂等键重新提交（副作用不会重复）
        lookup 传输失败 ──> 退避后重试对账
        对账重试次数耗尽 ──> durable_lookup 持久层核实（只返回确定事实）
    提交预算耗尽且最后错误为 RETRYABLE ──> FAILURE_RETRY_EXHAUSTED
    提交预算耗尽且最后错误为 AMBIGUOUS ──> durable_lookup 核实后定性

任何情况下，返回的条目都只有确定终态：SUCCESS / SUCCESS_AFTER_RECONCILE /
FAILURE_PERMANENT / FAILURE_RETRY_EXHAUSTED，不存在 UNKNOWN。
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .errors import BatchError, ErrorKind


class ItemStatus(str, Enum):
    SUCCESS = "SUCCESS"
    SUCCESS_AFTER_RECONCILE = "SUCCESS_AFTER_RECONCILE"
    FAILURE_PERMANENT = "FAILURE_PERMANENT"
    FAILURE_RETRY_EXHAUSTED = "FAILURE_RETRY_EXHAUSTED"


TERMINAL = set(ItemStatus)


@dataclass(frozen=True)
class Item:
    id: str
    payload: object
    idem_key: str


@dataclass
class ItemResult:
    id: str
    status: ItemStatus
    attempts: int = 0            # 提交调用次数（含重放）
    reconciles: int = 0          # 在线对账调用次数
    durable_lookups: int = 0     # 持久层核实次数
    retries: int = 0             # 因可重试错误触发的重试次数
    last_error: Optional[int] = None
    error_history: list[int] = field(default_factory=list)
    committed_code: Optional[int] = None


@dataclass
class Stats:
    total: int = 0
    wall_seconds: float = 0.0
    submits: int = 0
    retries: int = 0
    reconciles: int = 0
    durable_lookups: int = 0
    by_status: dict = field(default_factory=dict)
    retry_histogram: dict = field(default_factory=dict)   # 提交次数 -> 条目数
    reconcile_histogram: dict = field(default_factory=dict)


@dataclass
class _Config:
    max_attempts: int = 4            # 每个条目最多提交次数（含首次）
    max_reconciles: int = 3          # 每轮对账最多重试次数（在线对账）
    base_delay: float = 0.0005       # 退避基数（秒）
    max_delay: float = 0.05
    jitter: float = 0.0
    workers: int = 32
    sleep: Callable[[float], None] = time.sleep


def _backoff(cfg: _Config, attempt_index: int) -> float:
    delay = min(cfg.base_delay * (2 ** attempt_index), cfg.max_delay)
    if cfg.jitter:
        delay *= 1.0 + random.uniform(-cfg.jitter, cfg.jitter)
    return max(delay, 0.0)


def _settle_one(server, item: Item, cfg: _Config) -> ItemResult:
    """单个条目的完整状态机。只被 submit_batch / submit_single 调用。"""
    res = ItemResult(id=item.id, status=ItemStatus.FAILURE_RETRY_EXHAUSTED)
    attempt = 0
    reconcile_round = 0

    while True:
        # ---- 提交 ----
        try:
            rec = server.submit(item.idem_key, item.payload, attempt)
            res.attempts += 1
            res.committed_code = rec.code
            if rec.ok:
                res.status = (ItemStatus.SUCCESS_AFTER_RECONCILE
                              if reconcile_round > 0 else ItemStatus.SUCCESS)
            else:
                res.status = ItemStatus.FAILURE_PERMANENT
                res.last_error = rec.code
            return res
        except BatchError as exc:
            res.attempts += 1
            res.error_history.append(exc.code)
            res.last_error = exc.code
            kind = exc.kind

        if kind == ErrorKind.NON_RETRYABLE:
            res.status = ItemStatus.FAILURE_PERMANENT
            return res

        if kind == ErrorKind.RETRYABLE:
            if attempt + 1 >= cfg.max_attempts:
                res.status = ItemStatus.FAILURE_RETRY_EXHAUSTED
                return res
            res.retries += 1
            cfg.sleep(_backoff(cfg, attempt))
            attempt += 1
            continue

        # ---- AMBIGUOUS：禁止盲目重放，先对账 ----
        reconcile_round += 1
        decided = False
        for r in range(cfg.max_reconciles):
            try:
                rec = server.lookup(item.idem_key, r)
                res.reconciles += 1
            except BatchError as exc:
                res.reconciles += 1
                res.error_history.append(exc.code)
                res.last_error = exc.code
                cfg.sleep(_backoff(cfg, r))
                continue
            if rec is not None:
                res.committed_code = rec.code
                if rec.ok:
                    res.status = ItemStatus.SUCCESS_AFTER_RECONCILE
                else:
                    res.status = ItemStatus.FAILURE_PERMANENT
                    res.last_error = rec.code
                return res
            # 确认未提交：带同一幂等键重新提交（服务端会按键去重，不会重复副作用）
            decided = True
            break

        if not decided:
            # 在线对账重试耗尽：走持久层核实，只可能得到确定事实
            fact = server.durable_lookup(item.idem_key)
            res.durable_lookups += 1
            if fact is None:
                # 确定没提交过：重新提交（同键）
                decided = True
            else:
                tag, rec = fact
                res.committed_code = rec.code
                if rec.ok:
                    res.status = ItemStatus.SUCCESS_AFTER_RECONCILE
                else:
                    res.status = ItemStatus.FAILURE_PERMANENT
                    res.last_error = rec.code
                return res

        # 对账确认“未提交”后重新提交，受总预算约束
        if attempt + 1 >= cfg.max_attempts:
            # 预算耗尽：此时已确认未提交，可安全定性为“重试耗尽”
            res.status = ItemStatus.FAILURE_RETRY_EXHAUSTED
            return res
        cfg.sleep(_backoff(cfg, attempt))
        attempt += 1
        continue


def submit_batch(server, items, *, max_attempts=4, max_reconciles=3,
                 base_delay=0.0005, max_delay=0.05, jitter=0.0,
                 workers=32, sleep=time.sleep):
    """批量提交。返回 (results, stats)，results 与 items 一一对应、顺序一致。"""
    cfg = _Config(max_attempts=max_attempts, max_reconciles=max_reconciles,
                  base_delay=base_delay, max_delay=max_delay, jitter=jitter,
                  workers=workers, sleep=sleep)
    items = list(items)
    started = time.perf_counter()

    if workers <= 1 or len(items) <= 1:
        results = [_settle_one(server, it, cfg) for it in items]
    else:
        results = [None] * len(items)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_settle_one, server, it, cfg): i
                       for i, it in enumerate(items)}
            for fut, i in futures.items():
                results[i] = fut.result()

    wall = time.perf_counter() - started
    stats = _aggregate(results, wall)
    return results, stats


def submit_single(server, item: Item, **kwargs) -> ItemResult:
    """单项提交，走与批量完全相同的状态机。"""
    results, _ = submit_batch(server, [item], workers=1, **kwargs)
    return results[0]


def _aggregate(results, wall: float) -> Stats:
    stats = Stats(total=len(results), wall_seconds=wall)
    for r in results:
        stats.submits += r.attempts
        stats.retries += r.retries
        stats.reconciles += r.reconciles
        stats.durable_lookups += r.durable_lookups
        stats.by_status[r.status.value] = stats.by_status.get(r.status.value, 0) + 1
        stats.retry_histogram[r.attempts] = stats.retry_histogram.get(r.attempts, 0) + 1
        stats.reconcile_histogram[r.reconciles] = stats.reconcile_histogram.get(r.reconciles, 0) + 1
    return stats
