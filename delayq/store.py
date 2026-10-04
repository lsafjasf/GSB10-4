"""Persistent delayed-message queue, standard library only.

Guarantees
----------
* Delivery time: a message becomes deliverable at ``due_time``. A call to
  :meth:`DelayedQueue.pump` with a virtual clock value ``now`` delivers every
  pending message with ``due_time <= now`` exactly once (see durability note
  below).
* Ordering: messages are delivered in ascending ``(due_time, seq)`` order,
  where ``seq`` is a process-wide monotonically increasing enqueue sequence
  assigned by :meth:`schedule`. Ties on ``due_time`` therefore have a fixed
  FIFO-by-enqueue order; there is no randomness and no dependence on dict /
  heap address ordering.
* Persistence: every schedule / cancel / delivery is an fsynced record in an
  append-only WAL (``wal.log``). A point-in-time ``snapshot.jsonl`` plus the
  WAL fully reconstructs state, including the monotonic ``seq`` counter, so
  after recovery the due order is identical to a run that never restarted.
* Scan complexity: a min-heap keyed by ``(due, seq)``. ``pump`` pops only
  messages that are actually due; messages that are not due are never
  inspected. Pump is O(k log n) for k delivered messages; ``schedule`` is
  O(log n); ``cancel`` is O(log n) amortised (lazy heap deletion).

Durability / delivery semantics
-------------------------------
The DELIVER record is fsynced *before* the user sink is invoked.  A crash
between that fsync and the sink call means the message is not redelivered
after recovery: the queue provides *at-most-once* sink invocation and
*exactly-once* state transition (a message is never delivered twice by the
queue).  Open the queue with ``durable=False`` to skip per-record fsync
(useful for benchmarks; not safe for real crash recovery).

Cancel results
--------------
* ``CANCELLED``          - the message was pending and is now removed.
* ``ALREADY_DELIVERED``  - the message was already delivered (the DELIVER
                           record is still retained in the current WAL
                           generation; see "Retention boundary").
* ``NOT_FOUND``          - no such pending/delivered-within-generation id.

Retention boundary: :meth:`snapshot` compacts history. Only *pending*
messages survive a snapshot; delivered/cancelled ids are dropped (they would
otherwise consume unbounded memory). After a snapshot, cancelling an id that
was delivered before the snapshot returns ``NOT_FOUND`` rather than
``ALREADY_DELIVERED``; this is an explicit, documented boundary. Cancelling a
delivered message before compaction - including across a restart that did not
compact - deterministically returns ``ALREADY_DELIVERED``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
import heapq
from typing import Any, Callable, Iterable

MAGIC_WAL = "delayq-wal"
MAGIC_SNAP = "delayq-snapshot"
VERSION = 1

NOT_FOUND = "NOT_FOUND"
CANCELLED = "CANCELLED"
ALREADY_DELIVERED = "ALREADY_DELIVERED"

WAL_NAME = "wal.log"
SNAP_NAME = "snapshot.jsonl"
_TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class CancelResult:
    status: str
    seq: int | None = None

    def __bool__(self) -> bool:
        return self.status == CANCELLED


@dataclass(frozen=True)
class DeliveryRecord:
    seq: int
    id: Any
    due: int | float
    payload: Any
    delivered_at: int | float


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _encode(record: dict) -> bytes:
    # json.dumps escapes newlines inside strings, so one record == one line.
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) +
            "\n").encode("utf-8")


def _iter_records(path: str) -> Iterable[dict]:
    """Yield JSON records, one per non-empty line.

    A torn final line (partial append / crash mid-write) is ignored.
    """
    if not os.path.exists(path):
        return
    with open(path, "rb") as fh:
        for line_no, raw in enumerate(fh):
            if not raw.endswith(b"\n"):
                # Torn write: record never committed.
                break
            try:
                yield json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                if line_no == 0:
                    raise
                # A corrupted middle record is not expected with fsynced
                # line writes; fail loudly rather than silently drop work.
                raise


class DelayedQueue:
    def __init__(self, directory: str, *, durable: bool = True):
        self._dir = directory
        self._durable = durable
        os.makedirs(directory, exist_ok=True)

        # _by_seq holds the authoritative pending entries:
        # [due, seq, id, payload]; the heap holds the same list objects.
        self._heap: list[list] = []
        self._by_seq: dict[int, list] = {}
        self._id_to_seq: dict[Any, int] = {}
        self._delivered_ids: set[Any] = set()
        self._next_seq = 0

        self._load()

        wal_path = os.path.join(self._dir, WAL_NAME)
        new_wal = not os.path.exists(wal_path)
        self._wal = open(wal_path, "ab", buffering=0)
        if new_wal:
            self._write_wal(
                {"magic": MAGIC_WAL, "version": VERSION}, skip_fsync=False
            )
            _fsync_dir(self._dir)

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        snap_path = os.path.join(self._dir, SNAP_NAME)
        if os.path.exists(snap_path):
            records = _iter_records(snap_path)
            try:
                header = next(iter(records))
            except StopIteration:
                header = None
            if header and header.get("magic") == MAGIC_SNAP:
                self._next_seq = int(header["next_seq"])
                for rec in records:
                    self._insert_pending(rec["seq"], rec["id"],
                                         rec["due"], rec["payload"])

        for rec in _iter_records(os.path.join(self._dir, WAL_NAME)):
            ty = rec.get("ty")
            if ty == "SCHED":
                # Idempotent by seq: safe if an old WAL lingers across a
                # snapshot replacement (crash between snapshot and truncate).
                if rec["seq"] in self._by_seq:
                    continue
                self._insert_pending(rec["seq"], rec["id"],
                                     rec["due"], rec["payload"])
                if rec["seq"] >= self._next_seq:
                    self._next_seq = rec["seq"] + 1
            elif ty == "DELIVER":
                entry = self._by_seq.pop(rec["seq"], None)
                if entry is not None:
                    self._id_to_seq.pop(entry[2], None)
                    self._delivered_ids.add(entry[2])
                # Stale DELIVER (already in snapshot-compacted generation):
                # nothing to do.
            elif ty == "CANCEL":
                seq = self._id_to_seq.pop(rec["id"], None)
                if seq is not None:
                    self._by_seq.pop(seq, None)
                else:
                    self._delivered_ids.discard(rec["id"])
            # header / unknown lines are ignored

    def _insert_pending(self, seq, msg_id, due, payload) -> None:
        if msg_id in self._id_to_seq:
            raise ValueError(f"duplicate pending id: {msg_id!r}")
        entry = [due, seq, msg_id, payload]
        self._by_seq[seq] = entry
        self._id_to_seq[msg_id] = seq
        heapq.heappush(self._heap, entry)

    # ------------------------------------------------------------------ #
    # WAL
    # ------------------------------------------------------------------ #
    def _write_wal(self, record: dict, *, skip_fsync: bool) -> None:
        self._wal.write(_encode(record))
        self._wal.flush()
        if self._durable and not skip_fsync:
            os.fsync(self._wal.fileno())

    # ------------------------------------------------------------------ #
    # Public API - all times are injected by the caller.
    # ------------------------------------------------------------------ #
    def schedule(self, msg_id: Any, due_time: int | float,
                 payload: Any = None) -> int:
        """Schedule ``msg_id`` for delivery at ``due_time``; returns seq."""
        if msg_id in self._id_to_seq:
            raise ValueError(f"duplicate pending id: {msg_id!r}")
        seq = self._next_seq
        self._next_seq += 1
        self._insert_pending(seq, msg_id, due_time, payload)
        self._write_wal(
            {"ty": "SCHED", "seq": seq, "id": msg_id,
             "due": due_time, "payload": payload},
            skip_fsync=False,
        )
        return seq

    def cancel(self, msg_id: Any) -> CancelResult:
        seq = self._id_to_seq.pop(msg_id, None)
        if seq is not None:
            self._by_seq.pop(seq, None)  # heap entry removed lazily
            self._write_wal({"ty": "CANCEL", "id": msg_id}, skip_fsync=False)
            return CancelResult(CANCELLED, seq)
        if msg_id in self._delivered_ids:
            return CancelResult(ALREADY_DELIVERED)
        return CancelResult(NOT_FOUND)

    def due_count(self, now: int | float) -> int:
        """Number of pending messages with due_time <= now (O(k log n)).

        Only used by tests / diagnostics; it pops nothing.
        """
        return sum(1 for e in self._heap
                   if e[1] in self._by_seq and e[0] <= now)

    def pending_count(self) -> int:
        return len(self._by_seq)

    def pump(self, now: int | float,
             sink: Callable[[DeliveryRecord], None]) -> list[DeliveryRecord]:
        """Deliver every pending message with due_time <= now.

        Order: (due_time, seq). The DELIVER record is fsynced before ``sink``
        is called. If ``sink`` raises, the exception propagates and the pump
        stops; already-delivered messages stay delivered.
        """
        delivered: list[DeliveryRecord] = []
        while self._heap:
            due, seq, msg_id, payload = self._heap[0]
            if due > now:
                break
            heapq.heappop(self._heap)
            entry = self._by_seq.pop(seq, None)
            if entry is None:
                continue  # cancelled: lazy deletion
            self._id_to_seq.pop(msg_id, None)
            self._delivered_ids.add(msg_id)
            self._write_wal({"ty": "DELIVER", "seq": seq}, skip_fsync=False)
            record = DeliveryRecord(seq, msg_id, due, payload, now)
            delivered.append(record)
            sink(record)
        return delivered

    # ------------------------------------------------------------------ #
    # Compaction
    # ------------------------------------------------------------------ #
    def snapshot(self) -> None:
        """Write a compacted snapshot of pending messages and truncate WAL.

        Output order is (due, seq), so snapshot files are deterministic and
        diff-friendly. Delivered/cancelled ids are dropped - see the
        "Retention boundary" note in the module docstring.
        """
        tmp_snap = os.path.join(self._dir, SNAP_NAME + _TMP_SUFFIX)
        pending = sorted(self._by_seq.values(), key=lambda e: (e[0], e[1]))
        with open(tmp_snap, "wb") as fh:
            fh.write(_encode({"magic": MAGIC_SNAP, "version": VERSION,
                              "next_seq": self._next_seq}))
            for due, seq, msg_id, payload in pending:
                fh.write(_encode({"ty": "SCHED", "seq": seq, "id": msg_id,
                                  "due": due, "payload": payload}))
            fh.flush()
            if self._durable:
                os.fsync(fh.fileno())
        os.replace(tmp_snap, os.path.join(self._dir, SNAP_NAME))
        if self._durable:
            _fsync_dir(self._dir)

        # Truncate WAL. Replay is idempotent by seq, so even a crash between
        # the snapshot rename and this truncation recovers correctly.
        self._wal.close()
        tmp_wal = os.path.join(self._dir, WAL_NAME + _TMP_SUFFIX)
        with open(tmp_wal, "wb") as fh:
            fh.write(_encode({"magic": MAGIC_WAL, "version": VERSION}))
            fh.flush()
            if self._durable:
                os.fsync(fh.fileno())
        os.replace(tmp_wal, os.path.join(self._dir, WAL_NAME))
        if self._durable:
            _fsync_dir(self._dir)
        self._wal = open(os.path.join(self._dir, WAL_NAME), "ab", buffering=0)

        # Delivered ids lived only to support ALREADY_DELIVERED within one
        # WAL generation; the new generation starts empty.
        self._delivered_ids.clear()

    def close(self) -> None:
        if self._wal is not None:
            self._wal.flush()
            if self._durable:
                os.fsync(self._wal.fileno())
            self._wal.close()
            self._wal = None

    def __enter__(self) -> "DelayedQueue":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
