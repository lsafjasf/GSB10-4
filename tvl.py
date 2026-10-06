"""Three-valued logic (3VL) and SQL NULL semantics. Standard library only.

Truth values are the singletons TRUE, FALSE, UNKNOWN.
SQL NULL is the singleton NULL; Python None is also accepted as NULL.

Core rules implemented (SQL:2023 semantics):
- Any comparison (=, <>, <, <=, >, >=) with NULL on either side -> UNKNOWN.
- NOT UNKNOWN -> UNKNOWN.
- AND: FALSE dominates; UNKNOWN dominates TRUE.
- OR:  TRUE dominates; UNKNOWN dominates FALSE.
- WHERE / JOIN ON / HAVING keep only rows whose predicate is TRUE
  (FALSE and UNKNOWN are both filtered out).
- Aggregates (COUNT(col), SUM, AVG, MIN, MAX) skip NULL inputs;
  if no non-NULL input remains, result is NULL (COUNT returns 0).
- GROUP BY treats all NULLs as one group.
- DISTINCT treats all NULLs as duplicates of each other.
- ORDER BY places NULLs first or last (explicit, configurable).
"""

import operator


class _Tri:
    """A three-valued logic truth value. Singletons only."""

    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name

    def __bool__(self):
        raise TypeError(
            "3VL truth value %s has no boolean coercion; "
            "use `is TRUE` / `is not TRUE` explicitly" % self._name
        )


TRUE = _Tri("TRUE")
FALSE = _Tri("FALSE")
UNKNOWN = _Tri("UNKNOWN")


class _Null:
    """The SQL NULL marker. Singleton; None is accepted as an alias."""

    __slots__ = ()

    def __repr__(self):
        return "NULL"

    def __bool__(self):
        raise TypeError("NULL has no boolean coercion")


NULL = _Null()


def is_null(value):
    return value is NULL or value is None


# ---------------------------------------------------------------------------
# Logical connectives (Kleene strong three-valued logic, as used by SQL)
# ---------------------------------------------------------------------------

_AND_TABLE = {
    (TRUE, TRUE): TRUE, (TRUE, FALSE): FALSE, (TRUE, UNKNOWN): UNKNOWN,
    (FALSE, TRUE): FALSE, (FALSE, FALSE): FALSE, (FALSE, UNKNOWN): FALSE,
    (UNKNOWN, TRUE): UNKNOWN, (UNKNOWN, FALSE): FALSE, (UNKNOWN, UNKNOWN): UNKNOWN,
}

_OR_TABLE = {
    (TRUE, TRUE): TRUE, (TRUE, FALSE): TRUE, (TRUE, UNKNOWN): TRUE,
    (FALSE, TRUE): TRUE, (FALSE, FALSE): FALSE, (FALSE, UNKNOWN): UNKNOWN,
    (UNKNOWN, TRUE): TRUE, (UNKNOWN, FALSE): UNKNOWN, (UNKNOWN, UNKNOWN): UNKNOWN,
}

_NOT_TABLE = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}


def _check_tri(v):
    if v not in (TRUE, FALSE, UNKNOWN):
        raise TypeError("expected TRUE/FALSE/UNKNOWN, got %r" % (v,))
    return v


def and_(a, b):
    return _AND_TABLE[(_check_tri(a), _check_tri(b))]


def or_(a, b):
    return _OR_TABLE[(_check_tri(a), _check_tri(b))]


def not_(a):
    return _NOT_TABLE[_check_tri(a)]


# ---------------------------------------------------------------------------
# Short-circuit variants: take a thunk for the right operand and skip it
# when the left operand already determines the result. Semantics are
# identical to and_/or_ (verified against the tables in the test suite).
# ---------------------------------------------------------------------------

def and_sc(a, b_thunk):
    _check_tri(a)
    if a is FALSE:
        return FALSE            # FALSE AND anything -> FALSE, skip b
    b = _check_tri(b_thunk())
    return _AND_TABLE[(a, b)]


def or_sc(a, b_thunk):
    _check_tri(a)
    if a is TRUE:
        return TRUE             # TRUE OR anything -> TRUE, skip b
    b = _check_tri(b_thunk())
    return _OR_TABLE[(a, b)]


# ---------------------------------------------------------------------------
# Comparisons: any NULL operand -> UNKNOWN
# ---------------------------------------------------------------------------

_OPS = {
    "=": operator.eq, "==": operator.eq,
    "<>": operator.ne, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge,
}


def compare(op, a, b):
    """SQL comparison. Returns TRUE/FALSE, or UNKNOWN if either side is NULL."""
    if op not in _OPS:
        raise ValueError("unknown operator %r" % (op,))
    if is_null(a) or is_null(b):
        return UNKNOWN
    return TRUE if _OPS[op](a, b) else FALSE


def eq(a, b):
    return compare("=", a, b)


def ne(a, b):
    return compare("<>", a, b)


def lt(a, b):
    return compare("<", a, b)


def le(a, b):
    return compare("<=", a, b)


def gt(a, b):
    return compare(">", a, b)


def ge(a, b):
    return compare(">=", a, b)


def is_null_pred(a):
    """SQL `IS NULL`: always TRUE or FALSE, never UNKNOWN."""
    return TRUE if is_null(a) else FALSE


# ---------------------------------------------------------------------------
# Row-level operators: WHERE, JOIN, HAVING keep only TRUE
# ---------------------------------------------------------------------------

def where(rows, pred):
    """Filter rows; a row survives only when pred(row) is TRUE."""
    return [r for r in rows if pred(r) is TRUE]


def inner_join(left_rows, right_rows, on):
    """Nested-loop inner join; a pair matches only when on(l, r) is TRUE."""
    return [(l, r) for l in left_rows for r in right_rows if on(l, r) is TRUE]


def having(groups, pred):
    """Keep only groups whose predicate is TRUE."""
    return {k: g for k, g in groups.items() if pred(g) is TRUE}


# ---------------------------------------------------------------------------
# Aggregates: NULL inputs are skipped; empty/all-NULL input -> NULL
# (COUNT is the exception: it counts and returns 0 on empty input).
# ---------------------------------------------------------------------------

def _non_null(values):
    return [v for v in values if not is_null(v)]


def count_star(rows):
    return len(list(rows))


def count(values):
    """COUNT(col): number of non-NULL values."""
    return len(_non_null(values))


def sum_(values):
    vals = _non_null(values)
    return sum(vals) if vals else NULL


def avg(values):
    vals = _non_null(values)
    return (sum(vals) / len(vals)) if vals else NULL


def min_(values):
    vals = _non_null(values)
    return min(vals) if vals else NULL


def max_(values):
    vals = _non_null(values)
    return max(vals) if vals else NULL


# ---------------------------------------------------------------------------
# GROUP BY / DISTINCT / ORDER BY: NULL placement
# ---------------------------------------------------------------------------

def _group_key(value):
    return NULL if is_null(value) else value


def group_by(rows, key):
    """All NULL keys collapse into a single group keyed by NULL."""
    groups = {}
    for row in rows:
        groups.setdefault(_group_key(key(row)), []).append(row)
    return groups


def distinct(values):
    """All NULLs are duplicates of each other -> at most one NULL survives."""
    seen = []
    for v in values:
        k = _group_key(v)
        if not any(k is NULL and s is NULL or k == s for s in seen):
            seen.append(k)
    return seen


def order_by(values, reverse=False, nulls_first=None):
    """Sort values with explicit NULL placement.

    nulls_first=None follows the common default tied to direction:
    ASC -> NULLS LAST, DESC -> NULLS FIRST (PostgreSQL convention).
    Pass True/False to force NULLS FIRST / NULLS LAST.
    """
    if nulls_first is None:
        nulls_first = reverse
    nulls = [NULL for v in values if is_null(v)]
    rest = sorted((v for v in values if not is_null(v)), reverse=reverse)
    return (nulls + rest) if nulls_first else (rest + nulls)
