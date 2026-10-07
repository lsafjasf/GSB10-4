"""Streaming collector for one ``<si>`` (shared string / inline string).

Handles plain strings (``<si><t>``) and rich text (``<si><r><rPr/><t/></r>``).
Phonetic runs (``<rPh>``) are skipped exactly as the OOXML spec requires for
visual text, while every real run and its ``<rPr>`` formatting marks is kept.
"""

from .model import RichText


def localname(qname):
    if qname.startswith("{"):
        return qname.rsplit("}", 1)[1]
    prefix, sep, local = qname.partition(":")
    return local if sep else prefix


class SiCollector:
    """Collects SAX events for a single ``<si>`` element."""

    def __init__(self):
        self.mode = None          # None | "plain" | "runs"
        self.plain_parts = []
        self.runs = []
        self.run_text = None
        self.run_props = None
        self.in_rpr = False
        self.text_depth = 0       # >0 while inside a real <t>
        self.ignored = 0          # depth inside <rPh>/<phoneticPr>
        self.finished = False

    def start(self, qname, attrs):
        tag = localname(qname)
        if self.ignored:
            self.ignored += 1
            return
        if tag in ("rPh", "phoneticPr"):
            self.ignored = 1
            return
        if self.in_rpr and tag != "rPr":
            # a formatting mark: record all attributes (b{}, color{rgb=...})
            self.run_props[tag] = {
                localname(key): val for key, val in attrs.items()
            }
            return
        if tag == "t":
            self.text_depth += 1
            if self.mode is None:
                self.mode = "plain"
        elif tag == "r":
            self.mode = "runs"
            self.run_text = []
            self.run_props = {}
        elif tag == "rPr" and self.mode == "runs":
            self.in_rpr = True

    def chars(self, content):
        if self.ignored or not self.text_depth:
            return
        if self.mode == "plain":
            self.plain_parts.append(content)
        elif self.run_text is not None:
            self.run_text.append(content)

    def end(self, qname):
        tag = localname(qname)
        if self.ignored:
            self.ignored -= 1
            return
        if tag == "t" and self.text_depth:
            self.text_depth -= 1
        elif tag == "rPr":
            self.in_rpr = False
        elif tag == "r" and self.mode == "runs":
            self.runs.append(("".join(self.run_text), self.run_props))
            self.run_text = None
            self.run_props = None
        elif tag in ("si", "is"):
            self.finished = True

    def result(self):
        if self.mode == "plain":
            return "".join(self.plain_parts)
        if self.runs:
            return RichText(self.runs)
        return ""
