"""
tvl.py — 三值逻辑（3VL）与空值（NULL）语义核心。

只使用 Python 标准库。

语义依据 SQL 标准（ISO/IEC 9075）：
  * 任何一侧为 NULL 的比较（= <> < <= > >=）结果为 UNKNOWN；
  * UNKNOWN 是与 TRUE / FALSE 并列的第三个真值；
  * NULL 是"值未知"标记：NULL = NULL 不是 TRUE，而是 UNKNOWN；
  * IS NULL / IS NOT NULL 永远返回 TRUE 或 FALSE，不会返回 UNKNOWN。

真值表（T=TRUE, F=FALSE, U=UNKNOWN）：

  NOT |       AND | T F U       OR  | T F U
  ----+      -----+------      -----+------
  T  F |        T | T F U        T  | T T T
  F  T |        F | F F F        F  | T F U
  U  U |        U | U F U        U  | T U U

空值识别：本模块用 NULL 单例表示空值，同时把 Python None 归一化为 NULL，
这样与 sqlite3（参考实现）交换数据时无需转换。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence


class Tri(Enum):
    """三值逻辑中的三个真值。"""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"

    def __bool__(self) -> bool:
        # 禁止 bool(tri)，防止把 UNKNOWN 静默当假/当真。
        # "过滤时 UNKNOWN 不通过" 是引擎规则（见 engine.py），不是布尔转换。
        raise TypeError(
            "Tri 不能隐式转 bool；请用 is_true()/is_false() 或 truth() 显式判定"
        )

    def __str__(self) -> str:
        return self.value


T = Tri.TRUE
F = Tri.FALSE
U = Tri.UNKNOWN


def is_true(t: Tri) -> bool:
    return t is Tri.TRUE


def is_false(t: Tri) -> bool:
    return t is Tri.FALSE


def is_unknown(t: Tri) -> bool:
    return t is Tri.UNKNOWN


class _Null:
    """NULL 单例：值未知（unknown value）。与任何值做算术/比较需调用方自行处理。"""

    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self) -> str:
        return "NULL"

    def __bool__(self) -> bool:
        raise TypeError("NULL 不能隐式转 bool；请先用 is_null() 判定")


NULL = _Null()


def is_null(v: Any) -> bool:
    """NULL 与 None 都视为空值（None 用于与 sqlite3 结果互操作）。"""
    return v is NULL or v is None


def null_norm(v: Any) -> Any:
    return NULL if v is None else v


# ---------------------------------------------------------------------------
# 逻辑连接词
# ---------------------------------------------------------------------------

def t_not(a: Tri) -> Tri:
    if a is T:
        return F
    if a is F:
        return T
    return U


_AND_TABLE = {
    (T, T): T, (T, F): F, (T, U): U,
    (F, T): F, (F, F): F, (F, U): F,
    (U, T): U, (U, F): F, (U, U): U,
}

_OR_TABLE = {
    (T, T): T, (T, F): T, (T, U): T,
    (F, T): T, (F, F): F, (F, U): U,
    (U, T): T, (U, F): U, (U, U): U,
}


def t_and(a: Tri, b: Tri) -> Tri:
    return _AND_TABLE[(a, b)]


def t_or(a: Tri, b: Tri) -> Tri:
    return _OR_TABLE[(a, b)]


# ---------------------------------------------------------------------------
# 比较：任一侧为空 -> UNKNOWN
# ---------------------------------------------------------------------------

_CMP_OPS = {
    "=": lambda a, b: a == b,
    "<>": lambda a, b: a != b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def compare(op: str, a: Any, b: Any) -> Tri:
    """空值安全比较。任一侧为 NULL（含 None）返回 UNKNOWN；否则按 op 比较。"""
    if is_null(a) or is_null(b):
        return U
    fn = _CMP_OPS.get(op)
    if fn is None:
        raise ValueError(f"不支持的比较符: {op}")
    return T if fn(a, b) else F


# ---------------------------------------------------------------------------
# 表达式树
#
# 谓词表达式求值时 *总是* 返回 Tri，绝不返回 Python bool，
# 因此 UNKNOWN 不会在任何地方被静默吞掉。
#
# And/Or 提供三种求值策略，用于验证"短路与求值顺序不改变结果"：
#   short_circuit=True, reverse=False  左到右 + 短路
#   short_circuit=True, reverse=True   右到左 + 短路
#   short_circuit=False                双侧都求值（不短路），再合并
# ---------------------------------------------------------------------------

class Expr:
    def eval(self, row: Mapping[str, Any]) -> Any:
        raise NotImplementedError


class Const(Expr):
    def __init__(self, value: Any):
        self.value = null_norm(value)

    def eval(self, row):
        return self.value


class Col(Expr):
    def __init__(self, name: str):
        self.name = name

    def eval(self, row):
        return null_norm(row[self.name])


class Cmp(Expr):
    def __init__(self, op: str, left: Expr, right: Expr):
        self.op = op
        self.left = left
        self.right = right

    def eval(self, row) -> Tri:
        return compare(self.op, self.left.eval(row), self.right.eval(row))


class IsNull(Expr):
    def __init__(self, inner: Expr, negate: bool = False):
        self.inner = inner
        self.negate = negate

    def eval(self, row) -> Tri:
        res = is_null(self.inner.eval(row))
        if self.negate:
            res = not res
        return T if res else F


class Not(Expr):
    def __init__(self, inner: Expr):
        self.inner = inner

    def eval(self, row) -> Tri:
        return t_not(self.inner.eval(row))


class _BinLogic(Expr):
    def __init__(self, left: Expr, right: Expr):
        self.left = left
        self.right = right

    @staticmethod
    def _combine(a: Tri, b: Tri) -> Tri:
        raise NotImplementedError

    def eval(self, row, short_circuit: bool = True, reverse: bool = False) -> Tri:
        first, second = (self.right, self.left) if reverse else (self.left, self.right)
        v1 = first.eval(row)
        if short_circuit and self._short_cut(v1) is not None:
            return self._short_cut(v1)
        v2 = second.eval(row)
        la, lb = (v2, v1) if reverse else (v1, v2)
        return self._combine(la, lb)

    @staticmethod
    def _short_cut(v: Tri):
        return None


class And(_BinLogic):
    @staticmethod
    def _combine(a, b):
        return t_and(a, b)

    @staticmethod
    def _short_cut(v):
        return F if v is F else None


class Or(_BinLogic):
    @staticmethod
    def _combine(a, b):
        return t_or(a, b)

    @staticmethod
    def _short_cut(v):
        return T if v is T else None


# 便捷构造
def eq(a, b): return Cmp("=", _as_expr(a), _as_expr(b))
def ne(a, b): return Cmp("<>", _as_expr(a), _as_expr(b))
def lt(a, b): return Cmp("<", _as_expr(a), _as_expr(b))
def le(a, b): return Cmp("<=", _as_expr(a), _as_expr(b))
def gt(a, b): return Cmp(">", _as_expr(a), _as_expr(b))
def ge(a, b): return Cmp(">=", _as_expr(a), _as_expr(b))
def IS_NULL(e): return IsNull(_as_expr(e))
def IS_NOT_NULL(e): return IsNull(_as_expr(e), negate=True)
def AND(a, b): return And(a, b)
def OR(a, b): return Or(a, b)
def NOT(a): return Not(a)


def _as_expr(x) -> Expr:
    if isinstance(x, Expr):
        return x
    if isinstance(x, str):
        return Col(x)
    return Const(x)
