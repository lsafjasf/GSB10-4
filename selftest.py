#!/usr/bin/env python3
"""appendlog 自测：边界用例 / 崩溃恢复 / 对拍 / 篡改检出 / 1GB 性能。

运行: python3 selftest.py
"""
import hashlib
import os
import random
import shutil
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import appendlog as al

RESULTS = []
TMP = tempfile.mkdtemp(prefix="appendlog_test_")


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  " + extra) if extra else ""))


def section(title):
    print("\n== %s ==" % title)


def p(*a):
    print(" ", *a)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_records(path, payloads):
    with al.AppendWriter(path) as w:
        for pl in payloads:
            w.append(pl, sync=False)
        w.sync()


def fmt_record_evidence(rec):
    if rec is None:
        return "(无完整记录)"
    return ("offset=%d payload_len=%d total_len=%d crc32=0x%08x (已重算比对一致)"
            % (rec.offset, rec.payload_len, rec.total_len, rec.crc32))


# ---------------------------------------------------------------- 1. 边界用例

def test_edge_cases():
    section("1. 边界用例：空文件 / 单条记录 / 尾部半条 / 中部损坏")

    # 空文件
    path = os.path.join(TMP, "empty.log")
    open(path, "wb").close()
    res = al.scan(path)
    tail = al.locate_tail(path, 1024)
    rep = al.recover(path)
    check("空文件 scan 干净", res.clean and len(res.records) == 0 and res.boundary == 0)
    check("空文件 locate_tail", tail.found and tail.tail_offset == 0 and tail.bytes_read == 0)
    check("空文件 recover 为 no-op", rep.action == "clean" and rep.size_after == 0)

    # 单条记录
    path = os.path.join(TMP, "single.log")
    write_records(path, [b"hello world"])
    res = al.scan(path)
    check("单条记录 scan", res.clean and len(res.records) == 1,
          fmt_record_evidence(res.last_record))
    got = list(al.iter_records(path))
    check("单条记录读取", len(got) == 1 and got[0][1] == b"hello world")

    # 尾部半条（模拟崩溃：只写入了下一条记录的前 17 字节）
    path = os.path.join(TMP, "half_tail.log")
    payloads = [b"rec-%d" % i for i in range(3)]
    write_records(path, payloads)
    good_size = os.path.getsize(path)
    with open(path, "ab") as f:
        f.write(al.encode_record(b"rec-3-truncated")[:17])
    got = list(al.iter_records(path))
    res = al.scan(path)
    check("读取方安全忽略尾部半条", [pl for _, pl in got] == payloads)
    check("边界准确报出", res.boundary == good_size and not res.clean,
          "boundary=%d defect=%s" % (res.boundary, res.defects[0].kind))
    tail = al.locate_tail(path, 1024)
    check("locate_tail 报出半条字节数",
          tail.tail_offset == good_size and tail.trailing_bytes == 17,
          "tail_offset=%d trailing=%d bytes_read=%d"
          % (tail.tail_offset, tail.trailing_bytes, tail.bytes_read))

    # 中部损坏：5 条记录，翻转第 3 条载荷的 1 字节
    path = os.path.join(TMP, "mid_corrupt.log")
    payloads = [b"payload-%d-aaaaaaaa" % i for i in range(5)]
    write_records(path, payloads)
    res0 = al.scan(path)
    victim = res0.records[2]
    with open(path, "r+b") as f:
        f.seek(victim.offset + al.HEADER_SIZE + 3)
        b = f.read(1)
        f.seek(victim.offset + al.HEADER_SIZE + 3)
        f.write(bytes([b[0] ^ 0x01]))
    res = al.scan(path)  # resync=True：坏记录之后继续校验
    kinds = [d.kind for d in res.defects]
    check("中部坏记录被报告", len(res.defects) == 1
          and res.defects[0].offset == victim.offset
          and kinds == ["payload_crc_mismatch"],
          "defect=%s" % (res.defects[0],))
    check("坏记录之后的记录仍被校验", len(res.records) == 4
          and [r.offset for r in res.records]
          == [res0.records[i].offset for i in (0, 1, 3, 4)])
    rep = al.recover(path)
    check("中部损坏恢复截断到最后完好边界",
          rep.action == "truncated" and rep.cut_offset == res0.records[1].end
          and os.path.getsize(path) == res0.records[1].end,
          "cut=%d 丢弃记录=%d条" % (rep.cut_offset, len(rep.discarded_records)))


# ------------------------------------------------- 2. 三处崩溃点恢复 + 对拍

def test_crash_recovery():
    section("2. 崩溃点覆盖（记录头 / 正文中段 / 校验字段）+ 恢复后对拍")

    payloads = [("record-%04d-" % i).encode() * (1 + i % 7) for i in range(150)]
    ref = os.path.join(TMP, "ref.log")
    write_records(ref, payloads)            # 进程从未中断的参照文件
    ref_hash = sha256(ref)
    p("参照文件: %d 条记录, %d 字节, sha256=%s..."
      % (len(payloads), os.path.getsize(ref), ref_hash[:16]))

    full = al.encode_record(payloads[100])  # 崩溃时正在写第 101 条
    cuts = {
        "记录头(写5/12B)": 5,
        "正文中段(写一半载荷)": al.HEADER_SIZE + len(payloads[100]) // 2,
        "校验字段(写2/4B)": al.HEADER_SIZE + len(payloads[100]) + 2,
    }
    for label, cut in cuts.items():
        path = os.path.join(TMP, "crash_%d.log" % cut)
        write_records(path, payloads[:100])
        with open(path, "ab") as f:          # 模拟强杀：记录只写出一部分
            f.write(full[:cut])
        rep = al.recover(path)
        ev = fmt_record_evidence(rep.last_good_record)
        check("崩溃于%s -> 截断恢复" % label,
              rep.action == "truncated"
              and rep.cut_offset == rep.size_before - cut
              and rep.defects[0].offset == rep.cut_offset,
              "缺陷=%s 截断于=%d 末条完好记录: %s"
              % (rep.defects[0].kind, rep.cut_offset, ev))
        write_records(path, payloads[100:])  # 恢复后继续追加
        h = sha256(path)
        check("崩溃于%s -> 续写与未中断逐字节一致" % label,
              h == ref_hash and os.path.getsize(path) == os.path.getsize(ref),
              "sha256=%s..." % h[:16])


# ---------------------------------------------------------------- 3. 篡改检出

def test_tamper_detection():
    section("3. 校验检出能力：单字节翻转 / 随机损坏 / 误报统计")

    payload = bytes(range(256))[:250]
    rec = al.encode_record(payload)
    p("样本记录: 载荷 %dB, 总长 %dB" % (len(payload), len(rec)))

    # 3a. 穷举单字节翻转：每个字节位置 x 3 种翻转模式
    total = detected = 0
    kinds = {}
    for pos in range(len(rec)):
        for mask in (0x01, 0x80, 0xFF):
            mut = bytearray(rec)
            mut[pos] ^= mask
            parsed = al._parse_record_buf(bytes(mut), 0, al.DEFAULT_MAX_PAYLOAD)
            total += 1
            if parsed is None:
                detected += 1
            else:
                kinds[pos] = kinds.get(pos, 0) + 1
    check("单字节翻转全部检出", detected == total,
          "%d/%d = %.2f%% (理论: CRC32 必检出 <=32bit 突发错误, 单字节翻转属 <=8bit 突发)"
          % (detected, total, 100.0 * detected / total))

    # 3b. 随机多字节损坏的漏检率（经验值，理论漏检 ~2^-32/次）
    rng = random.Random(20261004)
    trials = 300_000
    missed = 0
    for _ in range(trials):
        mut = bytearray(rec)
        for _ in range(rng.randint(1, 8)):
            mut[rng.randrange(len(mut))] ^= rng.randint(1, 255)
        if al._parse_record_buf(bytes(mut), 0, al.DEFAULT_MAX_PAYLOAD) is not None:
            missed += 1
    check("随机损坏漏检为 0", missed == 0,
          "%d 次随机损坏, 漏检 %d (理论期望 %.5f 次)" % (trials, missed, trials / 2**32))

    # 3c. 随机垃圾不会被误收为合法记录
    trials = 100_000
    accepted = 0
    for _ in range(trials):
        blob = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200)))
        if al._parse_record_buf(blob, 0, al.DEFAULT_MAX_PAYLOAD) is not None:
            accepted += 1
    check("随机垃圾误收为 0", accepted == 0, "%d 次随机垃圾, 误收 %d" % (trials, accepted))

    # 3d. 干净数据零误报
    path = os.path.join(TMP, "clean_fp.log")
    rng2 = random.Random(7)
    write_records(path, [bytes(rng2.getrandbits(8) for _ in range(rng2.randint(0, 512)))
                         for _ in range(5000)])
    res = al.scan(path)
    rep = al.recover(path)
    check("干净文件零误报", res.clean and len(res.records) == 5000
          and rep.action == "clean",
          "5000 条记录, 缺陷 %d, recover=no-op" % len(res.defects))

    # 3e. 被篡改样例的检出结果（文件级，含偏移与校验依据）
    path = os.path.join(TMP, "tampered.log")
    write_records(path, [b"important-data-%d" % i for i in range(4)])
    res0 = al.scan(path)
    victim = res0.records[1]
    with open(path, "r+b") as f:
        f.seek(victim.offset + al.HEADER_SIZE)   # 翻转第 2 条载荷首字节
        b = f.read(1)
        f.seek(victim.offset + al.HEADER_SIZE)
        f.write(bytes([b[0] ^ 0x40]))
    res = al.scan(path)
    d = res.defects[0]
    check("篡改样例检出并定位", len(res.defects) == 1 and d.offset == victim.offset,
          "offset=%d kind=%s %s" % (d.offset, d.kind, d.detail))


# ---------------------------------------------------------------- 4. 1GB 性能

def test_1gb_performance():
    section("4. 1GB 文件：恢复耗时与读取字节数（尾部定位不做全量扫描）")

    gib = 1 << 30
    payload_len = 65536
    rec_size = payload_len + al.OVERHEAD          # 65552
    count = gib // rec_size
    path = os.path.join(TMP, "big.log")

    t0 = time.perf_counter()
    with al.AppendWriter(path) as w:
        for i in range(count):
            w.append((b"%016d" % i) * (payload_len // 16), sync=False)
        w.sync()
    t_build = time.perf_counter() - t0
    size = os.path.getsize(path)

    # 模拟崩溃：下一条记录只写出 40000 字节（正文中段）
    partial = 40000
    with open(path, "ab") as f:
        f.write(al.encode_record(b"x" * payload_len)[:partial])
    crashed_size = os.path.getsize(path)
    p("文件: %d 条记录, %.3f GiB (构建耗时 %.2fs), 崩溃残留 %d 字节"
      % (count, size / gib, t_build, partial))

    # 快速尾部定位：只读一个窗口
    t0 = time.perf_counter()
    tail = al.locate_tail(path, max_record_size=rec_size)
    t_tail = time.perf_counter() - t0
    check("locate_tail 定位最后完整记录", tail.found and tail.tail_offset == size
          and tail.trailing_bytes == partial,
          "tail_offset=%d trailing=%d" % (tail.tail_offset, tail.trailing_bytes))
    p("locate_tail: 耗时 %.4fs, 读取 %d 字节 (文件 %.3f GiB 的 %.6f%%)"
      % (t_tail, tail.bytes_read, crashed_size / gib,
         100.0 * tail.bytes_read / crashed_size))
    check("尾部定位未全量扫描", tail.bytes_read <= 2 * rec_size,
          "bytes_read=%d <= 2*max_record_size=%d" % (tail.bytes_read, 2 * rec_size))

    # 对照：全量校验（报告中间坏记录所需），读取整个文件
    t0 = time.perf_counter()
    res = al.scan(path)
    t_scan = time.perf_counter() - t0
    p("scan(全量校验): 耗时 %.2fs (%.0f MB/s), 读取 %d 字节, 记录 %d 条, 缺陷 %d 个"
      % (t_scan, crashed_size / t_scan / 1e6, crashed_size,
         len(res.records), len(res.defects)))
    check("两种定位边界一致", res.boundary == tail.tail_offset,
          "boundary=%d" % res.boundary)

    # 恢复 + 续写对拍（与未中断写入比较末尾记录）
    t0 = time.perf_counter()
    rep = al.recover(path)
    t_rec = time.perf_counter() - t0
    check("1GB 恢复截断", rep.action == "truncated" and rep.size_after == size
          and os.path.getsize(path) == size,
          "截断耗时 %.4fs, cut=%d" % (t_rec, rep.cut_offset))
    with al.AppendWriter(path) as w:
        w.append(b"x" * payload_len)   # 补写崩溃时丢掉的记录
    res2 = al.scan(path)
    check("恢复后续写可正常校验", res2.clean and len(res2.records) == count + 1)


# ---------------------------------------------------------------- main

def main():
    print("appendlog 自测  tmp=%s" % TMP)
    try:
        test_edge_cases()
        test_crash_recovery()
        test_tamper_detection()
        test_1gb_performance()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n== 汇总: %d 项通过, %d 项失败 ==" % (len(RESULTS) - len(failed), len(failed)))
    for n in failed:
        print("  FAIL:", n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
