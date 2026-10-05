"""精确推断：变量消去（Variable Elimination），用于小规模误差对比。"""


def _multiply(fac_a, fac_b):
    """因子相乘。因子表示为 (vars_tuple, cards_tuple, table_list)。"""
    vars_a, cards_a, tab_a = fac_a
    vars_b, cards_b, tab_b = fac_b
    pos_a = {v: i for i, v in enumerate(vars_a)}
    pos_b = {v: i for i, v in enumerate(vars_b)}
    out_vars = tuple(vars_a) + tuple(v for v in vars_b if v not in pos_a)
    card_of = {}
    for v, c in zip(vars_a, cards_a):
        card_of[v] = c
    for v, c in zip(vars_b, cards_b):
        card_of[v] = c
    out_cards = tuple(card_of[v] for v in out_vars)

    # 输出下标 -> 各输入下标的步进
    def strides(cards):
        st = [1] * len(cards)
        for i in range(len(cards) - 2, -1, -1):
            st[i] = st[i + 1] * cards[i + 1]
        return st

    st_out = strides(out_cards)
    st_a = strides(cards_a)
    st_b = strides(cards_b)
    map_a = [pos_a[v] if v in pos_a else None for v in out_vars]
    map_b = [pos_b[v] if v in pos_b else None for v in out_vars]

    size = 1
    for c in out_cards:
        size *= c
    out = [0.0] * size
    for flat in range(size):
        rem = flat
        ia = 0
        ib = 0
        for j, v in enumerate(out_vars):
            x = rem // st_out[j]
            rem -= x * st_out[j]
            pa = map_a[j]
            if pa is not None:
                ia += x * st_a[pa]
            pb = map_b[j]
            if pb is not None:
                ib += x * st_b[pb]
        out[flat] = tab_a[ia] * tab_b[ib]
    return out_vars, out_cards, out


def _marginalize(fac, var):
    """对 var 求和消元。"""
    vars_, cards, table = fac
    pos = vars_.index(var)
    out_vars = vars_[:pos] + vars_[pos + 1:]
    out_cards = cards[:pos] + cards[pos + 1:]
    if not out_vars:
        return out_vars, out_cards, [sum(table)]
    stride = 1
    for c in cards[pos + 1:]:
        stride *= c
    block = stride * cards[pos]
    out_size = 1
    for c in out_cards:
        out_size *= c
    out = [0.0] * out_size
    for flat, w in enumerate(table):
        if not w:
            continue
        hi = flat // block
        lo = flat % stride
        out[hi * stride + lo] += w
    return out_vars, out_cards, out


def variable_elimination(graph, query_var):
    """返回 query_var 的精确边缘（L1 归一化）。"""
    factors = [
        (f.var_indices, f.cardinalities, list(f.table)) for f in graph.factors
    ]
    card_of = {}
    for f in graph.factors:
        for v, c in zip(f.var_indices, f.cardinalities):
            card_of[v] = c

    remaining = [v for v in range(graph.num_vars) if v != query_var]
    while remaining:
        # min-fill 简化版：选当前涉及因子总表长最小的变量
        best_var = None
        best_cost = None
        for v in remaining:
            cost = 0
            for vars_, _, tab in factors:
                if v in vars_:
                    cost += len(tab)
            if best_cost is None or cost < best_cost:
                best_cost = cost
                best_var = v
        v = best_var
        remaining.remove(v)
        involved = [f for f in factors if v in f[0]]
        factors = [f for f in factors if v not in f[0]]
        if not involved:
            continue
        acc = involved[0]
        for f in involved[1:]:
            acc = _multiply(acc, f)
        factors.append(_marginalize(acc, v))

    # 合并剩余因子并归一化
    acc = factors[0]
    for f in factors[1:]:
        acc = _multiply(acc, f)
    vars_, cards, table = acc
    if vars_ != (query_var,):
        # 把 query_var 排到第 0 位
        pos = vars_.index(query_var)
        out = [0.0] * cards[pos]
        stride = 1
        for c in cards[pos + 1:]:
            stride *= c
        block = stride * cards[pos]
        for flat, w in enumerate(table):
            x = (flat % block) // stride
            out[x] += w
        table = out
    total = sum(table)
    if total <= 0.0:
        raise ArithmeticError("精确推断得到全零边缘：模型本身不可满足")
    return [w / total for w in table]


def exact_marginals(graph):
    """全部变量的精确边缘。"""
    graph.freeze()
    return [variable_elimination(graph, v) for v in range(graph.num_vars)]
