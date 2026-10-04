"""playlist_dedup: 按真实文件判定对播放列表路径做解析与去重(仅标准库)。

去重规则(判定依据)
==================
1. 相对路径必须携带声明的基准目录 base;base 缺失/为空/为相对路径时
   明确拒绝(BASE_MISSING / BASE_INVALID),绝不退化为当前工作目录。
2. 词法规范化:折叠 "."、重复分隔符,并按 POSIX 语义处理 ".."——
   即先展开符号链接、再在物理路径上回退(与内核一致),而不是纯字符串回退。
3. 符号链接:逐分量解析,base 自身与路径中间的符号链接都会被展开;
   同一文件经链接目录的不同写法收敛到同一真实路径。
4. 规范形式 = 解析后的真实绝对路径(realpath);展示用键在 Windows 上
   再经 os.path.normcase(小写化 + 分隔符统一),POSIX 保持原样。
5. 同一真实文件的最终判定键 = (st_dev, st_ino) 设备号+inode:
   - 因此硬链接、任意符号链接写法、./ ../ 冗余写法都判为同一文件;
   - 大小写:在大小写不敏感的文件系统上,不同大小写写法解析到同一
     inode,自然判重;在大小写敏感的文件系统上,大小写不同即不同文件
     (若不存在则报 NOT_FOUND),与文件系统真实行为一致。
6. 去重保持首次出现顺序;重复项完整保留并记录 merged_into(首条索引)。
7. 无法解析的条目不丢:按 NOT_FOUND / PERMISSION_DENIED / SYMLINK_LOOP /
   NOT_A_DIRECTORY / BASE_* / INVALID_PATH 分类记录,不参与合并。
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "MAX_SYMLINK_HOPS",
    "ERROR_CODES",
    "PathError",
    "UniqueEntry",
    "DuplicateEntry",
    "DedupResult",
    "Resolver",
    "dedup_entries",
    "naive_string_dedup",
]

# 与 Linux 内核一致:单次解析最多跟随 40 层符号链接,超出判为成环。
MAX_SYMLINK_HOPS = 40

ERROR_CODES = (
    "BASE_MISSING",       # 相对路径未声明基准目录
    "BASE_INVALID",       # 基准目录不是绝对路径
    "BASE_NOT_FOUND",     # 基准目录不存在
    "BASE_PERMISSION",    # 基准目录无权限
    "BASE_NOT_A_DIRECTORY",  # 基准路径存在但不是目录
    "NOT_FOUND",          # 路径(或其父级)不存在 / 悬空符号链接
    "PERMISSION_DENIED",  # 路径任一分量无访问权限
    "SYMLINK_LOOP",       # 符号链接成环(或嵌套超过 MAX_SYMLINK_HOPS)
    "NOT_A_DIRECTORY",    # 中间分量不是目录(如 file/child)
    "INVALID_PATH",       # 空路径等非法输入
)

_STAGE_PREFIX = {
    "base": {
        "NOT_FOUND": "BASE_NOT_FOUND",
        "PERMISSION_DENIED": "BASE_PERMISSION",
        "NOT_A_DIRECTORY": "BASE_NOT_A_DIRECTORY",
    },
}


class PathError(Exception):
    """分类后的路径解析错误。code 取值见 ERROR_CODES。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"PathError({self.code!r}, {self.message!r})"


@dataclass
class UniqueEntry:
    """去重后保留的首现条目。"""

    index: int
    path: str
    base: Optional[str]
    real_path: str
    file_id: Tuple[int, int]  # (st_dev, st_ino)


@dataclass
class DuplicateEntry:
    """被合并的重复条目,信息不丢。"""

    index: int
    path: str
    base: Optional[str]
    real_path: str
    merged_into: int          # 首现条目的原始索引
    merged_into_path: str     # 首现条目的原始 path 字符串


@dataclass
class ErrorRecord:
    """解析失败条目,按 code 分类。"""

    index: int
    path: str
    base: Optional[str]
    code: str
    message: str


@dataclass
class DedupResult:
    unique: List[UniqueEntry] = field(default_factory=list)
    duplicates: List[DuplicateEntry] = field(default_factory=list)
    errors: List[ErrorRecord] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.unique) + len(self.duplicates) + len(self.errors)

    def error_summary(self) -> Dict[str, int]:
        summary: Dict[str, int] = {}
        for record in self.errors:
            summary[record.code] = summary.get(record.code, 0) + 1
        return summary

    def to_dict(self) -> dict:
        return {
            "unique": [vars(item) for item in self.unique],
            "duplicates": [vars(item) for item in self.duplicates],
            "errors": [vars(item) for item in self.errors],
            "error_summary": self.error_summary(),
        }


class Resolver:
    """路径解析器:词法规范化 + 逐分量符号链接解析 + 分类报错。

    内部对目录分量做缓存,一次 dedup_entries 调用内复用,
    使 20 万条量级的解析耗时接近线性、系统调用数接近去重后的目录数。
    """

    def __init__(self, max_hops: int = MAX_SYMLINK_HOPS):
        self.max_hops = max_hops
        # (分量元组) -> (kind, st_dev, st_ino, link_target)
        self._inspect_cache: Dict[Tuple[str, ...], Tuple[str, int, int, Optional[str]]] = {}
        # base 字符串 -> 解析后的 base 分量元组
        self._base_cache: Dict[str, Tuple[str, ...]] = {}

    # ---- 词法层 ----------------------------------------------------------

    @staticmethod
    def _split(path: str) -> List[str]:
        """按平台分隔符切分,丢弃空段与 '.',保留 '..'。

        POSIX 下反斜杠是合法文件名字符,不做转换;Windows 下 '/' 与 '\\' 都算。
        """
        if os.altsep:
            path = path.replace(os.altsep, os.sep)
        return [token for token in path.split(os.sep)
                if token not in ("", ".")]

    @staticmethod
    def _join(tokens: Tuple[str, ...]) -> str:
        if not tokens:
            return os.sep
        return os.sep + os.sep.join(tokens)

    @staticmethod
    def _classify_os_error(exc: OSError, what: str) -> PathError:
        if exc.errno == errno.ENOTDIR:
            return PathError("NOT_A_DIRECTORY", f"{what} 中间分量不是目录: {exc}")
        if exc.errno == errno.ENOENT:
            return PathError("NOT_FOUND", f"{what} 不存在: {exc}")
        if exc.errno in (errno.EACCES, errno.EPERM):
            return PathError("PERMISSION_DENIED", f"{what} 权限不足: {exc}")
        if exc.errno == errno.ELOOP:
            return PathError("SYMLINK_LOOP", f"{what} 符号链接成环: {exc}")
        return PathError("NOT_FOUND", f"{what} 无法访问: {exc}")

    # ---- 物理层 ----------------------------------------------------------

    def _inspect(self, physical: Tuple[str, ...]) -> Tuple[str, int, int, Optional[str]]:
        """返回 (kind, st_dev, st_ino, link_target);失败抛分类 PathError。"""
        cached = self._inspect_cache.get(physical)
        if cached is not None:
            return cached
        path = self._join(physical)
        try:
            st = os.lstat(path)
        except OSError as exc:
            raise self._classify_os_error(exc, path) from exc
        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:  # 竞态:lstat 后链接被删
                raise self._classify_os_error(exc, path) from exc
            outcome = ("link", st.st_dev, st.st_ino, target)
        elif stat.S_ISDIR(st.st_mode):
            outcome = ("dir", st.st_dev, st.st_ino, None)
        else:
            outcome = ("file", st.st_dev, st.st_ino, None)
        self._inspect_cache[physical] = outcome
        return outcome

    def _resolve_tokens(self, tokens: List[str]) -> Tuple[Tuple[str, ...], str, int, int]:
        """把词法 token 解析为物理路径,返回 (物理分量, 末分量类型, dev, ino)。"""
        resolved: List[str] = []
        pending = list(tokens)
        hops = 0
        last_kind = "missing"
        last_dev = last_ino = -1
        while pending:
            token = pending.pop(0)
            if token == "..":
                if resolved:
                    resolved.pop()
                last_kind = "dir"
                continue
            physical = tuple(resolved + [token])
            kind, dev, ino, target = self._inspect(physical)
            if kind == "link":
                hops += 1
                if hops > self.max_hops:
                    raise PathError(
                        "SYMLINK_LOOP",
                        f"符号链接成环或嵌套超过 {self.max_hops} 层: {self._join(physical)}",
                    )
                assert target is not None
                if os.path.isabs(target):
                    resolved = []
                pending = self._split(target) + pending
                continue
            resolved.append(token)
            last_kind, last_dev, last_ino = kind, dev, ino
        if not tokens:
            root_kind, root_dev, root_ino, _ = self._inspect(())
            return (), root_kind, root_dev, root_ino
        return tuple(resolved), last_kind, last_dev, last_ino

    # ---- 对外接口 --------------------------------------------------------

    def resolve_base(self, base: str) -> Tuple[str, ...]:
        """解析并校验基准目录;失败抛带 BASE_* 前缀的 PathError。"""
        cached = self._base_cache.get(base)
        if cached is not None:
            return cached
        if not isinstance(base, str) or not base.strip():
            raise PathError("BASE_MISSING", "基准目录为空")
        if not os.path.isabs(base):
            raise PathError("BASE_INVALID", f"基准目录必须是绝对路径: {base!r}")
        try:
            tokens, kind, _, _ = self._resolve_tokens(self._split(base))
        except PathError as exc:
            raise PathError(_STAGE_PREFIX["base"].get(exc.code, exc.code),
                            f"基准目录无效: {exc.message}") from exc
        if kind != "dir":
            raise PathError("BASE_NOT_A_DIRECTORY", f"基准路径不是目录: {base!r}")
        self._base_cache[base] = tokens
        return tokens

    def resolve_entry(self, path: str, base: Optional[str]) -> Tuple[str, Tuple[int, int]]:
        """解析单条路径,返回 (规范真实路径, (st_dev, st_ino))。"""
        if not isinstance(path, str) or not path.strip():
            raise PathError("INVALID_PATH", "路径为空或不是字符串")
        tokens = self._split(path)
        if os.path.isabs(path):
            start: Tuple[str, ...] = ()
            stage = "path"
        else:
            if base is None:
                raise PathError("BASE_MISSING",
                                f"相对路径缺少基准目录: {path!r}")
            start = self.resolve_base(base)
            stage = "path"
        try:
            resolved, kind, dev, ino = self._resolve_tokens(list(start) + tokens)
        except PathError as exc:
            raise PathError(_STAGE_PREFIX.get(stage, {}).get(exc.code, exc.code),
                            exc.message) from exc
        if kind == "missing":  # 理论上不可达,防御
            raise PathError("NOT_FOUND", f"路径不存在: {path!r}")
        real_path = os.path.normcase(self._join(resolved))
        return real_path, (dev, ino)


def _normalize_entry(entry) -> Tuple[str, Optional[str]]:
    """接受 'path'、(path, base) 或 {'path':..,'base':..} 三种条目形式。"""
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict):
        return entry.get("path"), entry.get("base")
    if isinstance(entry, (tuple, list)):
        if len(entry) == 1:
            return entry[0], None
        return entry[0], entry[1]
    raise TypeError(f"不支持的条目类型: {type(entry)!r}")


def dedup_entries(entries: Iterable, resolver: Optional[Resolver] = None) -> DedupResult:
    """按真实文件判定去重。

    entries: 可迭代,元素为 'path'、(path, base) 或 {'path','base'}。
    返回 DedupResult:unique 保持首次出现顺序;duplicates 记录 merged_into;
    errors 分类记录,三者合起来恰好覆盖全部输入索引,不丢任何条目。
    """
    resolver = resolver or Resolver()
    result = DedupResult()
    seen: Dict[Tuple[int, int], int] = {}  # file_id -> unique 列表下标
    for index, entry in enumerate(entries):
        try:
            path, base = _normalize_entry(entry)
        except TypeError as exc:
            result.errors.append(ErrorRecord(index, repr(entry), None,
                                             "INVALID_PATH", str(exc)))
            continue
        try:
            real_path, file_id = resolver.resolve_entry(path, base)
        except PathError as exc:
            result.errors.append(ErrorRecord(index, str(path), base,
                                             exc.code, exc.message))
            continue
        owner = seen.get(file_id)
        if owner is None:
            seen[file_id] = len(result.unique)
            result.unique.append(UniqueEntry(index, path, base, real_path, file_id))
        else:
            first = result.unique[owner]
            result.duplicates.append(DuplicateEntry(
                index, path, base, real_path, first.index, first.path))
    return result


def naive_string_dedup(entries: Iterable) -> List[str]:
    """朴素基线:按原始字符串去重(相对路径不带 base),保持首现顺序。"""
    seen = set()
    kept: List[str] = []
    for entry in entries:
        path, _ = _normalize_entry(entry)
        key = str(path)
        if key not in seen:
            seen.add(key)
            kept.append(key)
    return kept


# ---- CLI -----------------------------------------------------------------

def _iter_playlist_lines(handle) -> Iterable[Tuple[int, str, Optional[str]]]:
    """TSV: path<TAB>base(base 可缺省);# 开头与空行跳过。"""
    for lineno, line in enumerate(handle, 1):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        path = parts[0]
        base = parts[1] if len(parts) > 1 and parts[1] else None
        yield lineno, path, base


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="按真实文件判定对播放列表去重(相对路径必须声明基准目录)")
    parser.add_argument("playlist", help="播放列表文件,TSV: path[TAB base]")
    parser.add_argument("--base", default=None,
                        help="为未单独声明 base 的相对路径提供全局基准目录")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把完整结果(含 duplicates/errors)写成 JSON")
    args = parser.parse_args(argv)

    with open(args.playlist, "r", encoding="utf-8") as handle:
        entries = [(path, base if base is not None else args.base)
                   for _, path, base in _iter_playlist_lines(handle)]

    result = dedup_entries(entries)
    for item in result.unique:
        print(item.real_path)
    print(f"# unique={len(result.unique)} duplicates={len(result.duplicates)} "
          f"errors={len(result.errors)} {result.error_summary()}",
          file=sys.stderr)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(result.to_dict(), handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
