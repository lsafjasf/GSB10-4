"""Icon theme directory conventions and fallback-chain resolution (stdlib only).

Chain stages, in order, for a query (name, size, context):
  main       the requested theme
  inherited  themes from index.theme "Inherits=", in declared order
  hicolor    sibling theme named "hicolor", implicit last resort

Levels inside every stage:
  L1 exact-size    <theme>/<size>x<size>/<context>/<name>.<ext>
  L2 larger-size   size dirs larger than requested, ascending
  L3 smaller-size  size dirs smaller than requested, descending
  L4 scalable      <theme>/scalable/<context>/<name>.svg
  L5 unthemed      <theme>/<name>.<ext>

Deterministic rules:
  * directory names (theme, size, context) match case-sensitively
  * icon stem matches case-sensitively, extension case-insensitively
  * candidate order inside one directory: extension priority png > svg > xpm,
    then all-lowercase extension before mixed/upper-case, then filename asc
  * a corrupt file is never a hit; it is recorded and the chain continues
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

FIXED_EXTS = ("png", "svg", "xpm")
SCALABLE_EXTS = ("svg",)
PNG_SIG = b"\x89PNG\r\n\x1a\n"
SIZE_DIR_RE = re.compile(r"^(\d+)x\1$")


class DirCache:
    """Caches directory listings and counts real directory traversals."""

    def __init__(self):
        self._entries = {}
        self.scandir_count = 0

    def listdir(self, path):
        """Return sorted [(name, is_dir), ...] or None if unreadable."""
        if path not in self._entries:
            self.scandir_count += 1
            try:
                with os.scandir(path) as it:
                    self._entries[path] = sorted(
                        ((e.name, e.is_dir(follow_symlinks=False)) for e in it),
                        key=lambda item: item[0],
                    )
            except OSError:
                self._entries[path] = None
        return self._entries[path]


def check_icon_file(path):
    """Return None if the icon file looks valid, else a reason string."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError as exc:
        return f"unreadable: {exc.strerror or exc}"
    if not head:
        return "empty file"
    ext = path.rpartition(".")[2].lower()
    if ext == "png":
        if not head.startswith(PNG_SIG):
            return "bad PNG signature"
        if head[12:16] != b"IHDR":
            return "PNG missing IHDR chunk"
    elif ext == "svg":
        if not (head.lstrip()[:1] == b"<" and b"<svg" in head):
            return "not an SVG document"
    elif ext == "xpm":
        if b"XPM" not in head:
            return "not an XPM file"
    return None


def read_index(root):
    """Parse index.theme; return {"name": str|None, "inherits": [str]}."""
    result = {"name": None, "inherits": []}
    try:
        with open(os.path.join(root, "index.theme"), "r",
                  encoding="utf-8", errors="replace") as fh:
            section = None
            for raw in fh:
                line = raw.strip()
                if not line or line[0] in "#;":
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].strip()
                    continue
                if section != "Icon Theme":
                    continue
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                if key == "Name":
                    result["name"] = value.strip()
                elif key == "Inherits":
                    result["inherits"] = [v.strip() for v in value.split(",")
                                          if v.strip()]
    except OSError:
        pass
    return result


@dataclass
class Query:
    name: str
    size: int
    context: str


@dataclass
class Hit:
    stage: str
    theme: str
    level: int
    level_name: str
    path: str
    size_dir: str | None

    def describe(self):
        sized = f" [{self.size_dir}]" if self.size_dir else ""
        return (f"stage={self.stage} L{self.level} {self.level_name}{sized} "
                f"theme={self.theme} path={self.path}")


@dataclass
class CorruptFile:
    stage: str
    theme: str
    level: int
    level_name: str
    path: str
    reason: str


@dataclass
class ChainEntry:
    theme: str
    stage: str
    level: int
    level_name: str
    detail: str
    outcome: str  # 'hit' | 'miss' | 'corrupt' | 'skip'

    def __str__(self):
        return (f"[{self.stage}/L{self.level} {self.level_name}] "
                f"theme={self.theme} {self.outcome}: {self.detail}")


@dataclass
class Resolution:
    query: Query
    status: str  # 'hit' | 'missing'
    hit: Hit | None
    corrupt: list = field(default_factory=list)
    chain: list = field(default_factory=list)


def _candidate_key(filename):
    _, _, ext = filename.rpartition(".")
    ext_lower = ext.lower()
    try:
        rank = FIXED_EXTS.index(ext_lower)
    except ValueError:
        rank = len(FIXED_EXTS)
    return (rank, ext != ext_lower, filename)


class Resolver:
    """Resolves icon queries against one theme plus its fallback chain."""

    def __init__(self, theme_root, cache=None):
        self.cache = cache if cache is not None else DirCache()
        self.theme_root = os.path.normpath(theme_root)
        self.search_path = os.path.dirname(self.theme_root)

    def resolve(self, name, size, context):
        query = Query(name, int(size), context)
        corrupt, chain, visited = [], [], set()
        hit = self._resolve_in_theme(self.theme_root, query, corrupt, chain,
                                     visited, "main")
        if hit is None:
            hicolor = os.path.join(self.search_path, "hicolor")
            hit = self._resolve_in_theme(hicolor, query, corrupt, chain,
                                         visited, "hicolor")
        return Resolution(query, "hit" if hit else "missing",
                          hit, corrupt, chain)

    def _resolve_in_theme(self, root, query, corrupt, chain, visited, stage):
        root = os.path.normpath(root)
        if root in visited:
            return None
        visited.add(root)
        theme = os.path.basename(root)
        entries = self.cache.listdir(root)
        if entries is None:
            chain.append(ChainEntry(theme, stage, 0, "-", root,
                                    "skip: theme directory unreadable"))
            return None
        sizes = sorted(
            int(m.group(1))
            for name, is_dir in entries
            if is_dir for m in [SIZE_DIR_RE.match(name)] if m
        )
        for s in [query.size] if query.size in sizes else []:
            hit = self._try_size_dir(root, theme, stage, 1, "exact-size",
                                     s, query, corrupt, chain)
            if hit:
                return hit
        for s in [s for s in sizes if s > query.size]:
            hit = self._try_size_dir(root, theme, stage, 2, "larger-size",
                                     s, query, corrupt, chain)
            if hit:
                return hit
        for s in sorted((s for s in sizes if s < query.size), reverse=True):
            hit = self._try_size_dir(root, theme, stage, 3, "smaller-size",
                                     s, query, corrupt, chain)
            if hit:
                return hit
        hit = self._try_dir(os.path.join(root, "scalable", query.context),
                            theme, stage, 4, "scalable", None, query,
                            SCALABLE_EXTS, corrupt, chain)
        if hit:
            return hit
        hit = self._try_dir(root, theme, stage, 5, "unthemed", None, query,
                            FIXED_EXTS, corrupt, chain)
        if hit:
            return hit
        for parent in read_index(root)["inherits"]:
            if parent == "hicolor":
                continue
            hit = self._resolve_in_theme(
                os.path.join(self.search_path, parent),
                query, corrupt, chain, visited, "inherited")
            if hit:
                return hit
        return None

    def _try_size_dir(self, root, theme, stage, level, level_name, s,
                      query, corrupt, chain):
        size_dir = f"{s}x{s}"
        dirpath = os.path.join(root, size_dir, query.context)
        return self._try_dir(dirpath, theme, stage, level, level_name,
                             size_dir, query, FIXED_EXTS, corrupt, chain)

    def _try_dir(self, dirpath, theme, stage, level, level_name, size_dir,
                 query, exts, corrupt, chain):
        entries = self.cache.listdir(dirpath)
        if entries is None:
            chain.append(ChainEntry(theme, stage, level, level_name, dirpath,
                                    "miss: no such directory"))
            return None
        candidates = []
        for fname, is_dir in entries:
            if is_dir:
                continue
            stem, dot, ext = fname.rpartition(".")
            if dot and stem == query.name and ext.lower() in exts:
                candidates.append(fname)
        if not candidates:
            chain.append(ChainEntry(theme, stage, level, level_name, dirpath,
                                    "miss: no matching file"))
            return None
        candidates.sort(key=_candidate_key)
        for fname in candidates:
            path = os.path.join(dirpath, fname)
            reason = check_icon_file(path)
            if reason is None:
                chain.append(ChainEntry(theme, stage, level, level_name,
                                        path, f"hit: {fname}"))
                return Hit(stage, theme, level, level_name, path, size_dir)
            corrupt.append(CorruptFile(stage, theme, level, level_name,
                                       path, reason))
            chain.append(ChainEntry(theme, stage, level, level_name, path,
                                    f"corrupt: {fname}: {reason}"))
        return None
