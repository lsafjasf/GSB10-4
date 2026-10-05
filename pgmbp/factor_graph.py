"""因子图数据结构：变量（有限离散取值）与因子（势函数表）。"""

from itertools import product as _iproduct


class Factor:
    """一个势因子 f(x_{var_indices}) >= 0。

    table 为扁平表，下标按 var_indices 最右侧变量变化最快的
    混合进制编码（与 itertools.product 给出的取值组合一一对应）。
    """

    __slots__ = ("var_indices", "cardinalities", "table", "_strides", "_size")

    def __init__(self, var_indices, cardinalities, table):
        var_indices = tuple(var_indices)
        cardinalities = tuple(cardinalities)
        if len(var_indices) != len(set(var_indices)):
            raise ValueError("因子的变量不得重复: %r" % (var_indices,))
        if len(var_indices) != len(cardinalities):
            raise ValueError("var_indices 与 cardinalities 长度不一致")
        if any(k < 2 for k in cardinalities):
            raise ValueError("变量基数必须 >= 2")
        size = 1
        for k in cardinalities:
            size *= k
        if len(table) != size:
            raise ValueError(
                "因子表长度 %d 与取值组合数 %d 不符" % (len(table), size)
            )

        cleaned = [0.0] * size
        saw_value = False
        for idx, val in enumerate(table):
            if val is None:
                raise ValueError("因子表存在 None: 位置 %d" % idx)
            fval = float(val)
            if fval < 0.0:
                raise ValueError(
                    "因子表出现负值 %.6g: 位置 %d，势函数必须非负" % (fval, idx)
                )
            if fval > 0.0:
                saw_value = True
            cleaned[idx] = fval
        if not saw_value:
            raise ValueError("因子表全为 0：该因子定义的势无意义")

        self.var_indices = var_indices
        self.cardinalities = cardinalities
        self.table = cleaned
        self._size = size
        strides = [1] * len(cardinalities)
        for i in range(len(cardinalities) - 2, -1, -1):
            strides[i] = strides[i + 1] * cardinalities[i + 1]
        self._strides = tuple(strides)

    @property
    def arity(self):
        return len(self.var_indices)

    def index(self, assignment):
        """取值元组 -> 扁平下标。"""
        idx = 0
        for i, x in enumerate(assignment):
            idx += x * self._strides[i]
        return idx


class FactorGraph:
    """离散变量因子图。变量编号 0..n-1，用基数表示取值个数。"""

    __slots__ = ("cardinalities", "factors", "_var_factors", "_frozen")

    def __init__(self, cardinalities):
        cardinalities = tuple(int(k) for k in cardinalities)
        if any(k < 2 for k in cardinalities):
            raise ValueError("变量基数必须 >= 2")
        self.cardinalities = cardinalities
        self.factors = []
        self._var_factors = [[] for _ in cardinalities]
        self._frozen = False

    @property
    def num_vars(self):
        return len(self.cardinalities)

    @property
    def num_factors(self):
        return len(self.factors)

    def num_edges(self):
        """无向边数（将成对因子视作变量-变量边）。"""
        edges = set()
        for f in self.factors:
            if f.arity == 2:
                a, b = f.var_indices
                edges.add((a, b) if a < b else (b, a))
        return len(edges)

    def add_factor(self, var_indices, table):
        if self._frozen:
            raise RuntimeError("图已冻结，不能继续添加因子")
        var_indices = tuple(var_indices)
        for v in var_indices:
            if not 0 <= v < self.num_vars:
                raise ValueError("变量编号越界: %d" % v)
        cards = [self.cardinalities[v] for v in var_indices]
        fac = Factor(var_indices, cards, table)
        fi = len(self.factors)
        self.factors.append(fac)
        for v in var_indices:
            self._var_factors[v].append(fi)
        return fi

    def var_factor_incidence(self):
        """返回每个变量参与的因子编号列表。"""
        return self._var_factors

    def freeze(self):
        """校验：每个变量至少被一个因子覆盖，之后锁定结构。"""
        for v, fis in enumerate(self._var_factors):
            if not fis:
                raise ValueError("变量 %d 未被任何因子覆盖" % v)
        self._frozen = True
        return self

    def iter_assignments(self):
        """枚举整张图的所有联合赋值（仅供小规模精确法使用）。"""
        return _iproduct(*[range(k) for k in self.cardinalities])
