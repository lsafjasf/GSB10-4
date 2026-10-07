"""Factor graph: variables, factors, adjacency."""

from .factor import Factor


class FactorGraph:
    def __init__(self):
        self.cardinality = {}
        self.factors = []
        self.var_to_factors = {}

    def add_variable(self, name, cardinality):
        if cardinality < 2:
            raise ValueError("cardinality must be >= 2")
        if name in self.cardinality:
            raise ValueError(f"duplicate variable {name!r}")
        self.cardinality[name] = cardinality
        self.var_to_factors[name] = []

    def add_factor(self, variables, values):
        cards = []
        for v in variables:
            if v not in self.cardinality:
                raise ValueError(f"unknown variable {v!r} in factor")
            cards.append(self.cardinality[v])
        factor = Factor(variables, cards, values)
        idx = len(self.factors)
        self.factors.append(factor)
        for v in variables:
            self.var_to_factors[v].append(idx)
        return idx

    def variables(self):
        return list(self.cardinality)
