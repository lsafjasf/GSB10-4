"""样式继承链解析：断链报告、环检测、链式合并。

全部为迭代实现（父链是单指针结构），继承深度不设上限，
不受 Python 递归深度限制。
"""
from typing import Any, Dict, List, Optional, Tuple

from .model import Style


def normalize_based_on(value: Optional[str]) -> Optional[str]:
    """把 None / 空串 / 空白串统一归为“无父样式”。"""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def analyze_styles(styles: Dict[str, Style]):
    """扫描样式表，返回 (cycles, missing_edges)。

    cycles: [(环上样式名元组, 环外前驱样式名元组), ...]，按发现顺序去重，
            环内顺序为沿 based_on 的行走顺序。
    missing_edges: [(样式名, 缺失的父样式名), ...]，按发现顺序去重。
    """
    cycles: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = []
    missing: List[Tuple[str, str]] = []
    state: Dict[str, int] = {}  # 0=未访问 1=在当前栈上 2=已完成
    for name in styles:
        if state.get(name, 0) != 0:
            continue
        path: List[str] = []
        pos: Dict[str, int] = {}
        cur: Optional[str] = name
        while cur is not None and cur in styles and state.get(cur, 0) == 0:
            state[cur] = 1
            pos[cur] = len(path)
            path.append(cur)
            cur = normalize_based_on(styles[cur].based_on)
        if cur is not None and cur in styles and state.get(cur) == 1:
            idx = pos[cur]
            cycles.append((tuple(path[idx:]), tuple(path[:idx])))
        if cur is not None and cur not in styles:
            missing.append((path[-1], cur))
        for node in path:
            state[node] = 2
    return cycles, missing


def style_chain_layers(styles: Dict[str, Style], name: str):
    """返回 [(样式名, 自有属性), ...]，按优先级从高到低（近 -> 远）。

    断链在缺失处截断；遇环时环上每个成员恰好出现一次
    （行走顺序即优先级顺序，环入口最近）。
    """
    layers: List[Tuple[str, Dict[str, Any]]] = []
    pos: Dict[str, int] = {}
    cur: Optional[str] = name
    while cur is not None and cur in styles and cur not in pos:
        pos[cur] = len(layers)
        layers.append((cur, styles[cur].props))
        cur = normalize_based_on(styles[cur].based_on)
    return layers


def resolve_style_full(styles: Dict[str, Style], name: str,
                       cache: Dict[str, Dict[str, Tuple[Any, str]]]):
    """返回 {属性: (值, 来源样式名)}：name 沿继承链合并后的全部属性。

    合并方向：远的祖先先写，近的样式覆盖（近者优先）。
    断链：缺失父样式处截断，缺失部分由调用方回退到显式默认值。
    环：按“最远者优先”合并——从本次入口沿环走一整圈，每个成员生效一次，
    结果确定且与 style_chain_layers 的逐层手工推算一致。
    注意环成员的合并结果随入口位置而变（环 A>B>C 从 A 进入是 A>B>C，
    从 B 进入是 B>C>A），因此环成员不写入全局缓存；只有环外前缀可缓存。
    """
    if name in cache:
        return cache[name]
    path: List[str] = []
    pos: Dict[str, int] = {}
    cur: Optional[str] = name
    while cur is not None and cur in styles and cur not in cache and cur not in pos:
        pos[cur] = len(path)
        path.append(cur)
        cur = normalize_based_on(styles[cur].based_on)
    if cur is not None and cur in pos:  # 发现环
        idx = pos[cur]
        ring = path[idx:]
        base: Dict[str, Tuple[Any, str]] = {}
        # 最远者优先：从环上最远成员写起，环入口（最近）最后覆盖
        for offset in range(len(ring) - 1, -1, -1):
            member = ring[offset]
            base = _overlay(base, styles[member].props, member)
        path = path[:idx]
    else:
        base = dict(cache[cur]) if (cur is not None and cur in cache) else {}
    for i in range(len(path) - 1, -1, -1):
        node = path[i]
        base = _overlay(base, styles[node].props, node)
        cache[node] = base
    return cache[name] if name in cache else base


def _overlay(base: Dict[str, Tuple[Any, str]], props: Dict[str, Any],
             label: str) -> Dict[str, Tuple[Any, str]]:
    merged = dict(base)
    for key, value in props.items():
        merged[key] = (value, label)
    return merged
