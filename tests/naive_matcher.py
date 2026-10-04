"""naive_matcher.py -- 独立的朴素参考实现，仅用于对拍。

与 router.py 的 trie 实现刻意分开写：
* 不共享解析/匹配代码；
* glob 用手写字符扫描（而非 re）；
* 每次查询线性扫描全部规则。
裁决规则相同：命中前缀即收集，按显式 priority 取最高一组。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class NaiveRule:
    raw: str
    priority: int
    rule_id: str


def _split(pattern: str) -> List[str]:
    return [s for s in pattern.split("/") if s != ""]


def _seg_tokens(seg: str) -> Tuple[Tuple[str, str], ...]:
    """把一个段解析成 token 序列：('lit', 字面串) / ('star', '')。"""
    tokens: List[Tuple[str, str]] = []
    buf: List[str] = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == "\\" and i + 1 < len(seg) and seg[i + 1] in ("*", "\\"):
            buf.append(seg[i + 1])
            i += 2
        elif ch == "*":
            if buf:
                tokens.append(("lit", "".join(buf)))
                buf = []
            tokens.append(("star", ""))
            i += 1
        else:
            buf.append(ch)
            i += 1
    if buf:
        tokens.append(("lit", "".join(buf)))
    return tuple(tokens)


def _seg_match(tokens, text: str) -> bool:
    """无回溯的 '*' 匹配（每段只含固定 token 序列，足以处理 * 在任意位置）。"""
    # 手写双向贪心，避免依赖 fnmatch/re。
    if all(kind == "lit" for kind, _ in tokens):
        return text == "".join(val for _, val in tokens)
    n, m = len(tokens), len(text)
    left_tok = 0
    left_pos = 0
    right_tok = n - 1
    right_pos = m
    # 前导固定部分
    while left_tok <= right_tok and tokens[left_tok][0] == "lit":
        lit = tokens[left_tok][1]
        if not text.startswith(lit, left_pos):
            return False
        left_pos += len(lit)
        left_tok += 1
    # 后缀固定部分
    while left_tok <= right_tok and tokens[right_tok][0] == "lit":
        lit = tokens[right_tok][1]
        if right_pos - left_pos < len(lit) or not text.endswith(lit, 0, right_pos):
            return False
        right_pos -= len(lit)
        right_tok -= 1
    # 中间剩余 literal（夹在 star 之间）顺序查找
    while left_tok <= right_tok:
        kind, val = tokens[left_tok]
        if kind == "star":
            left_tok += 1
            continue
        if left_pos > right_pos or text.find(val, left_pos, right_pos) < 0:
            return False
        left_pos = text.find(val, left_pos, right_pos) + len(val)
        left_tok += 1
    return left_pos <= right_pos


class NaiveRouter:
    def __init__(self, rules: List[NaiveRule]):
        self._rules = [
            (r.rule_id, r.raw, r.priority, tuple(_seg_tokens(s) for s in _split(r.raw)))
            for r in rules
        ]

    def evaluate(self, path: str):
        req = _split(path)
        hit_ids = []
        top = None
        for rule_id, raw, pri, seg_tokens in self._rules:
            if len(seg_tokens) > len(req):
                continue  # 前缀语义：规则段不能多于请求段
            if all(_seg_match(t, req[i]) for i, t in enumerate(seg_tokens)):
                hit_ids.append(rule_id)
                if top is None or pri > top:
                    top = pri
        return {"hits": tuple(sorted(hit_ids)), "top_priority": top}
