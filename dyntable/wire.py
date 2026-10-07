"""Minimal binary wire primitives (standard library only)."""

import struct

from .errors import TruncatedData


def enc_u8(value):
    return struct.pack(">B", value)


def enc_u16(value):
    return struct.pack(">H", value)


def enc_u32(value):
    return struct.pack(">I", value)


def enc_str(text):
    data = text.encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError("string too long: %d bytes" % len(data))
    return enc_u16(len(data)) + data


class Reader:
    def __init__(self, data):
        self.data = data
        self.off = 0

    def remaining(self):
        return len(self.data) - self.off

    def _take(self, count):
        if self.remaining() < count:
            raise TruncatedData(
                "need %d bytes, only %d left"
                % (count, self.remaining())
            )
        chunk = self.data[self.off:self.off + count]
        self.off += count
        return chunk

    def u8(self):
        return self._take(1)[0]

    def u16(self):
        return struct.unpack(">H", self._take(2))[0]

    def u32(self):
        return struct.unpack(">I", self._take(4))[0]

    def string(self):
        length = self.u16()
        return self._take(length).decode("utf-8")
