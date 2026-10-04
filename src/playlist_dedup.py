"""播放列表路径解析与按真实文件去重（仅标准库，Python 3.10+）。

去重判定规则（与 docs/RULES.zh.md 对应）：
  1. 相对路径必须显式声明基准目录 base_dir；未声明时拒绝（MISSING_BASE），
     绝不回退到进程当前工作目录。
  2. 词法归一：os.path.normpath 折叠重复分隔符、"."、".."；
     Windows 下同时把 "/" 统一为 "\\"。
  3. 物理归一：os.path.realpath(strict=True) 逐级展开符号链接，
     要求目标真实存在；因此绝对路径与相对路径、链接别名都能收敛。
  4. 大小写：os.path.normcase 跟随平台语义（Windows 折叠大小写；
     POSIX 保留大小写）。
  5. 去重身份键为 (st_dev, st_ino)，即内核视角的真实文件；
     硬链接也会被视为同一文件。规范字符串仅用于展示。
  6. 去重保持首次出现顺序；重复项记录 merged_into（被并入条目的原始下标）。
  7. 错误分类报告，绝不合并成一句"找不到"：
     基准缺失 / 基准不存在 / 路径不存在 / 非目录分量 / 权限不足 /
     符号链接成环 / 其他系统错误。
"""

from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

__all__ = [
    "ErrorKind",
    "PathError",
    "Entry",
    "DedupResult",
    "validate_base_dir",
    "resolve_entry",
    "dedup_playlist",
    "naive_string_dedup",
]


class ErrorKind(str, Enum):
    EMPTY_PATH = "empty_path"
    MISSING_BASE = "missing_base"              # 相对路径且未声明基准目录
    BASE_NOT_FOUND = "base_not_found"          # 声明的基准目录不存在
    BASE_NOT_DIRECTORY = "base_not_directory"  # 基准路径不是目录
    NOT_FOUND = "not_found"                    # 目标或父目录不存在
    NOT_A_DIRECTORY = "not_a_directory"        # 路径中间分量是普通文件
    PERMISSION_DENIED = "permission_denied"    # 权限不足
    SYMLINK_LOOP = "symlink_loop"              # 符号链接成环
    OS_ERROR = "os_error"                      # 其他系统错误（兜底）


_ERRNO_TO_KIND = {
    errno.ENOENT: ErrorKind.NOT_FOUND,
    errno.ENOTDIR: ErrorKind.NOT_A_DIRECTORY,
    errno.EACCES: ErrorKind.PERMISSION_DENIED,
    errno.EPERM: ErrorKind.PERMISSION_DENIED,
    errno.ELOOP: ErrorKind.SYMLINK_LOOP,
}


@dataclass(frozen=True)
class PathError:
    kind: ErrorKind
    message: str
    errno: int | None = None


@dataclass
class Entry:
    index: int                          # 在原始播放列表中的下标（0 起）
    raw: str                            # 原始字符串，绝不修改/丢弃
    base_dir: str | None                # 解析时使用的基准目录
    canonical: str | None = None        # 归一后的规范绝对路径
    key: tuple[int, int] | None = None  # (st_dev, st_ino)，真实文件身份
    merged_into: int | None = None      # 重复时：被并入条目的原始下标
    error: PathError | None = None      # 失败时的分类错误

    @property
    def is_unique(self) -> bool:
        return self.error is None and self.merged_into is None and self.key is not None

    @property
    def is_duplicate(self) -> bool:
        return self.merged_into is not None

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass
class DedupResult:
    unique: list[Entry]
    duplicates: list[Entry]
    errors: list[Entry]

    def error_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.errors:
            name = entry.error.kind.value
            counts[name] = counts.get(name, 0) + 1
        return counts

    def to_dict(self) -> dict:
        def dump(entry: Entry) -> dict:
            return {
                "index": entry.index,
                "raw": entry.raw,
                "base_dir": entry.base_dir,
                "canonical": entry.canonical,
                "merged_into": entry.merged_into,
                "error": None if entry.error is None else {
                    "kind": entry.error.kind.value,
                    "message": entry.error.message,
                    "errno": entry.error.errno,
                },
            }

        return {
            "total": len(self.unique) + len(self.duplicates) + len(self.errors),
            "unique": [dump(e) for e in self.unique],
            "duplicates": [dump(e) for e in self.duplicates],
            "errors": [dump(e) for e in self.errors],
            "error_counts": self.error_counts(),
        }


def validate_base_dir(base_dir: str | None) -> PathError | None:
    """校验声明的基准目录。返回 None 表示可用。"""
    if base_dir is None:
        return None
    try:
        st = os.stat(base_dir)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            kind = ErrorKind.BASE_NOT_FOUND
        elif exc.errno == errno.ELOOP:
            kind = ErrorKind.SYMLINK_LOOP
        elif exc.errno in (errno.EACCES, errno.EPERM):
            kind = ErrorKind.PERMISSION_DENIED
        elif exc.errno == errno.ENOTDIR:
            kind = ErrorKind.BASE_NOT_DIRECTORY
        else:
            kind = ErrorKind.OS_ERROR
        return PathError(kind, f"基准目录不可用: {base_dir!r}: {exc.strerror or exc}", exc.errno)
    if not os.path.isdir(base_dir):
        return PathError(
            ErrorKind.BASE_NOT_DIRECTORY,
            f"基准路径不是目录: {base_dir!r}",
            None,
        )
    return None


def _classify(exc: OSError, target: str) -> PathError:
    kind = _ERRNO_TO_KIND.get(exc.errno, ErrorKind.OS_ERROR)
    return PathError(
        kind,
        f"{kind.value}: {target!r}: {exc.strerror or str(exc)}",
        exc.errno,
    )


def resolve_entry(raw: str, index: int, base_dir: str | None,
                  base_error: PathError | None) -> Entry:
    """解析单条路径。永不抛异常，失败信息写入 Entry.error。"""
    entry = Entry(index=index, raw=raw, base_dir=base_dir)

    if not isinstance(raw, str) or raw == "":
        entry.error = PathError(ErrorKind.EMPTY_PATH, "路径为空字符串")
        return entry

    if os.path.isabs(raw):
        path = raw
    else:
        if base_dir is None:
            entry.error = PathError(
                ErrorKind.MISSING_BASE,
                f"相对路径必须显式声明基准目录，拒绝按当前工作目录猜测: {raw!r}",
            )
            return entry
        if base_error is not None:
            entry.error = PathError(
                base_error.kind,
                f"{base_error.message}; 待解析相对路径: {raw!r}",
                base_error.errno,
            )
            return entry
        path = os.path.join(base_dir, raw)

    lex_normalized = os.path.normpath(path)

    try:
        physical = os.path.realpath(lex_normalized, strict=True)
        st = os.stat(physical)
    except OSError as exc:
        entry.error = _classify(exc, lex_normalized)
        return entry

    entry.canonical = os.path.normcase(physical)
    entry.key = (st.st_dev, st.st_ino)
    return entry


def dedup_playlist(items: Sequence[str] | Iterable[str],
                   base_dir: str | None = None) -> DedupResult:
    """解析并按真实文件去重，保持首次出现顺序。"""
    base_error = validate_base_dir(base_dir)

    unique: list[Entry] = []
    duplicates: list[Entry] = []
    errors: list[Entry] = []
    first_index_by_key: dict[tuple[int, int], int] = {}

    for index, raw in enumerate(items):
        entry = resolve_entry(raw, index, base_dir, base_error)
        if entry.is_error:
            errors.append(entry)
            continue
        owner = first_index_by_key.get(entry.key)
        if owner is None:
            first_index_by_key[entry.key] = index
            unique.append(entry)
        else:
            entry.merged_into = owner
            duplicates.append(entry)

    return DedupResult(unique=unique, duplicates=duplicates, errors=errors)


def naive_string_dedup(items: Sequence[str]) -> tuple[list[str], list[tuple[int, str, int]]]:
    """朴素字符串去重（对照组）：按原始字符串完全相等判定。"""
    first_index: dict[str, int] = {}
    unique: list[str] = []
    duplicates: list[tuple[int, str, int]] = []
    for index, raw in enumerate(items):
        if raw in first_index:
            duplicates.append((index, raw, first_index[raw]))
        else:
            first_index[raw] = index
            unique.append(raw)
    return unique, duplicates


def _main(argv: list[str]) -> int:
    base_dir = None
    path = None
    args = list(argv[1:])
    while args:
        flag = args.pop(0)
        if flag in ("-b", "--base"):
            base_dir = args.pop(0)
        elif flag in ("-h", "--help"):
            print("用法: playlist_dedup.py [-b 基准目录] 播放列表文件(每行一条; '-' 为标准输入)")
            return 0
        else:
            path = flag
    if path is None:
        print("缺少播放列表文件参数", file=sys.stderr)
        return 2
    if path == "-":
        items = [line.rstrip("\n").rstrip("\r") for line in sys.stdin]
    else:
        with open(path, "r", encoding="utf-8") as fh:
            items = [line.rstrip("\n").rstrip("\r") for line in fh]

    result = dedup_playlist(items, base_dir)
    payload = result.to_dict()
    summary = {
        "total": payload["total"],
        "unique_count": len(result.unique),
        "duplicate_count": len(result.duplicates),
        "error_count": len(result.errors),
        "error_counts": payload["error_counts"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
