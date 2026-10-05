# -*- coding: utf-8 -*-
"""打印四类场景的完整判定结果:等价类、论证数据、区分序列。

运行:python3 demo.py
"""

from fsm import analyze
import scenarios


def print_report(title, machine):
    print("=" * 72)
    print(title)
    print("=" * 72)
    r = analyze(machine)
    print("状态数=%d 输入数=%d 转移条数=%d" %
          (r["state_count"], r["input_count"], r["transition_count"]))
    print("可达状态数=%d 不可达状态=%s" %
          (r["reachable_count"], r["unreachable"]))
    print("缺失转移=%s" %
          (r["missing_transitions"] if r["missing_transitions"] else "无"))
    print("划分稳定(不存在区分序列的共归纳论证)= %s" % r["stable"])
    print("等价类数=%d" % r["class_count"])
    for c in r["classes"]:
        print("  类 %d (大小 %d): %s" %
              (c["class_id"], c["size"], c["states"]))
        ev = c.get("evidence_example")
        if ev is not None:
            print("    等价依据示例 %r ≡ %r(对每个输入逐项核对):" %
                  ev["pair"])
            for row in ev["rows"]:
                print("      输入 %r: 输出 %r vs %r; 后继类 %d vs %d" %
                      (row["input"], row["output_p"], row["output_q"],
                       row["succ_class_p"], row["succ_class_q"]))
            print("    一致=%s(块内一致且后继仍落同块,归纳得任意长序列均无法区分)"
                  % ev["consistent"])
    if r["witness_samples"]:
        print("跨类区分序列示例:")
        for w in r["witness_samples"][:5]:
            print("  对 %r: 输入序列 %r -> 输出 %r vs %r" %
                  (w["pair"], w["sequence"],
                   w["outputs_p"], w["outputs_q"]))
    print()


def main():
    print_report("情形 1:全部两两可区分", scenarios.all_distinguishable())
    print_report("情形 2:存在等价类", scenarios.with_equivalence())
    print_report("情形 3:含不可达状态", scenarios.with_unreachable())
    print_report("情形 4:转移缺失", scenarios.with_missing())


if __name__ == "__main__":
    main()
