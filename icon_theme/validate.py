"""Theme directory-convention validation and per-icon resolution reports.

Missing files and corrupt files are reported separately:
  * missing  -> no valid file anywhere in the chain (Resolution.status)
  * corrupt  -> file exists but fails the signature check (never a hit)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .theme import (
    DirCache,
    Resolver,
    FIXED_EXTS,
    SCALABLE_EXTS,
    SIZE_DIR_RE,
    check_icon_file,
    read_index,
    _candidate_key,
)

STANDARD_CONTEXTS = frozenset({
    "actions", "animations", "apps", "categories", "devices", "emblems",
    "emotes", "intl", "mimetypes", "places", "status",
})

SIZE_DIR_LOOSE_RE = re.compile(r"^(\d+)[xX](\d+)$")


@dataclass
class Problem:
    severity: str  # 'error' | 'warning' | 'info'
    path: str
    message: str

    def __str__(self):
        return f"[{self.severity}] {self.path}: {self.message}"


@dataclass
class CorruptEntry:
    path: str
    reason: str


@dataclass
class ThemeReport:
    root: str
    problems: list = field(default_factory=list)
    corrupt: list = field(default_factory=list)
    size_dirs: list = field(default_factory=list)
    icon_files: int = 0
    dirs_scanned: int = 0

    def ok(self):
        return not self.errors() and not self.corrupt

    def errors(self):
        return [p for p in self.problems if p.severity == "error"]

    def warnings(self):
        return [p for p in self.problems if p.severity == "warning"]

    def infos(self):
        return [p for p in self.problems if p.severity == "info"]


def validate_theme(root, cache=None):
    """Validate directory conventions of one theme; return a ThemeReport."""
    cache = cache if cache is not None else DirCache()
    root = os.path.normpath(root)
    report = ThemeReport(root=root)
    entries = cache.listdir(root)
    if entries is None:
        report.problems.append(
            Problem("error", root, "theme root is not a readable directory"))
        return report
    names = {n for n, _ in entries}
    if "index.theme" not in names:
        report.problems.append(Problem(
            "error", os.path.join(root, "index.theme"), "index.theme missing"))
    else:
        index = read_index(root)
        if not index["name"]:
            report.problems.append(Problem(
                "warning", os.path.join(root, "index.theme"),
                "index.theme has no Name= in [Icon Theme]"))
        search_path = os.path.dirname(root)
        for parent in index["inherits"]:
            if parent == "hicolor":
                continue
            if not os.path.isdir(os.path.join(search_path, parent)):
                report.problems.append(Problem(
                    "warning", os.path.join(search_path, parent),
                    f"inherited theme '{parent}' not found in search path"))
    for name, is_dir in sorted(entries):
        path = os.path.join(root, name)
        if is_dir:
            m = SIZE_DIR_RE.match(name)
            if m:
                report.size_dirs.append(int(m.group(1)))
                _validate_contexts(cache, path, report, FIXED_EXTS)
            elif name == "scalable":
                _validate_contexts(cache, path, report, SCALABLE_EXTS)
            else:
                loose = SIZE_DIR_LOOSE_RE.match(name)
                if loose and loose.group(1) == loose.group(2):
                    report.problems.append(Problem(
                        "error", path,
                        f"size directory must be lowercase "
                        f"'{loose.group(1)}x{loose.group(1)}'"))
                elif name.lower() == "scalable":
                    report.problems.append(Problem(
                        "error", path,
                        "scalable directory must be lowercase 'scalable'"))
                else:
                    report.problems.append(
                        Problem("warning", path, "unknown top-level directory"))
        else:
            if name == "index.theme":
                continue
            _validate_icon_file(path, report, FIXED_EXTS)
    report.size_dirs.sort()
    report.dirs_scanned = cache.scandir_count
    return report


def _validate_contexts(cache, dirpath, report, exts):
    for name, is_dir in sorted(cache.listdir(dirpath) or []):
        path = os.path.join(dirpath, name)
        if not is_dir:
            report.problems.append(Problem(
                "warning", path,
                "stray file inside size/scalable directory "
                "(icons belong in a context subdirectory)"))
            continue
        if name in STANDARD_CONTEXTS:
            pass
        elif name.lower() in STANDARD_CONTEXTS:
            report.problems.append(Problem(
                "error", path,
                f"context directory case mismatch, expected '{name.lower()}'"))
        else:
            report.problems.append(
                Problem("info", path, "non-standard context directory"))
        _validate_icon_dir(cache, path, report, exts)


def _validate_icon_dir(cache, dirpath, report, exts):
    by_stem = {}
    for name, is_dir in sorted(cache.listdir(dirpath) or []):
        path = os.path.join(dirpath, name)
        if is_dir:
            report.problems.append(Problem(
                "warning", path, "nested directory inside context directory"))
            continue
        _validate_icon_file(path, report, exts)
        stem, dot, _ = name.rpartition(".")
        if dot and stem:
            by_stem.setdefault(stem, []).append(name)
    for stem, files in sorted(by_stem.items()):
        if len(files) > 1:
            winner = sorted(files, key=_candidate_key)[0]
            report.problems.append(Problem(
                "info", os.path.join(dirpath, stem),
                f"{len(files)} files for one icon; resolution picks "
                f"'{winner}' (png>svg>xpm, lowercase ext first)"))


def _validate_icon_file(path, report, exts):
    report.icon_files += 1
    name = os.path.basename(path)
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        report.problems.append(
            Problem("warning", path, "not an icon file name"))
        return
    ext_lower = ext.lower()
    if ext_lower not in FIXED_EXTS:
        report.problems.append(
            Problem("error", path, f"unsupported extension '.{ext}'"))
        return
    if ext_lower not in exts:
        report.problems.append(Problem(
            "warning", path,
            f"extension '.{ext}' never matches at this level"))
    if ext != ext_lower:
        report.problems.append(Problem(
            "info", path,
            "non-lowercase extension; matches only via case-insensitive rule"))
    reason = check_icon_file(path)
    if reason:
        report.corrupt.append(CorruptEntry(path, reason))


def validate_icons(theme_root, queries, cache=None):
    """Resolve (name, size, context) queries; return (resolutions, summary).

    The summary keeps missing-without-corrupt and missing-with-only-corrupt
    in separate buckets so bad files are never confused with plain misses.
    """
    cache = cache if cache is not None else DirCache()
    resolver = Resolver(theme_root, cache=cache)
    results = [resolver.resolve(n, s, c) for n, s, c in queries]
    summary = {
        "total": len(results),
        "hits": sum(1 for r in results if r.status == "hit"),
        "missing": sum(1 for r in results if r.status == "missing"),
        "missing_clean": sum(1 for r in results
                             if r.status == "missing" and not r.corrupt),
        "missing_corrupt_only": sum(1 for r in results
                                    if r.status == "missing" and r.corrupt),
        "corrupt_files_seen": sum(len(r.corrupt) for r in results),
        "dirs_scanned": cache.scandir_count,
    }
    return results, summary
