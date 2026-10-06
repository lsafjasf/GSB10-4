"""bitmap_codec.py

位图索引的两种编码 + 编码选择 + 交并差运算（仅标准库）。

编码
----
ENC_RAW    紧凑位图：每 8 位打包成 1 字节，体积恒为 ceil(n/8)。
           随机/中等密度时最优，交并差可直接按机器字并行。

ENC_SPARSE 稀疏编码：只存“少数位”的位置（已排序、去重），
           位置之间做差分后用 LEB128 无符号变长整数（varint）编码。
           - 密度 d <= 0.5：存 1 的位置（negated=False）
           - 密度 d >  0.5：存 0 的位置（negated=True，取反语义）
           因此极稀疏和极稠密两头都只付少数位的代价。

序列化格式（serialize/deserialize）
----------------------------------
字节 0          : encoding << 1 | negated
之后 varint     : nbits
之后            : 编码负载（RAW 为定长字节，SPARSE 为 varint delta 流）

编码选择（choose_encoding）
--------------------------
给定 nbits 与 1 的个数 popcount，精确估算两种编码的字节数：
    raw_bytes    = ceil(n/8)
    sparse_bytes = minority_count * varint 平均字节数（用 n 上的最大间隔上界）
取更小者；相等时优先 RAW（运算更快）。位置已知时 encode() 会按
精确 varint 体积做最终选择。
对随机分布，分界密度约为 1/8（间隔 < 128 时每点 1 字节）；
d 极小或 d 接近 1 时 SPARSE 完胜，全空/全满为 O(1) 字节。
"""

from itertools import islice

ENC_RAW = 0
ENC_SPARSE = 1

_OP_AND = "AND"
_OP_OR = "OR"
_OP_XOR = "XOR"
_OP_DIFF = "DIFF"


# --------------------------------------------------------------------------
# LEB128 varint
# --------------------------------------------------------------------------
def _varint_encode(value, out):
    if value < 0:
        raise ValueError("varint 只支持非负整数")
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _varint_decode(buf, pos):
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 35:
            raise ValueError("varint 过长")


def _varint_len(value):
    n = 1
    while value >= 0x80:
        value >>= 7
        n += 1
    return n


# --------------------------------------------------------------------------
# 编码选择
# --------------------------------------------------------------------------
def choose_encoding(nbits, popcount):
    """按分布（n 与 1 的个数）选择编码，返回 (encoding, negated)。

    用 n 上的最大间隔上界估算 SPARSE 的每点 varint 字节数，
    保证估计值 >= 实际值的 1 字节，选择稳定可复现。
    """
    if not 0 <= popcount <= nbits:
        raise ValueError("popcount 超出范围")
    raw_bytes = (nbits + 7) // 8
    zeros = nbits - popcount
    negated = zeros < popcount
    minority = zeros if negated else popcount
    if minority == 0:
        return ENC_SPARSE, negated
    # 最大间隔上界：n / minority + 1
    max_gap = nbits // minority + 1
    per_point = _varint_len(max_gap)
    sparse_bytes = minority * per_point
    if sparse_bytes < raw_bytes:
        return ENC_SPARSE, negated
    return ENC_RAW, False


def _choose_by_exact_size(nbits, minority_positions, negated):
    """位置已知时按精确 varint 体积做最终裁决（平局选 RAW）。"""
    raw_bytes = (nbits + 7) // 8
    if not minority_positions:
        return ENC_SPARSE, negated
    sparse_bytes = _varint_len(minority_positions[0])
    for prev, cur in zip(minority_positions, islice(minority_positions, 1, None)):
        sparse_bytes += _varint_len(cur - prev)
    if sparse_bytes < raw_bytes:
        return ENC_SPARSE, negated
    return ENC_RAW, False


# --------------------------------------------------------------------------
# Bitmap
# --------------------------------------------------------------------------
class Bitmap:
    __slots__ = ("nbits", "encoding", "negated", "payload")

    def __init__(self, nbits, encoding, negated, payload):
        if nbits <= 0:
            raise ValueError("nbits 必须为正整数")
        if encoding not in (ENC_RAW, ENC_SPARSE):
            raise ValueError("未知编码")
        self.nbits = nbits
        self.encoding = encoding
        self.negated = bool(negated) and encoding == ENC_SPARSE
        self.payload = bytes(payload)

    # ---- 构造 -------------------------------------------------------------
    @classmethod
    def from_positions(cls, nbits, positions, encoding=None):
        """positions: 1 位位置的可迭代对象（可无序、可重复）。"""
        pos = sorted(set(positions))
        if pos and (pos[0] < 0 or pos[-1] >= nbits):
            raise ValueError("位置越界")
        if encoding is None:
            popcount = len(pos)
            sparse_negated = (nbits - popcount) < popcount
            minority = _minority_positions(nbits, pos, sparse_negated)
            encoding, negated = _choose_by_exact_size(
                nbits, minority, sparse_negated)
        else:
            encoding, negated = _normalize_encoding(nbits, len(pos), encoding)
            minority = _minority_positions(nbits, pos, negated)
        if encoding == ENC_RAW:  # RAW 不存在取反语义，按 1 的位置写
            negated = False
            minority = pos
        payload = _encode_payload(nbits, minority, encoding, negated)
        return cls(nbits, encoding, negated, payload)

    @classmethod
    def from_int(cls, nbits, value, encoding=None):
        if value < 0 or value >> nbits:
            raise ValueError("位图超出 nbits 范围")
        nbytes = (nbits + 7) // 8
        popcount = value.bit_count()
        if encoding is None:
            enc, neg = choose_encoding(nbits, popcount)
        else:
            enc, neg = _normalize_encoding(nbits, popcount, encoding)
        if enc == ENC_RAW:
            # 大整数直转字节，O(n/8)，避免逐位 Python 循环
            return cls(nbits, ENC_RAW, False,
                       value.to_bytes(nbytes, "little"))
        minority = (~value & ((1 << nbits) - 1)) if neg else value
        out = bytearray()
        last = 0
        while minority:  # 仅遍历少数位（稀疏端）
            lsb = minority & -minority
            p = lsb.bit_length() - 1
            _varint_encode(p - last, out)
            last = p
            minority ^= lsb
        return cls(nbits, ENC_SPARSE, neg, bytes(out))

    # ---- 解码 -------------------------------------------------------------
    def to_int(self):
        """解码为 Python 大整数（第 p 位 = 1<<p）。"""
        nbytes = (self.nbits + 7) // 8
        if self.encoding == ENC_RAW:
            return int.from_bytes(self.payload, "little")
        positions = self._sparse_positions()
        buf = bytearray(b"\xff" * nbytes if self.negated else b"\x00" * nbytes)
        if self.negated:
            for p in positions:
                buf[p >> 3] &= ~(1 << (p & 7)) & 0xFF
            if self.nbits % 8:
                buf[-1] &= (1 << (self.nbits % 8)) - 1
        else:
            for p in positions:
                buf[p >> 3] |= 1 << (p & 7)
        return int.from_bytes(buf, "little")

    def to_positions(self):
        return _int_to_positions(self.to_int())

    def popcount(self):
        if self.encoding == ENC_SPARSE and not self.negated:
            return len(self._sparse_positions())
        return self.to_int().bit_count()

    def _sparse_positions(self):
        out = []
        pos = 0
        last = 0
        data = self.payload
        while pos < len(data):
            delta, pos = _varint_decode(data, pos)
            last += delta
            out.append(last)
        return out

    # ---- 空间 -------------------------------------------------------------
    def payload_bytes(self):
        return len(self.payload)

    def total_bytes(self):
        """含统一头部：1 字节标志 + nbits 的 varint。"""
        return 1 + _varint_len(self.nbits) + len(self.payload)

    # ---- 序列化 -----------------------------------------------------------
    def serialize(self):
        out = bytearray([(self.encoding << 1) | int(self.negated)])
        _varint_encode(self.nbits, out)
        out += self.payload
        return bytes(out)

    @classmethod
    def deserialize(cls, blob):
        flag = blob[0]
        encoding = flag >> 1
        negated = bool(flag & 1)
        nbits, pos = _varint_decode(blob, 1)
        return cls(nbits, encoding, negated, blob[pos:])

    # ---- 集合运算（允许两种编码混用）--------------------------------------
    def _binop(self, other, op):
        if not isinstance(other, Bitmap):
            return NotImplemented
        if self.nbits != other.nbits:
            raise ValueError("两个位图 nbits 不一致，无法运算")
        a, b = self.to_int(), other.to_int()
        if op == _OP_AND:
            r = a & b
        elif op == _OP_OR:
            r = a | b
        elif op == _OP_XOR:
            r = a ^ b
        elif op == _OP_DIFF:
            r = a & ~b
        else:
            raise AssertionError(op)
        r &= (1 << self.nbits) - 1
        return Bitmap.from_int(self.nbits, r)

    def __and__(self, other):
        return self._binop(other, _OP_AND)

    def __or__(self, other):
        return self._binop(other, _OP_OR)

    def __xor__(self, other):
        return self._binop(other, _OP_XOR)

    def __sub__(self, other):
        return self._binop(other, _OP_DIFF)

    def __eq__(self, other):
        return isinstance(other, Bitmap) and self.nbits == other.nbits \
            and self.to_int() == other.to_int()

    def __hash__(self):
        return hash((self.nbits, self.to_int()))

    def __repr__(self):
        name = "RAW" if self.encoding == ENC_RAW else (
            "NSPARSE" if self.negated else "SPARSE")
        return f"<Bitmap n={self.nbits} enc={name} bytes={self.total_bytes()}>"


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _normalize_encoding(nbits, popcount, encoding):
    if encoding == ENC_RAW:
        return ENC_RAW, False
    if encoding == ENC_SPARSE:
        return ENC_SPARSE, (nbits - popcount) < popcount
    raise ValueError("未知编码")


def _minority_positions(nbits, ones_sorted, negated):
    if not negated:
        return list(ones_sorted)
    minority = []
    cursor = 0
    for p in ones_sorted:
        minority.extend(range(cursor, p))
        cursor = p + 1
    minority.extend(range(cursor, nbits))
    return minority


def _encode_payload(nbits, minority_sorted, encoding, negated):
    if encoding == ENC_RAW:
        nbytes = (nbits + 7) // 8
        buf = bytearray(b"\xff" * nbytes if negated else b"\x00" * nbytes)
        # negated 下这里只会被“按精确大小”路径传空列表；为稳妥仍实现
        for p in minority_sorted:
            if negated:
                buf[p >> 3] &= ~(1 << (p & 7)) & 0xFF
            else:
                buf[p >> 3] |= 1 << (p & 7)
        return bytes(buf)
    out = bytearray()
    last = 0
    for p in minority_sorted:
        _varint_encode(p - last, out)
        last = p
    return bytes(out)


def _int_to_positions(value):
    out = []
    while value:
        lsb = value & -value
        out.append(lsb.bit_length() - 1)
        value ^= lsb
    return out
