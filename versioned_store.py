"""Versioned data store: atomic version switch, rollback, crash recovery.

Stdlib only (Python 3.8+).

Consistency model (see CONSISTENCY.md):
  * Each committed version is an IMMUTABLE snapshot.
  * Switch/rollback is an atomic pointer swap: readers bind `self._current`
    exactly once per logical read, so one read always sees exactly one
    version -- never a mix of old and new.
  * Readers never block; writers/switch/rollback are serialized by one lock.
  * Durable state is a single atomically-replaced manifest (CURRENT):
        {"current": <num>, "keep": [<valid version nums>]}
    Commit protocol:
        1. write versions/<n>.json.tmp, fsync, os.replace -> versions/<n>.json
        2. fsync(versions dir)
        3. write CURRENT.tmp, fsync, os.replace -> CURRENT
        4. fsync(store dir)
    After a crash the manifest is either the old one (the new version file
    is an orphan and is deleted on recovery) or the new one (new file is in
    `keep` and valid). State is always fully old or fully new.
  * Version numbers increase monotonically. Rollback points CURRENT at an
    older version but never deletes files, so no committed data is lost and
    rolling forward creates the next higher version number.
"""

import json
import os
import threading

__all__ = ["VersionedStore", "SwitchError"]


class SwitchError(Exception):
    """Raised when a switch/rollback fails (including injected failures)."""


class _Version(object):
    """An immutable committed version. `data` must never be mutated after
    the version object is published as `store._current`."""

    __slots__ = ("num", "data")

    def __init__(self, num, data):
        self.num = num
        self.data = data


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_file(path, payload_bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(payload_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on POSIX (same directory)


class VersionedStore(object):
    def __init__(self, path):
        self.path = path
        self.versions_dir = os.path.join(path, "versions")
        os.makedirs(self.versions_dir, exist_ok=True)
        self._lock = threading.RLock()  # serializes put/commit/rollback
        self._staging = {}              # uncommitted writes (next version)
        self._current = None            # _Version, swapped atomically
        self._keep = frozenset()        # version numbers in the manifest
        # Test hooks (not part of the public API):
        self.fail_at = None    # step name -> raise SwitchError at that step
        self.crash_at = None   # step name -> os._exit(1) at that step
        self._recover()

    # ------------------------------------------------------------------
    # paths / manifest / recovery
    # ------------------------------------------------------------------
    def _version_file(self, num):
        return os.path.join(self.versions_dir, "%06d.json" % num)

    def _current_file(self):
        return os.path.join(self.path, "CURRENT")

    def _write_manifest(self, current, keep):
        payload = json.dumps({"current": current, "keep": sorted(keep)},
                             sort_keys=True).encode("utf-8")
        _atomic_write_file(self._current_file(), payload)

    def _recover(self):
        """Bring in-memory state in line with durable (on-disk) state.
        Deletes only files that the manifest does not list: after a crash
        such files are orphans of a switch whose pointer swap did not
        happen. Valid rollback history is listed in `keep` and survives."""
        cur = self._current_file()
        if os.path.exists(cur):
            with open(cur, "rb") as f:
                manifest = json.loads(f.read().decode("utf-8"))
            num = manifest["current"]
            keep = frozenset(manifest["keep"])
            for name in os.listdir(self.versions_dir):
                stem, ext = os.path.splitext(name)
                full = os.path.join(self.versions_dir, name)
                if ext == ".tmp":
                    os.remove(full)
                elif ext == ".json" and stem.isdigit() and int(stem) not in keep:
                    os.remove(full)
        else:
            num, keep = 0, frozenset({0})
            self._write_manifest(num, keep)
            _fsync_dir(self.path)
        if num == 0:
            data = {}
        else:
            with open(self._version_file(num), "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
        self._keep = keep
        self._current = _Version(num, data)

    # ------------------------------------------------------------------
    # test hooks
    # ------------------------------------------------------------------
    def _maybe_fail(self, point):
        if self.fail_at == point:
            self.fail_at = None
            raise SwitchError("injected failure at step: %s" % point)

    def _maybe_crash(self, point):
        if self.crash_at == point:
            os._exit(1)  # simulate power loss: no cleanup, no finally

    # ------------------------------------------------------------------
    # reads: lock-free, bind the version pointer exactly once
    # ------------------------------------------------------------------
    def version(self):
        return self._current.num

    def get(self, key, default=None):
        v = self._current
        return v.data.get(key, default)

    def get_many(self, keys):
        """Multi-key read: all values come from the SAME version."""
        v = self._current
        return v.num, {k: v.data.get(k) for k in keys}

    def snapshot(self):
        """Consistent full read: (version_num, copy of that version's data)."""
        v = self._current
        return v.num, dict(v.data)

    # ------------------------------------------------------------------
    # writes: buffered into staging, published by switch()/commit*()
    # ------------------------------------------------------------------
    def put(self, key, value):
        with self._lock:
            self._staging[key] = value

    def put_many(self, mapping):
        with self._lock:
            self._staging.update(mapping)

    # ------------------------------------------------------------------
    # version switch (commit)
    # ------------------------------------------------------------------
    def _next_version_num(self):
        return max(self._keep) + 1

    def switch(self):
        """Commit staged writes as a new version. Atomic: readers see either
        the old version or the new one, never a mix. On failure the store
        reconciles with disk and ends in a determined state."""
        with self._lock:
            new_num = self._next_version_num()
            new_data = dict(self._current.data)
            new_data.update(self._staging)
            new_keep = self._keep | frozenset({new_num})
            try:
                self._maybe_fail("before_version_file")
                # step 1+2: durable version file
                payload = json.dumps(new_data, sort_keys=True).encode("utf-8")
                _atomic_write_file(self._version_file(new_num), payload)
                _fsync_dir(self.versions_dir)
                self._maybe_fail("after_version_file")
                self._maybe_crash("before_pointer")
                # step 3+4: atomic, durable manifest (pointer) swap
                self._write_manifest(new_num, new_keep)
                self._maybe_crash("after_pointer_rename")
                self._maybe_fail("after_pointer")
                _fsync_dir(self.path)
            except Exception:
                self._staging = {}
                self._recover()  # reconcile: determined state, never half-way
                raise
            self._current = _Version(new_num, new_data)
            self._keep = new_keep
            self._staging = {}
            return new_num

    def commit_with_builder(self, builder):
        """Atomic stage+commit: ``builder(next_version_num)`` returns the
        mapping to publish, and the next version number is observed and
        committed under a single lock acquisition (so content derived from
        the version number cannot race a concurrent rollback)."""
        with self._lock:
            self._staging.update(builder(self._next_version_num()) or {})
            return self.switch()

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------
    def rollback(self, target_num):
        """Point current version back at a previously committed version.
        History files are never deleted, so no committed data is lost and
        rolling forward again is possible. Uncommitted staged writes are
        kept."""
        with self._lock:
            if target_num not in self._keep:
                raise SwitchError("no such version: %r" % target_num)
            try:
                self._maybe_fail("rollback_before_pointer")
                self._write_manifest(target_num, self._keep)
                self._maybe_fail("rollback_after_pointer")
                _fsync_dir(self.path)
            except Exception:
                self._recover()
                raise
            self._recover()  # reload target version's data from disk
            return target_num

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    def list_versions(self):
        return sorted(v for v in self._keep if v != 0)

    def disk_version(self):
        """Version number recorded in the durable manifest pointer."""
        with open(self._current_file(), "rb") as f:
            return json.loads(f.read().decode("utf-8"))["current"]
