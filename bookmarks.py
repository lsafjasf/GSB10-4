# -*- coding: utf-8 -*-
"""Netscape 书签文件（嵌套 HTML）解析与还原库。

特性
====
* 还原目录层级与兄弟顺序。顺序依据：节点在文件中的**结构顺序**
  （即 ``<DT>`` 条目在其父 ``<DL>`` 内出现的先后），不做任何按标题
  的排序或重排。
* 无损往返：解析保留全部附属属性（含属性书写顺序、大小写、未知属性）、
  目录描述（``<DD>``）、文档头（DOCTYPE / TITLE / H1 / META），序列化
  时按原样写回；对规范化输入保证字节级一致。
* 异常报告：父子关系不一致（PID 与结构父级不符）、孤立节点
  （PID 指向不存在的 ID）、重复标识（ID 重复）等，见 ``validate``。
* 全程迭代实现（显式栈），超深嵌套不会触发递归溢出。

仅使用 Python 标准库。
"""

import hashlib
import html
import re
from collections import OrderedDict

__all__ = [
    "Node", "BookmarkTree", "parse_string", "parse_file",
    "serialize", "write_file", "validate", "stats",
    "tree_digest", "trees_equal", "ANOMALY_KINDS",
]

ANOMALY_KINDS = (
    "duplicate_id",          # 重复标识：同一 ID 出现多次
    "parent_mismatch",       # 父子关系不一致：PID 声明的父级与结构父级不符
    "dangling_parent_ref",   # 孤立节点：PID 指向不存在的 ID
    "stray_node",            # 结构外节点：DT 条目出现在任何 DL 之外
    "unclosed_list",         # 未闭合的 DL，文件结束时自动闭合
    "anonymous_list",        # 没有前置 H3 的 DL，按匿名目录处理
)

# 一个标签 token：<!--注释-->、<!DOCTYPE ...>、<?...>、<...>
_TOKEN_RE = re.compile(r"<!--.*?-->|<![^>]*>|<\?[^>]*>|<[^>]*>", re.DOTALL)
# 标签内部：结束斜杠、标签名、其余属性文本、自闭合斜杠
_TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z0-9]+)([^>]*?)(/?)\s*>", re.DOTALL)
# 属性：name 或 name="v" 或 name='v' 或 name=v
_ATTR_RE = re.compile(
    r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)

_STRUCT_TAGS = {"dl", "dt", "h3", "a", "dd", "p", "h1", "title", "meta"}


class Node(object):
    """书签树节点。

    kind: "folder" | "bookmark"
    attrs: OrderedDict，保留属性的原始书写顺序、大小写与取值
    children: 仅目录有；列表顺序即文件中的结构顺序
    description: 条目后紧跟的 <DD> 文本（无则为 None）
    parent: 解析时建立的父指针（序列化/校验用，不影响输出内容）
    """

    __slots__ = ("kind", "title", "attrs", "children", "description", "parent")

    def __init__(self, kind, title="", attrs=None, description=None):
        self.kind = kind
        self.title = title
        self.attrs = attrs if attrs is not None else OrderedDict()
        self.children = []
        self.description = description
        self.parent = None

    def get_attr(self, name):
        """大小写不敏感地取属性值，不存在返回 None。"""
        lowered = name.lower()
        for key, value in self.attrs.items():
            if key.lower() == lowered:
                return value
        return None

    def is_folder(self):
        return self.kind == "folder"

    def __repr__(self):
        return "<Node %s %r attrs=%d children=%d>" % (
            self.kind, self.title, len(self.attrs), len(self.children))


class BookmarkTree(object):
    """解析结果：虚拟根目录 + 文档头信息 + 解析期异常。"""

    def __init__(self):
        self.root = Node("folder", title="")
        self.doctype = "NETSCAPE-Bookmark-file-1"
        self.meta = ('<META HTTP-EQUIV="Content-Type" '
                     'CONTENT="text/html; charset=UTF-8">')
        self.head_title = "Bookmarks"
        self.h1 = "Bookmarks"
        self.anomalies = []  # list[dict]，解析期结构异常

    def add_anomaly(self, kind, message, **detail):
        entry = {"kind": kind, "message": message}
        entry.update(detail)
        self.anomalies.append(entry)


# ---------------------------------------------------------------- 解析

def _unescape(value):
    # 快路径：没有 & 就不存在实体，避免 html.unescape 的固定开销
    return html.unescape(value) if "&" in value else value


def _parse_attrs(raw):
    attrs = OrderedDict()
    for match in _ATTR_RE.finditer(raw):
        name = match.group(1)
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        if value is None:
            value = ""
        attrs[name] = _unescape(value)
    return attrs


def parse_string(text):
    """解析书签 HTML 文本，返回 BookmarkTree。全程迭代，无递归。"""
    tree = BookmarkTree()
    root = tree.root
    stack = [root]            # 已打开 DL 对应的目录节点，栈底恒为 root
    root_dl_open = False
    pending_folder = None     # 最近的 H3，等待其后的 DL
    open_item = None          # 当前可接收 DD 描述的条目
    capture = None            # None | "h3" | "a" | "dd" | "h1" | "title"
    capture_node = None
    buf = []
    stray_nodes = []          # 出现在任何 DL 之外的条目，标题捕获后统一报告

    def close_dd():
        nonlocal capture
        if capture == "dd" and capture_node is not None:
            capture_node.description = _unescape("".join(buf)).strip()
        capture = None

    def attach(node):
        parent = stack[-1]
        node.parent = parent
        parent.children.append(node)

    pos = 0
    for match in _TOKEN_RE.finditer(text):
        if capture is not None and match.start() > pos:
            buf.append(text[pos:match.start()])
        pos = match.end()
        token = match.group(0)

        if token.startswith("<!--") or token.startswith("<!") or token.startswith("<?"):
            if token.startswith("<!") and not token.startswith("<!--"):
                inner = token[2:-1].strip()
                if inner.upper().startswith("DOCTYPE"):
                    tree.doctype = inner[len("DOCTYPE"):].strip()
            continue

        tag = _TAG_RE.match(token)
        if tag is None:
            continue
        closing, name, attr_raw = tag.group(1), tag.group(2).lower(), tag.group(3)
        if name not in _STRUCT_TAGS:
            continue

        if not closing:
            if name == "dl":
                close_dd()
                if not root_dl_open and len(stack) == 1 and pending_folder is None:
                    root_dl_open = True  # 根 DL：内容直接挂到虚拟根
                else:
                    if pending_folder is not None:
                        stack.append(pending_folder)
                        pending_folder = None
                    else:
                        anon = Node("folder", title="")
                        anon.parent = stack[-1] if len(stack) > 1 else None
                        stack.append(anon)
                        tree.add_anomaly(
                            "anonymous_list",
                            "发现没有前置 <H3> 的 <DL>，按匿名目录处理",
                            depth=len(stack) - 1)
                open_item = None
            elif name == "dt":
                close_dd()
                open_item = None
            elif name == "h3":
                close_dd()
                node = Node("folder", attrs=_parse_attrs(attr_raw))
                attach(node)
                pending_folder = node
                open_item = node
                capture, capture_node, buf = "h3", node, []
                if not root_dl_open:
                    stray_nodes.append(node)
            elif name == "a":
                close_dd()
                node = Node("bookmark", attrs=_parse_attrs(attr_raw))
                attach(node)
                open_item = node
                capture, capture_node, buf = "a", node, []
                if not root_dl_open:
                    stray_nodes.append(node)
            elif name == "dd":
                close_dd()
                if open_item is not None:
                    capture, capture_node, buf = "dd", open_item, []
            elif name == "h1":
                close_dd()
                capture, capture_node, buf = "h1", None, []
            elif name == "title":
                close_dd()
                capture, capture_node, buf = "title", None, []
            elif name == "meta":
                tree.meta = token
            # p 及其它结构标签：忽略
        else:
            if name == "dl":
                close_dd()
                if len(stack) > 1:
                    stack.pop()
                else:
                    root_dl_open = False
                open_item = None
            elif name in ("h3", "a") and capture == name and capture_node is not None:
                capture_node.title = _unescape("".join(buf)).strip()
                capture = None
            elif name == "dd":
                close_dd()
            elif name == "h1" and capture == "h1":
                tree.h1 = _unescape("".join(buf)).strip()
                capture = None
            elif name == "title" and capture == "title":
                tree.head_title = _unescape("".join(buf)).strip()
                capture = None

    close_dd()
    for node in stray_nodes:
        tree.add_anomaly(
            "stray_node",
            "%s %r 出现在任何 <DL> 之外，已挂到根目录"
            % ("目录" if node.kind == "folder" else "书签", node.title),
            node_title=node.title, node_kind=node.kind)
    while len(stack) > 1:
        node = stack.pop()
        tree.add_anomaly(
            "unclosed_list",
            "目录 %r 的 <DL> 未闭合，已在文件末尾自动闭合" % node.title,
            node_title=node.title, depth=len(stack) - 1)
    return tree


def parse_file(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding) as handle:
        return parse_string(handle.read())


# ---------------------------------------------------------------- 校验

def validate(tree):
    """基于 ID/PID 标识的一致性校验，返回异常列表（不修改树）。

    处理约定（保证往返无损，校验只报告不改写）：
    * duplicate_id        —— 保留先出现者作为 ID 的规范节点，后者仍原样保留；
    * parent_mismatch     —— 以结构嵌套位置为准，PID 仅作参考元数据；
    * dangling_parent_ref —— 节点保持其结构位置，标记为孤立节点。
    """
    anomalies = []
    id_owner = {}
    referenced = []

    stack = [tree.root]
    while stack:
        node = stack.pop()
        node_id = node.get_attr("id")
        if node_id is not None:
            if node_id in id_owner:
                anomalies.append({
                    "kind": "duplicate_id",
                    "message": "ID %r 重复：保留先出现的节点作为规范节点，"
                               "重复节点原样保留" % node_id,
                    "id": node_id,
                    "kept_title": id_owner[node_id].title,
                    "duplicate_title": node.title,
                })
            else:
                id_owner[node_id] = node
        stack.extend(reversed(node.children))

    stack = [tree.root]
    while stack:
        node = stack.pop()
        pid = node.get_attr("pid")
        if pid is not None:
            parent = node.parent
            parent_id = parent.get_attr("id") if parent is not None else None
            if pid not in id_owner:
                anomalies.append({
                    "kind": "dangling_parent_ref",
                    "message": "节点 %r 的 PID=%r 指向不存在的 ID，"
                               "判定为孤立节点，保持其结构位置" % (node.title, pid),
                    "node_title": node.title, "pid": pid,
                })
            elif parent_id != pid:
                anomalies.append({
                    "kind": "parent_mismatch",
                    "message": "节点 %r 声明父级 PID=%r，但结构父级 ID=%r，"
                               "以结构嵌套位置为准" % (node.title, pid, parent_id),
                    "node_title": node.title, "pid": pid,
                    "structural_parent_id": parent_id,
                })
        stack.extend(reversed(node.children))
    return anomalies


# ---------------------------------------------------------------- 序列化

def _escape_text(value):
    return html.escape(value, quote=False)


def _escape_attr(value):
    return html.escape(value, quote=True)


def _render_attrs(attrs):
    return "".join(' %s="%s"' % (key, _escape_attr(value))
                   for key, value in attrs.items())


def serialize(tree, indent=4):
    """把 BookmarkTree 写回 Netscape 书签格式文本。全程迭代，无递归。

    indent: 每层缩进的空格数；0 表示紧凑输出（深层场景文件更小）。
    """
    unit = " " * indent
    lines = [
        "<!DOCTYPE %s>" % tree.doctype,
        tree.meta,
        "<TITLE>%s</TITLE>" % _escape_text(tree.head_title),
        "<H1>%s</H1>" % _escape_text(tree.h1),
        "",
        "<DL><p>",
    ]

    # 帧: [node, child_index, emitted_dl]；根帧不输出 H3/DL
    frames = [[tree.root, 0, True]]
    while frames:
        frame = frames[-1]
        node, index = frame[0], frame[1]
        depth = len(frames) - 1  # 根的内容位于第 0 级
        if index < len(node.children):
            child = node.children[index]
            frame[1] = index + 1
            pad = unit * (depth + 1)
            if child.kind == "folder":
                lines.append("%s<DT><H3%s>%s</H3>"
                             % (pad, _render_attrs(child.attrs),
                                _escape_text(child.title)))
                if child.description is not None:
                    lines.append("%s<DD>%s" % (pad, _escape_text(child.description)))
                lines.append("%s<DL><p>" % pad)
                frames.append([child, 0, True])
            else:
                lines.append("%s<DT><A%s>%s</A>"
                             % (pad, _render_attrs(child.attrs),
                                _escape_text(child.title)))
                if child.description is not None:
                    lines.append("%s<DD>%s" % (pad, _escape_text(child.description)))
        else:
            frames.pop()
            if frames:  # 非根目录：闭合 DL
                pad = unit * (len(frames) - 1 + 1)
                lines.append("%s</DL><p>" % pad)
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"


def write_file(tree, path, encoding="utf-8", indent=4):
    text = serialize(tree, indent=indent)
    with open(path, "w", encoding=encoding, newline="\n") as handle:
        handle.write(text)
    return text


# ---------------------------------------------------------------- 对拍

def stats(tree):
    """返回 (节点总数, 最大深度)。根目录不计入节点数，根的孩子深度为 1。"""
    total = 0
    max_depth = 0
    stack = [(tree.root, 0)]
    while stack:
        node, depth = stack.pop()
        if node is not tree.root:
            total += 1
            if depth > max_depth:
                max_depth = depth
        for child in node.children:
            stack.append((child, depth + 1))
    return total, max_depth


def tree_digest(tree):
    """对结构、顺序与全部附属信息做流式 SHA-256（迭代，不占额外大树）。"""
    digest = hashlib.sha256()

    def feed(*parts):
        for part in parts:
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")

    feed("doc", tree.doctype, tree.meta, tree.head_title, tree.h1)
    stack = [(tree.root, 0)]
    while stack:
        node, depth = stack.pop()
        if node is not tree.root:
            feed("node", str(depth), node.kind, node.title,
                 repr(list(node.attrs.items())), repr(node.description))
        feed("open" if node.children else "leaf")
        for child in reversed(node.children):
            stack.append((child, depth + 1))
        feed("close")
    return digest.hexdigest()


def trees_equal(first, second):
    """逐节点比对两棵树：层级、兄弟顺序、标题、属性（含顺序）、描述、文档头。"""
    if (first.doctype, first.meta, first.head_title, first.h1) != \
            (second.doctype, second.meta, second.head_title, second.h1):
        return False
    stack = [(first.root, second.root)]
    while stack:
        node_a, node_b = stack.pop()
        if node_a.kind != node_b.kind or node_a.title != node_b.title:
            return False
        if list(node_a.attrs.items()) != list(node_b.attrs.items()):
            return False
        if node_a.description != node_b.description:
            return False
        if len(node_a.children) != len(node_b.children):
            return False
        for child_a, child_b in zip(node_a.children, node_b.children):
            stack.append((child_a, child_b))
    return True
