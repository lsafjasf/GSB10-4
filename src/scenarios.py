# -*- coding: utf-8 -*-
"""四类典型场景的样例状态机(供 demo / 自测使用)。"""

from fsm import FSM


def all_distinguishable():
    """情形 1:全部状态两两可区分。输出即状态名,'a' 一步即可区分任意对。"""
    states = ["s0", "s1", "s2", "s3"]
    inputs = ["a"]
    trans, out = {}, {}
    for i, s in enumerate(states):
        trans[(s, "a")] = states[(i + 1) % len(states)]
        out[(s, "a")] = "o" + s
    return FSM(states, inputs, "s0", trans, out)


def with_equivalence():
    """情形 2:存在等价类。A≡D, B≡C(一台 2 状态翻转机的两份副本)。"""
    states = ["A", "B", "C", "D"]
    inputs = ["0", "1"]
    trans = {
        ("A", "0"): "B", ("A", "1"): "D",
        ("D", "0"): "C", ("D", "1"): "A",
        ("B", "0"): "A", ("B", "1"): "C",
        ("C", "0"): "D", ("C", "1"): "B",
    }
    out = {
        ("A", "0"): 0, ("A", "1"): 1,
        ("D", "0"): 0, ("D", "1"): 1,
        ("B", "0"): 0, ("B", "1"): 0,
        ("C", "0"): 0, ("C", "1"): 0,
    }
    return FSM(states, inputs, "A", trans, out)


def with_unreachable():
    """情形 3:含不可达状态。G0/G1 从 M0 永不可达,只能单独报告。"""
    states = ["M0", "M1", "G0", "G1"]
    inputs = ["a"]
    trans = {
        ("M0", "a"): "M1",
        ("M1", "a"): "M0",
        ("G0", "a"): "G1",
        ("G1", "a"): "G0",
    }
    out = {
        ("M0", "a"): 0, ("M1", "a"): 1,
        ("G0", "a"): 0, ("G1", "a"): 1,
    }
    return FSM(states, inputs, "M0", trans, out)


def with_missing():
    """情形 4:转移缺失。P/b、R/a 缺失,补全语义下仍做等价判定。"""
    states = ["P", "Q", "R"]
    inputs = ["a", "b"]
    trans = {
        ("P", "a"): "Q",
        ("Q", "a"): "P", ("Q", "b"): "R",
        ("P", "b"): "R", ("R", "b"): "R",
    }
    out = {
        ("P", "a"): 0,
        ("Q", "a"): 0, ("Q", "b"): 1,
        ("P", "b"): 1, ("R", "b"): 1,
    }
    # 制造缺失:(R, a) 既无转移也无输出;(P? 不,P/b 已定义)
    trans.pop(("P", "b"))
    out.pop(("P", "b"))
    return FSM(states, inputs, "P", trans, out)
