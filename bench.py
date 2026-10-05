"""
bench.py — 100 万行基表、10 万次变化下 增量 vs 全量 耗时对比
运行: python3 bench.py
产物: out/benchmark.txt
"""

import os
import random
import time

from mvlib import BaseTable, MaterializedView

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
N_ROWS = 1_000_000
N_CHANGES = 100_000

AGGS = [('count', None), ('sum', 'amount'), ('avg', 'amount')]


def make_view(base, fallback_ratio):
    return MaterializedView(
        base, 'v',
        filter_fn=lambda r: r['amount'] >= 0,
        group_keys=['cat'], aggs=AGGS,
        fallback_ratio=fallback_ratio)


def gen_changes(rng):
    """40k update + 30k insert + 30k delete，乱序。"""
    chg = []
    for _ in range(40_000):
        pk = rng.randrange(N_ROWS)
        chg.append(('update', pk, {'pk': pk,
                                   'cat': 'c%02d' % rng.randrange(50),
                                   'amount': float(rng.randrange(0, 1000))}))
    for i in range(30_000):
        pk = N_ROWS + i
        chg.append(('insert', {'pk': pk,
                               'cat': 'c%02d' % rng.randrange(50),
                               'amount': float(rng.randrange(0, 1000))}))
    dels = rng.sample(range(N_ROWS), 30_000)
    chg.extend(('delete', pk) for pk in dels)
    rng.shuffle(chg)
    return chg


def compare(a, b):
    bad = 0
    for ra, rb in zip(a, b):
        for k in ra:
            va, vb = ra[k], rb[k]
            if isinstance(va, float):
                tol = 1e-9 * max(1.0, abs(va), abs(vb))
                if abs(va - vb) > tol:
                    bad += 1
            elif va != vb:
                bad += 1
    return bad


def main():
    os.makedirs(OUT, exist_ok=True)
    rng = random.Random(7)

    print('生成 %d 行基表 ...' % N_ROWS, flush=True)
    t = time.perf_counter()
    base = BaseTable()
    batch = []
    for i in range(N_ROWS):
        batch.append(('insert', {'pk': i, 'cat': 'c%02d' % (i % 50),
                                 'amount': float(rng.randrange(0, 1000))}))
        if len(batch) == 100_000:
            base.apply(batch)
            batch = []
    if batch:
        base.apply(batch)
    print('  生成耗时 %.2fs' % (time.perf_counter() - t), flush=True)

    # 两个相同起点的视图：一个走增量，一个 fallback_ratio=0 永远走全量
    base2 = BaseTable()
    base2._rows = dict(base.peek())  # 行 dict 不可变，安全共享
    v_inc = make_view(base, fallback_ratio=0.5)
    v_full = make_view(base2, fallback_ratio=0.0)

    t = time.perf_counter(); v_inc.refresh()
    t_init_inc = time.perf_counter() - t
    t = time.perf_counter(); v_full.refresh()
    t_init_full = time.perf_counter() - t
    print('初始全量构建: %.2fs / %.2fs' % (t_init_inc, t_init_full), flush=True)

    chg = gen_changes(random.Random(99))
    base.apply(chg)
    base2.apply(chg)

    t = time.perf_counter()
    info_inc = v_inc.refresh()
    t_inc = time.perf_counter() - t

    t = time.perf_counter()
    info_full = v_full.refresh()
    t_full = time.perf_counter() - t

    bad = compare(v_inc.rows(), v_full.rows())

    report = []
    report.append('物化视图刷新耗时对比 (100 万行 / 10 万次变化)')
    report.append('=' * 56)
    report.append('视图: filter amount>=0, group by cat(50 组), '
                  'count/sum/avg(amount)')
    report.append('变更构成: 40,000 update + 30,000 insert + 30,000 delete')
    report.append('硬件/解释器: Python %s' %
                  '%d.%d.%d' % __import__('sys').version_info[:3])
    report.append('-' * 56)
    report.append('初始全量构建          : %.3f s' % t_init_inc)
    report.append('增量刷新 (处理 %d 变更) : %.3f s   mode=%s'
                  % (info_inc['changes'], t_inc, info_inc['mode']))
    report.append('全量重算 (扫描 %d 行)  : %.3f s'
                  % (info_full['rows_read'], t_full))
    report.append('加速比                : %.1fx' % (t_full / t_inc))
    report.append('增量 vs 全量 逐行不一致: %d 个单元格' % bad)
    report.append('-' * 56)
    report.append('对照: 变更量超阈值时 refresh 自动走全量 (见 selftest 情形5)')
    txt = '\n'.join(report)
    print('\n' + txt)
    with open(os.path.join(OUT, 'benchmark.txt'), 'w') as f:
        f.write(txt + '\n')
    raise SystemExit(1 if bad else 0)


if __name__ == '__main__':
    main()
