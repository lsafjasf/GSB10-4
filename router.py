r"""Priority-based HTTP path route matcher.

The module intentionally has no third-party dependencies.  Its matching
contract is:

* Larger numeric priority wins.
* Longest-prefix matching is never used to reorder explicit priorities.
* Multiple matches at the same priority are a conflict; no winner is chosen.
* ``*`` is the only wildcard.  ``\*`` and ``\\`` escape a literal star and
  backslash respectively.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass
import sys
from typing import Any, Iterable, Iterator, Mapping


@dataclass(frozen=True)
class Rule:
    """A route rule.

    ``id`` must be unique in a matcher.  ``priority`` is an integer; larger
    values are matched first.  ``meta`` is opaque user data carried by the
    rule and returned with matches.
    """

    id: str
    pattern: str
    priority: int = 0
    meta: Any = None


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    literal_prefix: str
    segments: tuple[str, ...]

    @property
    def has_wildcard(self) -> bool:
        return len(self.segments) > 1


@dataclass(frozen=True)
class MatchResult:
    """Successful match result.

    ``matches`` contains the winning rule in normal mode.  In audit mode it
    contains matches from every priority, highest priority first.  Rule order
    inside each priority is sorted by rule id, never by load order.
    """

    request: str
    rule: Rule | None
    priority: int | None
    matches: tuple[Rule, ...]

    @property
    def ok(self) -> bool:
        return self.rule is not None


class RouteConflictError(Exception):
    """Raised when one request matches multiple rules at one priority."""

    def __init__(self, request: str, conflicts: Mapping[int, tuple[Rule, ...]]):
        self.request = request
        self.conflicts = {
            priority: tuple(rules) for priority, rules in conflicts.items()
        }
        parts = []
        for priority in sorted(self.conflicts, reverse=True):
            rules = self.conflicts[priority]
            ids = ", ".join(rule.id for rule in rules)
            parts.append(f"priority {priority}: {len(rules)} matches ({ids})")
        super().__init__(f"route conflict for {request!r}: " + "; ".join(parts))


class _Node:
    __slots__ = ("children", "exact", "wildcard")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.exact: list[CompiledRule] = []
        self.wildcard: list[CompiledRule] = []


def _split_pattern(pattern: str) -> tuple[str, tuple[str, ...]]:
    """Return (literal prefix before first '*', wildcard segments)."""

    segments: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if (
            char == "\\"
            and index + 1 < len(pattern)
            and pattern[index + 1] in ("*", "\\")
        ):
            current.append(pattern[index + 1])
            index += 2
            continue
        if char == "*":
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    segments.append("".join(current))
    return segments[0], tuple(segments)


def _wildcard_matches(segments: tuple[str, ...], path: str) -> bool:
    """Match unescaped wildcard segments against ``path``."""

    if len(segments) == 1:
        return path == segments[0]

    first = segments[0]
    if not path.startswith(first):
        return False
    position = len(first)

    for segment in segments[1:-1]:
        if not segment:
            continue
        found = path.find(segment, position)
        if found < 0:
            return False
        position = found + len(segment)

    last = segments[-1]
    if not last:
        return True
    return path.endswith(last) and len(path) - len(last) >= position


def _compile(rule: Rule) -> CompiledRule:
    literal_prefix, segments = _split_pattern(rule.pattern)
    return CompiledRule(rule=rule, literal_prefix=literal_prefix, segments=segments)


def _iter_trie_candidates(root: _Node, path: str) -> Iterator[list[CompiledRule]]:
    """Yield wildcard lists at every literal-prefix node, then exact rules."""

    node = root
    if node.wildcard:
        yield node.wildcard
    for char in path:
        node = node.children.get(char)
        if node is None:
            return
        if node.wildcard:
            yield node.wildcard
    if node.exact:
        yield node.exact


class RouteMatcher:
    """Deterministic priority route matcher.

    The index is one literal-prefix trie per priority.  Lookup walks only the
    nodes on the request path, so result ordering and conflict detection do
    not depend on rule insertion order.
    """

    def __init__(self, rules: Iterable[Rule] = ()):  # noqa: B008 - immutable default
        self._tries: dict[int, _Node] = {}
        self._priorities: list[int] = []  # ascending; queried in reverse
        self._rules: dict[str, Rule] = {}
        self._node_count = 0
        self.add_rules(rules)

    def __len__(self) -> int:
        return len(self._rules)

    def add_rules(self, rules: Iterable[Rule]) -> None:
        for rule in rules:
            self.add_rule(rule)

    def add_rule(self, rule: Rule) -> None:
        self._validate_rule(rule)
        if rule.id in self._rules:
            raise ValueError(f"duplicate rule id: {rule.id!r}")
        compiled = _compile(rule)

        root = self._tries.get(rule.priority)
        if root is None:
            root = _Node()
            self._tries[rule.priority] = root
            insort(self._priorities, rule.priority)
            self._node_count += 1

        node = root
        for char in compiled.literal_prefix:
            child = node.children.get(char)
            if child is None:
                child = _Node()
                node.children[char] = child
                self._node_count += 1
            node = child

        if compiled.has_wildcard:
            node.wildcard.append(compiled)
        else:
            node.exact.append(compiled)
        self._rules[rule.id] = rule

    def match(self, request: str, *, audit: bool = False) -> MatchResult:
        """Match ``request`` and return a deterministic result.

        Every priority is checked for same-priority conflicts, including
        priorities shadowed by a higher-priority match.  If no conflict
        exists, the unique match at the highest matching priority wins.
        Audit mode additionally returns every priority's matches in the
        successful result.
        """

        if not isinstance(request, str):
            raise TypeError("request must be a str")

        all_matches: list[Rule] = []
        conflicts: dict[int, tuple[Rule, ...]] = {}
        winner: Rule | None = None
        winner_priority: int | None = None

        for priority in reversed(self._priorities):
            matched = self._match_priority(priority, request)
            if not matched:
                continue
            if len(matched) > 1:
                conflicts[priority] = tuple(matched)
            elif winner is None:
                winner = matched[0]
                winner_priority = priority

            all_matches.extend(matched)

        if conflicts:
            raise RouteConflictError(request, conflicts)
        return MatchResult(
            request=request,
            rule=winner,
            priority=winner_priority,
            matches=tuple(all_matches) if audit else ((winner,) if winner else ()),
        )

    def _match_priority(self, priority: int, request: str) -> list[Rule]:
        root = self._tries[priority]
        matched: list[Rule] = []
        for candidates in _iter_trie_candidates(root, request):
            for compiled in candidates:
                if compiled.has_wildcard:
                    if _wildcard_matches(compiled.segments, request):
                        matched.append(compiled.rule)
                else:
                    matched.append(compiled.rule)
        matched.sort(key=lambda rule: rule.id)
        return matched

    def stats(self) -> dict[str, int]:
        """Return rule/index cardinality and approximate reachable bytes."""

        return {
            "rules": len(self._rules),
            "priorities": len(self._priorities),
            "trie_nodes": self._node_count,
            "index_bytes": _deep_size(self),
        }

    @staticmethod
    def _validate_rule(rule: Rule) -> None:
        if not isinstance(rule, Rule):
            raise TypeError("rule must be a Rule")
        if not isinstance(rule.id, str) or not rule.id:
            raise ValueError("rule id must be a non-empty str")
        if not isinstance(rule.pattern, str) or not rule.pattern:
            raise ValueError(f"rule {rule.id!r}: pattern must be a non-empty str")
        if not isinstance(rule.priority, int) or isinstance(rule.priority, bool):
            raise ValueError(f"rule {rule.id!r}: priority must be an int")


def _deep_size(obj: Any, seen: set[int] | None = None) -> int:
    """Approximate bytes reachable from obj on CPython, counting objects once."""

    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return 0
    seen.add(obj_id)

    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            size += _deep_size(key, seen)
            size += _deep_size(value, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += _deep_size(item, seen)
    elif hasattr(obj, "__dict__"):
        size += _deep_size(vars(obj), seen)
    elif hasattr(obj, "__slots__"):
        for slot in obj.__slots__:
            if hasattr(obj, slot):
                size += _deep_size(getattr(obj, slot), seen)
    return size


__all__ = [
    "CompiledRule",
    "MatchResult",
    "RouteConflictError",
    "RouteMatcher",
    "Rule",
]
