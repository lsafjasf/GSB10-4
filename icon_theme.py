"""icon_theme — 图标主题目录约定校验与回退链解析（仅标准库）。

目录约定：
    <root>/<theme>/index.theme            # [Icon Theme] 节，Inherits=a,b
    <root>/<theme>/<size>/<context>/<name><ext>
        size    : 16/24/32/48/64/128/256 之一，或 scalable
        context : CONTEXTS 之一（大小写敏感，必须完全匹配）
        ext     : 固定尺寸目录用 .png，scalable 目录用 .svg（大小写不敏感）

回退链（详见 README.md）：
    主题内 exact -> scalable -> nearest，然后沿 Inherits 父主题逐级重复，
    最后兜底 hicolor。每次命中记录 level，可追溯到是哪一级提供的。
"""

from __future__ import annotations

import configparser
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

FIXED_SIZES = (16, 24, 32, 48, 64, 128, 256)
SCALABLE = "scalable"
CONTEXTS = (
    "actions", "apps", "categories", "devices",
    "emblems", "emotes", "mimetypes", "places", "status",
)
RASTER_EXTS = (".png",)
SCALABLE_EXTS = (".svg",)
INDEX_FILE = "index.theme"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MIN_HEADER = 33  # 8 签名 + 4 长度 + 4 "IHDR" + 13 数据 + 4 CRC


class TraversalCounter:
    """统计目录遍历（scandir）与文件探测（stat/open）次数。"""

    def __init__(self):
        self.scandir = 0
        self.file_probe = 0

    def reset(self):
        self.scandir = 0
        self.file_probe = 0

    def __repr__(self):
        return f"TraversalCounter(scandir={self.scandir}, file_probe={self.file_probe})"


@dataclass(frozen=True)
class IconFile:
    path: str
    theme: str
    size_key: object  # int 或 "scalable"
    context: str
    name: str
    ext: str


@dataclass(frozen=True)
class Violation:
    kind: str    # 见 README「目录约定校验」
    path: str
    detail: str


@dataclass(frozen=True)
class BadFile:
    path: str
    reason: str


@dataclass(frozen=True)
class TraceEntry:
    level: str       # exact / scalable / nearest / parent:<theme>/...
    theme: str
    size_key: object
    outcome: str     # "hit" | "miss" | "bad"
    path: str


@dataclass
class LookupResult:
    name: str
    context: str
    size: int
    status: str              # "ok" | "missing" | "corrupt"
    level: str | None        # 命中级别；未命中为 None
    hit: IconFile | None
    bad_files: list          # list[BadFile]，链上遇到的坏文件（不计为命中）
    trace: list              # list[TraceEntry]，完整回退轨迹


class ThemeData:
    def __init__(self):
        self.files = {}          # (size_key, context) -> {name: [IconFile, ...]}
        self.inherits = []

    def add(self, icon):
        self.files.setdefault((icon.size_key, icon.context), {}).setdefault(icon.name, []).append(icon)

    def candidate(self, size_key, context, name):
        """确定性选取：扩展名全小写者优先，其次按扩展名、路径字典序。"""
        cands = self.files.get((size_key, context), {}).get(name)
        if not cands:
            return None
        return min(cands, key=lambda f: (f.ext != f.ext.lower(), f.ext, f.path))

    def sizes_with(self, context, name):
        return [sk for (sk, ctx), names in self.files.items()
                if ctx == context and isinstance(sk, int) and name in names]


def check_integrity(path):
    """返回 None 表示完好，否则返回坏文件原因字符串。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        try:
            with open(path, "rb") as fh:
                head = fh.read(PNG_MIN_HEADER)
        except OSError as exc:
            return f"unreadable: {exc}"
        if len(head) < PNG_MIN_HEADER:
            return f"truncated png header ({len(head)} < {PNG_MIN_HEADER} bytes)"
        if not head.startswith(PNG_SIGNATURE):
            return "bad png signature"
        if head[12:16] != b"IHDR":
            return "missing IHDR chunk"
        return None
    if ext == ".svg":
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            return f"xml parse error: {exc}"
        except OSError as exc:
            return f"unreadable: {exc}"
        if root.tag.rsplit("}", 1)[-1] != "svg":
            return f"root element is <{root.tag}>, not <svg>"
        return None
    return f"unsupported extension: {ext}"


class IconTheme:
    """扫描 root 下全部主题，建立索引，并提供回退链解析。"""

    def __init__(self, root, counter=None):
        self.root = root
        self.counter = counter or TraversalCounter()
        self.themes = {}           # theme -> ThemeData
        self.violations = []       # list[Violation]
        self._integrity_cache = {} # path -> None | reason
        self._scan()

    # ---- 索引构建（目录约定校验在扫描时同步进行） ----

    def _scandir(self, path):
        self.counter.scandir += 1
        return sorted(os.scandir(path), key=lambda e: e.name)

    def _scan(self):
        for theme_entry in self._scandir(self.root):
            if not theme_entry.is_dir():
                self.violations.append(Violation(
                    "bad-toplevel", theme_entry.path, "主题根下只允许主题目录"))
                continue
            self.themes[theme_entry.name] = self._scan_theme(theme_entry.name)

    def _scan_theme(self, theme):
        data = ThemeData()
        tdir = os.path.join(self.root, theme)
        has_index = False
        for entry in self._scandir(tdir):
            if entry.name == INDEX_FILE and entry.is_file():
                has_index = True
                data.inherits = self._parse_index(entry.path)
                continue
            if not entry.is_dir():
                self.violations.append(Violation(
                    "bad-toplevel", entry.path, "主题目录下只允许 index.theme 与尺寸目录"))
                continue
            size_key = self._parse_size_dir(entry)
            if size_key is None:
                continue
            self._scan_size_dir(data, theme, entry.path, size_key)
        if not has_index:
            self.violations.append(Violation(
                "missing-index", tdir, "缺少 index.theme"))
        self._check_ambiguous(data, theme)
        return data

    def _parse_size_dir(self, entry):
        if entry.name == SCALABLE:
            return SCALABLE
        if entry.name.isdigit():
            size = int(entry.name)
            if size in FIXED_SIZES:
                return size
            self.violations.append(Violation(
                "nonstandard-size", entry.path,
                f"尺寸 {size} 不在标准尺寸 {FIXED_SIZES} 内，已排除"))
            return None
        self.violations.append(Violation(
            "bad-size-dir", entry.path, "尺寸目录名必须是标准尺寸数字或 scalable"))
        return None

    def _scan_size_dir(self, data, theme, size_dir, size_key):
        allowed = SCALABLE_EXTS if size_key == SCALABLE else RASTER_EXTS
        for ctx_entry in self._scandir(size_dir):
            if not ctx_entry.is_dir():
                self.violations.append(Violation(
                    "file-in-size-dir", ctx_entry.path, "尺寸目录下只允许上下文目录"))
                continue
            context = ctx_entry.name
            if context not in CONTEXTS:  # 大小写敏感，Apps != apps
                self.violations.append(Violation(
                    "bad-context-dir", ctx_entry.path,
                    f"未知上下文 {context!r}（大小写敏感），已排除"))
                continue
            for f in self._scandir(ctx_entry.path):
                if not f.is_file():
                    self.violations.append(Violation(
                        "unexpected-dir", f.path, "上下文目录下只允许图标文件"))
                    continue
                stem, ext = os.path.splitext(f.name)
                if not stem:
                    self.violations.append(Violation(
                        "empty-name", f.path, "图标文件名为空"))
                    continue
                if ext.lower() not in allowed:  # 扩展名大小写不敏感
                    self.violations.append(Violation(
                        "bad-extension", f.path,
                        f"该尺寸目录只允许 {allowed}"))
                    continue
                data.add(IconFile(f.path, theme, size_key, context, stem, ext))

    def _check_ambiguous(self, data, theme):
        for (size_key, context), names in sorted(data.files.items(), key=str):
            for name, cands in sorted(names.items()):
                if len(cands) > 1:
                    self.violations.append(Violation(
                        "ambiguous-name", min(c.path for c in cands),
                        f"{theme}/{size_key}/{context}/{name} 有 "
                        f"{len(cands)} 个仅大小写/扩展名不同的候选，"
                        f"按确定性规则选取 {data.candidate(size_key, context, name).path}"))

    def _parse_index(self, path):
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
            raw = parser.get("Icon Theme", "Inherits", fallback="")
        except configparser.Error as exc:
            self.violations.append(Violation("bad-index", path, str(exc)))
            return []
        return [t.strip() for t in raw.split(",") if t.strip()]

    # ---- 回退链 ----

    def theme_chain(self, theme):
        """[请求主题, *Inherits 广度优先, hicolor]，去重且防环。"""
        if theme not in self.themes:
            return []
        order, seen, queue = [theme], {theme}, [theme]
        while queue:
            current = queue.pop(0)
            for parent in self.themes[current].inherits:
                if parent in self.themes and parent not in seen:
                    seen.add(parent)
                    order.append(parent)
                    queue.append(parent)
        if "hicolor" in self.themes and "hicolor" not in seen:
            order.append("hicolor")
        return order

    def _integrity(self, path):
        if path not in self._integrity_cache:
            self.counter.file_probe += 1
            self._integrity_cache[path] = check_integrity(path)
        return self._integrity_cache[path]

    def _try_level(self, data, theme, size_key, context, name, level, trace, bad):
        cand = data.candidate(size_key, context, name)
        if cand is None:
            trace.append(TraceEntry(level, theme, size_key, "miss", ""))
            return None
        reason = self._integrity(cand.path)
        if reason is not None:
            bad.append(BadFile(cand.path, reason))
            trace.append(TraceEntry(level, theme, size_key, "bad", cand.path))
            return None
        trace.append(TraceEntry(level, theme, size_key, "hit", cand.path))
        return cand

    def resolve(self, theme, name, context, size):
        """沿回退链解析一个图标，返回 LookupResult。"""
        trace, bad = [], []
        for depth, t in enumerate(self.theme_chain(theme)):
            data = self.themes[t]
            prefix = "theme" if depth == 0 else f"parent:{t}"
            hit = self._try_level(data, t, size, context, name,
                                  f"{prefix}/exact", trace, bad)
            if hit is None:
                hit = self._try_level(data, t, SCALABLE, context, name,
                                      f"{prefix}/scalable", trace, bad)
            if hit is None:
                # exact 已试过请求尺寸，nearest 只看其余尺寸
                sizes = [s for s in data.sizes_with(context, name) if s != size]
                if sizes:
                    # 最近尺寸：|s-req| 最小；并列时取较大者（确定规则）
                    nearest = min(sizes, key=lambda s: (abs(s - size), -s))
                    hit = self._try_level(data, t, nearest, context, name,
                                          f"{prefix}/nearest", trace, bad)
                else:
                    trace.append(TraceEntry(f"{prefix}/nearest", t, None, "miss", ""))
            if hit is not None:
                return LookupResult(name, context, size, "ok",
                                    trace[-1].level, hit, bad, trace)
        status = "corrupt" if bad else "missing"
        return LookupResult(name, context, size, status, None, None, bad, trace)

    # ---- 整树校验报告 ----

    def validate(self):
        """对索引内全部文件做完整性检查，返回 (violations, bad_files)。"""
        bad = []
        for theme in sorted(self.themes):
            data = self.themes[theme]
            for key in sorted(data.files, key=str):
                for name in sorted(data.files[key]):
                    for icon in data.files[key][name]:
                        reason = self._integrity(icon.path)
                        if reason is not None:
                            bad.append(BadFile(icon.path, reason))
        return list(self.violations), bad


def nearest_order(size):
    """naive 模式用的尺寸探测顺序：exact, scalable, 其余按最近优先。"""
    rest = sorted((s for s in FIXED_SIZES if s != size), key=lambda s: (abs(s - size), -s))
    return [size, SCALABLE] + rest


def naive_probe(root, chain, name, context, size, counter):
    """无索引的逐路径探测（基准对照用）：每次命中前要做多次 stat。"""
    for theme in chain:
        for size_key in nearest_order(size):
            exts = SCALABLE_EXTS if size_key == SCALABLE else RASTER_EXTS
            for ext in exts:
                path = os.path.join(root, theme, str(size_key), context, name + ext)
                counter.file_probe += 1
                if os.path.isfile(path):
                    return path
    return None
