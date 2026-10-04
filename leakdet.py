"""Handle/connection leak detector with acquisition-site attribution.

Standard library only. Python 3.8+.

Design
------
- Every ``acquire()`` returns a :class:`Handle` facade with a unique id and a
  refcount (starts at 1). The real state -- acquisition timestamp, explicit
  label and captured call stack -- lives in an internal Entry in the tracker;
  the tracker holds *no* strong reference to the Handle facade.
- ``release()`` decrements the refcount; when it reaches 0 the entry leaves
  the live set and the release (site + note, normal or exceptional path) is
  appended to a bounded release log.
- Releasing a handle whose entry is gone is recorded as a *double release*
  event; it never corrupts the accounting.
- If user code drops the Handle facade without releasing it, a weakref
  finalizer marks the entry *abandoned* -- a definite leak, not a suspicion.
- ``check_leaks()`` reports live entries older than a survival-time
  threshold; an ``exclude`` predicate whitelists legitimately long-lived
  handles to eliminate false positives.

Accounting invariant
--------------------
    acquired - released == len(live entries) == potential leaks
    a leak is a live entry with refcount > 0 (optionally age-filtered)

Memory bound
------------
State is O(live handles) + O(release_log_size) + O(1) counters.
Freed entries (and their stacks) are dropped, so steady-state churn of
acquire/release cycles (e.g. 1e6) uses constant memory.
"""

from __future__ import annotations

import collections
import itertools
import threading
import time
import traceback
import weakref

__all__ = ["Handle", "LeakInfo", "LeakTracker", "LeakReport"]


def _capture_site(stack_depth):
    """Capture the call stack outside of this module."""
    frames = traceback.extract_stack()
    frames = [f for f in frames if f.filename != __file__]
    frames = frames[:-1]  # drop _capture_site itself
    if stack_depth is not None:
        frames = frames[-stack_depth:]
    return frames


def _site_key(label, frames):
    if label:
        return label
    if frames:
        top = frames[-1]
        return f"{top.filename}:{top.lineno} ({top.name})"
    return "<unknown>"


def _format_stack(frames):
    return "".join(traceback.format_list(frames))


class _Entry:
    """Strongly-held state for one live acquisition (no ref to Handle)."""

    __slots__ = ("hid", "kind", "label", "frames", "acquired_at", "refcount",
                 "abandoned")

    def __init__(self, hid, kind, label, frames, acquired_at):
        self.hid = hid
        self.kind = kind
        self.label = label
        self.frames = frames
        self.acquired_at = acquired_at
        self.refcount = 1
        self.abandoned = False

    @property
    def site(self):
        return _site_key(self.label, self.frames)

    @property
    def acquire_stack(self):
        return _format_stack(self.frames) if self.frames else "<stack capture disabled>"


class LeakInfo:
    """Immutable snapshot of a leaking acquisition used in reports."""

    __slots__ = ("hid", "kind", "label", "acquired_at", "refcount", "abandoned",
                 "site", "acquire_stack")

    def __init__(self, entry):
        self.hid = entry.hid
        self.kind = entry.kind
        self.label = entry.label
        self.acquired_at = entry.acquired_at
        self.refcount = entry.refcount
        self.abandoned = entry.abandoned
        self.site = entry.site
        self.acquire_stack = entry.acquire_stack


class Handle:
    """Facade for one tracked handle/connection.

    The tracker only keeps weak references to Handle objects, so dropping
    one without release() triggers abandonment detection.
    """

    __slots__ = ("_tracker", "hid", "_finalizer", "__weakref__")

    def __init__(self, tracker, hid):
        self._tracker = tracker
        self.hid = hid
        self._finalizer = None

    def _entry(self):
        return self._tracker._state.get(self.hid)

    @property
    def kind(self):
        e = self._entry()
        return e.kind if e else "<released>"

    @property
    def label(self):
        e = self._entry()
        return e.label if e else None

    @property
    def refcount(self):
        e = self._entry()
        return e.refcount if e else 0

    @property
    def site(self):
        e = self._entry()
        return e.site if e else "<released>"

    def retain(self):
        """Increment the refcount (an extra logical owner)."""
        self._tracker.retain(self)
        return self

    def release(self, note=None):
        """Decrement the refcount; frees the handle when it reaches 0."""
        return self._tracker.release(self, note=note)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Releases on the exceptional path too; the exception propagates.
        self.release(note="ctx-exit" if exc_type is None else f"ctx-exit:{exc_type.__name__}")
        return False

    def __repr__(self):
        e = self._entry()
        if e is None:
            return f"<Handle #{self.hid} released>"
        return f"<Handle #{self.hid} {e.kind} site={e.site} ref={e.refcount}>"


class LeakReport:
    """Result of LeakTracker.check_leaks()."""

    def __init__(self, leaks, abandoned, double_releases, accounting):
        self.leaks = leaks                      # list[LeakInfo]
        self.abandoned = abandoned              # list[LeakInfo] (definite leaks)
        self.double_releases = double_releases  # list of event dicts
        self.accounting = accounting            # dict

    def by_site(self):
        """Group leaking handles by acquisition site."""
        groups = collections.defaultdict(list)
        for info in self.leaks:
            groups[info.site].append(info)
        return dict(groups)

    def format(self, max_stacks=3):
        lines = []
        acc = self.accounting
        lines.append("=== Leak Report ===")
        lines.append(
            "accounting: acquired={acquired} released={released} "
            "live={live} (acquired-released={diff}) double_releases={dbl} abandoned={ab}".format(
                acquired=acc["acquired"], released=acc["released"], live=acc["live"],
                diff=acc["acquired"] - acc["released"], dbl=acc["double_releases"],
                ab=acc["abandoned"],
            )
        )
        lines.append(f"leaks: {len(self.leaks)} handle(s), grouped by acquisition site:")
        for site, infos in sorted(self.by_site().items(), key=lambda kv: -len(kv[1])):
            ages = [time.monotonic() - i.acquired_at for i in infos]
            tag = " [abandoned]" if infos[0].abandoned else ""
            lines.append(
                f"  [{len(infos)}x{tag}] {site} "
                f"(ids={[i.hid for i in infos]}, max_age={max(ages):.3f}s)"
            )
            for info in infos[:max_stacks]:
                lines.append(f"    --- acquire stack for handle #{info.hid} ---")
                for ln in info.acquire_stack.rstrip().splitlines():
                    lines.append(f"    {ln}")
        if self.abandoned:
            lines.append(
                "definite leaks (handle object GC'd without release): "
                f"{[i.hid for i in self.abandoned]}"
            )
        if self.double_releases:
            lines.append(f"double-release events: {len(self.double_releases)}")
            for ev in self.double_releases[:max_stacks]:
                lines.append(f"    hid={ev['hid']} at {ev['site']} note={ev['note']}")
        if not self.leaks and not self.double_releases:
            lines.append("no leaks, no double releases: OK")
        return "\n".join(lines)


class LeakTracker:
    def __init__(self, stack_depth=8, capture_stacks=True, release_log_size=1000,
                 event_log_size=1000):
        self.stack_depth = stack_depth
        self.capture_stacks = capture_stacks
        self._lock = threading.Lock()
        self._state = {}  # hid -> _Entry (strong state, weak facades)
        self._ids = itertools.count(1)
        self._acquired = 0
        self._released = 0
        # Bounded audit logs: memory stays O(log_size), not O(total ops).
        self._release_log = collections.deque(maxlen=release_log_size)
        self._double_release_log = collections.deque(maxlen=event_log_size)

    # -- acquisition ------------------------------------------------------

    def acquire(self, kind="handle", label=None):
        """Acquire a tracked handle. The call site is recorded for attribution."""
        frames = _capture_site(self.stack_depth) if self.capture_stacks else []
        with self._lock:
            hid = next(self._ids)
            self._acquired += 1
            self._state[hid] = _Entry(hid, kind, label, frames, time.monotonic())
        h = Handle(self, hid)
        # If the Handle facade is GC'd while the entry is live -> definite leak.
        h._finalizer = weakref.finalize(h, self._on_abandoned, hid)
        return h

    # -- release ----------------------------------------------------------

    def release(self, handle, note=None):
        """Release one reference. Returns True if the handle was freed."""
        hid = handle.hid
        with self._lock:
            entry = self._state.get(hid)
            if entry is None or entry.refcount == 0:
                self._double_release_log.append({
                    "hid": hid,
                    "site": _site_key(None, _capture_site(4)) if self.capture_stacks else "?",
                    "note": note,
                    "time": time.monotonic(),
                })
                return False
            entry.refcount -= 1
            if entry.refcount > 0:
                return False
            del self._state[hid]
            self._released += 1
            self._release_log.append({
                "hid": hid,
                "kind": entry.kind,
                "site": entry.site,
                "note": note,
                "held_for": time.monotonic() - entry.acquired_at,
            })
        if handle._finalizer is not None:
            handle._finalizer.detach()
            handle._finalizer = None
        return True

    def retain(self, handle):
        with self._lock:
            entry = self._state.get(handle.hid)
            if entry is None:
                raise ValueError(f"cannot retain dead handle #{handle.hid}")
            entry.refcount += 1

    # -- introspection ----------------------------------------------------

    def _on_abandoned(self, hid):
        with self._lock:
            entry = self._state.get(hid)
            if entry is not None:
                entry.abandoned = True

    def accounting(self):
        with self._lock:
            return {
                "acquired": self._acquired,
                "released": self._released,
                "live": len(self._state),
                "double_releases": len(self._double_release_log),
                "abandoned": sum(1 for e in self._state.values() if e.abandoned),
                "balanced": self._acquired - self._released == len(self._state),
            }

    def release_log(self):
        with self._lock:
            return list(self._release_log)

    def check_leaks(self, max_age=None, exclude=None, now=None):
        """Return a LeakReport for live handles.

        max_age: only handles alive longer than this many seconds count as
                 leaks (survival-time threshold). None -> every live handle.
        exclude: optional predicate(LeakInfo) -> True to whitelist a handle
                 (false-positive exclusion, e.g. a known long-lived pool).
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            entries = list(self._state.values())
            dbl = list(self._double_release_log)
            acquired = self._acquired
            released = self._released
        abandoned, leaks = [], []
        for entry in entries:
            if entry.refcount <= 0:
                continue
            info = LeakInfo(entry)
            if entry.abandoned:
                abandoned.append(info)
            if max_age is not None and (now - entry.acquired_at) < max_age:
                continue
            if exclude is not None and exclude(info):
                continue
            leaks.append(info)
        acc = {
            "acquired": acquired,
            "released": released,
            "live": len(entries),
            "double_releases": len(dbl),
            "abandoned": len(abandoned),
        }
        return LeakReport(leaks, abandoned, dbl, acc)

    def reset(self):
        with self._lock:
            self._state.clear()
            self._release_log.clear()
            self._double_release_log.clear()
            self._acquired = 0
            self._released = 0
