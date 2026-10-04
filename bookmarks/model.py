"""书签文档模型：解析、校验、修复、统计、序列化。

文件格式（gsb-bookmarks v1，UTF-8 JSON）::

    {
      "format": "gsb-bookmarks",
      "version": 1,
      "roots": [ <node>, ... ]        // 顶层节点数组，数组下标即兄弟顺序
    }

节点::

    { "id": "n1", "type": "folder", "name": "...", "children": [ ... ] }
    { "id": "n2", "type": "url",    "name": "...", "url": "https://..." }

- 未知扩展字段（如 add_date、icon、tags）原样保留，保证无损往返。
- parent_id 为可选的冗余校验字段；根节点的 parent_id 约定为 ""（ROOT_PARENT_ID）。
  当 parent_id 与结构嵌套关系冲突时，以结构嵌套为准并修复、报告。
- 顺序依据：roots / children 数组的下标顺序（结构顺序），不做任何按标题排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import iterjson

FORMAT_ID = "gsb-bookmarks"
FORMAT_VERSION = 1
ROOT_PARENT_ID = ""

NODE_TYPES = ("folder", "url")


class BookmarkFormatError(ValueError):
    """致命格式错误：文件无法被还原为书签树。"""


@dataclass
class Issue:
    """一条异常记录。action 描述处理结果。"""

    code: str
    action: str
    message: str
    node_id: str = ""
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "action": self.action,
            "node_id": self.node_id,
            "path": self.path,
            "message": self.message,
        }


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, code: str, action: str, message: str,
            node_id: str = "", path: str = "") -> None:
        self.issues.append(Issue(code, action, message, node_id, path))

    @property
    def ok(self) -> bool:
        return not self.issues

    def counts(self) -> dict:
        result: dict[str, int] = {}
        for issue in self.issues:
            result[issue.code] = result.get(issue.code, 0) + 1
        return result

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issue_count": len(self.issues),
            "counts": self.counts(),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_text(self) -> str:
        if self.ok:
            return "未发现异常。\n"
        lines = [f"共发现 {len(self.issues)} 处异常："]
        for idx, issue in enumerate(self.issues, 1):
            lines.append(
                f"  {idx}. [{issue.code}] 处理={issue.action} "
                f"节点={issue.node_id or '-'} 位置={issue.path or '-'}"
            )
            lines.append(f"     {issue.message}")
        return "\n".join(lines) + "\n"


def _walk(roots: list):
    """迭代 DFS 遍历整棵树。

    产出 (container, index, node, parent_id, ancestors, segment)：
    - container/index：节点在其兄弟数组中的位置（可用于原地替换）；
    - parent_id：结构父节点的 id（根层为 ROOT_PARENT_ID）；
    - ancestors：活跃的路径段列表，仅本次产出期间有效，不要保存引用；
    - segment：本节点的路径段，如 "roots[0]" / "children[3]"。

    遍历时若向 roots 末尾追加元素，会被自然拾取（用于迁移修复）。
    """
    it_stack = [iter(enumerate(roots))]
    seg_stack: list[str] = []
    id_stack = [ROOT_PARENT_ID]
    containers = [roots]
    while it_stack:
        try:
            idx, node = next(it_stack[-1])
        except StopIteration:
            if len(it_stack) == 1:
                return
            it_stack.pop()
            seg_stack.pop()
            id_stack.pop()
            containers.pop()
            continue
        depth = len(containers)
        segment = ("roots" if depth == 1 else "children") + f"[{idx}]"
        parent_id = id_stack[-1]
        yield containers[-1], idx, node, parent_id, seg_stack, segment
        if isinstance(node, dict):
            children = node.get("children")
            if isinstance(children, list) and children:
                nid = node.get("id")
                seg_stack.append(segment)
                id_stack.append(nid if isinstance(nid, str) else ROOT_PARENT_ID)
                containers.append(children)
                it_stack.append(iter(enumerate(children)))


def _path_of(ancestors: list[str], segment: str) -> str:
    return "/".join([*ancestors, segment])


def _fresh_auto_id(seen: dict, counter: list) -> str:
    while True:
        candidate = f"auto-{counter[0]}"
        counter[0] += 1
        if candidate not in seen:
            return candidate


def _repair_tree(roots: list, report: Report) -> None:
    """两遍修复：第一遍修结构，第二遍按结构校正 parent_id。"""
    seen: dict[str, None] = {}
    counter = [1]

    # ---- 第一遍：结构校验与修复 ----
    for container, idx, node, _parent_id, anc, seg in _walk(roots):
        if not isinstance(node, dict):
            auto = _fresh_auto_id(seen, counter)
            report.add(
                "invalid_node", "replaced",
                f"节点不是对象（原值 {node!r}），已替换为占位链接节点",
                node_id=auto, path=_path_of(anc, seg),
            )
            node = {"id": auto, "type": "url", "name": "", "url": ""}
            container[idx] = node

        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            nid = _fresh_auto_id(seen, counter)
            node["id"] = nid
            report.add(
                "id_missing", "assigned",
                f"节点缺少 id，已自动分配 {nid!r}",
                node_id=nid, path=_path_of(anc, seg),
            )

        if nid in seen:
            report.add(
                "duplicate_id", "kept",
                f"重复的 id {nid!r}：两处节点均保留，索引以首次出现为准",
                node_id=nid, path=_path_of(anc, seg),
            )
        else:
            seen[nid] = None

        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            report.add(
                "unknown_type", "kept",
                f"未知类型 {ntype!r}：字段原样保留，按是否含 children 处理",
                node_id=nid, path=_path_of(anc, seg),
            )

        children = node.get("children")
        if children is not None and not isinstance(children, list):
            report.add(
                "invalid_children", "cleared",
                f"children 不是数组（原值类型 {type(children).__name__}），已按空目录处理",
                node_id=nid, path=_path_of(anc, seg),
            )
            node["children"] = []
            children = None

        if isinstance(children, list) and children and ntype == "url":
            report.add(
                "children_under_url", "moved_to_root",
                f"链接节点下不允许有子节点：{len(children)} 个子节点已按原顺序迁移到根列表末尾",
                node_id=nid, path=_path_of(anc, seg),
            )
            roots.extend(children)
            node["children"] = []

    # ---- 第二遍：parent_id 与结构父节点交叉校验 ----
    for _container, _idx, node, parent_id, anc, seg in _walk(roots):
        if not isinstance(node, dict) or "parent_id" not in node:
            continue
        actual = node["parent_id"]
        if actual == parent_id:
            continue
        nid = node.get("id", "")
        if isinstance(actual, str) and actual in seen:
            code, kind = "parent_mismatch", "父子关系不一致"
            detail = f"parent_id 指向 {actual!r}，但结构父节点是 {parent_id!r}"
        else:
            code, kind = "orphan_reference", "孤立节点"
            detail = f"parent_id 指向不存在的节点 {actual!r}"
        node["parent_id"] = parent_id
        report.add(
            code, "corrected",
            f"{kind}：{detail}，已按结构嵌套修正为 {parent_id!r}",
            node_id=nid if isinstance(nid, str) else "",
            path=_path_of(anc, seg),
        )


class BookmarkDocument:
    """一份已解析（并修复）的书签文档。"""

    def __init__(self, data: dict, report: Report) -> None:
        self.data = data
        self.report = report

    @classmethod
    def from_text(cls, text: str) -> "BookmarkDocument":
        data = iterjson.loads(text)  # JSONParseError 直接上抛
        if not isinstance(data, dict):
            raise BookmarkFormatError("顶层必须是 JSON 对象")
        if data.get("format") != FORMAT_ID:
            raise BookmarkFormatError(
                f"无法识别的 format 字段: {data.get('format')!r}（期望 {FORMAT_ID!r}）"
            )
        if data.get("version") != FORMAT_VERSION:
            raise BookmarkFormatError(
                f"不支持的 version: {data.get('version')!r}（期望 {FORMAT_VERSION}）"
            )
        roots = data.get("roots")
        if not isinstance(roots, list):
            raise BookmarkFormatError("缺少 roots 数组")
        report = Report()
        _repair_tree(roots, report)
        return cls(data, report)

    def to_text(self, indent: int | None = 2) -> str:
        return iterjson.dumps(self.data, indent=indent)

    def stats(self) -> dict:
        roots = self.data["roots"]
        nodes = folders = urls = others = max_depth = 0
        stack = [(node, 1) for node in roots]
        while stack:
            node, depth = stack.pop()
            nodes += 1
            ntype = node.get("type")
            if ntype == "folder":
                folders += 1
            elif ntype == "url":
                urls += 1
            else:
                others += 1
            if depth > max_depth:
                max_depth = depth
            children = node.get("children")
            if isinstance(children, list):
                stack.extend((child, depth + 1) for child in children
                             if isinstance(child, dict))
        return {
            "nodes": nodes,
            "folders": folders,
            "urls": urls,
            "other_types": others,
            "max_depth": max_depth,
            "roots": len(roots),
        }
