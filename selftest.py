#!/usr/bin/env python3
"""tapemark 自测 + 前进/后退序列演示 + 1MB/16MB 性能与内存基准. 仅用标准库."""

import io
import os
import resource
import struct
import tempfile
import time
import tracemalloc

import tapemark as tm

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def reader_for(data: bytes, chunk_size=tm.DEFAULT_CHUNK) -> tm.Reader:
    f = tempfile.TemporaryFile()
    f.write(data)
    f.flush()
    return tm.Reader(f, chunk_size=chunk_size)


def scan_all(data, chunk_size=tm.DEFAULT_CHUNK):
    r = reader_for(data, chunk_size)
    recs = []
    try:
        for rec in r.scan():
            recs.append(rec)
    finally:
        r._f.close()
    return recs, r.position, r.state


# --------------------------------------------------------------- 1. 空卷
def test_empty_volume():
    recs, pos, state = scan_all(tm.build_volume([]))
    check("空卷: 仅一条 VOLUMEMARK",
          len(recs) == 1 and recs[0].type == tm.TYPE_VOLUMEMARK
          and pos == 20 and state == tm.STATE_END_OF_VOLUME)


# --------------------------------------------------------------- 2. 单块
def test_single_block():
    payload = b"hello-tape"
    recs, pos, state = scan_all(tm.build_volume([payload]))
    check("单块: DATA + VOLUMEMARK",
          len(recs) == 2
          and recs[0].type == tm.TYPE_DATA and recs[0].payload_len == 10
          and recs[0].offset == 0 and recs[0].end == 24 + 10
          and recs[1].type == tm.TYPE_VOLUMEMARK
          and state == tm.STATE_END_OF_VOLUME)


# --------------------------------------------------------------- 3. 连续标记
def test_consecutive_marks():
    recs, pos, state = scan_all(
        tm.build_volume(["FILEMARK", "FILEMARK", "FILEMARK"]))
    types = [r.type for r in recs]
    check("连续标记: 3 个 FILEMARK 后 VOLUMEMARK",
          types == [tm.TYPE_FILEMARK] * 3 + [tm.TYPE_VOLUMEMARK] and len(recs) == 4)
    check("连续标记: 标记间距恒为 20 字节",
          all(recs[i + 1].offset - recs[i].offset == 20
              for i in range(len(recs) - 1)))


# --------------------------------------------------------------- 4. 标记字节序列嵌在数据中间
def test_mark_inside_data():
    fake_filemark = tm._encode_header(tm.TYPE_FILEMARK, 0) + struct.pack(">I", 20)
    fake_volumemark = tm._encode_header(tm.TYPE_VOLUMEMARK, 0) + struct.pack(">I", 20)
    payload = b"xxxx" + fake_filemark + fake_volumemark + tm.MAGIC + b"yyyy"
    recs, pos, state = scan_all(tm.build_volume([payload]))
    check("标记嵌在数据中: 不被误判, 仍是 1 个 DATA + 1 个 VOLUMEMARK",
          len(recs) == 2 and recs[0].type == tm.TYPE_DATA
          and recs[0].payload_len == len(payload)
          and recs[1].type == tm.TYPE_VOLUMEMARK,
          f"got {[(r.type_name, r.payload_len) for r in recs]}")

    # 损坏该数据块一个字节 -> 必须被数据 CRC 确定性发现, 而不是被错当成标记
    img = bytearray(tm.build_volume([payload]))
    img[16] ^= 0xFF
    r = reader_for(bytes(img))
    try:
        r.step_forward()
        ok = False
    except tm.FormatError as e:
        ok = (e.offset == 0 and "payload crc32 mismatch" in e.reason)
    r._f.close()
    check("标记嵌在数据中: 数据被破坏时 CRC 报错而非误判标记", ok)


# ---------------------------------- 5. 标记(记录头)跨物理块/读取块边界
def test_mark_spans_boundary():
    PHYS = 512
    payload_len = PHYS - 24 - 2            # 让第一条 DATA 记录在 510 处结束
    buf = io.BytesIO()
    tm.write_data_block(buf, b"z" * payload_len)   # 记录占 [0, 510)
    tm.write_filemark(buf)                          # 头在 [510,526), 跨过 512
    tm.write_volumemark(buf)
    img = buf.getvalue()
    recs, pos, state = scan_all(img, chunk_size=7)  # 极小流式块强制跨块读取
    check("标记跨边界: FILEMARK 头跨 512 物理边界仍正确",
          len(recs) == 3
          and recs[0].end == PHYS - 2
          and recs[1].type == tm.TYPE_FILEMARK and recs[1].offset == PHYS - 2
          and recs[2].type == tm.TYPE_VOLUMEMARK and state == tm.STATE_END_OF_VOLUME)

    # 数据中魔数恰好跨流式读取块边界, 解析不受影响
    payload = b"abcdefg" + tm.MAGIC + b"hijklmn"
    recs2, _, _ = scan_all(tm.build_volume([payload]), chunk_size=8)
    check("标记跨边界: 数据内魔数跨 chunk 边界不误判",
          len(recs2) == 2 and recs2[0].type == tm.TYPE_DATA
          and recs2[0].payload_len == len(payload))


# --------------------------------------------------------------- 6. 卷尾截断
def test_truncation():
    # 重新明确构造: DATA(100)=[0,124) FILEMARK=[124,144) VOLUMEMARK=[144,164)
    img = io.BytesIO()
    tm.write_data_block(img, b"d" * 100)
    tm.write_filemark(img)
    tm.write_volumemark(img)
    img = img.getvalue()

    def trunc_at(cut):
        r = reader_for(img[:cut])
        try:
            while True:
                r.step_forward()
        except tm.TruncatedVolumeError as e:
            return (e.records_read, e.missing_bytes, e.last_complete_end, e.phase)
        finally:
            r._f.close()

    check("截断-头中(128): 1 完整块, 缺 12 字节, 边界 124",
          trunc_at(128) == (1, 12, 124, "header"))
    check("截断-数据中(60): 0 完整块, 缺 56 字节, 边界 0",
          trunc_at(60) == (0, 56, 0, "payload"))
    check("截断-尾中(122): 0 完整块, 缺 2 字节, 边界 0",
          trunc_at(122) == (0, 2, 0, "trailer"))

    # 没有 VOLUMEMARK 干净收尾 => 截断而非静默结束
    check("截断-缺卷标记: 在数据块边界后遇到裸 EOF 报错",
          trunc_at(124) == (1, 16, 124, "header"))


# --------------------------------------------------------------- 7. 前进/后退 + 序列
def test_step_sequence():
    img = tm.build_volume([b"aaa", "FILEMARK", b"bb"])
    r = reader_for(img)
    seq = [(r.position, r.state, None)]
    ops = ["fwd", "fwd", "fwd", "fwd", "back", "back", "fwd", "back"]
    seen = []
    for op in ops:
        rec = r.step_forward() if op == "fwd" else r.step_back()
        seq.append((r.position, r.state, rec.type_name))
        seen.append(rec)
    # DATA(3)=27B, DATA(2)=26B, MARK=20B:
    # 位置序列 0 ->27 ->47 ->73 ->93 ->73 ->47 ->73 ->47
    expect_pos = [0, 27, 47, 73, 93, 73, 47, 73, 47]
    check("前进/后退: 位置序列严格确定",
          [s[0] for s in seq] == expect_pos,
          f"got {[s[0] for s in seq]}")
    check("前进/后退: 越过 VOLUMEMARK 后状态为 END_OF_VOLUME",
          seq[4][1] == tm.STATE_END_OF_VOLUME and seq[4][2] == "VOLUMEMARK")
    check("前进/后退: 后退后可再次前进, 记录一致",
          seen[6].type == seen[2].type and seen[6].offset == seen[2].offset)

    r2 = reader_for(img)
    try:
        r2.step_back()
        ok0 = False
    except tm.BeginningOfVolume:
        ok0 = True
    for _ in range(4):
        r2.step_forward()
    try:
        r2.step_forward()
        ok1 = False
    except tm.EndOfVolume:
        ok1 = True
    r2._f.close()
    check("前进/后退: 卷首后退、卷末前进均报错", ok0 and ok1)
    return seq


# --------------------------------------------------------------- 8. 确定性
def test_determinism():
    img = tm.build_volume([os.urandom(2000), "FILEMARK", os.urandom(333)])
    a, pos_a, _ = scan_all(img, chunk_size=4096)
    b, pos_b, _ = scan_all(img, chunk_size=65536)
    same = ([tuple(r) for r in a] == [tuple(r) for r in b] and pos_a == pos_b)
    check("确定性: 同镜像不同 chunk 解析结果完全一致", same)

    # 交错前进/后退多次后, 再从头解析, 结果仍完全一致(不依赖缓存状态)
    r = reader_for(img)
    for _ in range(3):
        r.step_forward()
    r.step_back(); r.step_back(); r.step_forward()
    c, pos_c, _ = scan_all(img)
    r._f.close()
    check("确定性: 交错步进后重解析与首次一致",
          [tuple(x) for x in c] == [tuple(x) for x in a] and pos_c == pos_a)


# --------------------------------------------------------------- 9. 序列演示(交付数据)
def demo_walk(seq):
    print("\n=== 前进/后退位置与状态序列(2 数据块 + 1 FILEMARK 卷) ===")
    lines = [f"{'step':>4} {'op':<5} {'position':>8} {'state':<13} record"]
    ops = ["-", "fwd", "fwd", "fwd", "fwd", "back", "back", "fwd", "back"]
    for i, ((pos, state, rec), op) in enumerate(zip(seq, ops)):
        lines.append(f"{i:>4} {op:<5} {pos:>8} {state:<13} {rec or '-'}")
    print("\n".join(lines))
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "parse_sequence.txt"), "w") as out:
        out.write("volume layout: DATA(3B)@0 FILEMARK@27 DATA(2B)@47 "
                  "VOLUMEMARK@73; image size 93\n")
        out.write("ops: fwd fwd fwd fwd back back fwd back\n")
        out.write("\n".join(lines) + "\n")


# --------------------------------------------------------------- 10. 基准
def write_volume_file(path, target_size):
    """流式写卷: 4KiB 数据块, 每 64 块一个 FILEMARK, 末尾 VOLUMEMARK."""
    block = bytes((i * 7 + 3) & 0xFF for i in range(4096))
    count = 0
    with open(path, "wb") as f:
        written = 0
        while written < target_size:
            tm.write_data_block(f, block)
            written += 4096 + 24
            count += 1
            if count % 64 == 0:
                tm.write_filemark(f)
                written += 20
        tm.write_volumemark(f)
    return os.path.getsize(path)


def bench(path, real_size, label):
    # 只测量解析阶段的 Python 堆峰值与耗时, 镜像生成不计入
    tracemalloc.start()
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        r = tm.Reader(f)
        n_data = n_fm = 0
        for rec in r.scan():
            if rec.type == tm.TYPE_DATA:
                n_data += 1
            elif rec.type == tm.TYPE_FILEMARK:
                n_fm += 1
    elapsed = time.perf_counter() - t0
    _, py_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB(Linux)
    print(f"{label:>6}: {real_size/1e6:5.1f}MB 镜像, {n_data} 数据块, {n_fm} 文件标记, "
          f"耗时 {elapsed*1000:7.1f} ms, Python堆峰值 {py_peak/1024:6.1f} KiB, "
          f"进程 RSS {rss/1024:6.1f} MiB, 吞吐 {real_size/elapsed/1e6:6.1f} MB/s")
    return py_peak


def test_benchmark():
    print("\n=== 1MB / 16MB 解析耗时与内存峰值(仅标准库, 流式读取) ===")
    with tempfile.TemporaryDirectory() as d:
        results = []
        for size, label in ((1 << 20, "1MB"), (16 << 20, "16MB")):
            path = os.path.join(d, f"vol_{label}.bin")
            real = write_volume_file(path, size)
            results.append((label, real, bench(path, real, label)))
        peak1, peak16 = results[0][2], results[1][2]
        # 内存不随卷长增长: 允许 16 倍体积下波动 25%(解释器分配噪声), 不允许随长度线性增长
        ratio = peak16 / peak1
        check("内存: 16MB 卷的解析堆峰值不随卷长增长(比值<1.25)",
              ratio < 1.25, f"peak1={peak1}, peak16={peak16}, ratio={ratio:.2f}")


def main():
    print("=== tapemark 边界与正确性自测 ===")
    test_empty_volume()
    test_single_block()
    test_consecutive_marks()
    test_mark_inside_data()
    test_mark_spans_boundary()
    test_truncation()
    seq = test_step_sequence()
    test_determinism()
    demo_walk(seq)
    test_benchmark()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 个用例失败: {FAILURES}")
        raise SystemExit(1)
    print("全部用例通过")


if __name__ == "__main__":
    main()
