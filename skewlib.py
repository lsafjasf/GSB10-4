"""数据倾斜再分配库（仅标准库）。

核心思想（经典 salting / key-splitting 方案）：
1. 识别热点键：同时满足"占比阈值"和"绝对量阈值"的判定规则，见 identify_hot_keys。
2. 拆分热点键：热点键按行号轮询（round-robin）加盐拆成若干个子分片，
   每个子分片是一个独立的"虚拟键" (key, salt_index)。
3. 分区指派：冷键保持整键哈希分区；热键子分片用 LPT（最长处理时间优先）
   贪心算法装入 num_partitions 个分区，把热键压力摊平。
4. 合并还原：下游把 (key, salt_index) 的盐脱掉，按原 key 累加，
   结果与不拆分直接聚合完全一致（对可交换、可结合的聚合而言）。

确定性保证：
- 阈值判定使用严格大于（>），等于阈值不视为热点，见 threshold_for。
- 占比阈值用 Fraction 精确计算，避免浮点误差。
- 冷键分区用 MD5 哈希（跨进程、跨运行稳定，不依赖 Python 的 hash 随机化）。
- 热键拆分用行号轮询而非随机数，同一输入序列必然得到同一拆分结果。
"""

from collections import Counter
from fractions import Fraction
from hashlib import md5


def threshold_for(total, ratio=0.1, abs_min=1):
    """计算热点阈值。

    热点判定规则：count > threshold 才是热点（严格大于）。
    threshold = max(ceil(total * ratio), abs_min)

    - ratio 经 Fraction(str(ratio)) 精确化，0.1 就是精确的 1/10，
      因此 total=1000, ratio=0.1 时阈值恰好是 100，无浮点歧义。
    - 等于阈值的键一律按冷键处理（确定性的边界行为）。
    """
    exact = Fraction(str(ratio)) * total
    ratio_part = -(-exact.numerator // exact.denominator)  # ceil
    return max(ratio_part, abs_min)


def identify_hot_keys(counts, ratio=0.1, abs_min=1):
    """识别热点键。

    参数:
        counts:  dict/Counter, key -> 行数
        ratio:   占比阈值（默认 0.1，即超过总行数 10% 的键）
        abs_min: 绝对量阈值（默认 1；阈值取两者较大者，
                 防止小数据集上占比阈值失去意义）
    返回:
        (hot_counts, threshold)
        hot_counts: dict，热点 key -> 行数（按行数降序）
        threshold:  本次判定实际使用的阈值
    """
    total = sum(counts.values())
    threshold = threshold_for(total, ratio, abs_min)
    hot = {k: c for k, c in counts.items() if c > threshold}
    hot = dict(sorted(hot.items(), key=lambda kv: (-kv[1], str(kv[0]))))
    return hot, threshold


def split_sizes(count, num_splits):
    """count 行轮询拆成 num_splits 片的大小分布（确定性，最大差 1）。

    前 count % num_splits 片各多 1 行——这正是逐行 round-robin 加盐的结果。
    """
    base, rem = divmod(count, num_splits)
    return [base + (1 if i < rem else 0) for i in range(num_splits)]


def _cold_partition(key, num_partitions):
    """冷键的稳定哈希分区（MD5，不依赖 PYTHONHASHSEED）。"""
    digest = md5(str(key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % num_partitions


class SkewPlan:
    """一次再分配的方案：热点集合、每键分片数、分片 -> 分区 的指派。"""

    def __init__(self, counts, num_partitions, ratio=0.1, abs_min=1):
        if num_partitions < 1:
            raise ValueError("num_partitions must be >= 1")
        self.num_partitions = num_partitions
        self.total = sum(counts.values())
        self.hot, self.threshold = identify_hot_keys(counts, ratio, abs_min)
        avg_load = self.total / num_partitions

        # 1) 生成所有分片
        self.num_splits = {}  # 热点 key -> 分片数
        shards = []           # (ShardId, size)
        for key, c in counts.items():
            if key in self.hot:
                # 子分片数：至少 2，且足以把每片压到平均负载附近
                n = max(2, -(-c // max(1, int(avg_load))))  # ceil(c/avg)
                self.num_splits[key] = n
                for i, size in enumerate(split_sizes(c, n)):
                    shards.append(((key, i), size))
            else:
                shards.append(((key, None), c))

        # 2) 冷键固定走哈希分区（与常规哈希分区行为一致，可直接定位）；
        #    热键子分片用 LPT 贪心，依次放入当前最空的分区，摊平热点压力。
        self.partition_of = {}  # ShardId -> partition
        self.loads = [0] * num_partitions
        hot_shards = []
        for shard_id, size in sorted(shards, key=lambda s: (-s[1], str(s[0]))):
            _key, salt = shard_id
            if salt is None:
                p = _cold_partition(shard_id[0], num_partitions)
                self.partition_of[shard_id] = p
                self.loads[p] += size
            else:
                hot_shards.append((shard_id, size))
        for shard_id, size in hot_shards:
            p = min(range(num_partitions), key=lambda i: self.loads[i])
            self.partition_of[shard_id] = p
            self.loads[p] += size

    def route(self, key, row_seq):
        """返回 (ShardId, partition)。row_seq 是该 key 已见的行号（0 起）。"""
        n = self.num_splits.get(key)
        if n is None:
            shard = (key, None)
        else:
            shard = (key, row_seq % n)
        return shard, self.partition_of[shard]

    def shard_distribution(self):
        """各热点键的子分片大小分布，供报告使用。"""
        return {
            key: split_sizes(self.hot[key], self.num_splits[key])
            for key in self.hot
        }


def run_partitioned(rows, plan):
    """按 plan 逐行路由并聚合，返回 partitions: list[Counter]。"""
    partitions = [Counter() for _ in range(plan.num_partitions)]
    seen = Counter()
    for key in rows:
        shard, p = plan.route(key, seen[key])
        seen[key] += 1
        partitions[p][shard] += 1
    return partitions


def merge_partitions(partitions):
    """脱盐合并：(key, salt) -> key，结果与直接 Counter(rows) 一致。"""
    merged = Counter()
    for part in partitions:
        for (key, _salt), value in part.items():
            merged[key] += value
    return merged


def run_baseline(rows):
    """不拆分的基线：直接全量聚合。"""
    return Counter(rows)
