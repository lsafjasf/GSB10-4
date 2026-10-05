"""FSM observability analysis: state distinguishability / equivalence classes.

Model: partial Mealy machine.
  - states:   hashable labels
  - alphabet: input symbols
  - delta:    dict[(state, input)] -> (next_state, output)

Definition of indistinguishability (observational equivalence):
  States p and q are indistinguishable iff for EVERY finite input sequence
  the observable behaviour is identical, where "observable behaviour" means:
    1. Definedness: if following the sequence from p leads to a state where
       the next input has a defined transition, but from q it does not
       (or vice versa), the difference IS observable (one machine reacts,
       the other stays silent) -> the states are distinguishable.
    2. Outputs: whenever both transitions are defined, the emitted outputs
       must be equal at every step.
  Two states are distinguishable iff they are not indistinguishable, i.e.
  there exists a finite input sequence (a "distinguishing sequence" or
  counterexample) on which the observable behaviours differ.

Standard library only.
"""

from collections import defaultdict, deque

__all__ = [
    "Machine",
    "reachable_states",
    "unreachable_states",
    "missing_transitions",
    "equivalence_partition",
    "distinguishing_sequence",
    "reference_partition",
    "verify_partition",
    "equivalence_evidence",
    "exhaustive_pair_check",
    "random_machine",
    "analyze",
]


class Machine:
    """Partial Mealy machine."""

    def __init__(self, states, alphabet, delta, initial=None):
        self.states = list(states)
        self.alphabet = list(alphabet)
        state_set = set(self.states)
        alpha_set = set(self.alphabet)
        self.delta = {}
        for (s, a), trans in delta.items():
            t, o = trans
            if s not in state_set:
                raise ValueError("unknown source state: %r" % (s,))
            if t not in state_set:
                raise ValueError("unknown target state: %r" % (t,))
            if a not in alpha_set:
                raise ValueError("unknown input symbol: %r" % (a,))
            self.delta[(s, a)] = (t, o)
        if initial is None:
            initial = self.states[0] if self.states else None
        if initial is not None and initial not in state_set:
            raise ValueError("unknown initial state: %r" % (initial,))
        self.initial = initial

    def step(self, s, a):
        """Return (next_state, output) or None if the transition is missing."""
        return self.delta.get((s, a))


# ---------------------------------------------------------------------------
# Reachability / completeness diagnostics (reported separately, never mixed
# into the equivalence classes)
# ---------------------------------------------------------------------------

def reachable_states(machine):
    """Set of states reachable from the initial state."""
    seen = set()
    if machine.initial is None:
        return seen
    queue = deque([machine.initial])
    seen.add(machine.initial)
    while queue:
        s = queue.popleft()
        for a in machine.alphabet:
            trans = machine.delta.get((s, a))
            if trans is not None and trans[0] not in seen:
                seen.add(trans[0])
                queue.append(trans[0])
    return seen


def unreachable_states(machine):
    """States not reachable from the initial state (reported separately)."""
    return sorted(set(machine.states) - reachable_states(machine), key=repr)


def missing_transitions(machine, states=None):
    """List of (state, input) pairs with no defined transition."""
    if states is None:
        states = machine.states
    out = []
    for s in states:
        for a in machine.alphabet:
            if (s, a) not in machine.delta:
                out.append((s, a))
    return out


# ---------------------------------------------------------------------------
# Fast equivalence computation: iterative partition refinement
# ---------------------------------------------------------------------------

def _output_signature(machine, s):
    sig = []
    for a in machine.alphabet:
        trans = machine.delta.get((s, a))
        sig.append(trans[1] if trans is not None else None)
    return tuple(sig)


def equivalence_partition(machine, states=None):
    """Partition `states` (default: reachable states) into equivalence classes
    of indistinguishable states.

    Returns (classes, color) where classes is a sorted list of sorted lists
    and color maps state -> class id.

    Method: two states are equivalent iff they have the same output/definedness
    signature AND, for every input, their successors are equivalent
    (bisimulation on the quotient).  We compute the greatest fixed point by
    refinement: start from the output-signature colouring, repeatedly refine
    by the tuple (signature, colours of successors) until stable.
    """
    if states is None:
        states = sorted(reachable_states(machine), key=repr)
    else:
        states = list(states)
    if not states:
        return [], {}

    sigs = {s: _output_signature(machine, s) for s in states}
    cmap = {}
    for s in states:
        if sigs[s] not in cmap:
            cmap[sigs[s]] = len(cmap)
    color = {s: cmap[sigs[s]] for s in states}

    while True:
        keys = {}
        for s in states:
            succ = []
            for a in machine.alphabet:
                trans = machine.delta.get((s, a))
                succ.append(color[trans[0]] if trans is not None else None)
            keys[s] = (sigs[s], tuple(succ))
        newmap = {}
        newcolor = {}
        for s in states:
            k = keys[s]
            if k not in newmap:
                newmap[k] = len(newmap)
            newcolor[s] = newmap[k]
        if len(newmap) == len(set(color.values())):
            color = newcolor
            break
        color = newcolor

    classes = defaultdict(list)
    for s in states:
        classes[color[s]].append(s)
    out = [sorted(members, key=repr) for _, members in sorted(classes.items())]
    return out, color


# ---------------------------------------------------------------------------
# Distinguishing sequence for a pair (BFS on the product automaton)
# ---------------------------------------------------------------------------

def distinguishing_sequence(machine, p, q):
    """Return a shortest input sequence distinguishing p from q, or None if
    they are indistinguishable.

    BFS over the product automaton (pairs of states).  A pair (u, v) is
    immediately distinguished by input a when exactly one of the two
    transitions is defined, or both are defined with different outputs.
    """
    if p == q:
        return None
    start = (p, q)
    prev = {start: None}  # pair -> (parent_pair, input)
    queue = deque([start])
    while queue:
        u, v = queue.popleft()
        for a in machine.alphabet:
            tu = machine.delta.get((u, a))
            tv = machine.delta.get((v, a))
            if (tu is None) != (tv is None):
                return _reconstruct(prev, (u, v)) + [a]
            if tu is not None and tu[1] != tv[1]:
                return _reconstruct(prev, (u, v)) + [a]
            if tu is not None:
                nxt = (tu[0], tv[0])
                if nxt not in prev:
                    prev[nxt] = ((u, v), a)
                    queue.append(nxt)
    return None


def _reconstruct(prev, pair):
    seq = []
    while prev[pair] is not None:
        parent, a = prev[pair]
        seq.append(a)
        pair = parent
    seq.reverse()
    return seq


# ---------------------------------------------------------------------------
# Reference implementation: naive pairwise probing (used for differential
# testing / "对拍" against the fast partition refinement)
# ---------------------------------------------------------------------------

def reference_partition(machine, states=None):
    """Equivalence classes computed the slow, obviously-correct way:
    test every pair of states with an exhaustive product-automaton search
    (distinguishing_sequence) and merge equivalent pairs via union-find.
    """
    if states is None:
        states = sorted(reachable_states(machine), key=repr)
    else:
        states = list(states)
    parent = {s: s for s in states}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for i in range(len(states)):
        for j in range(i + 1, len(states)):
            p, q = states[i], states[j]
            if find(p) == find(q):
                continue
            if distinguishing_sequence(machine, p, q) is None:
                union(p, q)

    classes = defaultdict(list)
    for s in states:
        classes[find(s)].append(s)
    return [sorted(m, key=repr) for m in
            sorted(classes.values(), key=lambda m: repr(m[0]))]


# ---------------------------------------------------------------------------
# Evidence / certificates
# ---------------------------------------------------------------------------

def verify_partition(machine, classes):
    """Independently verify that a partition is a bisimulation: for every
    class, every member state, and every input, the outputs (and
    definedness) agree and the successors land in the same class.

    Returns (ok, violations).  `ok == True` is a machine-checkable proof
    that no pair inside any class can be distinguished by ANY input
    sequence (coinductive argument): any hypothetical distinguishing
    sequence would have to start with some input a; but on a the outputs
    match and the successors are again in the same class, so the remainder
    of the sequence cannot distinguish them either -- contradiction by
    induction on sequence length.
    """
    color = {}
    for idx, members in enumerate(classes):
        for s in members:
            color[s] = idx
    violations = []
    for idx, members in enumerate(classes):
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                p, q = members[i], members[j]
                for a in machine.alphabet:
                    tp = machine.delta.get((p, a))
                    tq = machine.delta.get((q, a))
                    if (tp is None) != (tq is None):
                        violations.append((p, q, a, "definedness mismatch"))
                    elif tp is not None:
                        if tp[1] != tq[1]:
                            violations.append((p, q, a, "output mismatch"))
                        elif color.get(tp[0]) != color.get(tq[0]):
                            violations.append((p, q, a, "successor class mismatch"))
    return (not violations, violations)


def equivalence_evidence(machine, color, p, q):
    """Per-pair evidence data showing why p and q cannot be distinguished:
    for every input, both states emit the same output (or are both
    undefined) and move to successors inside the same equivalence class.
    """
    rows = []
    for a in machine.alphabet:
        tp = machine.delta.get((p, a))
        tq = machine.delta.get((q, a))
        rows.append({
            "input": a,
            "p_output": tp[1] if tp else None,
            "q_output": tq[1] if tq else None,
            "p_next": tp[0] if tp else None,
            "q_next": tq[0] if tq else None,
            "p_next_class": color.get(tp[0]) if tp else None,
            "q_next_class": color.get(tq[0]) if tq else None,
            "outputs_match": (tp is None and tq is None) or
                             (tp is not None and tq is not None and tp[1] == tq[1]),
            "successors_same_class": (tp is None and tq is None) or
                                     (tp is not None and tq is not None and
                                      color.get(tp[0]) == color.get(tq[0])),
        })
    return {"pair": (p, q), "same_class": color.get(p) == color.get(q), "rows": rows}


def exhaustive_pair_check(machine, p, q, max_depth=None):
    """Evidence data for the claim "no distinguishing sequence exists":
    exhaustively simulate ALL input sequences up to length `max_depth`
    (default: |S|^2, a sufficient bound, because a shortest distinguishing
    sequence never revisits a pair of states), counting every sequence;
    report whether a counterexample exists."""
    if max_depth is None:
        max_depth = len(machine.states) * len(machine.states)
    counterexample = [None]

    def dfs(u, v, seq, checked):
        if counterexample[0] is not None:
            return checked
        if len(seq) >= max_depth:
            return checked
        for a in machine.alphabet:
            checked += 1
            tu = machine.delta.get((u, a))
            tv = machine.delta.get((v, a))
            if (tu is None) != (tv is None):
                counterexample[0] = seq + [a]
                return checked
            if tu is not None:
                if tu[1] != tv[1]:
                    counterexample[0] = seq + [a]
                    return checked
                checked = dfs(tu[0], tv[0], seq + [a], checked)
                if counterexample[0] is not None:
                    return checked
        return checked

    n = dfs(p, q, [], 0)
    return {
        "pair": (p, q),
        "max_depth": max_depth,
        "sequences_checked": n,
        "counterexample_found": counterexample[0],
    }

# ---------------------------------------------------------------------------
# Random machine generator (for differential testing and benchmarks)
# ---------------------------------------------------------------------------

def random_machine(n_states, n_transitions, n_inputs, seed,
                   n_outputs=3, allow_unreachable=True):
    """Deterministic random partial Mealy machine."""
    import random
    rng = random.Random(seed)
    states = ["s%d" % i for i in range(n_states)]
    alphabet = ["i%d" % i for i in range(n_inputs)]
    outputs = ["o%d" % i for i in range(n_outputs)]
    delta = {}
    # Guarantee connectivity of a core so the machine is interesting, then
    # add random edges up to the budget (possibly leaving states unreachable).
    core = min(n_states, max(1, n_transitions // max(1, n_inputs)))
    order = states[:]
    rng.shuffle(order)
    for i in range(1, core):
        s = order[i]
        a = rng.choice(alphabet)
        t = rng.choice(order[:i])  # link into earlier part to keep reachable
        delta[(s, a)] = (t, rng.choice(outputs))
    # forward edge from each core state to keep a spanning structure
    for i in range(core - 1):
        s = order[i]
        a = rng.choice(alphabet)
        if (s, a) not in delta:
            delta[(s, a)] = (order[i + 1], rng.choice(outputs))
    # fill remaining budget with random edges
    attempts = 0
    while len(delta) < n_transitions and attempts < n_transitions * 20:
        attempts += 1
        s = rng.choice(states if allow_unreachable else order[:core])
        a = rng.choice(alphabet)
        if (s, a) in delta:
            continue
        delta[(s, a)] = (rng.choice(states), rng.choice(outputs))
    return Machine(states, alphabet, delta, initial=order[0])


# ---------------------------------------------------------------------------
# Full analysis helper
# ---------------------------------------------------------------------------

def analyze(machine):
    """Run the full observability analysis. Returns a report dict with
    clearly separated sections: unreachable states, missing transitions,
    and equivalence classes (reachable states only)."""
    unreachable = unreachable_states(machine)
    reachable = sorted(set(machine.states) - set(unreachable), key=repr)
    missing = missing_transitions(machine)
    classes, color = equivalence_partition(machine, states=reachable)
    ok, violations = verify_partition(machine, classes)
    return {
        "n_states": len(machine.states),
        "n_transitions": len(machine.delta),
        "initial": machine.initial,
        "unreachable_states": unreachable,
        "missing_transitions": missing,
        "equivalence_classes": classes,
        "color": color,
        "certificate_valid": ok,
        "certificate_violations": violations,
    }
