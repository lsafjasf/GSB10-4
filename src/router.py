"""router.py -- 带显式优先级的路由匹配库（仅标准库）。

语义总览（详见 PRIORITY.md）：

* 规则 pattern 由 '/' 分段；每段是字面量或含 '*' 的 glob（'*' 匹配段内任意
  字符串，含空串，不跨段）。'\\' 为转义符：'\\*' 是字面星号、'\\\\' 是字面
  反斜杠；其余 '\\x' 与结尾孤立 '\\' 在加载期直接报 PatternError。
* 匹配为“前缀匹配”：规则段序列是请求段序列的前缀（逐段匹配）即命中。
  空 pattern（'/'）命中一切请求。
* 裁决只看显式 priority（整数，越大越优先，默认 0）：
  取命中规则中 priority 最大的一组；恰好一条 -> 命中；多于一条 -> 冲突，
  冲突报告列出该优先级上的全部命中项。最长前缀（specificity）不参与裁决，
  仅作为冲突报告中的诊断信息，依据见 PRIORITY.md。
* 匹配结果与规则加载顺序无关：规则先按规范化 (pattern, priority) 排序去重
  再建索引，索引与输出排序均为全序，不含任何插入序。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "PatternError",
    "DuplicateRuleError",
    "Rule",
    "Hit",
    "MatchResult",
    "Router",
    "parse_pattern",
    "normalize_path",
    "index_memory_bytes",
]


class PatternError(ValueError):
    """规则 pattern 非法（如非法转义）。"""


class DuplicateRuleError(ValueError):
    """规范化后 (pattern, priority) 完全相同的规则。"""


# ---------------------------------------------------------------------------
# pattern 解析
# ---------------------------------------------------------------------------

def normalize_path(path: str) -> List[str]:
    """把请求路径规范化成段列表：丢弃空段（前导/尾随/重复 '/'）。"""
    if not isinstance(path, str):
        raise TypeError("path must be str")
    return [seg for seg in path.split("/") if seg != ""]


@dataclass(frozen=True, eq=False)
class Segment:
    """一个已编译的 pattern 段。

    is_glob=False 时 literal 为整段字面量；is_glob=True 时 regex 为整段锚定
    正则（'*' -> '.*'，不跨段）。literal_chars 为段内字面字符数（glob 段也计，
    用于 specificity 诊断）。相等性按 (is_glob, literal, 正则文本) 判定，
    与 re.Pattern 对象身份无关，保证相同 pattern 可去重。
    """
    is_glob: bool
    literal: str = ""
    regex: Optional[re.Pattern] = None
    literal_chars: int = 0

    def _key(self):
        return (
            self.is_glob,
            self.literal,
            (self.regex.pattern, self.regex.flags) if self.regex is not None else None,
        )

    def __eq__(self, other):
        return isinstance(other, Segment) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def matches(self, seg: str) -> bool:
        if self.is_glob:
            return self.regex.match(seg) is not None
        return seg == self.literal


def _compile_segment(raw: str, pattern: str) -> Segment:
    """解析单个原始段：处理转义，识别 glob。"""
    char_buffer: List[str] = []
    regex_parts: List[str] = []
    has_star = False
    n_literal = 0
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= n:
                raise PatternError(
                    "dangling escape at end of segment %r in pattern %r" % (raw, pattern)
                )
            nxt = raw[i + 1]
            if nxt not in ("*", "\\"):
                raise PatternError(
                    "invalid escape '\\%s' in pattern %r (only \\* and \\\\ allowed)"
                    % (nxt, pattern)
                )
            char_buffer.append(nxt)
            regex_parts.append(re.escape(nxt))
            n_literal += 1
            i += 2
        elif ch == "*":
            has_star = True
            regex_parts.append(".*")
            i += 1
        else:
            char_buffer.append(ch)
            regex_parts.append(re.escape(ch))
            n_literal += 1
            i += 1
    if has_star:
        return Segment(is_glob=True, regex=re.compile("".join(regex_parts)),
                       literal_chars=n_literal)
    return Segment(is_glob=False, literal="".join(char_buffer),
                   literal_chars=n_literal)


def parse_pattern(pattern: str) -> Tuple[Segment, ...]:
    """把 pattern 编译成段序列。空 pattern（''、'/'）-> 空元组，命中一切。"""
    if not isinstance(pattern, str):
        raise TypeError("pattern must be str")
    raw_segments = [seg for seg in pattern.split("/") if seg != ""]
    return tuple(_compile_segment(raw, pattern) for raw in raw_segments)


# ---------------------------------------------------------------------------
# 规则与结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    pattern: str
    priority: int = 0
    id: Optional[str] = None
    meta: Any = None

    def __post_init__(self):
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise TypeError("priority must be int")


@dataclass(frozen=True)
class Hit:
    """一条命中规则的视图。specificity 仅用于冲突报告诊断，不参与裁决。"""
    rule_id: str
    pattern: str
    priority: int
    specificity: Tuple[int, int, int]
    meta: Any = None


@dataclass(frozen=True)
class MatchResult:
    """match() 的返回。status: 'match' | 'conflict' | 'no_match'。"""
    status: str
    path: str
    winner: Optional[Hit]
    matches: Tuple[Hit, ...]
    top_priority: Optional[int]

    @property
    def ok(self) -> bool:
        return self.status == "match"

    def report(self) -> str:
        return format_report(self)


# ---------------------------------------------------------------------------
# trie 索引
# ---------------------------------------------------------------------------

class _Node:
    __slots__ = ("children", "globs", "rules")

    def __init__(self):
        self.children: Dict[str, "_Node"] = {}
        self.globs: List[Tuple[Segment, "_Node"]] = []
        self.rules: List[int] = []  # 命中于此的规则下标（前缀语义：段耗尽即挂载）


@dataclass
class _Compiled:
    segments: Tuple[Segment, ...]
    priority: int
    rule_id: str
    pattern: str
    specificity: Tuple[int, int, int]
    meta: Any


def _specificity(segments: Tuple[Segment, ...]) -> Tuple[int, int, int]:
    """最长前缀诊断指标：段数、字面段数、字面字符总数（glob 段也计字面部分），
    越大越具体。仅用于冲突报告诊断，不参与裁决。"""
    literal_segs = 0
    literal_chars = 0
    for seg in segments:
        if not seg.is_glob:
            literal_segs += 1
        literal_chars += seg.literal_chars
    return (len(segments), literal_segs, literal_chars)


class Router:
    """加载规则、构建索引、执行匹配。构建后只读，线程安全（无共享可变状态）。"""

    def __init__(self, rules: Iterable[Rule] = ()):  # noqa: D401
        self._rules: List[_Compiled] = []
        self._root = _Node()
        self._build(list(rules))

    # -- 构建 ------------------------------------------------------------

    def _build(self, rules: List[Rule]) -> None:
        # 1) 规范化 + 稳定排序：结果与加载顺序无关的关键。
        # norm_raw 是 segments 的唯一规范表示：转义形式唯一（\*、\\），
        # 重复/首尾 '/' 已归一化，故可作全序排序键，保证与加载顺序无关。
        keyed: Dict[Tuple[Tuple[Segment, ...], int], Tuple[str, Rule]] = {}
        for rule in rules:
            segments = parse_pattern(rule.pattern)
            key = (segments, rule.priority)
            if key in keyed:
                raise DuplicateRuleError(
                    "duplicate rule (pattern=%r, priority=%d)" % (rule.pattern, rule.priority)
                )
            norm_raw = "/" + "/".join(s for s in rule.pattern.split("/") if s != "")
            if norm_raw == "/":
                norm_raw = ""
            keyed[key] = (norm_raw, rule)
        ordered = sorted(keyed.items(), key=lambda kv: (kv[1][0], kv[0][1]))

        # 2) 赋稳定 id 并插入 trie。
        for idx, ((segments, priority), (_, rule)) in enumerate(ordered):
            rule_id = rule.id if rule.id is not None else "r%d" % idx
            compiled = _Compiled(
                segments=segments,
                priority=priority,
                rule_id=rule_id,
                pattern=rule.pattern,
                specificity=_specificity(segments),
                meta=rule.meta,
            )
            self._rules.append(compiled)
            node = self._root
            for seg in segments:
                if seg.is_glob:
                    nxt = None
                    for existing_seg, existing_node in node.globs:
                        if existing_seg == seg:
                            nxt = existing_node
                            break
                    if nxt is None:
                        nxt = _Node()
                        node.globs.append((seg, nxt))
                    node = nxt
                else:
                    node = node.children.setdefault(seg.literal, _Node())
            node.rules.append(idx)

    # -- 匹配 ------------------------------------------------------------

    def _collect(self, path: str) -> List[int]:
        """返回命中规则下标（升序，确定）。"""
        segments = normalize_path(path)
        hits: List[int] = []
        root = self._root
        hits.extend(root.rules)  # 空 pattern 命中一切
        # 迭代 DFS：栈元素为 (节点, 下一段下标)
        stack: List[Tuple[_Node, int]] = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if depth == len(segments):
                continue
            seg = segments[depth]
            child = node.children.get(seg)
            if child is not None:
                hits.extend(child.rules)
                stack.append((child, depth + 1))
            for glob_seg, glob_node in node.globs:
                if glob_seg.matches(seg):
                    hits.extend(glob_node.rules)
                    stack.append((glob_node, depth + 1))
        hits.sort()
        return hits

    def evaluate(self, path: str) -> MatchResult:
        """完整求值：返回全部命中（按 priority 降序、specificity 降序、id 升序）。"""
        indices = self._collect(path)
        hits = [
            Hit(
                rule_id=self._rules[i].rule_id,
                pattern=self._rules[i].pattern,
                priority=self._rules[i].priority,
                specificity=self._rules[i].specificity,
                meta=self._rules[i].meta,
            )
            for i in indices
        ]
        # 稳定排序，按次要到主要键：id 升序 -> specificity 降序 -> priority 降序。
        hits.sort(key=lambda h: h.rule_id)
        hits.sort(key=lambda h: h.specificity, reverse=True)
        hits.sort(key=lambda h: -h.priority)
        if not hits:
            return MatchResult("no_match", path, None, (), None)
        top = hits[0].priority
        top_group = [h for h in hits if h.priority == top]
        if len(top_group) == 1:
            return MatchResult("match", path, top_group[0], tuple(hits), top)
        return MatchResult("conflict", path, None, tuple(hits), top)

    def match(self, path: str) -> Optional[Hit]:
        """唯一命中时返回 Hit；无命中返回 None；冲突抛 RouteConflict。"""
        result = self.evaluate(path)
        if result.status == "conflict":
            raise RouteConflict(result)
        return result.winner

    # -- 观测 ------------------------------------------------------------

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def index_memory_bytes(self) -> int:
        """索引（trie + 已编译规则）常驻内存的近似字节数。

        统计 trie 节点、已编译 Segment/正则对象、规则视图及其自有字符串；
        不计入用户 meta 与 re 引擎内部缓存（C 层，不可达）。
        """
        return index_memory_bytes(self)


class RouteConflict(Exception):
    """同一最高优先级上有多条规则命中。"""

    def __init__(self, result: MatchResult):
        self.result = result
        top = result.top_priority
        conflicting = [h for h in result.matches if h.priority == top]
        self.conflicting = tuple(conflicting)
        super().__init__(
            "route conflict on %r: %d rules at priority %d: %s"
            % (
                result.path,
                len(conflicting),
                top,
                ", ".join(h.rule_id for h in conflicting),
            )
        )


# ---------------------------------------------------------------------------
# 冲突报告
# ---------------------------------------------------------------------------

def format_report(result: MatchResult) -> str:
    """生成确定性的纯文本报告；冲突时列出最高优先级上的全部命中项。"""
    lines = [
        "path: %s" % result.path,
        "status: %s" % result.status,
    ]
    if result.status == "no_match":
        return "\n".join(lines)
    lines.append("top_priority: %d" % result.top_priority)
    if result.status == "match":
        lines.append("winner: %s" % result.winner.rule_id)
    else:
        conflicting = [h for h in result.matches if h.priority == result.top_priority]
        lines.append("conflict_count: %d" % len(conflicting))
        lines.append("conflicting_rules (全部最高优先级命中项, specificity 仅诊断):")
        for h in conflicting:
            lines.append(
                "  - id=%s pattern=%r priority=%d specificity=(segments=%d, literal_segments=%d, literal_chars=%d)"
                % (h.rule_id, h.pattern, h.priority, *h.specificity)
            )
    lines.append("all_matches:")
    for h in result.matches:
        lines.append(
            "  - id=%s pattern=%r priority=%d specificity=(segments=%d, literal_segments=%d, literal_chars=%d)"
            % (h.rule_id, h.pattern, h.priority, *h.specificity)
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 内存统计
# ---------------------------------------------------------------------------

def _deep_size(obj: Any, seen: set) -> int:
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += _deep_size(k, seen) + _deep_size(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            size += _deep_size(item, seen)
    elif isinstance(obj, Segment):
        if obj.regex is not None:
            size += sys.getsizeof(obj.regex)
        size += _deep_size(obj.literal, seen)
    elif isinstance(obj, _Compiled):
        size += _deep_size(obj.segments, seen)
        size += _deep_size(obj.rule_id, seen)
        size += _deep_size(obj.pattern, seen)
        # meta 属于用户数据，不计入索引
    elif isinstance(obj, _Node):
        size += _deep_size(obj.children, seen)
        size += _deep_size(obj.globs, seen)
        size += _deep_size(obj.rules, seen)
    return size


def index_memory_bytes(router: Router) -> int:
    seen: set = set()
    total = _deep_size(router._root, seen)
    total += _deep_size(router._rules, seen)
    return total
