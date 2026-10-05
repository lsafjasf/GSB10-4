"""打印逐步顺序读取的「位置与状态序列」，并演示各类截断报告。

运行: python3 demo.py
"""

from tapemark import (
    TapeReader,
    TruncatedVolume,
    encode_data,
    encode_eof,
    encode_eov,
)


def main() -> None:
    image = b"".join([
        encode_data(b"hello volume"),
        encode_eof(),
        encode_data(encode_eof() + encode_eov() + b"xy"),
        encode_eof(),
        encode_eof(),
        encode_data(bytes(range(256)) * 4),
        encode_eov(),
    ])
    print(f"镜像总长 {len(image)} 字节，7 条记录\n")
    print("== 顺序前进（每一步之后的位置与状态）==")
    print(f"{'步':>2}  {'序号':>2}  {'类型':<4}  {'记录偏移':>8}  {'长度':>6}"
          f"  {'步后位置':>8}  {'步后状态'}")
    reader = TapeReader(image)
    step = 0
    while True:
        event = reader.step_forward()
        if event is None:
            print("step_forward() -> None（EOV 之后无更多记录）")
            break
        step += 1
        print(f"{step:>2}  {event.index:>2}  {event.kind.name:<4}  "
              f"{event.offset:>8}  {event.length:>6}  "
              f"{reader.position:>8}  {reader.state()}")

    print("\n== 后退三步（位置精确回退，状态重新推导）==")
    for _ in range(3):
        event = reader.step_back()
        print(f"后退 -> 序号 {event.index} {event.kind.name:<4} "
              f"记录偏移 {event.offset:>6}  步后位置 {reader.position:>6}  "
              f"步后状态 {reader.state()}")

    print("\n== 再前进一步（与首次解析完全一致）==")
    event = reader.step_forward()
    print(f"前进 -> 序号 {event.index} {event.kind.name:<4} "
          f"记录偏移 {event.offset:>6}  步后位置 {reader.position:>6}  "
          f"步后状态 {reader.state()}")

    print("\n== 卷尾截断（不静默结束，报告已读块数/缺失字节/完整块边界）==")
    cases = [
        ("空卷", b""),
        ("只有数据块、无 EOV", encode_data(b"hello volume")),
        ("EOV 记录头被截断", image[:len(image) - 3]),
    ]
    for name, data in cases:
        truncated = TapeReader(data)
        blocks = 0
        try:
            while True:
                event = truncated.step_forward()
                if event is None:
                    break
                blocks += 1
        except TruncatedVolume as error:
            print(f"[{name}] {error}")


if __name__ == "__main__":
    main()
