"""多版本存储：每个 key 一条按 commit_seq 升序的版本链。"""

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

TOMBSTONE = None


@dataclass
class Version:
    key: str
    commit_seq: int
    txn_id: int
    value: Any  # None 表示删除墓碑


class VersionStore:
    def __init__(self):
        self._chains: Dict[str, List[Version]] = {}

    def add(self, version: Version) -> None:
        chain = self._chains.setdefault(version.key, [])
        if chain and chain[-1].commit_seq >= version.commit_seq:
            raise ValueError("commit_seq must be increasing per key")
        chain.append(version)

    def chain(self, key: str) -> List[Version]:
        return self._chains.get(key, [])

    def keys(self) -> Iterator[str]:
        return iter(list(self._chains.keys()))

    def visible_at(self, key: str, seq: int) -> Optional[Version]:
        """seq 时刻可见的版本：commit_seq <= seq 的最新版本。"""
        result = None
        for v in self._chains.get(key, []):
            if v.commit_seq <= seq:
                result = v
            else:
                break
        return result

    def total_versions(self) -> int:
        return sum(len(c) for c in self._chains.values())

    def replace_chain(self, key: str, versions: List[Version]) -> None:
        if versions:
            self._chains[key] = versions
        else:
            self._chains.pop(key, None)
