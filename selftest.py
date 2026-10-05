"""
selftest.py — 自测 + 对拍 + 快照一致性验证
运行: python3 selftest.py
产物: out/diff_check.txt (对拍数据), out/snapshot_data.txt (刷新期间读到的数据)
"""

import json
import os
import random
import threading

from mvlib import BaseTable, MaterializedView

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
os.makedirs(OUT, exist_ok=True)


def make_view(base, aggs=None, fallback_ratio=0.5):
    return MaterializedView(
        base, 'v_sales',
        filter_fn=lambda r: r['amount'] >= 0,          # 过滤条件
        group_keys=['cat'],
        aggs=aggs or [('count', None), ('sum', 'amount'), ('avg', 'amount')],
        fallback_ratio=fallback_ratio)


def seed(base, n, rng):
    base.apply([('insert', {'pk': i, 'cat': 'c%02d' % (i % 10),
                            'amount': float(rng.randrange(0, 1000))})
                for i in range(n)])


def diff_rows(a, b):
    """逐行对拍，float 容差 1e-9。返回不一致描述列表。"""
    bad = []
    if len(a) != len(b):
        bad.append('行数不同: incremental=%d full=%d' % (len(a), len(b)))
    for i, (ra, rb) in enumerate(zip(a, b)):
        if ra.keys() != rb.keys():
            bad.append('第%d行列不同: %s vs %s' % (i, ra.keys(), rb.keys()))
            continue
        for k in ra:
            va, vb = ra[k], rb[k]
            if isinstance(va, float) or isinstance(vb, float):
                if abs(va - vb) > 1e-9:
                    bad.append('第%d行 %s: %r != %r' % (i, k, va, vb))
            elif va != vb:
                bad.append('第%d行 %s: %r != %r' % (i, k, va, vb))
    return bad


def run_case(name, base, view, changes, expect_mode=None):
    base.apply(changes)
    info = view.refresh()
    inc = view.rows()
    full = view.full_recompute_rows()
    bad = diff_rows(inc, full)
    status = 'PASS' if not bad else 'FAIL'
    if expect_mode and info['mode'] != expect_mode:
        status = 'FAIL(期望 %s 实际 %s)' % (expect_mode, info['mode'])
    rec = {
        'case': name, 'status': status,
        'mode': info['mode'], 'reason': info['reason'],
        'changes': info['changes'], 'rows_read': info['rows_read'],
        'view_rows': len(inc), 'mismatches': len(bad),
        'mismatch_detail': bad[:5],
        'sample_incremental': inc[:3],
        'sample_full': full[:3],
    }
    print('[%-4s] %-28s mode=%-11s changes=%-5d rows=%-4d mismatches=%d %s'
          % (status, name, info['mode'], info['changes'],
             len(inc), len(bad),
             ('(' + info['reason'] + ')') if info['reason'] else ''))
    return rec


def main():
    rng = random.Random(42)
    records = []

    # ---- 情形1: 只有新增 ----
    base = BaseTable(); seed(base, 1000, rng)
    view = make_view(base); view.refresh()  # 初始全量
    chg = [('insert', {'pk': 1000 + i, 'cat': 'c%02d' % (i % 10),
                       'amount': float(rng.randrange(0, 1000))})
           for i in range(200)]
    records.append(run_case('only-insert', base, view, chg, 'incremental'))

    # ---- 情形2: 只有删除 ----
    chg = [('delete', rng.randrange(0, 1000)) for _ in range(150)]
    records.append(run_case('only-delete', base, view, chg, 'incremental'))

    # ---- 情形3: 混合（新增+修改+删除，含跨组移动）----
    live = sorted(base.peek().keys())
    chg = []
    for i in range(60):   # update：含改组键 cat 的跨组移动
        pk = live[rng.randrange(len(live))]
        chg.append(('update', pk, {'pk': pk, 'cat': 'c%02d' % rng.randrange(10),
                                   'amount': float(rng.randrange(0, 1000))}))
    for i in range(40):   # insert
        pk = 9000 + i
        chg.append(('insert', {'pk': pk, 'cat': 'c%02d' % rng.randrange(10),
                               'amount': float(rng.randrange(0, 1000))}))
    for i in range(40):   # delete
        chg.append(('delete', live[rng.randrange(len(live))]))
    rng.shuffle(chg)
    records.append(run_case('mixed-ins-upd-del', base, view, chg, 'incremental'))

    # ---- 情形4: min/max 极值被删 -> 必须识别并退回全量 ----
    base2 = BaseTable(); seed(base2, 500, rng)
    view2 = make_view(base2, aggs=[('count', None), ('min', 'amount'),
                                   ('max', 'amount'), ('sum', 'amount')])
    view2.refresh()
    # 找到某组当前 max 所在的行并删掉它
    rows = base2.peek()
    best = {}
    for pk, r in rows.items():
        if r['cat'] not in best or r['amount'] > rows[best[r['cat']]]['amount']:
            best[r['cat']] = pk
    victim = best['c03']
    chg = [('delete', victim)]
    rec = run_case('minmax-delete->fallback', base2, view2, chg, 'full')
    assert rec['reason'], '应给出退回原因'
    records.append(rec)

    # ---- 情形5: 变化量过大 -> 退回全量 ----
    base3 = BaseTable(); seed(base3, 1000, rng)
    view3 = make_view(base3, fallback_ratio=0.5)
    view3.refresh()
    chg = [('update', i, {'pk': i, 'cat': 'c%02d' % rng.randrange(10),
                          'amount': float(rng.randrange(0, 1000))})
           for i in range(800)]  # 80% > 50% 阈值
    records.append(run_case('huge-churn->fallback', base3, view3, chg, 'full'))

    # ---- 情形6: 快照一致性 —— 刷新进行中到达的变更不得进入本次结果 ----
    base4 = BaseTable(); seed(base4, 2000, rng)
    view4 = make_view(base4); view4.refresh()
    chg1 = [('update', i, {'pk': i, 'cat': 'c05',
                           'amount': 1.0}) for i in range(100)]
    base4.apply(chg1)

    snap_holder = {}
    entered = threading.Event()
    proceed = threading.Event()

    def hook(rows, log):
        # 刷新已拿到快照；此刻并发写入一批新变更
        snap_holder['rows'] = rows
        snap_holder['log_size'] = len(log)
        entered.set()
        proceed.wait(5)  # 等并发写入完成后再继续计算

    late = [('insert', {'pk': 50000 + i, 'cat': 'c07', 'amount': 777.0})
            for i in range(50)]

    def writer():
        entered.wait(5)
        base4.apply(late)   # 刷新进行期间写入
        proceed.set()

    t = threading.Thread(target=writer)
    t.start()
    info = view4.refresh(after_snapshot_hook=hook)
    t.join()

    snap_rows = snap_holder['rows']
    snap_has_late = any(pk >= 50000 for pk in snap_rows)
    view_rows = view4.rows()
    # 本次刷新结果必须等于“快照时刻”的全量（不含 late 变更）
    groups, _ = view4._full_recompute(snap_rows)
    saved = view4._groups
    view4._groups = groups
    expect = view4.rows()
    view4._groups = saved
    bad = diff_rows(view_rows, expect)
    ok6 = (not snap_has_late) and not bad and info['mode'] == 'incremental'
    print('[%-4s] %-28s 快照含迟到变更=%s 对拍mismatches=%d'
          % ('PASS' if ok6 else 'FAIL', 'snapshot-consistency',
             snap_has_late, len(bad)))
    records.append({'case': 'snapshot-consistency',
                    'status': 'PASS' if ok6 else 'FAIL',
                    'mode': info['mode'], 'changes': info['changes'],
                    'rows_read': info['rows_read'],
                    'snapshot_contains_late_rows': snap_has_late,
                    'mismatches': len(bad)})

    # 第二次刷新应收编迟到的 50 行，并与全量一致
    info2 = view4.refresh()
    bad2 = diff_rows(view4.rows(), view4.full_recompute_rows())
    ok6b = info2['changes'] == 50 and not bad2
    print('[%-4s] %-28s 第二次刷新收编 %d 条迟到变更, mismatches=%d'
          % ('PASS' if ok6b else 'FAIL', 'late-changes-next-refresh',
             info2['changes'], len(bad2)))
    records.append({'case': 'late-changes-next-refresh',
                    'status': 'PASS' if ok6b else 'FAIL',
                    'mode': info2['mode'], 'changes': info2['changes'],
                    'mismatches': len(bad2)})

    # ---- 落盘：对拍数据 ----
    with open(os.path.join(OUT, 'diff_check.txt'), 'w') as f:
        f.write('增量刷新 vs 全量重算 逐行对拍结果\n')
        f.write('=' * 60 + '\n')
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')

    # ---- 落盘：刷新期间读到的快照数据 ----
    with open(os.path.join(OUT, 'snapshot_data.txt'), 'w') as f:
        f.write('情形6 刷新期间实际读到的快照（一致快照证据）\n')
        f.write('=' * 60 + '\n')
        f.write('快照行数: %d (刷新开始后写入的 50 行 pk>=50000 不在其中)\n'
                % len(snap_rows))
        f.write('快照内 changelog 条数: %d\n' % snap_holder['log_size'])
        f.write('快照是否含迟到行 pk>=50000: %s\n' % snap_has_late)
        f.write('快照前 5 行:\n')
        for pk in sorted(snap_rows)[:5]:
            f.write('  %s\n' % json.dumps(snap_rows[pk], ensure_ascii=False))
        f.write('刷新进行期间并发写入的变更(前 3 条, 应出现在下一次刷新):\n')
        for ch in late[:3]:
            f.write('  %s\n' % json.dumps(ch, ensure_ascii=False))

    n_fail = sum(1 for r in records if r['status'] != 'PASS')
    print('\n%d/%d 通过, 对拍数据 -> out/diff_check.txt, 快照数据 -> out/snapshot_data.txt'
          % (len(records) - n_fail, len(records)))
    raise SystemExit(1 if n_fail else 0)


if __name__ == '__main__':
    main()
