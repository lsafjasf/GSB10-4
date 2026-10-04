"""对拍测试：库输出必须与 crosscheck/expected.json 中的手工推算完全一致。"""

import json
import unittest
from pathlib import Path

from subtitle_tool.core import ShiftWindow, merge_sources, shift_global, shift_segmented
from subtitle_tool.srtio import parse_srt

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "crosscheck"


def load(name):
    return parse_srt((FIX / name).read_text(encoding="utf-8"), tag=name)


def simplify(cues):
    return [{"start": c.start, "end": c.end, "text": c.text} for c in cues]


class TestCrossCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads((FIX / "expected.json").read_text(encoding="utf-8"))
        cls.merged = merge_sources([load("a.srt"), load("b.srt")])

    def test_merge(self):
        exp = self.expected["merge"]
        self.assertEqual(simplify(self.merged.cues), exp["cues"])
        self.assertEqual([r.kind for r in self.merged.reports],
                         [r["kind"] for r in exp["reports"]])

    def test_shift_global_plus500(self):
        exp = self.expected["shift_global_plus500"]
        r = shift_global(self.merged.cues, 500)
        self.assertEqual(simplify(r.cues), exp["cues"])
        self.assertEqual(r.reports, [])

    def test_shift_global_minus1500_out_of_bounds(self):
        exp = self.expected["shift_global_minus1500_nostrict"]
        r = shift_global(self.merged.cues, -1500, strict=False)
        self.assertEqual(simplify(r.cues), exp["cues"])
        self.assertEqual([rep.kind for rep in r.reports],
                         [e["kind"] for e in exp["reports"]])
        self.assertEqual([rep.cue.text for rep in r.reports],
                         [e["text"] for e in exp["reports"]])

    def test_shift_segmented(self):
        exp = self.expected["shift_segmented"]
        windows = [ShiftWindow(w["start"], w["end"], w["offset_ms"])
                   for w in json.loads((FIX / "segments.json").read_text())]
        r = shift_segmented(self.merged.cues, windows)
        self.assertEqual(simplify(r.cues), exp["cues"])
        self.assertEqual(r.reports, [])


if __name__ == "__main__":
    unittest.main()
