"""Exact inference by variable elimination (sum-product)."""

from .factor import multiply, sum_out, normalize


def variable_elimination(graph, query, order=None):
    """Exact marginal P(query) as a Factor.

    Eliminates every non-query variable one by one (min-fill order unless an
    explicit order is given).  Exact; only feasible on small graphs.
    """
    if isinstance(query, str):
        query = (query,)
    query = tuple(query)
    for q in query:
        if q not in graph.cardinality:
            raise ValueError(f"unknown query variable {q!r}")

    factors = [f.copy() for f in graph.factors]
    hidden = [v for v in graph.variables() if v not in query]

    if order is None:
        order = _min_fill_order(graph, hidden)
    else:
        if set(order) != set(hidden):
            raise ValueError("elimination order must cover all hidden variables")

    for var in order:
        involved = [f for f in factors if var in f.vars]
        factors = [f for f in factors if var not in f.vars]
        if not involved:
            continue
        joint = involved[0]
        for f in involved[1:]:
            joint = multiply(joint, f)
        factors.append(sum_out(joint, var))

    joint = factors[0]
    for f in factors[1:]:
        joint = multiply(joint, f)
    # Reorder scope so it matches the requested query order.
    joint = _reorder(joint, query)
    return normalize(joint)


def _min_fill_order(graph, hidden):
    neighbors = {v: set() for v in graph.variables()}
    for f in graph.factors:
        for a in f.vars:
            for b in f.vars:
                if a != b:
                    neighbors[a].add(b)
    remaining = set(hidden)
    order = []
    while remaining:
        best, best_fill = None, None
        for v in remaining:
            nbrs = [u for u in neighbors[v] if u in remaining]
            fill = 0
            for i in range(len(nbrs)):
                for j in range(i + 1, len(nbrs)):
                    if nbrs[j] not in neighbors[nbrs[i]]:
                        fill += 1
            if best_fill is None or fill < best_fill:
                best, best_fill = v, fill
        order.append(best)
        remaining.discard(best)
        nbrs = [u for u in neighbors[best] if u in remaining]
        for i in range(len(nbrs)):
            for j in range(i + 1, len(nbrs)):
                neighbors[nbrs[i]].add(nbrs[j])
                neighbors[nbrs[j]].add(nbrs[i])
    return order


def _reorder(factor, query):
    from .factor import Factor
    cards = dict(zip(factor.vars, factor.cards))
    variables = tuple(v for v in query if v in cards)
    cardinalities = tuple(cards[v] for v in variables)
    values = []
    assignment = {}
    from itertools import product
    for combo in product(*(range(c) for c in cardinalities)):
        assignment.update(zip(variables, combo))
        values.append(factor.get(assignment))
    return Factor(variables, cardinalities, values)
