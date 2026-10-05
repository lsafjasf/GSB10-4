# -*- coding: utf-8 -*-
"""数据倾斜再分配库（仅标准库）。

流程:
    原始键 -> 计数 -> 热点识别 -> 热点键加“盐”拆到多个子分区
          -> 分桶聚合 -> 上游按原始键合并还原

热点识别依据（两个条件必须同时满足，边界均为 >=，含等于）:
    1. 绝对量:  count(key) >= abs_threshold
    2. 占比:    count(key) / total >= share_threshold
即 count(key) >= max(abs_threshold, share_threshold * total)。
绝对量阈值用于防止数据量很小时大量键被误判；占比阈值用于在数据量大时
抓住真正压垮分区的热点。

拆分规则:
    target = ceil(total / num_partitions)              每个分区期望负载
    splits(key) = ceil(count(key) / target)            该热点键拆出的子分区数
    对该键第 i 条记录（按出现顺序，从 0 起），盐 = i mod splits，
    落点分区 = (hash(key) + salt) mod num_partitions。
因此每个子分区大小为 floor(c/s) 或 ceil(c/s)，任意两个子分区相差至多 1。
盐只影响“路由”，不改变记录内容；合并时去掉盐按原始键归并即可。
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Sequence, Tuple


def stable_hash(key) -> int:
    """跨进程稳定的哈希（内置 hash 对字符串带随机盐，不能用于分区）。"""
    if isinstance(key, int):
        return (key * 2654435761) & 0xFFFFFFFF
    digest = hashlib.md5(str(key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True)
class SkewConfig:
    num_partitions: int
    share_threshold: float = 0.20   # 占比阈值
    abs_threshold: int = 1000       # 绝对量阈值

    def __post_init__(self):
        if self.num_partitions < 1:
            raise ValueError("num_partitions 必须 >= 1")
        if not 0.0 < self.share_threshold <= 1.0:
            raise ValueError("share_threshold 必须在 (0, 1]")
        if self.abs_threshold < 1:
            raise ValueError("abs_threshold 必须 >= 1")


# ---------------- 计数与热点识别 ----------------

def count_keys(records: Iterable[dict]) -> Counter:
    """统计每个原始键的记录数。"""
    counts: Counter = Counter()
    for rec in records:
        counts[rec["key"]] += 1
    return counts


def identify_hot_keys(counts: Dict, total: int, cfg: SkewConfig) -> Dict:
    """返回热点键 {key: count}。

    判定（边界含等于，确定且可复现）:
        count >= abs_threshold  且  count >= share_threshold * total
    """
    share_limit = cfg.share_threshold * total
    return {
        key: cnt
        for key, cnt in counts.items()
        if cnt >= cfg.abs_threshold and cnt >= share_limit
    }


def target_load(total: int, num_partitions: int) -> int:
    """每个分区的期望负载（向上取整，至少 1）。"""
    return max(1, math.ceil(total / num_partitions))


def split_factor(count: int, target: int) -> int:
    """热点键需要拆成多少个子分区。"""
    return max(1, math.ceil(count / target))


def build_split_plan(counts: Dict, total: int, cfg: SkewConfig) -> Dict:
    """返回 {hot_key: splits}，非热点键不在表中（视为 1）。"""
    hot = identify_hot_keys(counts, total, cfg)
    tgt = target_load(total, cfg.num_partitions)
    return {key: split_factor(cnt, tgt) for key, cnt in hot.items()}


def sub_partition_sizes(count: int, splits: int) -> List[int]:
    """热点键 count 条记录拆到 splits 个子分区后的大小分布（轮转的确定结果）。"""
    base, extra = divmod(count, splits)
    return [base + (1 if j < extra else 0) for j in range(splits)]


# ---------------- 分区路由 ----------------

def route_partition(key, seq: int, cfg: SkewConfig, plan: Dict) -> Tuple[int, int]:
    """返回 (partition_id, salt)。seq 为该键记录的出现序号（从 0 起）。"""
    base = stable_hash(key) % cfg.num_partitions
    splits = plan.get(key, 1)
    salt = seq % splits
    return (base + salt) % cfg.num_partitions, salt


def partition_records(
    records: Iterable[dict], cfg: SkewConfig, plan: Dict
) -> List[List[dict]]:
    """把记录按拆分计划分桶。非热点键走普通哈希分区。"""
    buckets: List[List[dict]] = [[] for _ in range(cfg.num_partitions)]
    seqs: Dict = defaultdict(int)
    for rec in records:
        key = rec["key"]
        seq = seqs[key]
        seqs[key] = seq + 1
        pid, salt = route_partition(key, seq, cfg, plan)
        buckets[pid].append({"key": key, "salt": salt, "value": rec["value"]})
    return buckets


def partition_loads(buckets: Sequence[Sequence]) -> List[int]:
    return [len(b) for b in buckets]


# ---------------- 聚合与合并还原 ----------------

def aggregate(buckets: Sequence[Iterable[dict]]) -> Dict:
    """分桶内按原始键求和（模拟 reduce 侧部分聚合）。"""
    agg: Dict = defaultdict(int)
    for bucket in buckets:
        for rec in bucket:
            agg[rec["key"]] += rec["value"]
    return dict(agg)


def merge_upstream(buckets: Sequence[Iterable[dict]]) -> Dict:
    """上游把拆出去的子分区按原始键原样合回（部分结果求和）。

    对可加聚合（如 sum）而言，合并 = 按原始键汇总各子分区的部分结果。
    """
    return aggregate(buckets)


def plain_partition_and_aggregate(
    records: Iterable[dict], cfg: SkewConfig
) -> Tuple[Dict, List[int]]:
    """不做任何拆分的基线：普通哈希分区 + 聚合，返回 (结果, 分区负载)。"""
    buckets: List[List[dict]] = [[] for _ in range(cfg.num_partitions)]
    for rec in records:
        buckets[stable_hash(rec["key"]) % cfg.num_partitions].append(rec)
    return aggregate(buckets), partition_loads(buckets)


def split_partition_and_aggregate(
    records: Iterable[dict], cfg: SkewConfig, plan: Dict
) -> Tuple[Dict, List[int]]:
    """走拆分路径：分桶 + 上游合并，返回 (合并后结果, 分区负载)。"""
    buckets = partition_records(records, cfg, plan)
    return merge_upstream(buckets), partition_loads(buckets)
