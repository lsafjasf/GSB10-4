"""Table-based factor over discrete variables.

A factor scope is an ordered tuple of variable names.  Values live in a flat
list; index of an assignment follows big-endian (last variable fastest) order:

    idx = assignment[vars[0]] * (cards[1]*...*cards[k-1]) + ... + assignment[vars[-1]]
"""

from itertools import product


class Factor:
    __slots__ = ("vars", "cards", "values")

    def __init__(self, variables, cardinalities, values):
        self.vars = tuple(variables)
        self.cards = tuple(cardinalities)
        if len(self.vars) != len(self.cards):
            raise ValueError("variables and cardinalities length mismatch")
        expected = 1
        for c in self.cards:
            expected *= c
        if len(values) != expected:
            raise ValueError(
                f"expected {expected} values for scope {self.vars}, got {len(values)}"
            )
        self.values = [float(v) for v in values]

    @staticmethod
    def unary(variable, cardinality, values):
        return Factor((variable,), (cardinality,), values)

    def get(self, assignment):
        idx = 0
        for name, card in zip(self.vars, self.cards):
            idx = idx * card + assignment[name]
        return self.values[idx]

    def slice_assignment(self, assignment):
        """Yield (idx, dict-assignment) for every row of the factor table."""
        for idx, combo in enumerate(product(*(range(c) for c in self.cards))):
            assignment.update(zip(self.vars, combo))
            yield idx, assignment
            for name in self.vars:
                del assignment[name]

    def copy(self):
        return Factor(self.vars, self.cards, self.values)


def multiply(f, g):
    """Pointwise product of two factors (scopes need not be disjoint)."""
    shared = set(f.vars) & set(g.vars)
    variables = f.vars + tuple(v for v in g.vars if v not in shared)
    cards = {}
    cards.update(zip(f.vars, f.cards))
    cards.update(zip(g.vars, g.cards))
    cardinalities = tuple(cards[v] for v in variables)
    values = []
    assignment = {}
    for combo in product(*(range(c) for c in cardinalities)):
        assignment.update(zip(variables, combo))
        values.append(f.get(assignment) * g.get(assignment))
    return Factor(variables, cardinalities, values)


def sum_out(f, variable):
    """Marginalize one variable out of a factor."""
    if variable not in f.vars:
        return f.copy()
    pos = f.vars.index(variable)
    variables = f.vars[:pos] + f.vars[pos + 1:]
    cardinalities = f.cards[:pos] + f.cards[pos + 1:]
    values = [0.0] * (len(f.values) // f.cards[pos])
    for idx, value in enumerate(f.values):
        rest = idx // f.cards[pos]
        stride = 1
        for c in f.cards[pos + 1:]:
            stride *= c
        out_idx = (idx // (stride * f.cards[pos])) * stride + idx % stride
        values[out_idx] += value
    return Factor(variables, cardinalities, values)


def normalize(f):
    """Return a normalized copy so values sum to 1. Raises on empty support."""
    total = sum(f.values)
    if not (total > 0.0):
        raise DegenerateFactorError(
            f"cannot normalize factor over {f.vars}: total mass {total!r}"
        )
    return Factor(f.vars, f.cards, [v / total for v in f.values])


class DegenerateFactorError(ValueError):
    """Factor carries no positive mass (zero everywhere or NaN)."""
