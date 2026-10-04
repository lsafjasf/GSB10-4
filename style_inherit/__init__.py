"""样式继承展开库：把 默认值/样式链/段落/内联 四层样式合并为逐字符最终属性。

纯标准库实现，Python 3.8+。
"""
from .model import Document, Paragraph, Run, Style
from .expand import (
    CharRow,
    Diagnostic,
    ExpandedDoc,
    ExpandedParagraph,
    ExpandedRun,
    expand,
    write_char_table_tsv,
)
from .resolver import analyze_styles, resolve_style_full, style_chain_layers

__all__ = [
    "Document",
    "Paragraph",
    "Run",
    "Style",
    "CharRow",
    "Diagnostic",
    "ExpandedDoc",
    "ExpandedParagraph",
    "ExpandedRun",
    "expand",
    "write_char_table_tsv",
    "analyze_styles",
    "resolve_style_full",
    "style_chain_layers",
]
