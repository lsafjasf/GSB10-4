# -*- coding: utf-8 -*-
"""有限状态机可观测性(状态不可区分性)检查库。

模型:确定性 Mealy 机 M = (S, Sigma, Gamma, delta, lambda, s0)
    delta(s, x) : 转移函数,允许部分定义(缺失转移)
    lambda(s, x): 输出函数,允许部分定义

不可区分(观测等价)定义
----------------------
把缺失转移补全为一个汇点 sink:
    * 缺失的输出记为保留符号 MISSING(读作 ⊥),
    * 缺失的后继记为 sink(sink 对任意输入输出 ⊥ 且转移到自身)。
则对状态 p, q:
    p ≡ q  <=>  对任意输入序列 w ∈ Sigma*, 从 p 和 q 出发执行 w,
                 每一步的输出(含 ⊥)逐位相同。
若存在 w 使输出不同,w 就是一条"区分序列";所有区分序列都不存在,
才判定为不可区分。该判定与参考实现 reference_classes(逐对试探,
每对状态在状态对空间上 BFS 搜索区分序列)结果完全一致。

只对"从初态可达"的状态划分等价类;不可达状态与缺失转移单独报告。
"""

from collections import defaultdict, deque

__all__ = [
    "FSM",
    "MISSING",
    "minimize",
    "partition_is_stable",
    "distinguishing_sequence",
    "simulate",
    "equivalence_evidence",
    "reference_classes",
    "analyze",
    "same_partition",
]

# 保留输出符号:表示该 (状态,输入) 上输出未定义。
MISSING = "\u22a5<undefined>"

# 汇点后继的块号(只可能与"等价于 sink"的真实状态同块)。
_SINK_BLOCK = -1


class FSM(object):
    """确定性 Mealy 机。

    trans : dict[(state, input)] -> state
    out   : dict[(state, input)] -> output(状态/输出/输入需可哈希)
    """

    def __init__(self, states, inputs, initial, trans, out):
        self.states = list(states)
        self.inputs = list(inputs)
        self.initial = initial
        self.trans = dict(trans)
        self.out = dict(out)
        if initial not in set(self.states):
            raise ValueError("initial state %r 不在状态集合中" % (initial,))

    # ---- 补全语义下的单步(s 为 None 表示汇点 sink) ----
    def step_output(self, s, x):
        if s is None:
            return MISSING
        return self.out.get((s, x), MISSING)

    def step_next(self, s, x):
        if s is None:
            return None
        return self.trans.get((s, x))

    # ---- 可达性 ----
    def reachable_states(self):
        """从初态 BFS 求可达状态集合(只走有定义的转移)。"""
        seen = {self.initial}
        queue = deque([self.initial])
        while queue:
            s = queue.popleft()
            for x in self.inputs:
                t = self.trans.get((s, x))
                if t is not None and t not in seen:
                    seen.add(t)
                    queue.append(t)
        return seen

    # ---- 缺失转移 ----
    def missing_transitions(self, states=None):
        """返回缺失项列表 [(s, x, 缺失内容), ...]。

        缺失内容为 'transition' / 'output' / 'both'。
        """
        if states is None:
            states = self.states
        result = []
        for s in states:
            for x in self.inputs:
                miss_t = (s, x) not in self.trans
                miss_o = (s, x) not in self.out
                if miss_t and miss_o:
                    kind = "both"
                elif miss_t:
                    kind = "transition"
                elif miss_o:
                    kind = "output"
                else:
                    continue
                result.append((s, x, kind))
        return result


def _ordered(blocks):
    """块内按 repr 排序,块间按首个元素 repr 排序,保证结果确定。"""
    for b in blocks:
        b.sort(key=repr)
    blocks.sort(key=lambda b: repr(b[0]))
    return blocks


def minimize(machine, subset):
    """Hopcroft 划分细化,求 subset(通常为可达状态集合)上的观测等价类。

    返回 (blocks, block_of):
        blocks   : list[list[state]],每个内层列表是一个等价类
        block_of : dict[state] -> int,状态到等价类编号
    """
    subset_set = set(subset)
    inputs = machine.inputs

    # 初始划分:输出签名相同的状态同块(lambda 一致)。
    groups = defaultdict(list)
    for s in subset_set:
        signature = tuple(machine.step_output(s, x) for x in inputs)
        groups[signature].append(s)
    blocks = list(groups.values())
    block_of = {}
    for i, block in enumerate(blocks):
        for s in block:
            block_of[s] = i

    # 反向转移:pred[x][t] = [s ... delta(s,x) = t]
    pred = dict((x, defaultdict(list)) for x in inputs)
    for s in subset_set:
        for x in inputs:
            t = machine.trans.get((s, x))
            if t is not None and t in subset_set:
                pred[x][t].append(s)

    # 工作表 (块, 输入);全部初始块入队(细化语义安全)。
    work = deque((i, x) for i in range(len(blocks)) for x in inputs)
    queued = set(work)

    while work:
        bidx, x = work.popleft()
        queued.discard((bidx, x))

        # X = { s | delta(s,x) ∈ B },按所属块归类。
        touched = defaultdict(list)
        for t in blocks[bidx]:
            for s in pred[x].get(t, ()):
                touched[block_of[s]].append(s)

        for d, marked in touched.items():
            if len(marked) == len(blocks[d]):
                continue  # 整块都在 X 中,不产生分裂
            marked_set = set(marked)
            kept = [s for s in blocks[d] if s not in marked_set]
            new_block = marked
            blocks[d] = kept
            nidx = len(blocks)
            blocks.append(new_block)
            for s in new_block:
                block_of[s] = nidx

            # Hopcroft 小半技巧:已在队中则两块都入,否则只入较小一半。
            for a in inputs:
                if (d, a) in queued:
                    queued.add((nidx, a))
                    work.append((nidx, a))
                else:
                    small = nidx if len(new_block) <= len(kept) else d
                    queued.add((small, a))
                    work.append((small, a))

    _ordered(blocks)
    # 排序后编号需重算
    block_of = {}
    for i, block in enumerate(blocks):
        for s in block:
            block_of[s] = i
    return blocks, block_of


def partition_is_stable(machine, blocks, block_of):
    """校验划分是否为稳定同余(不动点)。

    同一块内任意两个状态,对每个输入 x 都满足:
      (1) 输出相同;  (2) 后继所在块相同(缺失后继视为 sink 块)。
    稳定划分即共归纳证据:由块内一致 + 后继仍块内一致,对任意长度输入
    序列归纳可知输出永远一致,即不存在任何区分序列。
    """
    for block in blocks:
        rep = block[0]
        out_sig = tuple(machine.step_output(rep, x) for x in machine.inputs)
        next_sig = tuple(
            _SINK_BLOCK if machine.step_next(rep, x) is None
            else block_of[machine.step_next(rep, x)]
            for x in machine.inputs
        )
        for s in block[1:]:
            if tuple(machine.step_output(s, x) for x in machine.inputs) != out_sig:
                return False
            cur = tuple(
                _SINK_BLOCK if machine.step_next(s, x) is None
                else block_of[machine.step_next(s, x)]
                for x in machine.inputs
            )
            if cur != next_sig:
                return False
    return True


def simulate(machine, state, sequence):
    """从 state 执行输入序列,返回输出列表(缺失记 MISSING)。"""
    outputs = []
    for x in sequence:
        outputs.append(machine.step_output(state, x))
        state = machine.step_next(state, x)
    return outputs


def distinguishing_sequence(machine, p, q):
    """在"状态对"积机上 BFS,搜索区分 p, q 的最短输入序列。

    找到返回 dict:
        {"sequence": [...], "outputs_p": [...], "outputs_q": [...]}
    不存在(等价)返回 None; p == q 也返回 None。
    """
    if p == q:
        return None
    start = (p, q)
    parent = {start: None}  # pair -> (prev_pair, input)
    queue = deque([start])
    hit = None
    while queue and hit is None:
        u, v = queue.popleft()
        for x in machine.inputs:
            if machine.step_output(u, x) != machine.step_output(v, x):
                hit = ((u, v), x)
                break
            nu = machine.step_next(u, x)
            nv = machine.step_next(v, x)
            if nu is None and nv is None:
                continue  # 双方都进入 sink,输出恒同,不必再展开
            pair = (nu, nv)
            if pair not in parent:
                parent[pair] = ((u, v), x)
                queue.append(pair)
    if hit is None:
        return None

    (u, v), last_x = hit
    sequence = [last_x]
    cur = (u, v)
    while parent[cur] is not None:
        cur, x = parent[cur]
        sequence.append(x)
    sequence.reverse()
    return {
        "sequence": sequence,
        "outputs_p": simulate(machine, p, sequence),
        "outputs_q": simulate(machine, q, sequence),
    }


def equivalence_evidence(machine, p, q, block_of):
    """给出 p, q 同属一个等价类的逐输入论证数据。"""
    rows = []
    for x in machine.inputs:
        sp = machine.step_next(p, x)
        sq = machine.step_next(q, x)
        rows.append({
            "input": x,
            "output_p": machine.step_output(p, x),
            "output_q": machine.step_output(q, x),
            "succ_class_p": _SINK_BLOCK if sp is None else block_of[sp],
            "succ_class_q": _SINK_BLOCK if sq is None else block_of[sq],
        })
    same = block_of.get(p) == block_of.get(q)
    return {
        "pair": (p, q),
        "class": block_of.get(p) if same else None,
        "same_class": same,
        "rows": rows,
        "consistent": same and all(
            r["output_p"] == r["output_q"]
            and r["succ_class_p"] == r["succ_class_q"]
            for r in rows
        ),
    }


def reference_classes(machine, subset):
    """参考实现:逐对试探。

    对每一对无序状态 (p, q) 在状态对积机上 BFS:
      * 搜到输出不一致 -> 该对可区分;
      * 搜遍可达状态对仍无输出不一致 -> 等价,合并。
    用并查集收集等价类,返回 (blocks, block_of),格式同 minimize。
    """
    states = list(subset)
    parent = dict((s, s) for s in states)

    def find(a):
        root = a
        while parent[root] != root:
            root = parent[root]
        while parent[a] != a:
            a, parent[a] = parent[a], root
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(states)):
        for j in range(i + 1, len(states)):
            if distinguishing_sequence(machine, states[i], states[j]) is None:
                union(states[i], states[j])

    groups = defaultdict(list)
    for s in states:
        groups[find(s)].append(s)
    blocks = _ordered(list(groups.values()))
    block_of = {}
    for i, block in enumerate(blocks):
        for s in block:
            block_of[s] = i
    return blocks, block_of


def same_partition(blocks_a, block_a, blocks_b, block_b):
    """两个划分是否相同(只看"哪些状态同块")。"""
    states = list(block_a)
    if set(states) != set(block_b):
        return False
    for i in range(len(states)):
        for j in range(i + 1, len(states)):
            p, q = states[i], states[j]
            if (block_a[p] == block_a[q]) != (block_b[p] == block_b[q]):
                return False
    return True


def analyze(machine):
    """完整检查,返回结构化报告。"""
    reachable = machine.reachable_states()
    unreachable = [s for s in machine.states if s not in reachable]
    missing = machine.missing_transitions()

    blocks, block_of = minimize(machine, reachable)
    stable = partition_is_stable(machine, blocks, block_of)

    # 每个等价类挑一个代表,并给出一对等价状态的论证(类大小 >= 2 时)。
    classes = []
    for cid, block in enumerate(blocks):
        rep = block[0]
        entry = {
            "class_id": cid,
            "size": len(block),
            "states": list(block),
            "representative": rep,
        }
        if len(block) >= 2:
            entry["evidence_example"] = equivalence_evidence(
                machine, block[0], block[1], block_of)
        classes.append(entry)

    # 跨类代表对的区分序列样本(最多 25 条,仅用于演示,穷举见自测)。
    witnesses = []
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            if len(witnesses) >= 25:
                break
            w = distinguishing_sequence(
                machine, blocks[i][0], blocks[j][0])
            if w is not None:
                witnesses.append({
                    "pair": (blocks[i][0], blocks[j][0]),
                    **w,
                })

    return {
        "state_count": len(machine.states),
        "input_count": len(machine.inputs),
        "transition_count": len(machine.trans),
        "reachable_count": len(reachable),
        "unreachable": sorted(unreachable, key=repr),
        "missing_transitions": missing,
        "class_count": len(blocks),
        "classes": classes,
        "stable": stable,
        "witness_samples": witnesses,
    }
