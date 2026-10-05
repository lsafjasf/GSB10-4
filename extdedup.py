"""External-memory dedup operator, Python 3 standard library only.

Design (sort-based external dedup):
  * In-memory table is a plain ``set``: membership costs one hash probe plus an
    equality check; equal keys collapse there.  Memory is accounted with
    ``sys.getsizeof`` (exact for the key objects and the set table).
  * When the accounted memtable size reaches ``mem_limit_bytes * fraction`` the
    unique keys are sorted and written to an immutable, length-prefixed run
    file.  The memtable is then cleared.
  * On finalisation the runs are merged with an external k-way merge
    (``heapq.merge``), in multiple passes if there are too many runs, and equal
    adjacent keys collapse via ``==``.  Output order is globally sorted.

Correctness note on hash collisions: spill order and merge equality use only
the *real* ordering/equality of the keys, never hash values.  Python ``set``
resolves hash buckets with ``__eq__`` too.  Two distinct keys that share a hash
can therefore never be treated as duplicates.
"""

import atexit
import heapq
import os
import shutil
import signal
import struct
import sys
import tempfile
import threading

_LEN = struct.Struct(">I")
_MISSING = object()
_RUN_PREFIX = "extdedup-"


def _identity_dumps(key):
    return key


def _identity_loads(data):
    return data


class _RunWriter:
    """Writes length-prefixed records: 4-byte big-endian length + payload."""

    def __init__(self, path, dumps, write_buffer_bytes=1 << 16):
        self._f = open(path, "wb", buffering=write_buffer_bytes)
        self._dumps = dumps
        self.count = 0
        self.bytes_written = 0

    def write(self, key):
        payload = self._dumps(key)
        self._f.write(_LEN.pack(len(payload)))
        self._f.write(payload)
        self.count += 1
        self.bytes_written += _LEN.size + len(payload)

    def close(self):
        self._f.close()


class _RunReader:
    """Streams length-prefixed records back in the order they were written."""

    def __init__(self, path, loads, read_buffer_bytes=1 << 16):
        self._f = open(path, "rb", buffering=read_buffer_bytes)
        self._loads = loads

    def __iter__(self):
        return self

    def __next__(self):
        header = self._f.read(4)
        if not header:
            raise StopIteration
        (length,) = _LEN.unpack(header)
        return self._loads(self._f.read(length))

    def close(self):
        self._f.close()


class ExternalDedup:
    """Streaming dedup operator with a bounded memtable and disk spill.

    Parameters
    ----------
    mem_limit_bytes:
        Total memory budget for the operator's data structures.  The memtable
        may grow to ``memtable_fraction`` (default 50%) of this value before a
        spill is forced.
    work_dir:
        Directory that holds spill runs.  Every instance creates a unique
        ``extdedup-*`` subdirectory inside it, so files left behind by a
        crashed run can never be read by a later run.  Defaults to the system
        temp directory.
    dumps / loads:
        Serialisation hooks for keys that are not ``bytes``.  Must be mutual
        inverses and preserve ordering: ``a < b`` iff
        ``dumps(a) < dumps(b)``.
    memtable_fraction:
        Fraction of ``mem_limit_bytes`` the memtable may occupy before spilling.
    max_open_runs:
        Maximum simultaneously open run files during merging; larger run counts
        are reduced with extra merge passes.
    reader_buffer_bytes:
        Read buffer per open run file.  Reserved merge memory is
        ``max_open_runs * reader_buffer_bytes``.
    """

    def __init__(
        self,
        mem_limit_bytes,
        work_dir=None,
        *,
        dumps=None,
        loads=None,
        memtable_fraction=0.5,
        max_open_runs=128,
        reader_buffer_bytes=1 << 15,
    ):
        if mem_limit_bytes <= 0:
            raise ValueError("mem_limit_bytes must be positive")
        if not 0.0 < memtable_fraction < 1.0:
            raise ValueError("memtable_fraction must be in (0, 1)")
        base = work_dir if work_dir is not None else tempfile.gettempdir()
        os.makedirs(base, exist_ok=True)

        self.mem_limit_bytes = int(mem_limit_bytes)
        self.memtable_budget = max(1, int(self.mem_limit_bytes * memtable_fraction))
        self.max_open_runs = max(2, int(max_open_runs))
        self.reader_buffer_bytes = int(reader_buffer_bytes)
        self._dumps = dumps or _identity_dumps
        self._loads = loads or _identity_loads

        self.run_dir = tempfile.mkdtemp(prefix=_RUN_PREFIX, dir=base)
        self._seen = set()
        self._key_bytes = 0
        self._runs = []
        self._run_seq = 0
        self.spill_count = 0
        self.spill_bytes = 0
        self.merge_bytes = 0
        self._closed = False
        self._finalized = False

        atexit.register(shutil.rmtree, self.run_dir, True)
        self._install_signal_cleanup()

    # ------------------------------------------------------------------ API

    def add(self, key):
        """Insert a key.  Returns ``True`` if it was new, ``False`` if a dup."""
        if self._finalized:
            raise RuntimeError("cannot add() after the operator was iterated")
        if key in self._seen:
            return False
        self._seen.add(key)
        self._key_bytes += sys.getsizeof(key)
        if self._key_bytes + sys.getsizeof(self._seen) >= self.memtable_budget:
            self._spill()
        return True

    def __iter__(self):
        """Yield the unique keys in globally sorted order, exactly once."""
        if self._closed:
            raise RuntimeError("operator is closed")
        self._finalized = True
        if not self._runs:
            # Everything fit in memory: no spill file is ever created.
            result = sorted(self._seen)
            self._seen.clear()
            self._key_bytes = 0
            return iter(result)
        self._spill()
        runs = self._runs
        while len(runs) > self.max_open_runs:
            runs = self._merge_pass(runs)
        return self._drain(runs)

    def close(self):
        """Remove every spill file.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def disk_bytes(self):
        """Bytes currently held on disk by this operator's run files."""
        total = 0
        try:
            for name in os.listdir(self.run_dir):
                total += os.path.getsize(os.path.join(self.run_dir, name))
        except FileNotFoundError:
            pass
        return total

    # --------------------------------------------------------------- internals

    def _new_run_path(self):
        path = os.path.join(self.run_dir, f"run-{self._run_seq:06d}.tmp")
        self._run_seq += 1
        return path

    def _spill(self):
        if not self._seen:
            return
        path = self._new_run_path()
        writer = _RunWriter(path, self._dumps)
        try:
            for key in sorted(self._seen):
                writer.write(key)
        finally:
            writer.close()
        self._runs.append(path)
        self.spill_count += 1
        self.spill_bytes += writer.bytes_written
        self._seen.clear()
        self._key_bytes = 0

    def _open_readers(self, paths):
        return [
            _RunReader(path, self._loads, self.reader_buffer_bytes) for path in paths
        ]

    @staticmethod
    def _emit_unique(key, last):
        return last is _MISSING or key != last

    def _merge_pass(self, runs):
        merged = []
        for start in range(0, len(runs), self.max_open_runs):
            group = runs[start : start + self.max_open_runs]
            if len(group) == 1:
                merged.append(group[0])
                continue
            path = self._new_run_path()
            writer = _RunWriter(path, self._dumps)
            readers = self._open_readers(group)
            try:
                last = _MISSING
                for key in heapq.merge(*readers):
                    if self._emit_unique(key, last):
                        writer.write(key)
                        last = key
            finally:
                for reader in readers:
                    reader.close()
                writer.close()
            for old_path in group:
                _silent_remove(old_path)
            merged.append(path)
            self.merge_bytes += writer.bytes_written
        return merged

    def _drain(self, runs):
        readers = self._open_readers(runs)
        try:
            last = _MISSING
            for key in heapq.merge(*readers):
                if self._emit_unique(key, last):
                    yield key
                    last = key
        finally:
            for reader in readers:
                reader.close()
            for path in runs:
                _silent_remove(path)

    def _install_signal_cleanup(self):
        try:
            if threading.current_thread() is not threading.main_thread():
                return
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous = signal.getsignal(signum)

                def handler(signum, frame, previous=previous):
                    self.close()
                    if callable(previous):
                        previous(signum, frame)
                    raise SystemExit(128 + int(signum))

                signal.signal(signum, handler)
        except (ValueError, OSError):
            # No permission to install handlers (non-main thread, embedded use).
            pass


def dedup(iterable, mem_limit_bytes, **kwargs):
    """Convenience: lazily dedup an iterable with the given memory budget."""
    with ExternalDedup(mem_limit_bytes, **kwargs) as operator:
        for key in iterable:
            operator.add(key)
        for key in operator:
            yield key


def sweep_stale_run_dirs(base_dir=None):
    """Remove leftover ``extdedup-*`` directories (best-effort crash recovery).

    Stale directories are never read by new instances (instance directories are
    unique), so this is purely a disk-reclamation helper.
    """
    base = base_dir if base_dir is not None else tempfile.gettempdir()
    removed = []
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return removed
    for name in names:
        if name.startswith(_RUN_PREFIX):
            path = os.path.join(base, name)
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


def _silent_remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
