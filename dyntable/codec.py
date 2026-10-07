"""Encoder/decoder pair sharing one dynamic-table state machine.

Two logical channels (QPACK-style, RFC 9204):

1. Encoder instruction stream: reliable, ordered.  Carries table
   mutations (Insert, SetCapacity).  The stream is split into chunks;
   every chunk carries a monotonically increasing sequence number so
   the decoder can detect lost chunks (gap) and out-of-order/duplicate
   chunks (sequence number lower than expected).

2. Header blocks: each block starts with a prefix
   (EncodedInsertCount, BaseSign, DeltaBase).  The decoder reconstructs
   the Required Insert Count (RFC 9204 section 4.5.1.1) and compares it
   with its Known Received Count; if the block depends on inserts that
   have not arrived, it raises BlockedHeaderBlock instead of decoding
   garbage.
"""

import struct

from .errors import (
    BlockedHeaderBlock,
    InvalidHeaderBlock,
    InvalidInstruction,
    LostUpdateError,
    OutOfOrderUpdateError,
)
from .table import DynamicTable, entry_size
from .wire import Reader, enc_str, enc_u8, enc_u32

# Instruction stream tags.
INS_INSERT = 0x01
INS_SET_CAPACITY = 0x02

# Header field tags.
F_INDEXED_PRE_BASE = 0x01
F_INDEXED_POST_BASE = 0x02
F_LITERAL = 0x03
F_LITERAL_NAME_REF = 0x04

CHUNK_HEADER = struct.Struct(">II")  # seq, payload_length


def encode_insert_count(ric, max_entries):
    """RFC 9204 section 4.5.1.1 (encoder side)."""
    if ric == 0:
        return 0
    return (ric % (2 * max_entries)) + 1


def decode_insert_count(eic, total_inserts, max_entries):
    """RFC 9204 section 4.5.1.1 (decoder side)."""
    if eic == 0:
        return 0
    if max_entries == 0:
        raise InvalidHeaderBlock(
            "non-zero EncodedInsertCount with zero-sized table"
        )
    full_range = 2 * max_entries
    if eic > full_range:
        raise InvalidHeaderBlock(
            "EncodedInsertCount %d exceeds FullRange %d"
            % (eic, full_range)
        )
    max_value = total_inserts + max_entries
    max_wrapped = (max_value // full_range) * full_range
    ric = max_wrapped + eic - 1
    if ric > max_value:
        if ric <= full_range:
            raise InvalidHeaderBlock(
                "EncodedInsertCount %d wraps past zero" % eic
            )
        ric -= full_range
    if ric == 0:
        raise InvalidHeaderBlock("reconstructed RequiredInsertCount is 0")
    return ric


class Encoder:
    def __init__(self, max_capacity=4096):
        self.table = DynamicTable(max_capacity)
        self._seq = 0
        self._stream = bytearray()
        # Newest-entry indexes over the dynamic table; entries pointing
        # at evicted absolute indexes are discarded lazily on lookup.
        self._full_map = {}
        self._name_map = {}

    def _find(self, name, value=None):
        mapping = self._full_map if value is not None else self._name_map
        key = (name, value) if value is not None else name
        abs_idx = mapping.get(key)
        if abs_idx is None:
            return None
        if abs_idx >= self.table.dropped_count:
            return abs_idx
        del mapping[key]
        return None

    def insert(self, name, value):
        """Queue an insert instruction; False if the entry cannot fit."""
        if not self.table.insert(name, value):
            return False
        abs_idx = self.table.insert_count - 1
        self._full_map[(name, value)] = abs_idx
        self._name_map[name] = abs_idx
        self._prune_indexes()
        self._stream += enc_u8(INS_INSERT) + enc_str(name) + enc_str(value)
        return True

    def set_capacity(self, capacity):
        """Queue a dynamic-table capacity update instruction."""
        self.table.set_capacity(capacity)  # raises if above the limit
        self._prune_indexes()
        self._stream += enc_u8(INS_SET_CAPACITY) + enc_u32(capacity)

    def _prune_indexes(self):
        # Remove index keys that point at entries evicted by the
        # most recent insert/capacity update, unless a newer entry
        # with the same key has already replaced them.
        for ev_name, ev_value in self.table.last_evicted:
            full_key = (ev_name, ev_value)
            if self._full_map.get(full_key, None) is not None \
                    and self._full_map[full_key] < self.table.dropped_count:
                del self._full_map[full_key]
            if self._name_map.get(ev_name) is not None \
                    and self._name_map[ev_name] < self.table.dropped_count:
                del self._name_map[ev_name]

    def flush_stream_chunk(self):
        """Pack queued instructions into one sequenced chunk."""
        chunk = (
            CHUNK_HEADER.pack(self._seq, len(self._stream))
            + bytes(self._stream)
        )
        self._seq += 1
        self._stream.clear()
        return chunk

    def encode_header_block(self, headers, index=True):
        """Encode a header list; returns the header block bytes.

        New entries inserted while encoding are queued on the
        instruction stream; flush_stream_chunk() must be delivered
        before (or together with) this block.
        """
        plans = []
        for name, value in headers:
            abs_idx = self._find(name, value)
            if abs_idx is not None:
                plans.append((F_INDEXED_PRE_BASE, abs_idx, name, value))
                continue
            name_idx = self._find(name)
            if index and self.insert(name, value):
                plans.append(
                    (F_INDEXED_PRE_BASE, self.table.insert_count - 1,
                     name, value)
                )
            elif name_idx is not None:
                plans.append((F_LITERAL_NAME_REF, name_idx, name, value))
            else:
                plans.append((F_LITERAL, None, name, value))

        # An insert later in this same block may have evicted an entry
        # referenced earlier; such references would be undecodable, so
        # fall back to a literal representation for them.
        dropped = self.table.dropped_count
        plans = [
            (F_LITERAL, None, name, value)
            if abs_idx is not None and abs_idx < dropped
            else (tag, abs_idx, name, value)
            for tag, abs_idx, name, value in plans
        ]

        base = self.table.insert_count
        ric = 0
        for _tag, abs_idx, _name, _value in plans:
            if abs_idx is not None:
                ric = max(ric, abs_idx + 1)

        out = bytearray()
        out += enc_u32(encode_insert_count(ric, self.table.max_entries))
        out += enc_u8(0)  # base sign: base >= ric for this encoder
        out += enc_u32(base - ric)
        for tag, abs_idx, name, value in plans:
            if tag == F_INDEXED_PRE_BASE:
                out += enc_u8(tag) + enc_u32(base - 1 - abs_idx)
            elif tag == F_LITERAL_NAME_REF:
                out += enc_u8(tag) + enc_u32(base - 1 - abs_idx)
                out += enc_str(value)
            else:
                out += enc_u8(tag) + enc_str(name) + enc_str(value)
        return bytes(out)


class Decoder:
    def __init__(self, max_capacity=4096):
        self.table = DynamicTable(max_capacity)
        self.known_received_count = 0  # QPACK Known Received Count
        self._expected_seq = 0

    def feed_stream_chunk(self, chunk):
        """Apply one encoder-stream chunk; detects loss and reordering."""
        if len(chunk) < CHUNK_HEADER.size:
            raise InvalidInstruction(
                "chunk too short: %d bytes" % len(chunk)
            )
        seq, length = CHUNK_HEADER.unpack_from(chunk, 0)
        if seq > self._expected_seq:
            raise LostUpdateError(
                "expected chunk seq %d, got %d: %d update chunk(s) lost"
                % (self._expected_seq, seq, seq - self._expected_seq)
            )
        if seq < self._expected_seq:
            raise OutOfOrderUpdateError(
                "expected chunk seq %d, got %d: duplicate or "
                "out-of-order update" % (self._expected_seq, seq)
            )
        payload = chunk[CHUNK_HEADER.size:]
        if len(payload) != length:
            raise InvalidInstruction(
                "chunk length mismatch: header=%d actual=%d"
                % (length, len(payload))
            )
        reader = Reader(payload)
        while reader.remaining():
            tag = reader.u8()
            if tag == INS_INSERT:
                name = reader.string()
                value = reader.string()
                if not self.table.insert(name, value):
                    raise InvalidInstruction(
                        "insert of %d-byte entry exceeds table "
                        "capacity %d"
                        % (entry_size(name, value), self.table.capacity)
                    )
            elif tag == INS_SET_CAPACITY:
                # CapacityLimitExceeded propagates to the caller.
                self.table.set_capacity(reader.u32())
            else:
                raise InvalidInstruction(
                    "unknown instruction tag 0x%02x" % tag
                )
        self._expected_seq += 1
        self.known_received_count = self.table.insert_count

    def decode_header_block(self, data):
        """Decode a header block; raises instead of mis-decoding."""
        reader = Reader(data)
        eic = reader.u32()
        base_sign = reader.u8()
        delta_base = reader.u32()

        ric = decode_insert_count(
            eic, self.known_received_count, self.table.max_entries
        )
        if ric > self.known_received_count:
            raise BlockedHeaderBlock(
                "block requires insert count %d, decoder only knows "
                "%d: encoder updates missing or not yet arrived"
                % (ric, self.known_received_count)
            )
        if base_sign == 0:
            base = ric + delta_base
        else:
            base = ric - delta_base - 1
            if base < 0:
                raise InvalidHeaderBlock(
                    "negative Base: ric=%d delta=%d" % (ric, delta_base)
                )
        if base > self.table.insert_count:
            raise BlockedHeaderBlock(
                "block Base %d beyond known insert count %d"
                % (base, self.table.insert_count)
            )

        headers = []
        while reader.remaining():
            tag = reader.u8()
            if tag == F_INDEXED_PRE_BASE:
                headers.append(self.table.get_relative(reader.u32(), base))
            elif tag == F_INDEXED_POST_BASE:
                headers.append(
                    self.table.get_post_base(reader.u32(), base)
                )
            elif tag == F_LITERAL:
                headers.append((reader.string(), reader.string()))
            elif tag == F_LITERAL_NAME_REF:
                name, _ = self.table.get_relative(reader.u32(), base)
                headers.append((name, reader.string()))
            else:
                raise InvalidHeaderBlock(
                    "unknown field tag 0x%02x" % tag
                )
        return headers
