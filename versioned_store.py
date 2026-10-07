"""Versioned KV store with atomic version switch and rollback.

Consistency model (stdlib only, CPython 3.8+):

- Atomic switch, no read-write mixed window. The "current version" is a
  single immutable tuple ``(version_id, data)`` stored in one attribute.
  Readers load that attribute exactly once per ``view()``/``get()`` call.
  CPython guarantees atomicity of a single attribute load/store (GIL),
  so a reader always sees either the complete old version or the complete
  new version -- never a mixture.
- Writers are serialized by a lock; a commit builds the new version
  off to the side and only publishes it with one atomic tuple swap.
- Durability: the on-disk commit point is ``os.replace("CURRENT")``
  (atomic rename, POSIX). Before that point a crash leaves the OLD
  version durable; after it the NEW version is durable. There is no
  intermediate on-disk state, so recovery is always deterministic.
- Rollback is the same atomic switch to a previously committed version.
  Old versions are retained, so rollback never loses data and you can
  roll forward again.
"""

from __future__ import annotations

import json
import os
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

# Injectable failure points, in commit order. Used by tests to prove that
# every failure leaves the store in a deterministic state.
FAIL_POINTS = (
    "before_persist",   # staging built, nothing written yet
    "before_journal",   # version file durable, journal not appended
    "before_rename",    # journal appended, CURRENT not yet replaced
    "before_swap",      # CURRENT replaced (durable commit done), memory not swapped
)


class CommitError(RuntimeError):
    """Raised when a commit/switch fails (including injected failures)."""


class VersionNotFound(KeyError):
    pass


class ReadView:
    """An immutable, consistent snapshot of one version.

    Every item in a ReadView is guaranteed to come from the same version:
    the view is built from a single atomic load of the current pointer.
    """

    __slots__ = ("version_id", "_data")

    def __init__(self, version_id: Optional[int], data: Mapping[str, Any]):
        self.version_id = version_id
        self._data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def items(self):
        return self._data.items()

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)


class VersionedStore:
    def __init__(self, path: Optional[str] = None):
        """path=None -> pure in-memory store; otherwise durable under `path`."""
        self._wlock = threading.Lock()          # serializes writers only
        self._versions: Dict[int, Mapping[str, Any]] = {}   # id -> immutable data (cache)
        self._history: List[int] = []           # activation order of version ids
        self._current: Tuple[Optional[int], Mapping[str, Any]] = (
            None, MappingProxyType({}))
        self._next_id = 1
        self._path = path
        self._fail_at: Optional[str] = None     # test hook, see FAIL_POINTS
        if path:
            os.makedirs(os.path.join(path, "versions"), exist_ok=True)
            self._recover()

    # ------------------------------------------------------------------ reads
    # Read path takes no lock: one atomic attribute load of the tuple.
    def view(self) -> ReadView:
        vid, data = self._current
        return ReadView(vid, data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._current[1].get(key, default)

    @property
    def current_version(self) -> Optional[int]:
        return self._current[0]

    @property
    def history(self) -> List[int]:
        return list(self._history)

    def list_versions(self) -> List[int]:
        ids = set(self._versions)
        if self._path:
            vdir = os.path.join(self._path, "versions")
            for name in os.listdir(vdir):
                if name.endswith(".json"):
                    ids.add(int(name[:-5]))
        return sorted(ids)

    # ----------------------------------------------------------------- writes
    def commit(self, mutator: Callable[[Mapping[str, Any], int], Mapping[str, Any]]) -> int:
        """Build a new version via mutator(old_data, new_id) and atomically
        switch to it. Returns the new version id."""
        with self._wlock:
            new_id = self._next_id
            new_data = dict(mutator(self._current[1], new_id))
            self._activate(new_id, new_data, is_new=True)
            return new_id

    def switch_to(self, version_id: int) -> int:
        """Atomically switch to an existing version (roll forward/backward)."""
        with self._wlock:
            data = self._version_data(version_id)
            self._activate(version_id, dict(data), is_new=False)
            return version_id

    def rollback(self, target: Optional[int] = None) -> int:
        """Roll back to `target` (default: the previously active version).

        Old versions are retained, so no data is lost; switch_to() can move
        forward again afterwards."""
        with self._wlock:
            if target is None:
                current = self._current[0]
                previous = [v for v in self._history if v != current]
                if not previous:
                    raise VersionNotFound("no earlier version to roll back to")
                target = previous[-1]
            data = self._version_data(target)
            self._activate(target, dict(data), is_new=False)
            return target

    # ------------------------------------------------------------------ core
    def _activate(self, vid: int, data: Dict[str, Any], is_new: bool) -> None:
        """Commit pipeline. Durable commit point = os.replace(CURRENT);
        in-memory commit point = the single tuple swap at the end."""
        self._maybe_fail("before_persist", vid)
        if self._path and is_new:
            self._persist_version(vid, data)
        self._maybe_fail("before_journal", vid)
        if self._path:
            self._append_journal({"op": "commit" if is_new else "activate",
                                  "id": vid, "ts": time.time()})
        self._maybe_fail("before_rename", vid)
        if self._path:
            self._persist_current(vid)          # durable atomic commit point
        self._maybe_fail("before_swap", vid)
        frozen = MappingProxyType(dict(data))
        self._versions[vid] = frozen
        self._current = (vid, frozen)           # in-memory atomic commit point
        self._history.append(vid)
        if is_new and vid >= self._next_id:
            self._next_id = vid + 1

    def _version_data(self, vid: int) -> Mapping[str, Any]:
        if vid in self._versions:
            return self._versions[vid]
        if self._path:
            return MappingProxyType(self._load_version(vid))
        raise VersionNotFound(vid)

    # ------------------------------------------------------------ persistence
    def _persist_version(self, vid: int, data: Dict[str, Any]) -> None:
        vdir = os.path.join(self._path, "versions")
        tmp = os.path.join(vdir, f".{vid}.tmp")
        final = os.path.join(vdir, f"{vid}.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)

    def _load_version(self, vid: int) -> Dict[str, Any]:
        final = os.path.join(self._path, "versions", f"{vid}.json")
        try:
            with open(final, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            raise VersionNotFound(vid) from None

    def _persist_current(self, vid: int) -> None:
        tmp = os.path.join(self._path, ".CURRENT.tmp")
        final = os.path.join(self._path, "CURRENT")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(vid))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)                  # atomic on POSIX

    def _append_journal(self, record: Dict[str, Any]) -> None:
        with open(os.path.join(self._path, "journal.log"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _read_current(self) -> Optional[int]:
        try:
            with open(os.path.join(self._path, "CURRENT"), "r",
                      encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _recover(self) -> None:
        """Deterministic recovery: CURRENT is the only source of truth.
        Whatever it names is a fully committed version; anything not named
        by it never happened from the reader's perspective."""
        current = self._read_current()
        max_id = 0
        vdir = os.path.join(self._path, "versions")
        for name in os.listdir(vdir):
            if name.endswith(".json"):
                max_id = max(max_id, int(name[:-5]))
        if current is not None:
            data = self._load_version(current)
            frozen = MappingProxyType(data)
            self._versions[current] = frozen
            self._current = (current, frozen)
            self._history.append(current)
        self._next_id = max(max_id + 1, (current or 0) + 1, 1)

    # ------------------------------------------------------------- test hooks
    def _maybe_fail(self, point: str, vid: Optional[int] = None) -> None:
        if self._fail_at == point:
            raise CommitError(f"injected failure at {point}")
        if os.environ.get("VS_KILL_AT") == point and \
                os.environ.get("VS_KILL_VID", str(vid)) == str(vid):
            os._exit(42)  # simulate process crash mid-commit
